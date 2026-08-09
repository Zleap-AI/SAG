"""Persisted scheduling metadata stored inside ``Job.payload_json``."""

from __future__ import annotations

from typing import Final

SCHEDULER_KEY: Final = "_scheduler"
DELETE_PRIORITY: Final = 0
RESUME_PRIORITY: Final = 10
NORMAL_PRIORITY: Final = 50
SOURCE_MAINTENANCE: Final = "source_maintenance"
DELETE_WAITING_SOURCE: Final = "delete_waiting_source"

_UNSET = object()


def get_priority(payload: dict | None) -> int:
    scheduler = (payload or {}).get(SCHEDULER_KEY)
    if not isinstance(scheduler, dict):
        return NORMAL_PRIORITY
    value = scheduler.get("priority")
    return value if isinstance(value, int) and not isinstance(value, bool) else NORMAL_PRIORITY


def get_blocked_reason(payload: dict | None) -> str | None:
    scheduler = (payload or {}).get(SCHEDULER_KEY)
    if not isinstance(scheduler, dict):
        return None
    value = scheduler.get("blocked_reason")
    return value if isinstance(value, str) and value else None


def set_scheduler(
    payload: dict | None,
    *,
    priority: int | None = None,
    blocked_reason: str | None | object = _UNSET,
) -> dict:
    result = dict(payload or {})
    scheduler = dict(result.get(SCHEDULER_KEY) or {})
    if priority is not None:
        scheduler["priority"] = priority
    if blocked_reason is not _UNSET:
        if blocked_reason:
            scheduler["blocked_reason"] = blocked_reason
        else:
            scheduler.pop("blocked_reason", None)
    if scheduler:
        result[SCHEDULER_KEY] = scheduler
    else:
        result.pop(SCHEDULER_KEY, None)
    return result
