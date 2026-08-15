from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from sag_api.core.config import settings
from sag_api.core.deps import _fnos_identity_signer
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.fnos.identity import GatewayIdentity, InternalIdentitySigner
from sag_api.sag.dto import ProcessCheckpoint, ProcessOutcome


class FnOSHostSimulator:
    """Exact HTTP-over-UDS simulator for the four fnOS operations SAG uses."""

    def __init__(self, socket_path: Path, shared_root: Path) -> None:
        self.socket_path = socket_path
        self.shared_root = shared_root
        self.requests: list[dict] = []
        self.bearers: list[str] = []
        self.server: asyncio.AbstractServer | None = None

    async def __aenter__(self):
        self.server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))
        return self

    async def __aexit__(self, *_args):
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()
        self.socket_path.unlink(missing_ok=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            headers_blob = await reader.readuntil(b"\r\n\r\n")
            lines = headers_blob.decode("latin-1").split("\r\n")
            assert lines[0] == "POST /api/v1/trimapp HTTP/1.1"
            headers = {
                key.lower(): value.strip()
                for line in lines[1:]
                if ":" in line
                for key, value in [line.split(":", 1)]
            }
            body = await reader.readexactly(int(headers["content-length"]))
            request = json.loads(body)
            assert set(request) == {"reqId", "req", "appName", "data"}
            assert request["appName"] == "sag"
            bearer = headers.get("authorization", "").removeprefix("Bearer ")
            assert bearer in {"e2e-token-one", "e2e-token-two"}
            self.requests.append(request)
            self.bearers.append(bearer)
            payload = self._response(request)
            encoded = json.dumps(
                {"reqId": request["reqId"], "code": 0, "msg": "", "data": payload}
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(encoded)}\r\nConnection: close\r\n\r\n".encode()
                + encoded
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def _response(self, request: dict):
        operation = request["req"]
        if operation == "trim.system.getPlatformConfig":
            assert request["data"] == {}
            return {"systemLanguage": "zh-CN", "systemVersion": "1.2.0500"}
        if operation == "trim.file.getSharedAccessibleFolders":
            assert request["data"] == {}
            return {"paths": [str(self.shared_root)]}
        if operation == "trim.file.checkUserACL":
            assert request["data"]["uid"] == 1000
            return [
                {
                    "path": path,
                    "readable": True,
                    "writable": False,
                    "deletable": False,
                }
                for path in request["data"]["path"]
            ]
        if operation == "trim.file.convertPath":
            paths = request["data"]["path"]
            if isinstance(paths, str):
                paths = [paths]
            return {
                "status": 0,
                "result": [
                    {"path": path, "semanticPath": f"团队资料/{Path(path).name}"}
                    for path in paths
                ],
            }
        raise AssertionError(f"unexpected fnOS operation: {operation}")


class DeterministicEngineManager:
    """Test engine seam; authorization, scan, copy, DB and queue remain production code."""

    supports_document_source_exclusions = True
    instance: DeterministicEngineManager | None = None

    def __init__(self, _settings) -> None:
        type(self).instance = self
        self.counter = 0
        self.deleted: list[str] = []
        self.maintenance_gate = asyncio.Event()
        self.maintenance_gate.set()
        self.maintenance_started = asyncio.Event()

    async def provision(self, *_args, **_kwargs) -> None:
        return None

    async def release(self, *_args, **_kwargs) -> None:
        return None

    async def aclose_all(self) -> None:
        return None

    async def begin_document_maintenance(self, *_args, **_kwargs) -> None:
        self.maintenance_started.set()
        await self.maintenance_gate.wait()

    async def end_document_maintenance(self, *_args, **_kwargs) -> None:
        return None

    async def delete_document_data(self, _config_id, source_id, *, source) -> None:
        del source
        self.deleted.append(source_id)

    async def process_document(
        self,
        _config_id,
        path,
        *,
        on_stage,
        checkpoint,
        on_checkpoint,
        should_pause,
        **_kwargs,
    ) -> ProcessOutcome:
        assert path and Path(path).is_file()
        assert not await should_pause()
        self.counter += 1
        derived = f"e2e-derived-{self.counter}"
        chunk = f"chunk-{self.counter}"
        await on_stage("loading")
        next_checkpoint = ProcessCheckpoint(
            source_id=derived,
            chunk_ids=[chunk],
            processed_chunk_ids=[chunk],
            event_count=1,
            event_ids=[f"event-{self.counter}"],
            token_usage=10,
        )
        await on_checkpoint(next_checkpoint)
        await on_stage("extracting")
        return ProcessOutcome(
            source_id=derived,
            chunk_count=1,
            event_count=1,
            chunk_ids=[chunk],
            processed_chunk_ids=[chunk],
            event_ids=[f"event-{self.counter}"],
            token_usage=10,
        )

    async def universe_overview_stats(self, *_args, **_kwargs) -> dict:
        return {"event_count": 1, "entity_count": 0, "relation_count": 0, "time_buckets": []}


def _headers(signer: InternalIdentitySigner, uid: int = 1000) -> dict[str, str]:
    return signer.sign(
        GatewayIdentity(uid, "Admin" if uid == 1000 else "Other", True),
        uuid4().hex,
        int(time.time()),
    )


async def _wait_import(client, signer, job_id: str) -> dict:
    for _ in range(200):
        response = await client.get(
            f"/api/v1/fnos/nas/imports/{job_id}", headers=_headers(signer)
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in {"succeeded", "failed"}:
            return payload
        await asyncio.sleep(0.02)
    raise AssertionError("NAS import did not finish")


async def _wait_document(client, signer, source_id: str, document_id: str) -> dict:
    for _ in range(300):
        response = await client.get(
            f"/api/v1/sources/{source_id}/documents/{document_id}",
            headers=_headers(signer),
        )
        if response.status_code == 200 and response.json()["status"] == "ready":
            return response.json()
        await asyncio.sleep(0.02)
    raise AssertionError("document did not reach READY")


async def _wait_process_job(document_id: str) -> Job:
    from sag_api.core.db import SessionLocal

    for _ in range(200):
        async with SessionLocal() as session:
            child = await session.scalar(
                select(Job)
                .where(
                    Job.document_id == document_id,
                    Job.type == JobType.PROCESS_DOCUMENT,
                )
                .order_by(Job.created_at.desc())
            )
            if child is not None and child.status == JobStatus.SUCCEEDED:
                return child
        await asyncio.sleep(0.01)
    raise AssertionError("document process job did not reach SUCCEEDED")


@pytest.mark.asyncio
async def test_real_uds_http_queue_filesystem_import_and_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sag_api import main
    from sag_api.core.db import SessionLocal
    from sag_api.services.retrieval_service import _hidden_document_derivatives

    shared_root = tmp_path / "shared"
    shared_root.mkdir()
    nas_file = shared_root / "handbook.md"
    nas_file.write_text("# Policy\n\nold knowledge", encoding="utf-8")
    uploads = tmp_path / "uploads"
    data = tmp_path / "data"
    secret = tmp_path / "internal-secret"
    secret.write_text("a" * 64, encoding="ascii")
    secret.chmod(0o600)
    socket_path = Path("/tmp") / f"sag-nas-e2e-{uuid4().hex[:8]}.sock"

    monkeypatch.setitem(settings.__dict__, "auth_mode", "fnos")
    monkeypatch.setitem(settings.__dict__, "fnos_uid", 1000)
    monkeypatch.setitem(settings.__dict__, "fnos_username", "Admin")
    monkeypatch.setitem(settings.__dict__, "fnos_username_isolation", False)
    monkeypatch.setitem(settings.__dict__, "fnos_internal_secret_file", str(secret))
    monkeypatch.setitem(settings.__dict__, "fnos_open_api_socket", str(socket_path))
    monkeypatch.setitem(settings.__dict__, "upload_dir", str(uploads))
    monkeypatch.setitem(settings.__dict__, "data_dir", str(data))
    monkeypatch.setitem(settings.__dict__, "job_concurrency", 2)
    monkeypatch.setitem(settings.__dict__, "engine_warmup_count", 0)
    monkeypatch.setenv("TRIM_API_TOKEN", "e2e-token-one")
    monkeypatch.setattr(main, "EngineManager", DeterministicEngineManager)
    _fnos_identity_signer.cache_clear()
    signer = InternalIdentitySigner.from_file(secret)

    async with FnOSHostSimulator(socket_path, shared_root) as host:
        app = main.create_app()
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            engine = DeterministicEngineManager.instance
            assert engine is not None
            async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
                created_source = await client.post(
                    "/api/v1/sources",
                    headers=_headers(signer),
                    json={"name": f"fnOS E2E {uuid4().hex[:8]}"},
                )
                assert created_source.status_code == 201, created_source.text
                source_id = created_source.json()["id"]

                status = await client.get("/api/v1/fnos/nas/status", headers=_headers(signer))
                assert status.status_code == 200, status.text
                assert status.json()["mode"] == "automatic"
                assert status.json()["system_version"] == "1.2.0500"
                folder = status.json()["folders"][0]
                assert folder["display_path"].startswith("团队资料/")
                assert str(shared_root) not in status.text

                monkeypatch.setenv("TRIM_API_TOKEN", "e2e-token-two")
                scanned = await client.post(
                    "/api/v1/fnos/nas/scan",
                    headers=_headers(signer),
                    json={"source_id": source_id, "folder_id": folder["id"], "recursive": True},
                )
                assert scanned.status_code == 200, scanned.text
                scan_file = scanned.json()["files"][0]
                assert scan_file["state"] == "new"
                assert scan_file["selection_token"]
                assert str(shared_root) not in scanned.text

                accepted = await client.post(
                    "/api/v1/fnos/nas/imports",
                    headers=_headers(signer),
                    json={"source_id": source_id, "selection_tokens": [scan_file["selection_token"]]},
                )
                assert accepted.status_code == 202, accepted.text
                progress = await _wait_import(client, signer, accepted.json()["job_id"])
                assert progress["status"] == "succeeded"
                assert progress["created"] == 1
                document_id = progress["results"][0]["document_id"]
                await _wait_document(client, signer, source_id, document_id)
                await _wait_process_job(document_id)

                file_response = await client.get(
                    f"/api/v1/sources/{source_id}/documents/{document_id}/file",
                    headers=_headers(signer),
                )
                assert file_response.status_code == 200
                assert file_response.content == nas_file.read_bytes()
                async with SessionLocal() as session:
                    document = await session.get(Document, document_id)
                    assert document is not None
                    assert Path(document.storage_path).is_relative_to(uploads)
                    assert document.storage_path != str(nas_file)
                    first_derived = document.sag_source_id
                    assert first_derived
                    first_private_path = document.storage_path

                other_headers = _headers(signer, uid=1001)
                for method, url, body in [
                    ("GET", "/api/v1/sources", None),
                    ("GET", f"/api/v1/sources/{source_id}", None),
                    ("GET", f"/api/v1/sources/{source_id}/documents/{document_id}/file", None),
                    (
                        "POST",
                        "/api/v1/fnos/nas/scan",
                        {"source_id": source_id, "folder_id": folder["id"], "recursive": True},
                    ),
                ]:
                    denied = await client.request(method, url, headers=other_headers, json=body)
                    assert denied.status_code == 401
                    other_headers = _headers(signer, uid=1001)

                nas_file.write_text("# Policy\n\nnew replacement knowledge", encoding="utf-8")
                changed = await client.post(
                    "/api/v1/fnos/nas/scan",
                    headers=_headers(signer),
                    json={"source_id": source_id, "folder_id": folder["id"], "recursive": True},
                )
                assert changed.status_code == 200, changed.text
                changed_file = changed.json()["files"][0]
                assert changed_file["state"] == "changed"
                assert changed_file["document_id"] == document_id

                engine.maintenance_started.clear()
                engine.maintenance_gate.clear()
                changed_import = await client.post(
                    "/api/v1/fnos/nas/imports",
                    headers=_headers(signer),
                    json={
                        "source_id": source_id,
                        "selection_tokens": [changed_file["selection_token"]],
                    },
                )
                assert changed_import.status_code == 202, changed_import.text
                changed_progress = await _wait_import(
                    client, signer, changed_import.json()["job_id"]
                )
                assert changed_progress["updated"] == 1
                await asyncio.wait_for(engine.maintenance_started.wait(), timeout=3)
                async with SessionLocal() as session:
                    source = await session.get(Source, source_id)
                    document = await session.get(Document, document_id)
                    assert source is not None and document is not None
                    assert document.status == DocumentStatus.PENDING
                    assert document.id == document_id
                hidden = await _hidden_document_derivatives([source])
                assert first_derived in hidden.source_ids

                engine.maintenance_gate.set()
                await _wait_document(client, signer, source_id, document_id)
                async with SessionLocal() as session:
                    replaced = await session.get(Document, document_id)
                    assert replaced is not None
                    assert replaced.id == document_id
                    assert replaced.storage_path == first_private_path
                    assert Path(replaced.storage_path).read_bytes() == nas_file.read_bytes()
                    assert replaced.origin_sha256 == hashlib.sha256(nas_file.read_bytes()).hexdigest()
                    assert replaced.sag_source_id != first_derived
                    duplicates = list(
                        (
                            await session.scalars(
                                select(Document).where(
                                    Document.source_id == source_id,
                                    Document.origin_kind == "fnos_shared",
                                )
                            )
                        ).all()
                    )
                    assert [item.id for item in duplicates] == [document_id]

    assert {request["req"] for request in host.requests} == {
        "trim.system.getPlatformConfig",
        "trim.file.getSharedAccessibleFolders",
        "trim.file.checkUserACL",
        "trim.file.convertPath",
    }
    assert "e2e-token-one" in host.bearers
    assert "e2e-token-two" in host.bearers
    _fnos_identity_signer.cache_clear()
