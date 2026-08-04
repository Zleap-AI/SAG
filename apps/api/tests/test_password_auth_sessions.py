from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.config import settings
from sag_api.core.errors import ApiError, AuthError
from sag_api.core.security import create_access_token, decode_token, hash_password
from sag_api.db.models import User


def password_mode(monkeypatch: pytest.MonkeyPatch, bootstrap: str = "a" * 64) -> None:
    monkeypatch.setitem(settings.__dict__, "auth_mode", "password")
    monkeypatch.setitem(settings.__dict__, "auth_bootstrap_token", bootstrap)
    monkeypatch.setitem(settings.__dict__, "auth_password_min_length", 12)


def auth_app(sessions):
    from sag_api.api.v1.auth import router
    from sag_api.core.db import get_session

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

    return app


@pytest.mark.asyncio
async def test_upgrade_bootstrap_and_local_reset_revoke_all_older_password_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches password mode accepting pre-upgrade or pre-reset JWTs without a version."""
    password_mode(monkeypatch)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as session:
        legacy = User(
            email="",
            password_hash=hash_password("admin"),
            password_initialized=False,
            auth_version=0,
            auth_singleton=1,
            name="Legacy",
        )
        session.add(legacy)
        await session.commit()
        legacy_id = legacy.id
    pre_upgrade_token = create_access_token(legacy_id)

    app = auth_app(sessions)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        initialized = await client.post(
            "/api/v1/auth/login",
            json={
                "name": "Legacy",
                "password": "new secure password",
                "bootstrap_token": "a" * 64,
            },
        )
        assert initialized.status_code == 200
        first_password_token = initialized.json()["access_token"]
        assert decode_token(first_password_token)["auth_version"] == 1
        assert (
            await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {pre_upgrade_token}"},
            )
        ).status_code == 401
        assert (
            await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {first_password_token}"},
            )
        ).status_code == 200

        async with sessions() as session:
            user = await session.get(User, legacy_id)
            user.password_initialized = False
            user.auth_version += 1
            await session.commit()
        monkeypatch.setitem(settings.__dict__, "auth_bootstrap_token", "b" * 64)

        assert (
            await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {first_password_token}"},
            )
        ).status_code == 401
        old_bootstrap = await client.post(
            "/api/v1/auth/login",
            json={
                "name": "Legacy",
                "password": "replacement password",
                "bootstrap_token": "a" * 64,
            },
        )
        assert old_bootstrap.status_code == 401
        async with sessions() as session:
            still_waiting = await session.get(User, legacy_id)
            assert still_waiting is not None
            assert still_waiting.password_initialized is False
            assert still_waiting.auth_version == 2
        reset_initialized = await client.post(
            "/api/v1/auth/login",
            json={
                "name": "Legacy",
                "password": "replacement password",
                "bootstrap_token": "b" * 64,
            },
        )
        assert reset_initialized.status_code == 200
        reset_token = reset_initialized.json()["access_token"]
        assert decode_token(reset_token)["auth_version"] == 3
        assert (
            await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {reset_token}"},
            )
        ).status_code == 200

    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_login_and_register_create_exactly_one_password_user(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches check-then-insert races creating two fnOS owners."""
    from sag_api.services import auth_service

    password_mode(monkeypatch)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
        await connection.execute(
            text(
                "CREATE UNIQUE INDEX ux_users_auth_singleton "
                "ON users (auth_singleton)"
            )
        )
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    real_hash = auth_service.hash_password_async
    both_hashing = asyncio.Event()
    hashing_count = 0

    async def synchronized_hash(password: str) -> str:
        nonlocal hashing_count
        hashing_count += 1
        if hashing_count == 2:
            both_hashing.set()
        await asyncio.wait_for(both_hashing.wait(), timeout=5)
        return await real_hash(password)

    monkeypatch.setattr(auth_service, "hash_password_async", synchronized_hash)

    async def login():
        async with sessions() as session:
            return await auth_service.authenticate_or_register(
                session,
                name="Login owner",
                password="login password",
                bootstrap_token="a" * 64,
            )

    async def register():
        async with sessions() as session:
            return await auth_service.register_user(
                session,
                email="register@example.com",
                name="Register owner",
                password="register password",
                bootstrap_token="a" * 64,
            )

    results = await asyncio.gather(login(), register(), return_exceptions=True)
    successes = [result for result in results if isinstance(result, User)]
    failures = [result for result in results if isinstance(result, AuthError)]
    async with sessions() as session:
        count = await session.scalar(select(func.count()).select_from(User))

    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].message == "身份验证失败"
    assert count == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_rejects_missing_or_stale_password_auth_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches MCP bypassing the database-backed JWT revocation check."""
    from sag_api.mcp import mount

    password_mode(monkeypatch)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
        from sag_api.db.models import Source

        await connection.run_sync(Source.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(mount, "SessionLocal", sessions)

    async with sessions() as session:
        user = User(
            email="",
            password_hash=hash_password("valid password"),
            password_initialized=True,
            auth_version=4,
            auth_singleton=1,
            name="Owner",
        )
        session.add(user)
        await session.commit()
        user_id = user.id

    async def accepted(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    parent = FastAPI()
    parent.state.engine_manager = object()
    app = mount.ScopedKnowledgeMCP(parent, accepted)
    transport = httpx.ASGITransport(app=app)

    tokens = {
        "missing": create_access_token(user_id),
        "stale": create_access_token(user_id, {"auth_version": 3}),
        "current": create_access_token(user_id, {"auth_version": 4}),
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.post(
            "/",
            headers={"Authorization": f"Bearer {tokens['missing']}"},
        )
        stale = await client.post(
            "/",
            headers={"Authorization": f"Bearer {tokens['stale']}"},
        )
        current = await client.post(
            "/",
            headers={"Authorization": f"Bearer {tokens['current']}"},
        )

    assert missing.status_code == 401
    assert stale.status_code == 401
    assert current.status_code == 204
    await engine.dispose()


@pytest.mark.asyncio
async def test_password_mode_startup_refuses_ambiguous_legacy_users_without_deleting_them(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches migration silently selecting one owner from legitimate multi-user data."""
    from sag_api.core import db

    password_mode(monkeypatch)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-multi.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
        connection_user_rows = [
            {
                "id": f"user-{index}",
                "email": f"user-{index}@example.com",
                "password_hash": "preserve",
                "password_initialized": False,
                "auth_version": 0,
                "auth_singleton": None,
                "name": f"User {index}",
                "is_active": True,
            }
            for index in (1, 2)
        ]
        await connection.execute(User.__table__.insert(), connection_user_rows)
    monkeypatch.setattr(db, "engine", engine)

    with pytest.raises(RuntimeError, match="多个旧用户"):
        await db._ensure_password_auth_singleton()

    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(User.id, User.auth_singleton).order_by(User.id)
            )
        ).all()
    assert rows == [("user-1", None), ("user-2", None)]
    await engine.dispose()
