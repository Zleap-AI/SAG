from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _sanitize_environment() -> None:
    sensitive = ("API_KEY", "PASSWORD", "TOKEN", "SECRET", "DATABASE_URL")
    for key in list(os.environ):
        if any(marker in key.upper() for marker in sensitive):
            os.environ.pop(key, None)


def _apply_memory_limit(memory_mb: int) -> None:
    try:
        import resource

        limit = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, OSError, ValueError):
        return


def _limits(values: dict[str, Any]):  # noqa: ANN202
    from octx import ArchiveLimits

    return ArchiveLimits(**values)


def _can_read_import_metadata(report: dict[str, Any]) -> bool:
    format_layer = report.get("format")
    capabilities = report.get("capabilities")
    if not isinstance(format_layer, dict) or not isinstance(capabilities, dict):
        return False
    if format_layer.get("valid") is not True or format_layer.get("fully_validated") is not True:
        return False
    return all(
        isinstance(layer, dict)
        and layer.get("fully_validated") is True
        and (name == "vectors" or layer.get("valid") is True)
        for name, layer in capabilities.items()
    )


def _validate(payload: dict[str, Any]) -> dict[str, Any]:
    from octx import open_octx, validate_octx

    path = Path(payload["path"])
    limits = _limits(payload["limits"])
    report = validate_octx(path, limits=limits, max_issues=limits.max_issues)
    report_data = report.to_dict()
    manifest: dict[str, Any] = {}
    counts: dict[str, int] = {}
    if _can_read_import_metadata(report_data):
        with open_octx(path, limits=limits, validate=False) as package:
            manifest = dict(package.manifest)
            available = set(package.available_paths)
            counters = {
                "chunks": ("data/chunks.jsonl", package.iter_chunks),
                "events": ("data/events.jsonl", package.iter_events),
                "entities": ("data/entities.jsonl", package.iter_entities),
                "chunk_events": (
                    "relations/chunk-events.jsonl",
                    package.iter_chunk_events,
                ),
                "event_entities": (
                    "relations/event-entities.jsonl",
                    package.iter_event_entities,
                ),
            }
            counts = {"documents": sum(1 for _ in package.iter_documents())}
            counts.update(
                {
                    name: sum(1 for _ in iterator()) if logical_path in available else 0
                    for name, (logical_path, iterator) in counters.items()
                }
            )
    return {"report": report_data, "manifest": manifest, "record_counts": counts}


def _build(payload: dict[str, Any]) -> dict[str, Any]:
    from octx import create_octx
    from octx.errors import OctxValidationError

    limits = _limits(payload.pop("limits"))
    try:
        result = create_octx(
            payload.pop("workspace"),
            output=payload.pop("output"),
            source=payload.pop("source"),
            limits=limits,
            **payload,
        )
    except OctxValidationError as error:
        report_obj = getattr(error, "report", None)
        if report_obj is None:
            for arg in error.args:
                if hasattr(arg, "to_dict"):
                    report_obj = arg
                    break
        report_dict = report_obj.to_dict() if report_obj is not None else None
        raise _WorkerReport(
            "OctxValidationError",
            str(error),
            report=report_dict,
        ) from error
    return result.to_dict()


class _WorkerReport(Exception):
    """Structured worker failure carrying an OCTX validation report across the process boundary."""

    def __init__(self, error_type: str, message: str, *, report: dict[str, Any] | None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.report = report


def worker_entry(connection, operation: str, payload: dict[str, Any]) -> None:  # noqa: ANN001
    try:
        _sanitize_environment()
        _apply_memory_limit(int(payload.pop("memory_mb")))
        if operation == "validate":
            result = _validate(payload)
        elif operation == "build":
            result = _build(payload)
        else:
            raise ValueError(f"unknown OCTX worker operation: {operation}")
        connection.send({"ok": True, "result": result})
    except _WorkerReport as error:
        connection.send(
            {
                "ok": False,
                "error_type": error.error_type,
                "message": error.message,
                "report": error.report,
            }
        )
    except BaseException as error:  # noqa: BLE001
        connection.send(
            {"ok": False, "error_type": type(error).__name__, "message": str(error)}
        )
    finally:
        connection.close()
