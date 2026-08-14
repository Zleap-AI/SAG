from __future__ import annotations

import importlib.metadata
import os
import platform
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.db.models import Job, OctxTransfer
from sag_api.enums import JobType

_TRACE_LIMIT = 100
_STRING_LIMIT = 2000
_LIST_LIMIT = 100
_DEPTH_LIMIT = 6
_OMITTED_KEYS = {
    "artifact_path",
    "body",
    "content",
    "raw_text",
    "storage_path",
    "vector",
    "vectors",
}
_SECRET_FRAGMENTS = (
    "api_key",
    "credential",
    "password",
    "secret",
    "token",
)
_POSIX_HOME_PATH = re.compile(r"/(Users|home)/[^\s\"']+")
_WINDOWS_HOME_PATH = re.compile(r"[A-Za-z]:\\Users\\[^\s\"']+")


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def sanitize_octx_diagnostic(value: Any, *, _depth: int = 0) -> Any:
    """Bound diagnostic data and remove content, credentials, and local paths."""
    if _depth >= _DEPTH_LIMIT:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, str):
        cleaned = _POSIX_HOME_PATH.sub("[local_path]", value)
        cleaned = _WINDOWS_HOME_PATH.sub("[local_path]", cleaned)
        return cleaned[:_STRING_LIMIT]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:_LIST_LIMIT]:
            key = str(raw_key)
            lowered = key.lower()
            if lowered in _OMITTED_KEYS:
                continue
            if any(fragment in lowered for fragment in _SECRET_FRAGMENTS):
                cleaned[key] = "[redacted]"
                continue
            cleaned[key] = sanitize_octx_diagnostic(item, _depth=_depth + 1)
        return cleaned
    if isinstance(value, (list, tuple, set)):
        return [
            sanitize_octx_diagnostic(item, _depth=_depth + 1)
            for item in list(value)[:_LIST_LIMIT]
        ]
    return sanitize_octx_diagnostic(str(value), _depth=_depth + 1)


def append_octx_trace(
    transfer: OctxTransfer,
    *,
    stage: str,
    state: str,
    details: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    checkpoint = dict(transfer.checkpoint or {})
    trace = list(checkpoint.get("diagnostic_trace") or [])
    entry: dict[str, Any] = {
        "at": (now or datetime.now(UTC)).isoformat(),
        "stage": stage,
        "state": state,
    }
    if details:
        entry["details"] = sanitize_octx_diagnostic(details)
    trace.append(entry)
    checkpoint["diagnostic_trace"] = trace[-_TRACE_LIMIT:]
    transfer.checkpoint = checkpoint


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _storage_health() -> dict[str, Any]:
    configured = Path(settings.data_dir) / "octx"
    probe = configured
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    result: dict[str, Any] = {
        "configured": True,
        "exists": configured.exists(),
        "writable": os.access(probe, os.W_OK),
    }
    try:
        usage = shutil.disk_usage(probe)
    except OSError as error:
        result["disk_check_error"] = error.__class__.__name__
    else:
        result["free_bytes"] = usage.free
        result["total_bytes"] = usage.total
    return result


async def build_octx_diagnostic_snapshot(
    session: AsyncSession,
    transfer_id: str,
) -> dict[str, Any]:
    transfer = await session.get(OctxTransfer, transfer_id)
    if transfer is None:
        raise LookupError(f"OCTX transfer not found: {transfer_id}")

    jobs_result = await session.execute(
        select(Job)
        .where(
            Job.type.in_((JobType.OCTX_PREFLIGHT, JobType.OCTX_IMPORT, JobType.OCTX_EXPORT)),
            Job.payload["transfer_id"].as_string() == transfer_id,
        )
        .order_by(Job.created_at.desc())
        .limit(20)
    )
    jobs = list(jobs_result.scalars())
    database_backend = make_url(settings.database_url).get_backend_name()
    checkpoint = dict(transfer.checkpoint or {})
    snapshot = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "transfer": {
            "id": transfer.id,
            "direction": _enum_value(transfer.direction),
            "status": _enum_value(transfer.status),
            "scope": str(checkpoint.get("export_scope") or "source"),
            "document_id": checkpoint.get("document_id"),
            "progress": transfer.progress,
            "source_id": transfer.target_source_id,
            "asset_id": transfer.asset_id,
            "release_id": transfer.release_id,
            "package_version": transfer.package_version,
            "package_digest": transfer.package_digest,
            "error": transfer.error,
            "warnings": transfer.warnings,
            "validation_report": transfer.validation_report,
            "checkpoint": transfer.checkpoint,
            "created_at": transfer.created_at,
            "updated_at": transfer.updated_at,
        },
        "jobs": [
            {
                "id": job.id,
                "type": _enum_value(job.type),
                "status": _enum_value(job.status),
                "progress": job.progress,
                "attempts": job.attempts,
                "error": job.error,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
            }
            for job in jobs
        ],
        "environment": {
            "sag_version": _package_version("sag-api"),
            "octx_version": _package_version("octx"),
            "python": platform.python_version(),
            "platform": sys.platform,
            "os_release": platform.release(),
            "architecture": platform.machine(),
            "database_backend": database_backend,
            "vector_provider": settings.sag_vector_provider,
            "relational_provider": settings.sag_relational_provider or "sqlite",
            "embedding_model": settings.embedding_model,
            "embedding_dimensions": settings.embedding_dimensions,
            "job_concurrency": settings.job_concurrency,
            "octx_worker_memory_mb": settings.octx_worker_memory_mb,
            "octx_worker_timeout_seconds": settings.octx_worker_timeout_seconds,
            "octx_arrow_vector_reuse_enabled": settings.octx_arrow_vector_reuse_enabled,
            "octx_limits": {
                "max_upload_mb": settings.octx_max_upload_mb,
                "max_entries": settings.octx_max_entries,
                "max_file_mb": settings.octx_max_file_mb,
                "max_uncompressed_mb": settings.octx_max_uncompressed_mb,
                "max_jsonl_records": settings.octx_max_jsonl_records,
            },
            "storage": _storage_health(),
        },
    }
    return sanitize_octx_diagnostic(snapshot)
