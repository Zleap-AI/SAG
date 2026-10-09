"""Safe projection from runtime failures to JSON-RPC errors."""

from __future__ import annotations

from typing import Any

from zleap.sag import SagError

from .protocol import RpcErrorData

_SENSITIVE_PARTS = ("api_key", "apikey", "authorization", "password", "secret", "token")


def _safe_details(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[truncated]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:1000]
    if isinstance(value, list | tuple):
        return [_safe_details(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, dict):
        return {
            str(key)[:100]: _safe_details(item, depth=depth + 1)
            for key, item in list(value.items())[:50]
            if not any(part in str(key).lower() for part in _SENSITIVE_PARTS)
        }
    return str(value)[:1000]


def safe_error_data(error: BaseException) -> RpcErrorData:
    """Return only caller-safe SAG diagnostics; never project causes or tracebacks."""
    if isinstance(error, SagError):
        return RpcErrorData(
            code=error.code,
            operation=error.operation,
            stage=error.stage,
            retryable=error.retryable,
            provider=error.provider,
            itemId=error.item_id,
            message=error.message,
            details=_safe_details(error.details) if error.details else None,
        )
    if isinstance(error, ValueError):
        return RpcErrorData(code="INVALID_ARGUMENT", retryable=False, message=str(error)[:1000])
    return RpcErrorData(code="INTERNAL_ERROR", retryable=False, message="SAG runtime request failed")
