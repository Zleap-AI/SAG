from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user
from sag_api.core.errors import ApiError, AuthError
from sag_api.db.models import User
from sag_api.fnos.identity import (
    GatewayIdentity,
    InternalIdentitySigner,
    parse_gateway_identity,
)
from sag_api.services.fnos_user_service import get_or_create_fnos_user


def test_gateway_identity_uses_uid_not_username() -> None:
    """Catches treating the mutable display name as the fnOS account key."""
    identity = parse_gateway_identity(
        {
            "x-trim-userid": "1000",
            "x-trim-username": "Alice",
            "x-trim-isadmin": "false",
        }
    )
    assert identity == GatewayIdentity(uid=1000, username="Alice", is_admin=False)


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({"x-trim-username": "Alice", "x-trim-isadmin": "false"}, "missing UID"),
        (
            {"x-trim-userid": "10.5", "x-trim-username": "Alice", "x-trim-isadmin": "false"},
            "non-decimal UID",
        ),
        (
            {"x-trim-userid": "0", "x-trim-username": "Alice", "x-trim-isadmin": "false"},
            "UID zero",
        ),
        (
            {"x-trim-userid": "1", "x-trim-username": "Alice", "x-trim-isadmin": "yes"},
            "invalid admin value",
        ),
        (
            {"x-trim-userid": "1", "x-trim-username": "Alice", "X-Trim-Userid": "1", "x-trim-isadmin": "false"},
            "duplicate logical UID header",
        ),
    ],
)
def test_gateway_identity_rejects_untrusted_header_shapes(headers: dict[str, str], reason: str) -> None:
    """Catches ambiguous or malformed gateway headers selecting an unintended identity."""
    with pytest.raises(AuthError):
        parse_gateway_identity(headers)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda h: h.pop("x-trim-username"), "missing username"),
        (lambda h: h.update({"X-Trim-Username": "Other"}), "duplicate username header"),
        (lambda h: h.update({"x-trim-username": "汉" * 121}), "over 120 characters"),
        (lambda h: h.update({"x-trim-username": "bad\ud800"}), "invalid UTF-8"),
    ],
)
def test_gateway_identity_silently_normalizes_bad_usernames(mutation, reason: str) -> None:
    """Username hashing is defense-in-depth; its anomalies must never surface to users."""
    headers = {"x-trim-userid": "1", "x-trim-username": "Alice", "x-trim-isadmin": "true"}
    mutation(headers)
    identity = parse_gateway_identity(headers)
    assert identity == GatewayIdentity(uid=1, username="", is_admin=True)


def test_internal_signature_rejects_uid_substitution() -> None:
    """Catches a caller changing the signed identity's stable numeric identifier."""
    signer = InternalIdentitySigner(b"s" * 32, max_age_seconds=30)
    headers = signer.sign(GatewayIdentity(1000, "Alice", False), "req-1", 100)
    headers["x-sag-internal-uid"] = "1001"
    with pytest.raises(AuthError):
        signer.verify(headers, expected_uid=1000, now=100)


def test_internal_signature_binds_expected_username_only_when_requested() -> None:
    """Catches a gateway/worker identity split routing one user's request to another's data."""
    signer = InternalIdentitySigner(b"s" * 32, max_age_seconds=30)
    headers = signer.sign(GatewayIdentity(1000, "Alice", False), "req-1", 100)
    with pytest.raises(AuthError):
        signer.verify(headers, expected_uid=1000, now=100, expected_username="Bob")

    headers = signer.sign(GatewayIdentity(1000, "Alice", False), "req-2", 100)
    verified = signer.verify(headers, expected_uid=1000, now=100, expected_username="Alice")
    assert verified == GatewayIdentity(1000, "Alice", False)

    # expected_username=None (isolation switch off) must tolerate any username.
    headers = signer.sign(GatewayIdentity(1000, "Alicia", False), "req-3", 100)
    verified = signer.verify(headers, expected_uid=1000, now=100, expected_username=None)
    assert verified == GatewayIdentity(1000, "Alicia", False)


def test_internal_signature_round_trips_an_empty_normalized_username() -> None:
    """Catches the empty fallback username being rejected on the internal hop."""
    signer = InternalIdentitySigner(b"s" * 32, max_age_seconds=30)
    headers = signer.sign(GatewayIdentity(1000, "", False), "req-1", 100)
    verified = signer.verify(headers, expected_uid=1000, now=100, expected_username="")
    assert verified == GatewayIdentity(1000, "", False)


