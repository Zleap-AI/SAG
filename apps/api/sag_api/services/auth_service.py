"""认证与用户领域逻辑（单用户）。"""

from __future__ import annotations

import hmac
import secrets
import unicodedata

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.errors import AuthError, ConflictError, ForbiddenError, ValidationError
from sag_api.core.security import (
    consume_dummy_password_check,
    hash_password_async,
    password_bytes,
    verify_password_async,
)
from sag_api.db.models import User

_AUTH_FAILURE = "身份验证失败"


def _bootstrap_value() -> str:
    from sag_api.core.config import settings

    configured = settings.auth_bootstrap_token
    get_secret_value = getattr(configured, "get_secret_value", None)
    return get_secret_value() if get_secret_value is not None else str(configured)


def _valid_bootstrap(candidate: str | None) -> bool:
    configured = _bootstrap_value()
    if not candidate or not configured:
        return False
    try:
        return hmac.compare_digest(
            candidate.encode("utf-8"),
            configured.encode("utf-8"),
        )
    except UnicodeError:
        return False


def _normalized_name(value: str) -> str:
    try:
        normalized = unicodedata.normalize("NFKC", value.strip())
        normalized.encode("utf-8")
    except UnicodeError:
        return ""
    return normalized


def _same_text(left: str, right: str) -> bool:
    normalized_left = _normalized_name(left)
    normalized_right = _normalized_name(right)
    return bool(normalized_left and normalized_right) and hmac.compare_digest(
        normalized_left.encode("utf-8"),
        normalized_right.encode("utf-8"),
    )


def _valid_new_password(password: str | None) -> bool:
    from sag_api.core.config import settings

    if not password or len(password) < settings.auth_password_min_length:
        return False
    try:
        password_bytes(password)
    except (UnicodeError, ValueError):
        return False
    return True


async def _first_user(session: AsyncSession) -> User | None:
    return await session.scalar(select(User).order_by(User.created_at.asc()).limit(1))


async def get_single_user(session: AsyncSession) -> User | None:
    """Return the one local workspace user without treating its name as a credential."""
    user = await _first_user(session)
    return user if user is not None and _normalized_name(user.name) else None


