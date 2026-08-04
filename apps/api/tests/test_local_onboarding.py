from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.config import settings
from sag_api.core.errors import ApiError, AuthError
from sag_api.core.security import hash_password, verify_password
from sag_api.db.models import User
from sag_api.schemas.auth import LoginRequest, RegisterRequest
from sag_api.services.auth_service import authenticate_or_register, register_user


@pytest.mark.asyncio
async def test_login_creates_and_resumes_local_identity_without_email() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        created = await authenticate_or_register(session, name="  Ada  ")
        created_id = created.id
        resumed = await authenticate_or_register(session, name="Ada")
        with pytest.raises(AuthError):
            await authenticate_or_register(session, name="Blocked rename", password="wrong")
        renamed = await authenticate_or_register(session, name="Aha")

        legacy = User(
            email="legacy@example.com",
            password_hash=hash_password("legacy-password"),
            name="Legacy",
        )
        session.add(legacy)
        await session.commit()
        resumed_legacy = await authenticate_or_register(session, name="Legacy")

    assert created.email == ""
    assert resumed.id == created_id
    assert renamed.id == created_id
    assert renamed.name == "Aha"
    assert resumed_legacy.id == legacy.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_password_mode_requires_bootstrap_then_password_without_renaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a production login falling back to name-only token issuance or rename."""
    monkeypatch.setitem(settings.__dict__, "auth_mode", "password")
    monkeypatch.setitem(settings.__dict__, "auth_bootstrap_token", "b" * 64)
    monkeypatch.setitem(settings.__dict__, "auth_password_min_length", 12)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        with pytest.raises(AuthError, match="身份验证失败"):
            await authenticate_or_register(
                session,
                name="Ada",
                password="correct horse",
            )
        with pytest.raises(AuthError, match="身份验证失败"):
            await authenticate_or_register(
                session,
                name="Ada",
                password="short",
                bootstrap_token="b" * 64,
            )

        created = await authenticate_or_register(
            session,
            name="Ada",
            password="correct horse",
            bootstrap_token="b" * 64,
        )
        original_hash = created.password_hash

        assert created.password_initialized is True
        assert created.password_hash != "correct horse"
        assert verify_password("correct horse", created.password_hash)

        for attempt in (
            {"name": "Ada"},
            {"name": "Ada", "password": "wrong password"},
            {"name": "Renamed", "password": "correct horse"},
        ):
            with pytest.raises(AuthError, match="身份验证失败"):
                await authenticate_or_register(session, **attempt)

        resumed = await authenticate_or_register(
            session,
            name="Ada",
            password="correct horse",
        )
        await session.refresh(resumed)
        with pytest.raises(AuthError, match="身份验证失败"):
            await authenticate_or_register(
                session,
                name="Ada",
                password="replacement password",
                bootstrap_token="b" * 64,
            )

        assert resumed.id == created.id
        assert resumed.name == "Ada"
        assert resumed.password_hash == original_hash

    await engine.dispose()


@pytest.mark.asyncio
async def test_password_mode_upgrades_existing_passwordless_user_only_with_private_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an upgrade that accepts the legacy implicit password or LAN-only name."""
    monkeypatch.setitem(settings.__dict__, "auth_mode", "password")
    monkeypatch.setitem(settings.__dict__, "auth_bootstrap_token", "c" * 64)
    monkeypatch.setitem(settings.__dict__, "auth_password_min_length", 12)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        legacy = User(
            email="",
            password_hash=hash_password("admin"),
            password_initialized=False,
            name="Legacy",
        )
        session.add(legacy)
        await session.commit()
        legacy_hash = legacy.password_hash

        for bootstrap_token in (None, "wrong"):
            with pytest.raises(AuthError, match="身份验证失败"):
                await authenticate_or_register(
                    session,
                    name="Legacy",
                    password="new secure password",
                    bootstrap_token=bootstrap_token,
                )
        with pytest.raises(AuthError, match="身份验证失败"):
            await authenticate_or_register(
                session,
                name="Attacker",
                password="new secure password",
                bootstrap_token="c" * 64,
            )
        legacy.is_active = False
        await session.commit()
        with pytest.raises(AuthError, match="身份验证失败"):
            await authenticate_or_register(
                session,
                name="Legacy",
                password="new secure password",
                bootstrap_token="c" * 64,
            )
        legacy.is_active = True
        await session.commit()
        await session.refresh(legacy)
        assert legacy.password_hash == legacy_hash
        assert legacy.password_initialized is False
        assert legacy.auth_version == 0

        upgraded = await authenticate_or_register(
            session,
            name="Legacy",
            password="new secure password",
            bootstrap_token="c" * 64,
        )
        assert upgraded.password_initialized is True
        assert verify_password("new secure password", upgraded.password_hash)

        with pytest.raises(AuthError, match="身份验证失败"):
            await authenticate_or_register(session, name="Legacy", password="admin")
        assert (
            await authenticate_or_register(
                session,
                name="Legacy",
                password="new secure password",
            )
        ).id == legacy.id

    await engine.dispose()


