from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_ROUTING_KEY = b"m" * 32

@pytest.fixture
async def session_factory():
    from sag_api.db.base import Base
    from sag_api.db.models import User

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            User(
                id="fnos_1000",
                email="fnos-1000@local.invalid",
                password_hash="not-used",
                password_initialized=False,
                auth_singleton=1,
                name="Alice",
            )
        )
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_fnos_mcp_grant_is_returned_once_and_stored_only_as_a_digest(session_factory) -> None:
    from sag_api.db.models import FnOSMcpGrant, User
    from sag_api.services import fnos_mcp_access

    async with session_factory() as session:
        user = await session.get(User, "fnos_1000")
        assert user is not None
        issued = await fnos_mcp_access.issue_grant(
            session,
            user=user,
            expires_in_days=30,
            identity_uid=1000,
            identity_username="Alice",
            routing_key=_ROUTING_KEY,
        )
        stored = await session.get(FnOSMcpGrant, issued.id)

    assert issued.token.startswith("sagf_mcp_")
    assert stored is not None
    assert issued.token not in stored.secret_digest
    assert stored.user_id == "fnos_1000"
    assert stored.expires_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_fnos_mcp_grant_rejects_expired_and_revoked_credentials(session_factory) -> None:
    from sag_api.db.models import User
    from sag_api.services import fnos_mcp_access

    async with session_factory() as session:
        user = await session.get(User, "fnos_1000")
        assert user is not None
        issued = await fnos_mcp_access.issue_grant(
            session,
            user=user,
            expires_in_days=7,
            identity_uid=1000,
            identity_username="Alice",
            routing_key=_ROUTING_KEY,
        )
        valid = await fnos_mcp_access.authenticate_grant(session, issued.token)
        assert valid is not None
        await fnos_mcp_access.revoke_grant(session, grant_id=issued.id, user=user)
        assert await fnos_mcp_access.authenticate_grant(session, issued.token) is None

        expired = await fnos_mcp_access.issue_grant(
            session,
            user=user,
            expires_in_days=90,
            identity_uid=1000,
            identity_username="Alice",
            routing_key=_ROUTING_KEY,
            now=datetime.now(UTC) - timedelta(days=91),
        )
        assert await fnos_mcp_access.authenticate_grant(session, expired.token) is None


@pytest.mark.asyncio
async def test_fnos_mcp_grant_rejects_unapproved_expiry(session_factory) -> None:
    from sag_api.db.models import User
    from sag_api.services import fnos_mcp_access

    async with session_factory() as session:
        user = await session.get(User, "fnos_1000")
        assert user is not None
        with pytest.raises(ValueError, match="7、30 或 90"):
            await fnos_mcp_access.issue_grant(
                session,
                user=user,
                expires_in_days=8,
                identity_uid=1000,
                identity_username="Alice",
                routing_key=_ROUTING_KEY,
            )


def test_fnos_mcp_grant_rejects_malformed_token_without_raising() -> None:
    from sag_api.services.fnos_mcp_access import route_grant

    assert route_grant("sagf_mcp_incomplete", routing_key=_ROUTING_KEY) is None


@pytest.mark.asyncio
async def test_fnos_mcp_mount_authentication_requires_an_active_grant(session_factory) -> None:
    from sag_api.db.models import User
    from sag_api.mcp.mount import authenticate_fnos_mcp_grant
    from sag_api.services import fnos_mcp_access

    async with session_factory() as session:
        user = await session.get(User, "fnos_1000")
        assert user is not None
        issued = await fnos_mcp_access.issue_grant(
            session,
            user=user,
            expires_in_days=7,
            identity_uid=1000,
            identity_username="Alice",
            routing_key=_ROUTING_KEY,
        )
        assert await authenticate_fnos_mcp_grant(session, issued.token) == "fnos_1000"
        await fnos_mcp_access.revoke_grant(session, grant_id=issued.id, user=user)
        assert await authenticate_fnos_mcp_grant(session, issued.token) is None


@pytest.mark.asyncio
async def test_fnos_mcp_descriptor_issues_and_revokes_expiring_grants(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sag_api.api.v1.system import router
    from sag_api.core.config import settings
    from sag_api.core.db import get_session
    from sag_api.db.base import Base
    from sag_api.fnos.identity import GatewayIdentity, InternalIdentitySigner

    secret_file = tmp_path / "identity.key"
    secret_file.write_text("c" * 64, encoding="ascii")
    secret_file.chmod(0o600)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def session_override():
        async with factory() as session:
            yield session

    monkeypatch.setitem(settings.__dict__, "auth_mode", "fnos")
    monkeypatch.setitem(settings.__dict__, "fnos_uid", 1000)
    monkeypatch.setitem(settings.__dict__, "fnos_username", "Alice")
    monkeypatch.setitem(settings.__dict__, "fnos_username_isolation", True)
    monkeypatch.setitem(settings.__dict__, "fnos_internal_secret_file", str(secret_file))
    from sag_api.core import deps

    deps._fnos_identity_signer.cache_clear()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_session] = session_override
    signer = InternalIdentitySigner.from_file(secret_file)
    now = int(time.time())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fnos") as client:
        headers = signer.sign(GatewayIdentity(1000, "Alice", False), "descriptor", now)
        descriptor = await client.get("/api/v1/system/mcp", headers=headers)
        assert descriptor.status_code == 200, descriptor.text
        assert descriptor.json()["http"]["path"] == "/mcp/"
        assert "stdio" not in descriptor.json()

        headers = signer.sign(GatewayIdentity(1000, "Alice", False), "issue", now + 1)
        issued = await client.post(
            "/api/v1/system/mcp/grants",
            headers=headers,
            json={"expires_in_days": 90},
        )
        assert issued.status_code == 201, issued.text
        body = issued.json()
        assert body["token"].startswith("sagf_mcp_")
        assert body["expires_in_days"] == 90

        headers = signer.sign(GatewayIdentity(1000, "Alice", False), "after-issue", now + 2)
        listed = await client.get("/api/v1/system/mcp", headers=headers)
        assert "token" not in listed.json()["grants"][0]

        headers = signer.sign(GatewayIdentity(1000, "Alice", False), "active-delete", now + 3)
        from sag_api.core.errors import ConflictError

        with pytest.raises(ConflictError, match="先撤销"):
            await client.delete(f"/api/v1/system/mcp/grants/{body['id']}/record", headers=headers)

        headers = signer.sign(GatewayIdentity(1000, "Alice", False), "revoke", int(time.time()))
        revoked = await client.delete(f"/api/v1/system/mcp/grants/{body['id']}", headers=headers)
        assert revoked.status_code == 204, revoked.text

        headers = signer.sign(GatewayIdentity(1000, "Alice", False), "after-revoke", int(time.time()))
        listed = await client.get("/api/v1/system/mcp", headers=headers)
        assert listed.json()["grants"] == []
        assert [grant["id"] for grant in listed.json()["inactive_grants"]] == [body["id"]]

        headers = signer.sign(GatewayIdentity(1000, "Alice", False), "delete-record", int(time.time()))
        deleted = await client.delete(f"/api/v1/system/mcp/grants/{body['id']}/record", headers=headers)
        assert deleted.status_code == 204, deleted.text

        headers = signer.sign(GatewayIdentity(1000, "Alice", False), "after-delete", int(time.time()))
        listed = await client.get("/api/v1/system/mcp", headers=headers)
        assert listed.json()["inactive_grants"] == []

    deps._fnos_identity_signer.cache_clear()
    await engine.dispose()
