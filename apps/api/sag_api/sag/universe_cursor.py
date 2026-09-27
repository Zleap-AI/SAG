"""Universe 游标与共享小工具 —— 与引擎生命周期无关的纯函数。

从 `engine_manager` 抽出：签名游标编解码（HMAC + base64url）、游标字段取值、
以及时区/权重归一化。这些函数无状态、无 I/O，独立成模块后
`engine_manager` 只保留引擎生命周期与查询编排。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "cursor_datetime",
    "cursor_float",
    "cursor_int",
    "database_time",
    "decode_universe_cursor",
    "encode_universe_cursor",
    "universe_cursor_scope",
    "utc_time",
    "weight",
]


def urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def urlsafe_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(f"{value}{'=' * (-len(value) % 4)}".encode("ascii"))


def universe_cursor_scope(source_config_id: str) -> str:
    return hashlib.sha256(source_config_id.encode("utf-8")).hexdigest()[:24]


def encode_universe_cursor(payload: dict[str, Any], secret: str) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return f"{urlsafe_encode(raw)}.{urlsafe_encode(signature)}"


def decode_universe_cursor(value: str, secret: str) -> dict[str, Any]:
    if not value or len(value) > 2048:
        raise ValueError("invalid universe cursor")
    try:
        encoded_payload, encoded_signature = value.split(".", 1)
        decoded = urlsafe_decode(encoded_payload)
        signature = urlsafe_decode(encoded_signature)
        expected = hmac.new(secret.encode("utf-8"), decoded, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid universe cursor")
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
        raise ValueError("invalid universe cursor") from error
    if not isinstance(payload, dict) or payload.get("v") != 2:
        raise ValueError("invalid universe cursor")
    return payload


def cursor_float(payload: dict[str, Any], key: str) -> float:
    try:
        value = float(payload[key])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid universe cursor") from error
    if not math.isfinite(value):
        raise ValueError("invalid universe cursor")
    return value


def cursor_datetime(payload: dict[str, Any], key: str) -> datetime:
    try:
        value = datetime.fromisoformat(str(payload[key]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid universe cursor") from error
    return value


def cursor_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    # bool is an int subclass; a "true" rank is a malformed cursor, not rank 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid universe cursor")
    return value


def weight(value: Any) -> float:
    return float(value) if value is not None else 1.0


def database_time(value: datetime | None) -> datetime | None:
    """zleap-sag currently stores naive UTC datetimes in SQLite."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def utc_time(value: datetime | None) -> datetime | None:
    """Normalize zleap-sag's naive SQLite timestamps at the API boundary."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
