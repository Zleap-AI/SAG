"""Validation and authentication of fnOS gateway identities."""

from __future__ import annotations

import hashlib
import heapq
import hmac
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from sag_api.core.errors import AuthError

_UID_RE = re.compile(r"[1-9][0-9]*\Z")
_TIMESTAMP_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_SECRET_RE = re.compile(r"[0-9a-f]{64}\Z")
_SIGNATURE_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REPLAY_IDS = 8_192

_GATEWAY_UID = "x-trim-userid"
_GATEWAY_USERNAME = "x-trim-username"
_GATEWAY_IS_ADMIN = "x-trim-isadmin"
_INTERNAL_HEADERS = (
    "X-SAG-Internal-Uid",
    "X-SAG-Internal-Username",
    "X-SAG-Internal-Isadmin",
    "X-SAG-Internal-Timestamp",
    "X-SAG-Internal-Request-Id",
    "X-SAG-Internal-Signature",
)


@dataclass(frozen=True)
class GatewayIdentity:
    uid: int
    username: str
    is_admin: bool


class _ReplayCache:
    """A bounded, process-local set of consumed request IDs."""

    def __init__(self, max_entries: int = _MAX_REPLAY_IDS) -> None:
        self._max_entries = max_entries
        self._entries: dict[str, int] = {}
        self._expiry: list[tuple[int, str]] = []
        self._lock = Lock()

    def consume(self, request_id: str, *, expires_at: int, now: int) -> bool:
        with self._lock:
            while self._expiry and self._expiry[0][0] < now:
                expiry, expired_id = heapq.heappop(self._expiry)
                if self._entries.get(expired_id) == expiry:
                    del self._entries[expired_id]
            if request_id in self._entries or len(self._entries) >= self._max_entries:
                return False
            self._entries[request_id] = expires_at
            heapq.heappush(self._expiry, (expires_at, request_id))
            return True


def _header(headers: Mapping[str, str], name: str) -> str:
    values = [value for key, value in headers.items() if key.lower() == name.lower()]
    if len(values) != 1 or not isinstance(values[0], str):
        raise AuthError("fnOS 身份请求头无效")
    return values[0]


def _optional_header(headers: Mapping[str, str], name: str) -> str:
    values = [value for key, value in headers.items() if key.lower() == name.lower()]
    if len(values) != 1 or not isinstance(values[0], str):
        return ""
    return values[0]


def _uid(value: str) -> int:
    if not _UID_RE.fullmatch(value):
        raise AuthError("fnOS 用户 ID 无效")
    return int(value)


_MAX_USERNAME_CHARS = 120


def normalize_username(value: object) -> str:
    """Coerce any gateway-supplied username into a safe string, never raising.

    The username only strengthens tenant-key derivation; a missing or malformed
    value silently degrades to pure-UID isolation instead of blocking the user.
    """
    if not isinstance(value, str) or not value or len(value) > _MAX_USERNAME_CHARS:
        return ""
    try:
        value.encode("utf-8")
    except UnicodeError:
        return ""
    return value