@pytest.mark.parametrize(
    ("signed_at", "verified_at", "reason"),
    [(69, 100, "stale signature"), (106, 100, "future signature")],
)
def test_internal_signature_rejects_stale_or_excessively_future_timestamp(
    signed_at: int, verified_at: int, reason: str
) -> None:
    """Catches replaying old credentials or accepting a clock-skewed future credential."""
    signer = InternalIdentitySigner(b"s" * 32, max_age_seconds=30)
    headers = signer.sign(GatewayIdentity(1000, "Alice", False), "req-1", signed_at)
    with pytest.raises(AuthError):
        signer.verify(headers, expected_uid=1000, now=verified_at)


def test_internal_signature_rejects_duplicate_logical_headers() -> None:
    """Catches case-variant duplicate headers bypassing deterministic signature checks."""
    signer = InternalIdentitySigner(b"s" * 32, max_age_seconds=30)
    headers = signer.sign(GatewayIdentity(1000, "Alice", False), "req-1", 100)
    headers["x-sag-internal-username"] = "Mallory"
    with pytest.raises(AuthError):
        signer.verify(headers, expected_uid=1000, now=100)


def test_internal_signature_rejects_request_id_over_128_characters() -> None:
    """Catches unbounded request identifiers becoming a signed-header resource sink."""
    signer = InternalIdentitySigner(b"s" * 32, max_age_seconds=30)
    with pytest.raises(AuthError):
        signer.sign(GatewayIdentity(1000, "Alice", False), "r" * 129, 100)


def test_internal_signature_rejects_a_replayed_request_id() -> None:
    """Catches a captured valid internal hop being reused during its validity window."""
    signer = InternalIdentitySigner(b"s" * 32, max_age_seconds=30)
    headers = signer.sign(GatewayIdentity(1000, "Alice", False), "req-once", 100)

    assert signer.verify(headers, expected_uid=1000, now=100) == GatewayIdentity(
        1000, "Alice", False
    )
    with pytest.raises(AuthError):
        signer.verify(headers, expected_uid=1000, now=100)


def test_internal_signature_uses_the_specified_canonical_payload() -> None:
    """Catches a protocol drift that would make the worker and API sign different bytes."""
    identity = GatewayIdentity(uid=1000, username="Alice", is_admin=True)
    signer = InternalIdentitySigner(b"s" * 32, max_age_seconds=30)

    headers = signer.sign(identity, "req-1", 100)

    payload = b"v1\n100\nreq-1\n1000\nAlice\n1"
    assert headers["X-SAG-Internal-Signature"] == hmac.new(
        b"s" * 32, payload, hashlib.sha256
    ).hexdigest()
    assert signer.verify(headers, expected_uid=1000, now=100) == identity


def test_internal_signer_file_requires_private_lowercase_32_byte_hex_key(tmp_path: Path) -> None:
    """Catches loading a readable or malformed shared identity-signing secret."""
    path = tmp_path / "identity.key"
    path.write_text("a" * 64, encoding="ascii")
    path.chmod(0o600)

    signer = InternalIdentitySigner.from_file(path)
    assert signer.verify(
        signer.sign(GatewayIdentity(8, "Ada", False), "req-1", 100),
        expected_uid=8,
        now=100,
    ) == GatewayIdentity(8, "Ada", False)

    path.chmod(0o640)
    with pytest.raises(AuthError):
        InternalIdentitySigner.from_file(path)

    path.chmod(0o600)
    path.write_text("A" * 64, encoding="ascii")
    with pytest.raises(AuthError):
        InternalIdentitySigner.from_file(path)


@pytest.mark.asyncio
async def test_fnos_user_is_stable_by_uid_and_only_refreshes_name() -> None:
    """Catches fnOS renames creating another local account or altering other stable fields."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as session:
        created = await get_or_create_fnos_user(session, GatewayIdentity(1000, "Alice", False))
        password_hash = created.password_hash
        updated = await get_or_create_fnos_user(session, GatewayIdentity(1000, "Alicia", True))
        users = (await session.scalars(select(User))).all()

    assert created.id == "fnos_1000"
    assert created.email == "fnos-1000@local.invalid"
    assert created.password_initialized is False
    assert created.auth_singleton == 1
    assert updated.id == created.id
    assert updated.name == "Alicia"
    assert updated.password_hash == password_hash
    assert len(users) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_fnos_user_display_name_falls_back_when_username_is_empty() -> None:
    """Catches the empty normalized username producing a blank display name."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as session:
        created = await get_or_create_fnos_user(session, GatewayIdentity(1000, "", False))
        created_name = created.name
        refreshed = await get_or_create_fnos_user(session, GatewayIdentity(1000, "Alice", False))
        refreshed_name = refreshed.name
        reverted = await get_or_create_fnos_user(session, GatewayIdentity(1000, "", False))

    assert created_name == "fnos_1000"
    assert refreshed_name == "Alice"
    assert reverted.name == "fnos_1000"
    await engine.dispose()


