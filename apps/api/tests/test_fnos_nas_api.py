from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.api.v1.fnos_nas import router
from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.deps import (
    get_current_user,
    get_fnos_identity,
    get_fnos_nas_access,
    get_fnos_nas_registry,
    get_fnos_nas_scanner,
    get_job_queue,
)
from sag_api.core.errors import ApiError
from sag_api.db.models import Document, FnOSNasLegacyFolder, Job, Source, User
from sag_api.enums import ConnectorKind, JobStatus, JobType, SourceType
from sag_api.fnos.identity import GatewayIdentity
from sag_api.fnos.nas_registry import FnOSNasScanRegistry, NasScanEntry
from sag_api.jobs import JobQueue
from sag_api.services.fnos_nas_access import NasFolder, NasMode, NasStatus, ResolvedNasRoot
from sag_api.services.fnos_nas_scanner import NasScanFile, NasScanResult, NasScanSummary


class FakeQueue(JobQueue):
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, job_id: str) -> None:
        self.enqueued.append(job_id)

    def begin_source_maintenance(self, source_id: str, job_id: str) -> None:
        pass

    def source_maintenance_requested(self, source_id: str) -> bool:
        return False

    async def finish_source_maintenance(self, source_id: str, job_id: str) -> None:
        pass


class FakeAccess:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.deleted: list[str] = []
        self.mode = NasMode.AUTOMATIC

    async def status(self, _session, identity: GatewayIdentity, _language: str) -> NasStatus:
        if not identity.is_admin:
            return NasStatus(False, NasMode.UNAVAILABLE, None, False, [], "administrator_required")
        return NasStatus(
            True,
            self.mode,
            "1.2.0500",
            True,
            [NasFolder("folder-token", "NAS/Documents", "host_api", True)],
        )

    async def register_legacy_folder(self, _session, path: str) -> NasFolder:
        return NasFolder("legacy-token", path, "legacy_manual", True)

    async def delete_legacy_folder(self, _session, folder_id: str) -> None:
        self.deleted.append(folder_id)

    async def resolve_root(self, _session, _identity, folder_id: str) -> ResolvedNasRoot:
        return ResolvedNasRoot(self.root, str(self.root), "NAS/Documents", "host_api", folder_id)


class FakeScanner:
    async def scan(self, _session, *, identity, source, root, recursive) -> NasScanResult:
        del identity, source, root, recursive
        return NasScanResult(
            scan_id="scan-id",
            folder="NAS/Documents",
            files=[
                NasScanFile(
                    selection_token="opaque-selection",
                    name="handbook.pdf",
                    display_path="Policies/handbook.pdf",
                    extension=".pdf",
                    size_bytes=10,
                    modified_at=datetime(2026, 8, 13, tzinfo=UTC),
                    state="new",
                    selected_by_default=True,
                    document_id=None,
                )
            ],
            summary=NasScanSummary(visited=1, eligible=1, new=1),
            truncated=False,
            truncated_reason=None,
            selection_expires_at=datetime(2026, 8, 13, 1, tzinfo=UTC),
        )