def _request_id(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise AuthError("fnOS 请求 ID 无效") from error
    if not value or len(value) > 128:
        raise AuthError("fnOS 请求 ID 无效")
    return value


def _internal_admin(value: str) -> bool:
    if value == "0":
        return False
    if value == "1":
        return True
    raise AuthError("fnOS 管理员标记无效")


def _gateway_admin(value: str) -> bool:
    if value == "false":
        return False
    if value == "true":
        return True
    raise AuthError("fnOS 管理员标记无效")


def _identity(uid: int, username: str, is_admin: bool) -> GatewayIdentity:
    if type(uid) is not int or uid < 1:
        raise AuthError("fnOS 用户 ID 无效")
    if type(is_admin) is not bool:
        raise AuthError("fnOS 管理员标记无效")
    return GatewayIdentity(uid=uid, username=normalize_username(username), is_admin=is_admin)


def parse_gateway_identity(headers: Mapping[str, str]) -> GatewayIdentity:
    """Parse the identity headers supplied by the trusted fnOS gateway."""
    return GatewayIdentity(
        uid=_uid(_header(headers, _GATEWAY_UID)),
        username=normalize_username(_optional_header(headers, _GATEWAY_USERNAME)),
        is_admin=_gateway_admin(_header(headers, _GATEWAY_IS_ADMIN)),
    )


class InternalIdentitySigner:
    """Signs gateway identity for the API's local, private hop."""

    def __init__(self, key: bytes, *, max_age_seconds: int = 30) -> None:
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("fnOS internal identity key must be 32 bytes")
        if type(max_age_seconds) is not int or max_age_seconds < 1:
            raise ValueError("max_age_seconds must be a positive integer")
        self._key = key
        self._max_age_seconds = max_age_seconds
        self._replayed_request_ids = _ReplayCache()

    @classmethod
    def from_file(cls, path: Path, max_age_seconds: int = 30) -> InternalIdentitySigner:
        try:
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise AuthError("fnOS 内部身份密钥文件权限无效")
            encoded = path.read_text(encoding="ascii")
        except (OSError, UnicodeError) as error:
            raise AuthError("fnOS 内部身份密钥文件无效") from error
        if not _SECRET_RE.fullmatch(encoded):
            raise AuthError("fnOS 内部身份密钥文件无效")
        key = bytes.fromhex(encoded)
        if len(key) != 32:
            raise AuthError("fnOS 内部身份密钥文件无效")
        return cls(key, max_age_seconds=max_age_seconds)

    def _payload(self, identity: GatewayIdentity, request_id: str, timestamp: int) -> bytes:
        identity = _identity(identity.uid, identity.username, identity.is_admin)
        request_id = _request_id(request_id)
        if type(timestamp) is not int or timestamp < 0:
            raise AuthError("fnOS 时间戳无效")
        return "\n".join(
            (
                "v1",
                str(timestamp),
                request_id,
                str(identity.uid),
                identity.username,
                "1" if identity.is_admin else "0",
            )
        ).encode("utf-8")

    def sign(self, identity: GatewayIdentity, request_id: str, now: int) -> dict[str, str]:
        """Return signed internal headers for one gateway-to-API request."""
        if not isinstance(identity, GatewayIdentity):
            raise AuthError("fnOS 身份无效")
        payload = self._payload(identity, request_id, now)
        return {
            "X-SAG-Internal-Uid": str(identity.uid),
            "X-SAG-Internal-Username": identity.username,
            "X-SAG-Internal-Isadmin": "1" if identity.is_admin else "0",
            "X-SAG-Internal-Timestamp": str(now),
            "X-SAG-Internal-Request-Id": request_id,
            "X-SAG-Internal-Signature": hmac.new(self._key, payload, hashlib.sha256).hexdigest(),
        }

    def verify(
        self,
        headers: Mapping[str, str],
        expected_uid: int | None,
        now: int,
        *,
        expected_username: str | None = None,
    ) -> GatewayIdentity:
        """Validate a signed internal identity and bind it to this fnOS worker."""
        uid = _uid(_header(headers, _INTERNAL_HEADERS[0]))
        username = normalize_username(_header(headers, _INTERNAL_HEADERS[1]))
        is_admin = _internal_admin(_header(headers, _INTERNAL_HEADERS[2]))
        timestamp_text = _header(headers, _INTERNAL_HEADERS[3])
        request_id = _request_id(_header(headers, _INTERNAL_HEADERS[4]))
        signature = _header(headers, _INTERNAL_HEADERS[5])
        if not _TIMESTAMP_RE.fullmatch(timestamp_text) or not _SIGNATURE_RE.fullmatch(signature):
            raise AuthError("fnOS 内部身份签名无效")
        if type(expected_uid) is not int or expected_uid < 1 or uid != expected_uid:
            raise AuthError("fnOS 用户 ID 不匹配")
        if expected_username is not None and username != expected_username:
            raise AuthError("fnOS 用户名不匹配")
        if type(now) is not int or now < 0:
            raise AuthError("fnOS 时间戳无效")
        timestamp = int(timestamp_text)
        if now - timestamp > self._max_age_seconds or timestamp - now > 5:
            raise AuthError("fnOS 内部身份签名已过期")
        identity = GatewayIdentity(uid=uid, username=username, is_admin=is_admin)
        expected = hmac.new(
            self._key, self._payload(identity, request_id, timestamp), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise AuthError("fnOS 内部身份签名无效")
        if not self._replayed_request_ids.consume(
            request_id,
            expires_at=timestamp + self._max_age_seconds,
            now=now,
        ):
            raise AuthError("fnOS 内部身份请求已使用")
        return identity
