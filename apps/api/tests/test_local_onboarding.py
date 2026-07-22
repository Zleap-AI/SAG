from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.errors import AuthError
from sag_api.core.security import hash_password
from sag_api.db.models import User
from sag_api.services.auth_service import authenticate_or_register


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