@pytest.mark.asyncio
async def test_password_mode_register_requires_bootstrap_and_hides_existing_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches the legacy /register route bypassing the production bootstrap gate."""
    from sag_api.services import auth_service

    monkeypatch.setitem(settings.__dict__, "auth_mode", "password")
    monkeypatch.setitem(settings.__dict__, "auth_bootstrap_token", "d" * 64)
    monkeypatch.setitem(settings.__dict__, "auth_password_min_length", 12)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        dummy_checks: list[str] = []

        async def record_dummy_check(password: str) -> None:
            dummy_checks.append(password)

        monkeypatch.setattr(
            auth_service,
            "consume_dummy_password_check",
            record_dummy_check,
        )
        route_valid_short_password = RegisterRequest(
            email="owner@example.com",
            password="12345678",
            name="Owner",
            bootstrap_token="d" * 64,
        ).password
        with pytest.raises(AuthError, match="身份验证失败"):
            await register_user(
                session,
                email="owner@example.com",
                password=route_valid_short_password,
                name="Owner",
                bootstrap_token="d" * 64,
            )
        assert dummy_checks == [route_valid_short_password]
        dummy_checks.clear()

        with pytest.raises(AuthError, match="身份验证失败"):
            await register_user(
                session,
                email="owner@example.com",
                password="correct horse",
                name="Owner",
            )
        assert dummy_checks == ["correct horse"]
        dummy_checks.clear()
        created = await register_user(
            session,
            email="owner@example.com",
            password="correct horse",
            name="Owner",
            bootstrap_token="d" * 64,
        )
        assert created.password_initialized is True
        assert dummy_checks == []

        for email in ("owner@example.com", "attacker@example.com"):
            with pytest.raises(AuthError, match="身份验证失败"):
                await register_user(
                    session,
                    email=email,
                    password="another secure password",
                    name="Attacker",
                    bootstrap_token="d" * 64,
                )
        assert dummy_checks == ["another secure password", "another secure password"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_embedded_upgrade_marks_existing_users_as_needing_password_initialization(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an embedded DB migration treating legacy implicit hashes as initialized."""
    from sqlalchemy import inspect

    from sag_api.core import db

    database = tmp_path / "legacy.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TABLE users (
                    id VARCHAR(36) PRIMARY KEY,
                    email VARCHAR(255) NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    name VARCHAR(120) NOT NULL,
                    is_active BOOLEAN NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO users
                    (id, email, password_hash, name, is_active, created_at, updated_at)
                VALUES
                    ('legacy', '', 'not-plaintext', 'Legacy', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )

    monkeypatch.setattr(db, "engine", engine)
    await db._ensure_columns()

    async with engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: {column["name"] for column in inspect(sync).get_columns("users")}
        )
        initialized = await connection.scalar(
            text("SELECT password_initialized FROM users WHERE id = 'legacy'")
        )

    assert "password_initialized" in columns
    assert initialized == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_password_mode_http_route_has_one_generic_failure_and_forwards_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a request model/route dropping bootstrap or exposing which credential failed."""
    from sag_api.api.v1.auth import router
    from sag_api.core.db import get_session

    monkeypatch.setitem(settings.__dict__, "auth_mode", "password")
    monkeypatch.setitem(settings.__dict__, "auth_bootstrap_token", "f" * 64)
    monkeypatch.setitem(settings.__dict__, "auth_password_min_length", 12)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def session_override():
        async with sessions() as session:
            yield session

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_session] = session_override

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, error: ApiError):
        return JSONResponse(
            status_code=error.status_code,
            content={"error": {"code": error.code, "message": error.message}},
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        wrong_bootstrap = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "ada@example.com",
                "name": "Ada",
                "password": "correct horse",
                "bootstrap_token": "wrong",
            },
        )
        initialized = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "ada@example.com",
                "name": "Ada",
                "password": "correct horse",
                "bootstrap_token": "f" * 64,
            },
        )
        missing_password = await client.post(
            "/api/v1/auth/login",
            json={"name": "Ada"},
        )
        wrong_password = await client.post(
            "/api/v1/auth/login",
            json={"name": "Ada", "password": "wrong password"},
        )
        wrong_name = await client.post(
            "/api/v1/auth/login",
            json={"name": "Renamed", "password": "correct horse"},
        )
        correct = await client.post(
            "/api/v1/auth/login",
            json={"name": "Ada", "password": "correct horse"},
        )
        replayed_bootstrap = await client.post(
            "/api/v1/auth/login",
            json={
                "name": "Ada",
                "password": "replacement password",
                "bootstrap_token": "f" * 64,
            },
        )
        old_password = await client.post(
            "/api/v1/auth/login",
            json={"name": "Ada", "password": "correct horse"},
        )

    expected_failure = {
        "error": {"code": "unauthorized", "message": "身份验证失败"}
    }
    assert wrong_bootstrap.status_code == 401
    assert wrong_bootstrap.json() == expected_failure
    assert initialized.status_code == 201
    assert missing_password.status_code == 401
    assert missing_password.json() == expected_failure
    assert wrong_password.status_code == 401
    assert wrong_password.json() == expected_failure
    assert wrong_name.status_code == 401
    assert wrong_name.json() == expected_failure
    assert correct.status_code == 200
    assert correct.json()["user"]["id"] == initialized.json()["user"]["id"]
    assert correct.json()["user"]["name"] == "Ada"
    assert replayed_bootstrap.status_code == 401
    assert replayed_bootstrap.json() == expected_failure
    assert old_password.status_code == 200
    assert old_password.json()["user"]["id"] == initialized.json()["user"]["id"]
    await engine.dispose()


@pytest.mark.parametrize("request_model", [LoginRequest, RegisterRequest])
def test_auth_schema_rejects_passwords_over_72_utf8_bytes(request_model) -> None:
    """Catches multibyte passwords reaching bcrypt after exceeding its byte boundary."""
    payload = {"name": "Ada", "password": "密" * 25}
    if request_model is RegisterRequest:
        payload["email"] = "ada@example.com"
    with pytest.raises(PydanticValidationError, match="72"):
        request_model(**payload)


@pytest.mark.asyncio
async def test_auth_request_validation_never_echoes_password_or_bootstrap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Catches FastAPI's default 422 body echoing invalid authentication inputs."""
    from sag_api.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    password = "p" * 73
    bootstrap = "b" * 257

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            "/api/v1/auth/login",
            json={"name": "Ada", "password": password},
        )
        register = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "ada@example.com",
                "name": "Ada",
                "password": "correct horse",
                "bootstrap_token": bootstrap,
            },
        )

    expected = {"error": {"code": "unauthorized", "message": "身份验证失败"}}
    assert login.status_code == 401
    assert register.status_code == 401
    assert login.json() == expected
    assert register.json() == expected
    observable = f"{login.text}\n{register.text}\n{caplog.text}"
    assert password not in observable
    assert bootstrap not in observable


@pytest.mark.asyncio
async def test_password_mode_unicode_name_and_bootstrap_are_normalized_or_rejected_not_crashed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches compare_digest receiving non-ASCII str values and raising TypeError."""
    monkeypatch.setitem(settings.__dict__, "auth_mode", "password")
    monkeypatch.setitem(settings.__dict__, "auth_bootstrap_token", "a" * 64)
    monkeypatch.setitem(settings.__dict__, "auth_password_min_length", 12)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as session:
        with pytest.raises(AuthError, match="身份验证失败"):
            await authenticate_or_register(
                session,
                name="艾达",
                password="correct horse",
                bootstrap_token="初始化密钥",
            )
        created = await authenticate_or_register(
            session,
            name="Ａda",
            password="correct horse",
            bootstrap_token="a" * 64,
        )
        assert created.name == "Ada"
        assert (
            await authenticate_or_register(
                session,
                name="Ada",
                password="correct horse",
            )
        ).id == created.id
        with pytest.raises(AuthError, match="身份验证失败"):
            await authenticate_or_register(
                session,
                name="艾达",
                password="correct horse",
            )

    await engine.dispose()