@pytest.mark.asyncio
async def test_fnos_mode_requires_signed_internal_identity_and_disables_local_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches fnOS mode accepting a browser bearer token or exposing local auth setup."""
    secret_path = tmp_path / "identity.key"
    secret_path.write_text("b" * 64, encoding="ascii")
    secret_path.chmod(0o600)
    signer = InternalIdentitySigner.from_file(secret_path)
    monkeypatch.setitem(settings.__dict__, "auth_mode", "fnos")
    monkeypatch.setitem(settings.__dict__, "fnos_uid", 1000)
    monkeypatch.setitem(settings.__dict__, "fnos_internal_secret_file", str(secret_path))
    monkeypatch.setitem(settings.__dict__, "fnos_username", "Alice")
    monkeypatch.setitem(settings.__dict__, "fnos_username_isolation", True)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def session_override():
        async with sessions() as session:
            yield session

    app = FastAPI()
    from sag_api.api.v1.auth import router

    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_session] = session_override

    @app.get("/protected")
    async def protected(user: User = Depends(get_current_user)) -> dict[str, str]:
        return {"id": user.id, "name": user.name}

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, error: ApiError):
        return JSONResponse(
            status_code=error.status_code,
            content={"error": {"code": error.code, "message": error.message}},
        )

    transport = httpx.ASGITransport(app=app)
    headers = signer.sign(
        GatewayIdentity(1000, "Alice", False), "req-1", int(time.time())
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing_internal = await client.get(
            "/protected", headers={"Authorization": "Bearer definitely-not-a-jwt"}
        )
        signed = await client.get("/protected", headers=headers)
        replayed_signed = await client.get("/protected", headers=headers)
        mismatched = signer.sign(GatewayIdentity(1000, "Bob", False), "req-2", int(time.time()))
        rejected = await client.get("/protected", headers=mismatched)
        session = await client.get("/api/v1/auth/session")
        register = await client.post(
            "/api/v1/auth/register", json={"email": "a@example.com", "password": "password123"}
        )
        login = await client.post("/api/v1/auth/login", json={"name": "Alice"})
        setup = await client.post("/api/v1/auth/session", json={"name": "Alice"})
        reset = await client.delete("/api/v1/auth/session")
        malformed_register = await client.post(
            "/api/v1/auth/register", content="{", headers={"Content-Type": "application/json"}
        )
        empty_register = await client.post("/api/v1/auth/register", json={})
        malformed_login = await client.post(
            "/api/v1/auth/login", content="{", headers={"Content-Type": "application/json"}
        )
        empty_login = await client.post("/api/v1/auth/login", json={})
        malformed_setup = await client.post(
            "/api/v1/auth/session", content="{", headers={"Content-Type": "application/json"}
        )
        empty_setup = await client.post("/api/v1/auth/session", json={})

    assert missing_internal.status_code == 401
    assert signed.json() == {"id": "fnos_1000", "name": "Alice"}
    assert replayed_signed.status_code == 401
    assert rejected.status_code == 401
    assert session.json() == {"setup_required": False, "user": None}
    assert [register.status_code, login.status_code, setup.status_code, reset.status_code] == [404, 404, 404, 404]
    assert [
        malformed_register.status_code,
        empty_register.status_code,
        malformed_login.status_code,
        empty_login.status_code,
        malformed_setup.status_code,
        empty_setup.status_code,
    ] == [404, 404, 404, 404, 404, 404]
    await engine.dispose()


@pytest.mark.asyncio
async def test_fnos_mode_ignores_username_when_isolation_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches username isolation being enforced when the feature switch is off."""
    secret_path = tmp_path / "identity.key"
    secret_path.write_text("b" * 64, encoding="ascii")
    secret_path.chmod(0o600)
    signer = InternalIdentitySigner.from_file(secret_path)
    monkeypatch.setitem(settings.__dict__, "auth_mode", "fnos")
    monkeypatch.setitem(settings.__dict__, "fnos_uid", 1000)
    monkeypatch.setitem(settings.__dict__, "fnos_internal_secret_file", str(secret_path))
    monkeypatch.setitem(settings.__dict__, "fnos_username", "Alice")
    monkeypatch.setitem(settings.__dict__, "fnos_username_isolation", False)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def session_override():
        async with sessions() as session:
            yield session

    app = FastAPI()
    from sag_api.api.v1.auth import router

    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_session] = session_override

    @app.get("/protected")
    async def protected(user: User = Depends(get_current_user)) -> dict[str, str]:
        return {"id": user.id, "name": user.name}

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, error: ApiError):
        return JSONResponse(
            status_code=error.status_code,
            content={"error": {"code": error.code, "message": error.message}},
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        renamed_headers = signer.sign(
            GatewayIdentity(1000, "Bob", False), "req-3", int(time.time())
        )
        renamed = await client.get("/protected", headers=renamed_headers)

    assert renamed.status_code == 200  # 关：改名/username 漂移不 401
    await engine.dispose()
