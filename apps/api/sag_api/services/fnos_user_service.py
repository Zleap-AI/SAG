"""Persistence for the single fnOS identity assigned to this SAG database."""

from __future__ import annotations

import secrets

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.security import hash_password_async
from sag_api.db.models import User
from sag_api.fnos.identity import GatewayIdentity


async def get_or_create_fnos_user(session: AsyncSession, identity: GatewayIdentity) -> User:
    """Return the stable local user for a verified fnOS numeric identity."""
    user_id = f"fnos_{identity.uid}"
    display_name = identity.username or f"fnos_{identity.uid}"
    user = await session.get(User, user_id)
    if user is not None:
        if user.name != display_name:
            user.name = display_name
            await session.commit()
            await session.refresh(user)
        return user

    user = User(
        id=user_id,
        email=f"fnos-{identity.uid}@local.invalid",
        password_hash=await hash_password_async(secrets.token_urlsafe(32)),
        password_initialized=False,
        auth_singleton=1,
        name=display_name,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.get(User, user_id)
        if existing is None:
            raise
        return existing
    await session.refresh(user)
    return user