async def initialize_single_user(session: AsyncSession, *, name: str) -> User:
    """Create the local workspace profile once, or return the profile another request created."""
    existing = await _first_user(session)
    normalized_name = _normalized_name(name)
    if existing is not None:
        if _normalized_name(existing.name):
            return existing
        existing.name = normalized_name
        await session.commit()
        await session.refresh(existing)
        return existing

    user = User(
        email="",
        password_hash=await hash_password_async(secrets.token_urlsafe(32)),
        password_initialized=False,
        auth_singleton=1,
        name=normalized_name,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await _first_user(session)
        if existing is None:
            raise
        return existing
    await session.refresh(user)
    return user


async def reset_single_user(session: AsyncSession) -> None:
    """Return the local workspace to first-use setup without deleting user-owned data."""
    user = await _first_user(session)
    if user is None:
        return
    user.name = ""
    await session.commit()


async def _commit_new_password_user(session: AsyncSession, user: User) -> User:
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise AuthError(_AUTH_FAILURE) from error
    await session.refresh(user)
    return user


async def register_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    name: str = "",
    bootstrap_token: str | None = None,
) -> User:
    from sag_api.core.config import settings

    if settings.auth_mode == "password":
        existing = await _first_user(session)
        valid_password = _valid_new_password(password)
        valid_bootstrap = _valid_bootstrap(bootstrap_token)
        if existing is not None:
            await consume_dummy_password_check(password)
            raise AuthError(_AUTH_FAILURE)
        if not valid_bootstrap or not valid_password:
            await consume_dummy_password_check(password)
            raise AuthError(_AUTH_FAILURE)
        user = User(
            email=email,
            password_hash=await hash_password_async(password),
            password_initialized=True,
            auth_version=1,
            auth_singleton=1,
            name=_normalized_name(name or email.split("@")[0]),
        )
        return await _commit_new_password_user(session, user)

    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise ConflictError("该邮箱已注册")

    # 个人向：首个注册即唯一账号；注册关闭时仅放行首个用户（部署引导）
    user_count = await session.scalar(select(func.count()).select_from(User)) or 0
    if user_count > 0 and not settings.allow_registration:
        raise ForbiddenError("注册已关闭")

    user = User(
        email=email,
        password_hash=await hash_password_async(password),
        password_initialized=True,
        name=name or email.split("@")[0],
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def authenticate(session: AsyncSession, *, email: str, password: str) -> User:
    user = await session.scalar(select(User).where(User.email == email))
    password_matches = (
        await verify_password_async(password, user.password_hash)
        if user is not None
        else False
    )
    if user is None:
        await consume_dummy_password_check(password)
    if user is None or not password_matches:
        raise AuthError("邮箱或密码错误")
    if not user.is_active:
        raise ForbiddenError("账号已停用")
    return user


async def authenticate_or_register(
    session: AsyncSession,
    *,
    name: str = "",
    email: str = "",
    password: str | None = None,
    bootstrap_token: str | None = None,
) -> User:
    from sag_api.core.config import settings

    name = name.strip()
    email = email.strip().lower()

    if settings.auth_mode == "password":
        name = _normalized_name(name)
        user = await _first_user(session)
        if user is None:
            valid_password = _valid_new_password(password)
            if not name or not valid_password or not _valid_bootstrap(bootstrap_token):
                if valid_password:
                    await consume_dummy_password_check(password or "")
                raise AuthError(_AUTH_FAILURE)
            user = User(
                email=email,
                password_hash=await hash_password_async(password or ""),
                password_initialized=True,
                auth_version=1,
                auth_singleton=1,
                name=name,
            )
            return await _commit_new_password_user(session, user)

        name_matches = bool(name) and _same_text(name, user.name)
        valid_password = _valid_new_password(password)
        if not user.password_initialized:
            if (
                not name_matches
                or not user.is_active
                or not _valid_bootstrap(bootstrap_token)
                or not valid_password
            ):
                if valid_password:
                    await consume_dummy_password_check(password or "")
                raise AuthError(_AUTH_FAILURE)
            password_hash = await hash_password_async(password or "")
            result = await session.execute(
                update(User)
                .where(
                    User.id == user.id,
                    User.password_initialized.is_(False),
                )
                .values(
                    password_hash=password_hash,
                    password_initialized=True,
                    auth_version=User.auth_version + 1,
                    auth_singleton=1,
                )
            )
            if result.rowcount != 1:
                await session.rollback()
                raise AuthError(_AUTH_FAILURE)
            await session.commit()
            initialized = await session.get(User, user.id)
            if initialized is None:
                raise AuthError(_AUTH_FAILURE)
            return initialized

        password_matches = (
            await verify_password_async(password or "", user.password_hash)
            if valid_password
            else False
        )
        if (
            bootstrap_token is not None
            or not name_matches
            or not password_matches
            or not user.is_active
        ):
            raise AuthError(_AUTH_FAILURE)
        return user

    password_supplied = bool(password)
    password = password or "admin"
    rename_local_user = False

    if email:
        user = await session.scalar(select(User).where(User.email == email))
    else:
        user = None
        if name:
            user = await session.scalar(
                select(User).where(User.name == name).order_by(User.created_at.asc()).limit(1)
            )
        if user is None:
            user = await session.scalar(
                select(User).order_by(User.created_at.asc()).limit(1)
            )
            rename_local_user = user is not None and bool(name) and user.name != name

    if user is not None:
        if password_supplied and not await verify_password_async(password, user.password_hash):
            raise AuthError("身份验证失败")
        if not user.is_active:
            raise ForbiddenError("账号已停用")
        if rename_local_user:
            user.name = name
            await session.commit()
            await session.refresh(user)
        return user

    if not name:
        raise ValidationError("请先填写名字")

    user = User(
        email=email,
        password_hash=await hash_password_async(password),
        password_initialized=password_supplied,
        name=name,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def get_user(session: AsyncSession, user_id: str) -> User | None:
    return await session.get(User, user_id)


async def get_user_for_token_payload(
    session: AsyncSession,
    payload: dict,
) -> User | None:
    from sag_api.core.config import settings

    user_id = payload.get("sub")
    user = await get_user(session, user_id) if isinstance(user_id, str) and user_id else None
    if user is None or not user.is_active:
        return None
    if settings.auth_mode == "password":
        claimed_version = payload.get("auth_version")
        if (
            type(claimed_version) is not int
            or claimed_version != user.auth_version
            or not user.password_initialized
        ):
            return None
    return user
