"""Local DSH connector state persists independently of request handlers."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from sag_api.api.v1.system import _request_is_loopback
from sag_api.core.config import settings
from sag_api.core.db import SessionLocal, init_db
from sag_api.core.errors import NotFoundError
from sag_api.db.models import Document, Setting, Source, User
from sag_api.enums import DocumentStatus
from sag_api.services import dsh_integration_service
from sag_api.services.dsh_integration_service import (
    authenticate_connector,
    connection_file_path,
    get_or_create_state,
    regenerate_token,
    update_default_source,
    write_connection_file,
)


async def _register(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"dsh-api-{uuid4().hex}@example.test",
            "password": "password123",
        },
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@asynccontextmanager
async def _connector_api_resource():
    """Create an API source and remove it through the production cleanup path."""
    from sag_api.core.config import settings
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import User
    from sag_api.main import create_app
    from sag_api.services.source_service import delete_source

    app = create_app()
    email = f"dsh-api-cleanup-{uuid4().hex}@example.test"
    source_ids: list[str] = []
    engine_manager = None
    transport = httpx.ASGITransport(app=app)
    try:
        async with app.router.lifespan_context(app):
            engine_manager = app.state.engine_manager
            async with httpx.AsyncClient(transport=transport, base_url="http://sag") as client:
                response = await client.post(
                    "/api/v1/auth/register",
                    json={"email": email, "password": "password123"},
                )
                assert response.status_code == 201
                jwt_headers = {
                    "Authorization": f"Bearer {response.json()['access_token']}"
                }
                source = await client.post(
                    "/api/v1/sources",
                    headers=jwt_headers,
                    json={"name": f"dsh-api-cleanup-{uuid4().hex}"},
                )
                assert source.status_code == 201
                source_id = source.json()["id"]
                source_ids.append(source_id)
                connection = await client.get("/api/v1/system/dsh-connection")
                connector_headers = {
                    "Authorization": f"Bearer {connection.json()['accessToken']}"
                }
                yield client, app, jwt_headers, connector_headers, source_id, source_ids
    finally:
        async with SessionLocal() as session:
            assert engine_manager is not None
            for cleanup_source_id in reversed(source_ids):
                if await session.get(Source, cleanup_source_id) is not None:
                    await delete_source(
                        session,
                        cleanup_source_id,
                        engine_manager=engine_manager,
                        upload_dir=settings.upload_dir,
                    )
            await session.execute(delete(User).where(User.email == email))
            await session.commit()


@pytest.fixture
async def db_session() -> AsyncSession:
    await init_db()
    async with SessionLocal() as session:
        await session.execute(
            delete(Setting).where(Setting.scope == "global", Setting.key == "dsh_integration")
        )
        await session.execute(delete(User).where(User.email.like("dsh-integration-%@example.test")))
        await session.execute(delete(User).where(User.email.like("dsh-api-%@example.test")))
        await session.execute(delete(Source).where(Source.name.like("dsh-integration-%")))
        await session.commit()
        yield session
        await session.rollback()
        await session.execute(
            delete(Setting).where(Setting.scope == "global", Setting.key == "dsh_integration")
        )
        await session.execute(delete(User).where(User.email.like("dsh-integration-%@example.test")))
        await session.execute(delete(User).where(User.email.like("dsh-api-%@example.test")))
        await session.execute(delete(Source).where(Source.name.like("dsh-integration-%")))
        await session.commit()


@pytest.fixture
def connection_path() -> Path:
    return connection_file_path(settings)


def test_connection_file_path_uses_override_and_platform_config_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    override = tmp_path / "custom.json"
    assert connection_file_path(SimpleNamespace(dsh_connection_file=str(override))) == override

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "platform", "darwin")
    assert connection_file_path(SimpleNamespace(dsh_connection_file=None)) == (
        tmp_path / "home" / "Library" / "Application Support" / "SAG" / "dsh-connection.json"
    )

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert connection_file_path(SimpleNamespace(dsh_connection_file=None)) == (
        tmp_path / "xdg" / "sag" / "dsh-connection.json"
    )

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    assert connection_file_path(SimpleNamespace(dsh_connection_file=None)) == (
        tmp_path / "appdata" / "SAG" / "dsh-connection.json"
    )


@pytest.fixture
async def source(db_session: AsyncSession) -> Source:
    created = Source(
        name=f"dsh-integration-{uuid4().hex}",
        sag_source_config_id=f"dsh-{uuid4().hex}"[:36],
    )
    db_session.add(created)
    await db_session.commit()
    return created


@pytest.mark.asyncio
async def test_connection_file_is_written_owner_only(
    db_session: AsyncSession,
    connection_path: Path,
):
    connection_path.parent.mkdir(parents=True, exist_ok=True)
    connection_path.write_text("stale", encoding="utf-8")

    written = await write_connection_file(db_session)

    payload = json.loads(written.read_text(encoding="utf-8"))
    assert written == connection_path
    assert payload["schemaVersion"] == 1
    assert payload["apiUrl"] == "http://127.0.0.1:8000/api/v1"
    assert payload["mcpUrl"] == "http://127.0.0.1:8000/mcp/"
    assert payload["accessToken"].startswith("sag_local_")
    assert not list(connection_path.parent.glob(f".{connection_path.name}.*.tmp"))
    if os.name != "nt":
        assert stat.S_IMODE(written.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_connection_file_removes_temporary_file_when_replace_fails(
    db_session: AsyncSession,
    connection_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    connection_path.parent.mkdir(parents=True, exist_ok=True)
    connection_path.write_text("stable", encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(dsh_integration_service.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        await write_connection_file(db_session)

    assert connection_path.read_text(encoding="utf-8") == "stable"
    assert not list(connection_path.parent.glob(f".{connection_path.name}.*.tmp"))


@pytest.mark.asyncio
async def test_connection_file_replace_stays_in_publication_task(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    publication_thread = threading.get_ident()
    replace_threads: list[int] = []
    original_replace = dsh_integration_service._replace_connection_file

    def record_replace_thread(target: Path, payload: dict[str, object]) -> None:
        replace_threads.append(threading.get_ident())
        original_replace(target, payload)

    monkeypatch.setattr(
        dsh_integration_service,
        "_replace_connection_file",
        record_replace_thread,
    )

    await write_connection_file(db_session)

    assert replace_threads == [publication_thread]


@pytest.mark.asyncio
async def test_concurrent_settings_and_regeneration_publish_latest_state(
    db_session: AsyncSession,
    source: Source,
    connection_path: Path,
):
    async def select_source_and_publish() -> None:
        async with SessionLocal() as session:
            await update_default_source(session, source.id)
            await write_connection_file(session)

    async def regenerate_and_publish() -> None:
        async with SessionLocal() as session:
            await regenerate_token(session)
            await write_connection_file(session)

    await asyncio.gather(select_source_and_publish(), regenerate_and_publish())

    async with SessionLocal() as session:
        persisted = await get_or_create_state(session)
    published = json.loads(connection_path.read_text(encoding="utf-8"))
    assert published["accessToken"] == persisted.token
    assert published["defaultSourceId"] == persisted.default_source_id == source.id


@pytest.mark.asyncio
async def test_lifespan_writes_dsh_connection_file(connection_path: Path):
    from sag_api.main import create_app

    app = create_app()

    async with app.router.lifespan_context(app):
        payload = json.loads(connection_path.read_text(encoding="utf-8"))

    assert payload["apiUrl"] == "http://127.0.0.1:8000/api/v1"


@pytest.mark.asyncio
async def test_lifespan_warns_and_continues_when_connection_file_write_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    from sag_api import main as main_module

    async def fail_to_write(*_args, **_kwargs):
        raise OSError("read-only config directory")

    warnings: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(dsh_integration_service, "write_connection_file", fail_to_write)
    monkeypatch.setattr(
        main_module.log,
        "warning",
        lambda message, *args: warnings.append((message, args)),
    )
    app = main_module.create_app()

    async with app.router.lifespan_context(app):
        pass

    assert len(warnings) == 1
    assert warnings[0][0] == "DSH 本机连接文件刷新失败：%s"
    assert str(warnings[0][1][0]) == "read-only config directory"


@pytest.mark.asyncio
async def test_lifespan_does_not_hide_connector_database_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    from sag_api.main import create_app

    async def fail_to_read_state(*_args, **_kwargs):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(dsh_integration_service, "write_connection_file", fail_to_read_state)
    app = create_app()

    with pytest.raises(SQLAlchemyError, match="database unavailable"):
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.asyncio
async def test_regenerate_and_default_source_refresh_the_connection_file(
    source: Source,
    connection_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from sag_api.main import create_app

    monkeypatch.setattr(settings, "dsh_public_url", "http://127.0.0.1:18080/local/sag")
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://sag") as client:
            auth_headers = await _register(client)
            before = json.loads(connection_path.read_text(encoding="utf-8"))
            changed = await client.put(
                "/api/v1/system/dsh/settings",
                headers={**auth_headers, "Host": "attacker.example:9999"},
                json={"default_source_id": source.id},
            )
            after_settings = json.loads(connection_path.read_text(encoding="utf-8"))
            regenerated = await client.post(
                "/api/v1/system/dsh/regenerate",
                headers={**auth_headers, "Host": "attacker.example:9999"},
            )
            after_regeneration = json.loads(connection_path.read_text(encoding="utf-8"))
            exported = await client.get(
                "/api/v1/system/dsh/export",
                headers={**auth_headers, "Host": "attacker.example:9999"},
            )

    assert changed.status_code == 200
    assert after_settings["defaultSourceId"] == source.id
    assert after_settings["accessToken"] == before["accessToken"]
    assert after_settings["apiUrl"] == "http://127.0.0.1:18080/local/sag/api/v1"
    assert regenerated.status_code == 200
    assert after_regeneration["defaultSourceId"] == source.id
    assert after_regeneration["accessToken"] != before["accessToken"]
    assert after_regeneration["apiUrl"] == "http://127.0.0.1:18080/local/sag/api/v1"
    assert after_regeneration["mcpUrl"] == "http://127.0.0.1:18080/local/sag/mcp/"
    assert exported.json()["apiUrl"] == "http://attacker.example:9999/api/v1"


@pytest.mark.asyncio
async def test_settings_and_regeneration_report_connection_file_write_failure(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    from sag_api.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://sag") as client:
            auth_headers = await _register(client)

            async def fail_to_write(*_args, **_kwargs):
                raise OSError("read-only config directory")

            monkeypatch.setattr(dsh_integration_service, "write_connection_file", fail_to_write)
            changed = await client.put(
                "/api/v1/system/dsh/settings",
                headers=auth_headers,
                json={"default_source_id": None},
            )
            regenerated = await client.post(
                "/api/v1/system/dsh/regenerate",
                headers=auth_headers,
            )

    assert changed.status_code == 500
    assert regenerated.status_code == 500


@pytest.mark.asyncio
async def test_dsh_connector_state_is_stable_until_regenerated(db_session: AsyncSession):
    first = await get_or_create_state(db_session)
    second = await get_or_create_state(db_session)

    assert first.token.startswith("sag_local_")
    assert second.token == first.token

    regenerated = await regenerate_token(db_session)

    assert regenerated.token != first.token


@pytest.mark.asyncio
async def test_dsh_connector_state_is_created_once_for_concurrent_first_access(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    original_load_row = dsh_integration_service._load_row
    first_read_barrier = asyncio.Barrier(2)
    load_counts: dict[int, int] = {}

    async def synchronized_load_row(session: AsyncSession) -> Setting | None:
        row = await original_load_row(session)
        session_id = id(session)
        load_counts[session_id] = load_counts.get(session_id, 0) + 1
        if row is None and load_counts[session_id] == 1:
            await first_read_barrier.wait()
        return row

    monkeypatch.setattr(dsh_integration_service, "_load_row", synchronized_load_row)

    async def create_state() -> str:
        async with SessionLocal() as session:
            return (await get_or_create_state(session)).token

    first_token, second_token = await asyncio.gather(create_state(), create_state())

    async with SessionLocal() as session:
        row_count = await session.scalar(
            select(func.count()).select_from(Setting).where(
                Setting.scope == "global", Setting.key == "dsh_integration"
            )
        )

    assert first_token == second_token
    assert row_count == 1


@pytest.mark.asyncio
async def test_dsh_regeneration_and_default_source_update_do_not_lose_each_other(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    source: Source,
):
    initial = await get_or_create_state(db_session)
    original_load_row = dsh_integration_service._load_row
    write_snapshot_barrier = asyncio.Barrier(2)
    load_counts: dict[int, int] = {}

    async def synchronized_load_row(session: AsyncSession) -> Setting | None:
        row = await original_load_row(session)
        session_id = id(session)
        load_counts[session_id] = load_counts.get(session_id, 0) + 1
        if row is not None and load_counts[session_id] == 2:
            await write_snapshot_barrier.wait()
        return row

    monkeypatch.setattr(dsh_integration_service, "_load_row", synchronized_load_row)

    async def regenerate() -> str:
        async with SessionLocal() as session:
            return (await regenerate_token(session)).token

    async def update_source() -> None:
        async with SessionLocal() as session:
            await update_default_source(session, source.id)

    regenerated_token, _ = await asyncio.gather(regenerate(), update_source())
    final = await get_or_create_state(db_session)

    assert final.token != initial.token
    assert final.token == regenerated_token
    assert final.default_source_id == source.id


@pytest.mark.asyncio
async def test_dsh_default_source_must_exist(db_session: AsyncSession, source: Source):
    state = await update_default_source(db_session, source.id)

    assert state.default_source_id == source.id
    with pytest.raises(NotFoundError):
        await update_default_source(db_session, "missing")

    cleared = await update_default_source(db_session, None)

    assert cleared.default_source_id is None


@pytest.mark.asyncio
async def test_dsh_connector_authentication_requires_matching_token_and_active_user(
    db_session: AsyncSession,
):
    state = await get_or_create_state(db_session)

    inactive = User(
        email="dsh-integration-inactive@example.test",
        password_hash="hash",
        is_active=False,
    )
    newer_active = User(
        email="dsh-integration-newer-active@example.test",
        password_hash="hash",
        is_active=True,
        created_at=datetime(1, 1, 1, 0, 0, 1, tzinfo=UTC),
    )
    older_active = User(
        email="dsh-integration-older-active@example.test",
        password_hash="hash",
        is_active=True,
        created_at=datetime(1, 1, 1, tzinfo=UTC),
    )
    db_session.add_all([inactive, newer_active, older_active])
    await db_session.commit()

    authenticated = await authenticate_connector(db_session, state.token)

    assert authenticated is not None
    assert authenticated.id == older_active.id
    assert await authenticate_connector(db_session, "not-the-connector-token") is None


@pytest.mark.asyncio
async def test_loopback_can_discover_dsh_connection():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://sag") as client:
            response = await client.get("/api/v1/system/dsh-connection")

    assert response.status_code == 200
    body = response.json()
    assert body["schemaVersion"] == 1
    assert body["apiUrl"] == "http://sag/api/v1"
    assert body["mcpUrl"] == "http://sag/mcp/"
    assert body["accessToken"].startswith("sag_local_")


@pytest.mark.parametrize(
    ("peer", "expected"),
    [
        (None, False),
        (("localhost", 53000), False),
        (("not-an-ip", 53000), False),
        (("127.0.0.1", 53000), True),
        (("::1", 53000), True),
    ],
)
def test_dsh_discovery_identifies_loopback_from_asgi_peer_only(
    peer: tuple[str, int] | None,
    expected: bool,
):
    request = Request({"type": "http", "client": peer})

    assert _request_is_loopback(request) is expected


@pytest.mark.asyncio
async def test_non_loopback_cannot_read_connection_secret():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app, client=("192.168.1.20", 53000))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://sag") as client:
            response = await client.get(
                "/api/v1/system/dsh-connection",
                headers={"X-Forwarded-For": "127.0.0.1"},
            )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_connector_token_calls_approved_knowledge_apis(
    monkeypatch: pytest.MonkeyPatch,
):
    from sag_api.sag.dto import ChunkInfo

    async with _connector_api_resource() as (
        client,
        app,
        _jwt_headers,
        connector_headers,
        source_id,
        source_ids,
    ):
        listed = await client.get("/api/v1/sources", headers=connector_headers)
        created = await client.post(
            "/api/v1/sources",
            headers=connector_headers,
            json={"name": f"dsh-connector-created-{uuid4().hex}"},
        )
        if created.status_code == 201:
            source_ids.append(created.json()["id"])
        uploaded = await client.post(
            f"/api/v1/sources/{source_id}/documents",
            headers=connector_headers,
            files={"file": ("dsh.md", b"# dsh", "text/markdown")},
        )
        document_id = uploaded.json()["id"]
        documents = await client.get(
            f"/api/v1/sources/{source_id}/documents",
            headers=connector_headers,
        )
        document = await client.get(
            f"/api/v1/sources/{source_id}/documents/{document_id}",
            headers=connector_headers,
        )
        ingested = await client.post(
            f"/api/v1/sources/{source_id}/documents/ingest",
            headers=connector_headers,
            json={"title": "dsh ingest", "text": "connector content"},
        )
        reprocessed = await client.post(
            f"/api/v1/sources/{source_id}/documents/{document_id}/reprocess",
            headers=connector_headers,
        )

        async def fake_get_chunk(_source_config_id, chunk_id, *, source=None):
            assert source is not None and source.id == source_id
            return ChunkInfo(
                chunk_id=chunk_id,
                heading="DSH",
                content="connector readable content",
                rank=1,
            )

        monkeypatch.setattr(app.state.engine_manager, "get_chunk", fake_get_chunk)
        read = await client.get(
            f"/api/v1/sources/{source_id}/chunks/chunk-for-dsh",
            headers=connector_headers,
        )
        searched = await client.post(
            "/api/v1/search",
            headers=connector_headers,
            json={"query": "connector", "source_ids": [source_id]},
        )
        async with SessionLocal() as session:
            deletable = Document(
                source_id=source_id,
                filename="dsh-delete.md",
                content_type="text/markdown",
                size_bytes=0,
                storage_path="",
                status=DocumentStatus.PENDING,
            )
            session.add(deletable)
            await session.commit()
            deletable_id = deletable.id
        deleted = await client.delete(
            f"/api/v1/sources/{source_id}/documents/{deletable_id}",
            headers=connector_headers,
        )

    assert listed.status_code == 200
    assert created.status_code == 201
    assert uploaded.status_code == 201
    assert documents.status_code == 200
    assert document.status_code == 200
    assert ingested.status_code == 201
    assert reprocessed.status_code == 200
    assert read.status_code == 200
    assert read.json()["content"] == "connector readable content"
    assert searched.status_code == 200
    assert deleted.status_code == 200


@pytest.mark.asyncio
async def test_connector_token_cannot_call_sag_control_plane_apis():
    async with _connector_api_resource() as (
        client,
        _app,
        jwt_headers,
        connector_headers,
        source_id,
        source_ids,
    ):
        configured = await client.put(
            "/api/v1/system/dsh/settings",
            headers=jwt_headers,
            json={"default_source_id": source_id},
        )
        assert configured.status_code == 200
        delete_target = await client.post(
            "/api/v1/sources",
            headers=jwt_headers,
            json={"name": f"dsh-jwt-delete-{uuid4().hex}"},
        )
        assert delete_target.status_code == 201
        delete_target_id = delete_target.json()["id"]
        source_ids.append(delete_target_id)

        connector_model = await client.get(
            "/api/v1/system/model-config", headers=connector_headers
        )
        connector_capabilities = await client.get(
            "/api/v1/system/dsh", headers=connector_headers
        )
        connector_agents = await client.get("/api/v1/agents", headers=connector_headers)
        connector_source_delete = await client.delete(
            f"/api/v1/sources/{delete_target_id}", headers=connector_headers
        )
        connector_export = await client.get(
            "/api/v1/system/dsh/export", headers=connector_headers
        )
        connector_settings = await client.put(
            "/api/v1/system/dsh/settings",
            headers=connector_headers,
            json={"default_source_id": None},
        )
        connector_regenerate = await client.post(
            "/api/v1/system/dsh/regenerate", headers=connector_headers
        )

        assert connector_model.status_code == 401
        assert connector_capabilities.status_code == 200
        assert connector_capabilities.json() == {
            "schemaVersion": 1,
            "capabilities": [
                "sources.list",
                "sources.create",
                "knowledge.search",
                "knowledge.read",
                "documents.list",
                "documents.get",
                "documents.upload",
                "documents.ingest",
                "documents.reprocess",
                "documents.delete",
            ],
            "upload": {
                "maxMb": settings.max_upload_mb,
                "extensions": sorted(
                    extension.lstrip(".") for extension in settings.allowed_upload_exts
                ),
            },
            "defaultSourceId": source_id,
        }
        assert connector_agents.status_code == 401
        assert connector_source_delete.status_code == 401
        assert connector_export.status_code == 401
        assert connector_settings.status_code == 401
        assert connector_regenerate.status_code == 401

        jwt_model = await client.get("/api/v1/system/model-config", headers=jwt_headers)
        jwt_capabilities = await client.get("/api/v1/system/dsh", headers=jwt_headers)
        jwt_agents = await client.get("/api/v1/agents", headers=jwt_headers)
        jwt_export = await client.get("/api/v1/system/dsh/export", headers=jwt_headers)
        jwt_settings = await client.put(
            "/api/v1/system/dsh/settings",
            headers=jwt_headers,
            json={"default_source_id": source_id},
        )
        jwt_source_delete = await client.delete(
            f"/api/v1/sources/{delete_target_id}", headers=jwt_headers
        )
        jwt_regenerate = await client.post(
            "/api/v1/system/dsh/regenerate", headers=jwt_headers
        )
        restored = await client.put(
            "/api/v1/system/dsh/settings",
            headers=jwt_headers,
            json={"default_source_id": None},
        )

    assert jwt_model.status_code == 200
    assert jwt_capabilities.status_code == 200
    assert jwt_agents.status_code == 200
    assert jwt_export.status_code == 200
    assert jwt_settings.status_code == 200
    assert jwt_source_delete.status_code == 200
    assert jwt_regenerate.status_code == 200
    assert restored.status_code == 200


@pytest.mark.asyncio
async def test_connector_global_search_is_structured_only(
    monkeypatch: pytest.MonkeyPatch,
):
    from sag_api.api.v1 import search as search_module
    from sag_api.sag import RetrievedSection, SearchOutcome
    from sag_api.schemas.search import SearchResponse, SectionOut
    from sag_api.services import universe_service

    class SpyLLM:
        configured = True

        def __init__(self) -> None:
            self.complete_calls = 0

        async def complete(self, _messages) -> str:
            self.complete_calls += 1
            return "JWT summary [1]"

    save_calls = 0

    async def fake_prepare(session, _engine_manager, body):
        source = await session.get(Source, body.source_ids[0])
        assert source is not None
        section = RetrievedSection(
            chunk_id="structured-chunk",
            heading="Structured evidence",
            content="Connector receives this evidence without SAG generation.",
            score=0.98,
            rank=0,
            source_id="document-structured",
            source_config_id=source.sag_source_config_id,
        )
        return search_module._PreparedGlobalSearch(
            sources=[source],
            outcome=SearchOutcome(query=body.query, sections=[section], stats={"sources": 1}),
            response=SearchResponse(
                query=body.query,
                sections=[
                    SectionOut(
                        chunk_id=section.chunk_id,
                        heading=section.heading,
                        content=section.content,
                        score=section.score,
                        rank=section.rank,
                        source_id=source.id,
                        source_name=source.name,
                    )
                ],
                stats={"sources": 1},
            ),
        )

    async def fake_save_exploration(*_args, **_kwargs):
        nonlocal save_calls
        save_calls += 1
        return SimpleNamespace(id="jwt-exploration"), SimpleNamespace()

    monkeypatch.setattr(search_module, "_prepare_global_search", fake_prepare)
    monkeypatch.setattr(universe_service, "save_exploration", fake_save_exploration)
    spy_llm = SpyLLM()

    async with _connector_api_resource() as (
        client,
        app,
        jwt_headers,
        connector_headers,
        source_id,
        _source_ids,
    ):
        app.state.llm = spy_llm
        request = {
            "query": "structured search",
            "source_ids": [source_id],
            "save_exploration": True,
        }
        connector_response = await client.post(
            "/api/v1/search", headers=connector_headers, json=request
        )

        assert connector_response.status_code == 200
        connector_body = connector_response.json()
        assert connector_body["sections"][0]["chunk_id"] == "structured-chunk"
        assert connector_body["summary"] == ""
        assert connector_body["exploration_id"] is None
        assert spy_llm.complete_calls == 0
        assert save_calls == 0

        jwt_response = await client.post(
            "/api/v1/search", headers=jwt_headers, json=request
        )

    assert jwt_response.status_code == 200
    assert jwt_response.json()["summary"] == "JWT summary [1]"
    assert jwt_response.json()["exploration_id"] == "jwt-exploration"
    assert spy_llm.complete_calls == 1
    assert save_calls == 1


@pytest.mark.asyncio
async def test_authenticated_dsh_capabilities_settings_and_export():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://sag") as client:
            unauthenticated_capabilities = await client.get("/api/v1/system/dsh")
            unauthenticated_export = await client.get("/api/v1/system/dsh/export")
            unauthenticated_settings = await client.put(
                "/api/v1/system/dsh/settings",
                json={"default_source_id": None},
            )
            unauthenticated_regeneration = await client.post("/api/v1/system/dsh/regenerate")
            discovered = await client.get("/api/v1/system/dsh-connection")
            auth_headers = await _register(client)
            missing_source = await client.put(
                "/api/v1/system/dsh/settings",
                headers=auth_headers,
                json={"default_source_id": "missing"},
            )
            source = await client.post(
                "/api/v1/sources",
                headers=auth_headers,
                json={"name": f"dsh-api-{uuid4().hex}"},
            )
            assert source.status_code == 201
            source_id = source.json()["id"]

            changed = await client.put(
                "/api/v1/system/dsh/settings",
                headers=auth_headers,
                json={"default_source_id": source_id},
            )
            capabilities = await client.get("/api/v1/system/dsh", headers=auth_headers)
            exported = await client.get("/api/v1/system/dsh/export", headers=auth_headers)

    assert unauthenticated_capabilities.status_code == 401
    assert unauthenticated_export.status_code == 401
    assert unauthenticated_settings.status_code == 401
    assert unauthenticated_regeneration.status_code == 401
    assert missing_source.status_code == 404
    assert changed.status_code == 200
    assert changed.json()["defaultSourceId"] == source_id
    assert capabilities.status_code == 200
    assert capabilities.json()["defaultSourceId"] == source_id
    assert "accessToken" not in capabilities.json()
    assert exported.status_code == 200
    assert exported.headers["content-disposition"] == 'attachment; filename="sag-dsh.json"'
    assert exported.headers["content-type"].startswith("application/json")
    assert exported.json()["accessToken"] == discovered.json()["accessToken"]
    assert exported.json()["defaultSourceId"] == source_id


@pytest.mark.asyncio
async def test_authenticated_dsh_regeneration_preserves_default_source():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://sag") as client:
            auth_headers = await _register(client)
            before = await client.get("/api/v1/system/dsh-connection")
            regenerated = await client.post("/api/v1/system/dsh/regenerate", headers=auth_headers)
            after = await client.get("/api/v1/system/dsh-connection")

    assert regenerated.status_code == 200
    assert "accessToken" not in regenerated.json()
    assert before.json()["accessToken"] != after.json()["accessToken"]
    assert regenerated.json()["defaultSourceId"] == after.json()["defaultSourceId"]


@pytest.mark.asyncio
async def test_dsh_capability_json_matches_v1_plugin_contract():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://sag") as client:
            auth_headers = await _register(client)
            source = await client.post(
                "/api/v1/sources",
                headers=auth_headers,
                json={"name": f"dsh-contract-{uuid4().hex}"},
            )
            assert source.status_code == 201
            source_id = source.json()["id"]
            changed = await client.put(
                "/api/v1/system/dsh/settings",
                headers=auth_headers,
                json={"default_source_id": source_id},
            )
            assert changed.status_code == 200

            response = await client.get("/api/v1/system/dsh", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "schemaVersion": 1,
        "capabilities": [
            "sources.list",
            "sources.create",
            "knowledge.search",
            "knowledge.read",
            "documents.list",
            "documents.get",
            "documents.upload",
            "documents.ingest",
            "documents.reprocess",
            "documents.delete",
        ],
        "upload": {
            "maxMb": settings.max_upload_mb,
            "extensions": [
                "csv",
                "docx",
                "epub",
                "htm",
                "html",
                "json",
                "markdown",
                "md",
                "pdf",
                "pptx",
                "text",
                "tsv",
                "txt",
                "xls",
                "xlsx",
            ],
        },
        "defaultSourceId": source_id,
    }
    assert "maxUploadMb" not in body["upload"]
    assert "allowedExtensions" not in body["upload"]
    assert all(not extension.startswith(".") for extension in body["upload"]["extensions"])