@pytest.fixture
async def api_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (
            User.__table__,
            Source.__table__,
            Document.__table__,
            Job.__table__,
            FnOSNasLegacyFolder.__table__,
        ):
            await connection.run_sync(table.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as seed:
        source = Source(
            name="Private",
            source_type=SourceType.DOCUMENT,
            connector_kind=ConnectorKind.FILE_UPLOAD,
            sag_source_config_id="source-config",
        )
        seed.add(source)
        await seed.commit()

    secret = tmp_path / "secret.key"
    secret.write_text("a" * 64, encoding="ascii")
    secret.chmod(0o600)
    registry = FnOSNasScanRegistry(secret_file=secret)
    queue = FakeQueue()
    access = FakeAccess(tmp_path)
    scanner = FakeScanner()
    identity = GatewayIdentity(1000, "Alice", True)

    async def session_override():
        async with sessions() as session:
            yield session

    async def user_override() -> User:
        return User(id="fnos_1000", email="fnos@local.invalid", password_hash="x", name="Alice")

    def access_override():
        return access

    def scanner_override():
        return scanner

    def registry_override():
        return registry

    def queue_override():
        return queue

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_current_user] = user_override
    app.dependency_overrides[get_fnos_nas_access] = access_override
    app.dependency_overrides[get_fnos_nas_scanner] = scanner_override
    app.dependency_overrides[get_fnos_nas_registry] = registry_override
    app.dependency_overrides[get_job_queue] = queue_override

    @app.exception_handler(ApiError)
    async def handle_error(_request: Request, error: ApiError):
        return JSONResponse(status_code=error.status_code, content=error.to_envelope())

    monkeypatch.setitem(settings.__dict__, "auth_mode", "fnos")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(
            client=client,
            app=app,
            sessions=sessions,
            source=source,
            identity=identity,
            registry=registry,
            queue=queue,
            access=access,
        )
    await engine.dispose()


def _set_identity(context, identity: GatewayIdentity) -> None:
    context.app.dependency_overrides[get_fnos_identity] = lambda: identity


@pytest.mark.asyncio
async def test_status_hides_folders_from_non_admin_and_has_exact_admin_limits(api_context) -> None:
    _set_identity(api_context, GatewayIdentity(1000, "Alice", False))
    hidden = await api_context.client.get("/api/v1/fnos/nas/status")
    assert hidden.status_code == 200
    assert hidden.json()["eligible"] is False
    assert hidden.json()["folders"] == []

    _set_identity(api_context, api_context.identity)
    visible = await api_context.client.get("/api/v1/fnos/nas/status")
    assert visible.status_code == 200
    assert visible.json()["folders"][0]["display_path"] == "NAS/Documents"
    assert visible.json()["limits"] == {
        "max_files": 5000,
        "max_import_files": 500,
        "max_import_bytes": 2147483648,
        "max_file_bytes": settings.max_upload_mb * 1024 * 1024,
    }
    assert "/vol" not in visible.text


@pytest.mark.asyncio
async def test_non_admin_mutations_are_forbidden(api_context) -> None:
    _set_identity(api_context, GatewayIdentity(1000, "Alice", False))
    response = await api_context.client.post(
        "/api/v1/fnos/nas/scan",
        json={"source_id": api_context.source.id, "folder_id": "folder-token", "recursive": True},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "nas_administrator_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/fnos/nas/status", None),
        ("POST", "/api/v1/fnos/nas/legacy-folders", {"path": "/vol1/Documents"}),
        ("DELETE", "/api/v1/fnos/nas/legacy-folders/folder", None),
        ("POST", "/api/v1/fnos/nas/scan", {"source_id": "source", "folder_id": "folder"}),
        ("POST", "/api/v1/fnos/nas/imports", {"source_id": "source", "selection_tokens": ["token"]}),
        ("GET", "/api/v1/fnos/nas/imports/job", None),
    ],
)
async def test_all_nas_routes_are_hidden_outside_fnos_mode(
    api_context, monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: dict | None
) -> None:
    api_context.app.dependency_overrides.pop(get_fnos_identity, None)
    monkeypatch.setitem(settings.__dict__, "auth_mode", "password")
    response = await api_context.client.request(method, path, json=body)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_scan_and_legacy_routes_return_only_safe_shapes(api_context) -> None:
    _set_identity(api_context, api_context.identity)
    api_context.access.mode = NasMode.LEGACY_MANUAL
    created = await api_context.client.post(
        "/api/v1/fnos/nas/legacy-folders", json={"path": "/vol1/Documents"}
    )
    assert created.status_code == 201
    assert created.json()["source"] == "legacy_manual"
    deleted = await api_context.client.delete("/api/v1/fnos/nas/legacy-folders/legacy-token")
    assert deleted.status_code == 204

    api_context.access.mode = NasMode.AUTOMATIC
    scanned = await api_context.client.post(
        "/api/v1/fnos/nas/scan",
        json={"source_id": api_context.source.id, "folder_id": "folder-token", "recursive": True},
    )
    assert scanned.status_code == 200
    assert scanned.json()["files"][0]["display_path"] == "Policies/handbook.pdf"
    assert "/vol" not in scanned.text


