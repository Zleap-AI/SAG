from __future__ import annotations

import httpx
import pytest
from sqlalchemy import delete


@pytest.mark.asyncio
async def test_password_mode_rejects_name_only_login() -> None:
    from sag_api.core.config import settings
    from sag_api.main import app

    previous = settings.auth_mode
    settings.auth_mode = "password"
    try:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                registered = await client.post(
                    "/api/v1/auth/register",
                    json={"email": "owner@example.test", "password": "StrongPassword123", "name": "Owner"},
                )
                assert registered.status_code == 201

                response = await client.post("/api/v1/auth/login", json={"name": "Attacker"})
                email_only = await client.post(
                    "/api/v1/auth/login", json={"email": "owner@example.test"}
                )
                wrong_password = await client.post(
                    "/api/v1/auth/login",
                    json={"email": "owner@example.test", "password": "WrongPassword"},
                )

        assert response.status_code == 401
        assert email_only.status_code == 401
        assert wrong_password.status_code == 401
    finally:
        settings.auth_mode = previous


@pytest.mark.asyncio
async def test_local_mode_rejects_password_registration() -> None:
    from sag_api.core.config import settings
    from sag_api.main import app

    previous = settings.auth_mode
    settings.auth_mode = "local"
    try:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/auth/register",
                    json={"email": "local-register@example.test", "password": "StrongPassword123"},
                )

        assert response.status_code == 403
    finally:
        settings.auth_mode = previous


@pytest.mark.asyncio
async def test_password_mode_reports_first_account_setup() -> None:
    from sag_api.core.config import settings
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import User
    from sag_api.main import app

    previous_mode = settings.auth_mode
    previous_registration = settings.allow_registration
    settings.auth_mode = "password"
    settings.allow_registration = False
    try:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with SessionLocal() as session:
                await session.execute(delete(User))
                await session.commit()
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/v1/auth/status")

        assert response.status_code == 200
        assert response.json() == {
            "mode": "password",
            "registration_required": True,
            "registration_open": True,
        }
    finally:
        settings.auth_mode = previous_mode
        settings.allow_registration = previous_registration


@pytest.mark.asyncio
async def test_password_mode_allows_first_credential_account_after_local_identity() -> None:
    from sag_api.core.config import settings
    from sag_api.core.db import SessionLocal
    from sag_api.core.security import hash_password
    from sag_api.db.models import User
    from sag_api.main import app

    previous_mode = settings.auth_mode
    previous_registration = settings.allow_registration
    settings.auth_mode = "password"
    settings.allow_registration = False
    try:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with SessionLocal() as session:
                await session.execute(delete(User))
                session.add(User(email="", password_hash=hash_password("admin"), name="Local Owner"))
                await session.commit()
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/auth/register",
                    json={"email": "owner@example.test", "password": "StrongPassword123", "name": "Owner"},
                )

        assert response.status_code == 201
        assert response.json()["user"]["email"] == "owner@example.test"
    finally:
        settings.auth_mode = previous_mode
        settings.allow_registration = previous_registration


@pytest.mark.asyncio
async def test_password_mode_logs_in_with_email_and_password_without_name() -> None:
    from sag_api.core.config import settings
    from sag_api.main import app

    previous_mode = settings.auth_mode
    previous_registration = settings.allow_registration
    settings.auth_mode = "password"
    settings.allow_registration = True
    try:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                registered = await client.post(
                    "/api/v1/auth/register",
                    json={"email": "login@example.test", "password": "StrongPassword123", "name": "Owner"},
                )
                assert registered.status_code == 201

                response = await client.post(
                    "/api/v1/auth/login",
                    json={"email": "login@example.test", "password": "StrongPassword123"},
                )
                status = await client.get("/api/v1/auth/status")

        assert response.status_code == 200
        assert response.json()["user"]["id"] == registered.json()["user"]["id"]
        assert status.json()["registration_required"] is False
    finally:
        settings.auth_mode = previous_mode
        settings.allow_registration = previous_registration


@pytest.mark.asyncio
async def test_password_mode_rejects_token_issued_by_local_mode() -> None:
    from sag_api.core.config import settings
    from sag_api.main import app

    previous_mode = settings.auth_mode
    settings.auth_mode = "local"
    try:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                local_login = await client.post("/api/v1/auth/login", json={"name": "Local Owner"})
                assert local_login.status_code == 200
                headers = {"Authorization": f"Bearer {local_login.json()['access_token']}"}

                settings.auth_mode = "password"
                response = await client.get("/api/v1/auth/me", headers=headers)

        assert response.status_code == 401
    finally:
        settings.auth_mode = previous_mode


@pytest.mark.asyncio
async def test_local_mode_does_not_create_password_credentials_from_login_payload() -> None:
    from sag_api.core.config import settings
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import User
    from sag_api.main import app

    previous_mode = settings.auth_mode
    settings.auth_mode = "local"
    try:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with SessionLocal() as session:
                await session.execute(delete(User))
                await session.commit()
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/auth/login",
                    json={"name": "Local Owner", "email": "ignored@example.test", "password": "ignored"},
                )

        assert response.status_code == 200
        assert response.json()["user"]["email"] == ""
    finally:
        settings.auth_mode = previous_mode
