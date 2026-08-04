"""认证原语：密码哈希（bcrypt）与 JWT 令牌。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from sag_api.core.config import settings

_ALGO = "HS256"
_BCRYPT_MAX_BYTES = 72  # bcrypt 硬限制
_DUMMY_PASSWORD_HASH = "$2b$12$Q3mUJ8FjeDbG763OlYG3Mev4bz75RlMTo16Ou.4TcmbahOtkx08e2"


def password_bytes(password: str) -> bytes:
    encoded = password.encode("utf-8")
    if len(encoded) > _BCRYPT_MAX_BYTES:
        raise ValueError("密码的 UTF-8 编码不得超过 72 字节")
    return encoded


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password_bytes(password), password_hash.encode("utf-8"))
    except (ValueError, TypeError, UnicodeError):
        return False


async def hash_password_async(password: str) -> str:
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, password_hash: str) -> bool:
    return await asyncio.to_thread(verify_password, password, password_hash)


async def consume_dummy_password_check(password: str) -> None:
    await verify_password_async(password, _DUMMY_PASSWORD_HASH)


def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=_ALGO)


def decode_token(token: str) -> dict[str, Any]:
    """解码并校验 JWT；失败抛 `jwt.PyJWTError`。"""
    return jwt.decode(token, settings.secret_key, algorithms=[_ALGO])