@pytest.mark.asyncio
async def test_import_submission_is_durable_bounded_and_redacted(api_context) -> None:
    _set_identity(api_context, api_context.identity)
    entry = NasScanEntry(
        canonical_root="/vol1/Documents",
        canonical_path="/vol1/Documents/handbook.pdf",
        display_path="handbook.pdf",
        size_bytes=10,
        mtime_ns=1,
        folder_source="host_api",
    )
    token = api_context.registry.register(1000, api_context.source.id, [entry]).selection_tokens[0]
    accepted = await api_context.client.post(
        "/api/v1/fnos/nas/imports",
        json={"source_id": api_context.source.id, "selection_tokens": [token]},
    )
    assert accepted.status_code == 202
    assert accepted.json()["accepted"] == 1
    assert "/vol" not in accepted.text
    assert api_context.queue.enqueued == [accepted.json()["job_id"]]

    async with api_context.sessions() as session:
        job = await session.get(Job, accepted.json()["job_id"])
        assert job is not None
        assert job.type is JobType.IMPORT_NAS_DOCUMENTS
        assert job.status is JobStatus.QUEUED
        assert job.payload["entries"][0] == asdict(entry)


@pytest.mark.asyncio
async def test_import_submission_rejects_total_bytes_over_limit(api_context) -> None:
    _set_identity(api_context, api_context.identity)
    entry = NasScanEntry(
        canonical_root="/vol1/Documents",
        canonical_path="/vol1/Documents/huge.pdf",
        display_path="huge.pdf",
        size_bytes=2147483649,
        mtime_ns=1,
        folder_source="host_api",
    )
    token = api_context.registry.register(1000, api_context.source.id, [entry]).selection_tokens[0]
    response = await api_context.client.post(
        "/api/v1/fnos/nas/imports",
        json={"source_id": api_context.source.id, "selection_tokens": [token]},
    )
    assert response.status_code == 422
    assert api_context.queue.enqueued == []


@pytest.mark.asyncio
async def test_import_progress_redacts_private_job_payload(api_context) -> None:
    _set_identity(api_context, api_context.identity)
    async with api_context.sessions() as session:
        job = Job(
            type=JobType.IMPORT_NAS_DOCUMENTS,
            status=JobStatus.RUNNING,
            source_id=api_context.source.id,
            progress=0.5,
            payload={
                "owner_uid": 1000,
                "entries": [{"canonical_path": "/vol1/private.pdf"}],
                "summary": {"total": 1, "completed": 1, "created": 1, "updated": 0, "skipped": 0, "failed": 0},
                "results": [
                    {
                        "display_path": "private.pdf",
                        "outcome": "created",
                        "document_id": "doc",
                        "reason": None,
                    }
                ],
            },
        )
        session.add(job)
        await session.commit()

    response = await api_context.client.get(f"/api/v1/fnos/nas/imports/{job.id}")
    assert response.status_code == 200
    assert response.json()["created"] == 1
    assert response.json()["results"][0]["display_path"] == "private.pdf"
    assert "/vol" not in response.text
    assert "canonical_path" not in response.text

    async with api_context.sessions() as session:
        wrong = Job(type=JobType.SYNC_SOURCE, status=JobStatus.QUEUED)
        session.add(wrong)
        await session.commit()
    missing = await api_context.client.get(f"/api/v1/fnos/nas/imports/{wrong.id}")
    assert missing.status_code == 404
