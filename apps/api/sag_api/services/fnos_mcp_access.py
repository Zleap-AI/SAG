"""Issue and validate fnOS-only, revocable MCP credentials."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.db.models import FnOSMcpGrant, User
from sag_api.fnos.identity import GatewayIdentity, normalize_username

SUPPORTED_MCP_GRANT_DAYS = frozenset({7, 30, 90})
_TOKEN_PREFIX = "sagf_mcp_"
_ROUTE_VERSION = "v1"


@dataclass(frozen=True, slots=True)
class IssuedMcpGrant:
    id: str
    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthenticatedMcpGrant:
    id: str
    user_id: str
    expires_at: datetime


def _utc(now: datetime | None) -> datetime:
    current = now or datetime.now(UTC)
    return current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _routing_payload(*, uid: int, username: str, expires_at: datetime) -> str:
    return _b64_encode(
        json.dumps(
            {"uid": uid, "username": username, "exp": int(expires_at.timestamp())},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )


def _routing_signature(*, grant_id: str, secret: str, payload: str, routing_key: bytes) -> str:
    signed = f"{_ROUTE_VERSION}\n{grant_id}\n{secret}\n{payload}".encode()
    return hmac.new(routing_key, signed, hashlib.sha256).hexdigest()


def _split_token(token: str) -> tuple[str, str, str, str] | None:
    if not isinstance(token, str) or not token.startswith(_TOKEN_PREFIX):
        return None
    parts = token[len(_TOKEN_PREFIX) :].split(".")
    if len(parts) != 4:
        return None
    grant_id, secret, payload, signature = parts
    if not grant_id or not secret or not payload or len(signature) != 64:
        return None
    return grant_id, secret, payload, signature


async def issue_grant(
    session: AsyncSession,
    *,
    user: User,
    expires_in_days: int,
    identity_uid: int,
    identity_username: str,
    routing_key: bytes,
    now: datetime | None = None,
) -> IssuedMcpGrant:
    if expires_in_days not in SUPPORTED_MCP_GRANT_DAYS:
        raise ValueError("MCP 凭据有效期只能是 7、30 或 90 天")
    if type(identity_uid) is not int or identity_uid < 1 or user.id != f"fnos_{identity_uid}":
        raise ValueError("fnOS MCP 凭据必须绑定当前 fnOS 用户")
    if not isinstance(routing_key, bytes) or len(routing_key) < 32:
        raise ValueError("fnOS MCP 路由密钥无效")

    issued_at = _utc(now)
    expires_at = issued_at + timedelta(days=expires_in_days)
    secret = secrets.token_urlsafe(32)
    grant = FnOSMcpGrant(
        user_id=user.id,
        secret_digest=_digest(secret),
        expires_at=expires_at,
    )
    session.add(grant)
    await session.flush()
    username = normalize_username(identity_username)
    payload = _routing_payload(uid=identity_uid, username=username, expires_at=expires_at)
    signature = _routing_signature(
        grant_id=grant.id,
        secret=secret,
        payload=payload,
        routing_key=routing_key,
    )
    await session.commit()
    return IssuedMcpGrant(
        id=grant.id,
        token=f"{_TOKEN_PREFIX}{grant.id}.{secret}.{payload}.{signature}",
        expires_at=expires_at,
    )


async def authenticate_grant(
    session: AsyncSession,
    token: str,
    *,
    now: datetime | None = None,
) -> AuthenticatedMcpGrant | None:
    parsed = _split_token(token)
    if parsed is None:
        return None
    grant_id, secret, _payload, _signature = parsed
    grant = await session.get(FnOSMcpGrant, grant_id)
    current = _utc(now)
    if (
        grant is None
        or grant.revoked_at is not None
        or grant.expires_at <= current
        or not hmac.compare_digest(grant.secret_digest, _digest(secret))
    ):
        return None
    return AuthenticatedMcpGrant(id=grant.id, user_id=grant.user_id, expires_at=grant.expires_at)


async def revoke_grant(session: AsyncSession, *, grant_id: str, user: User, now: datetime | None = None) -> bool:
    grant = await session.scalar(
        select(FnOSMcpGrant).where(FnOSMcpGrant.id == grant_id, FnOSMcpGrant.user_id == user.id)
    )
    if grant is None:
        return False
    if grant.revoked_at is None:
        grant.revoked_at = _utc(now)
        await session.commit()
    return True


async def delete_inactive_grant(
    session: AsyncSession,
    *,
    grant_id: str,
    user: User,
    now: datetime | None = None,
) -> str:
    """Delete an expired or revoked grant record without allowing active-token removal."""
    grant = await session.scalar(
        select(FnOSMcpGrant).where(FnOSMcpGrant.id == grant_id, FnOSMcpGrant.user_id == user.id)
    )
    if grant is None:
        return "missing"
    if grant.revoked_at is None and grant.expires_at > _utc(now):
        return "active"
    await session.delete(grant)
    await session.commit()
    return "deleted"


def route_grant(token: str, *, routing_key: bytes, now: int | None = None) -> GatewayIdentity | None:
    """Authenticate enough of a grant to choose its private fnOS worker.

    Database revocation intentionally remains a worker concern.  This function
    only establishes a non-forgeable, short-lived routing identity for the
    gateway and must never be used as the final authorization decision.
    """

    parsed = _split_token(token)
    if parsed is None or not isinstance(routing_key, bytes) or len(routing_key) < 32:
        return None
    grant_id, secret, payload, signature = parsed
    expected = _routing_signature(grant_id=grant_id, secret=secret, payload=payload, routing_key=routing_key)
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        data = json.loads(_b64_decode(payload))
        uid = data["uid"]
        username = normalize_username(data.get("username"))
        expires_at = data["exp"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError):
        return None
    current = int(time.time()) if now is None else now
    if type(uid) is not int or uid < 1 or type(expires_at) is not int or expires_at <= current:
        return None
    return GatewayIdentity(uid=uid, username=username, is_admin=False)
