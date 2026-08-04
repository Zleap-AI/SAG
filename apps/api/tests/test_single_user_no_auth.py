from __future__ import annotations

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.api.v1.auth import router
from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user
from sag_api.core.errors import ApiError
from sag_api.db.models import User


@pytest.mark.asyncio
async def test_single_user_mode_initializes_once_and_never_authenticates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches fnOS requiring credentials or creating a second identity on a new browser."""
    monkeypatch.setitem(settings.__dict__, "auth_mode", "single_user")

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
        empty = await client.get("/api/v1/auth/session")
        created = await client.post("/api/v1/auth/session", json={"name": "  Ada  "})
        existing = await client.get("/api/v1/auth/session")
        repeated_setup = await client.post(
            "/api/v1/auth/session", json={"name": "Someone Else"}
        )
        anonymous = await client.get("/protected")
        invalid_bearer = await client.get(
            "/protected", headers={"Authorization": "Bearer definitely-not-a-jwt"}
        )
        reset = await client.delete("/api/v1/auth/session")
        after_reset = await client.get("/api/v1/auth/session")
        protected_after_reset = await client.get("/protected")
        reinitialized = await client.post(
            "/api/v1/auth/session", json={"name": "Grace"}
        )

    assert empty.status_code == 200
    assert empty.json() == {"setup_required": True, "user": None}
    assert created.status_code == 201
    created_user = created.json()["user"]
    assert created.json()["setup_required"] is False
    assert created_user["name"] == "Ada"
    assert existing.json()["user"]["id"] == created_user["id"]
    assert repeated_setup.json()["user"]["id"] == created_user["id"]
    assert repeated_setup.json()["user"]["name"] == "Ada"
    assert anonymous.json() == {"id": created_user["id"], "name": "Ada"}
    assert invalid_bearer.json() == anonymous.json()
    assert reset.status_code == 204
    assert after_reset.json() == {"setup_required": True, "user": None}
    assert protected_after_reset.status_code == 401
    assert reinitialized.status_code == 201
    assert reinitialized.json()["user"]["id"] == created_user["id"]
    assert reinitialized.json()["user"]["name"] == "Grace"

    await engine.dispose()
