from __future__ import annotations

import asyncio
import logging
import multiprocessing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sag_api.core.config import Settings
from sag_api.core.error_taxonomy import ErrorCode, ErrorLayer, ErrorStage
from sag_api.core.errors import ApiError, ValidationError
from sag_api.octx._worker import worker_entry
from sag_api.octx.limits import build_archive_limits
from sag_api.octx.storage import StoredUpload

logger = logging.getLogger(__name__)


def _summarize_report_issues(report: dict[str, Any], limit: int = 30) -> list[dict[str, Any]]:
    if not isinstance(report, dict):
        return []
    seen: set[tuple[Any, ...]] = set()
    issues: list[dict[str, Any]] = []

    def _push(layer: str, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        key = (
            layer,
            raw.get("code"),
            raw.get("message"),
            raw.get("path"),
            raw.get("record_id"),
        )
        if key in seen:
            return
        seen.add(key)
        issues.append(
            {
                "layer": layer,
                "code": raw.get("code"),
                "severity": raw.get("severity"),
                "message": raw.get("message"),
                "path": raw.get("path"),
                "record_id": raw.get("record_id"),
            }
        )

    for raw in report.get("issues") or []:
        _push("top", raw)
        if len(issues) >= limit:
            return issues
    fmt = report.get("format")
    if isinstance(fmt, dict):
        for raw in fmt.get("issues") or []:
            _push("format", raw)
            if len(issues) >= limit:
                return issues
    caps = report.get("capabilities") or {}
    if isinstance(caps, dict):
        for name, layer in caps.items():
            if not isinstance(layer, dict):
                continue
            for raw in layer.get("issues") or []:
                _push(f"cap:{name}", raw)
                if len(issues) >= limit:
                    return issues
    return issues


def _report_is_importable(report: dict[str, Any]) -> bool:
    """Accept invalid vectors only when every non-vector layer is fully valid."""
    format_layer = report.get("format")
    if not isinstance(format_layer, dict):
        return False
    if format_layer.get("valid") is not True or format_layer.get("fully_validated") is not True:
        return False
    capabilities = report.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    for name, layer in capabilities.items():
        if not isinstance(layer, dict) or layer.get("fully_validated") is not True:
            return False
        if name != "vectors" and layer.get("valid") is not True:
            return False
    return True


@dataclass(frozen=True, slots=True)
class ValidatedPackage:
    manifest: dict[str, Any]
    report: dict[str, Any]
    upload_sha256: str
    size_bytes: int
    input_signature: dict[str, int]
    capabilities: dict[str, Any]
    record_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class BuildPackageRequest:
    workspace: Path
    output: Path
    name: str | None = None
    version: str | None = None
    source: Path | None = None
    derive: bool = False
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BuiltPackage:
    output: Path
    workspace: Path
    asset_id: str
    version: str
    package_digest: str
    report: dict[str, Any]


def _run_process(operation: str, payload: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=worker_entry, args=(child, operation, payload))
    process.start()
    child.close()
    try:
        if not parent.poll(timeout_seconds):
            process.terminate()
            process.join(2)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(2)
            raise TimeoutError(f"OCTX {operation} exceeded {timeout_seconds}s")
        message = parent.recv()
    finally:
        parent.close()
        process.join(2)
    if not message["ok"]:
        error = RuntimeError(f"{message['error_type']}: {message['message']}")
        error.error_type = message["error_type"]
        error.report = message.get("report")
        raise error
    return message["result"]


def _limits_payload(settings: Settings) -> dict[str, Any]:
    limits = build_archive_limits(settings)
    return {
        name: getattr(limits, name)
        for name in (
            "max_entries",
            "max_file_size",
            "max_total_uncompressed",
            "max_compression_ratio",
            "max_jsonl_line_size",
            "max_jsonl_records",
            "max_json_depth",
            "max_yaml_depth",
            "max_arrow_dimension",
            "max_arrow_batches",
            "max_arrow_rows",
            "max_arrow_values",
            "max_issues",
        )
    }


class OctxRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def _execute(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = {
            **payload,
            "limits": _limits_payload(self.settings),
            "memory_mb": self.settings.octx_worker_memory_mb,
        }
        try:
            return await asyncio.to_thread(
                _run_process,
                operation,
                payload,
                timeout_seconds=self.settings.octx_worker_timeout_seconds,
            )
        except TimeoutError as error:
            raise ApiError(
                str(error),
                code=ErrorCode.OCTX_RESOURCE_LIMIT,
                layer=ErrorLayer.API,
                stage=(ErrorStage.OCTX_VALIDATE if operation == "validate" else ErrorStage.OCTX_PUBLISH),
                retryable=False,
            ) from error

    async def validate_package(self, upload: StoredUpload) -> ValidatedPackage:
        if not upload.unchanged():
            raise ValidationError(
                "OCTX upload changed after staging",
                code=ErrorCode.OCTX_INVALID_PACKAGE,
                stage=ErrorStage.OCTX_VALIDATE,
            )
        result = await self._execute("validate", {"path": str(upload.path)})
        report = result["report"]
        if not _report_is_importable(report):
            issues = _summarize_report_issues(report)
            logger.warning(
                "OCTX validate report failed: valid=%s fully_validated=%s issues=%s",
                report.get("valid"),
                report.get("fully_validated"),
                issues,
            )
            error = ValidationError(
                "OCTX package validation failed",
                code=ErrorCode.OCTX_INVALID_PACKAGE,
                stage=ErrorStage.OCTX_VALIDATE,
            )
            error.report = report
            error.issues = issues
            raise error
        manifest = result["manifest"]
        return ValidatedPackage(
            manifest=manifest,
            report=report,
            upload_sha256=upload.sha256,
            size_bytes=upload.size_bytes,
            input_signature=upload.signature.to_dict(),
            capabilities=dict(manifest.get("capabilities") or {}),
            record_counts=result["record_counts"],
        )

    async def build_package(self, request: BuildPackageRequest) -> BuiltPackage:
        try:
            result = await self._execute(
                "build",
                {
                    "workspace": str(request.workspace),
                    "output": str(request.output),
                    "source": str(request.source) if request.source else None,
                    "name": request.name,
                    "version": request.version,
                    "derive": request.derive,
                    "capabilities": request.capabilities,
                },
            )
        except RuntimeError as error:
            report = getattr(error, "report", None)
            error_type = getattr(error, "error_type", "RuntimeError")
            if error_type == "OctxValidationError":
                issues = _summarize_report_issues(report) if isinstance(report, dict) else []
                logger.warning(
                    "OCTX build raised OctxValidationError: message=%s issues=%s",
                    str(error),
                    issues,
                )
                wrapped = ValidationError(
                    str(error),
                    code=ErrorCode.OCTX_VALIDATION_INCOMPLETE,
                    stage=ErrorStage.OCTX_PUBLISH,
                )
                wrapped.report = report
                wrapped.issues = issues
                raise wrapped from error
            raise
        report = result["validation"]
        if not (report["valid"] and report["fully_validated"]):
            issues = _summarize_report_issues(report)
            logger.warning(
                "OCTX build report incomplete: valid=%s fully_validated=%s issues=%s",
                report.get("valid"),
                report.get("fully_validated"),
                issues,
            )
            error = ValidationError(
                "created OCTX package did not fully validate",
                code=ErrorCode.OCTX_VALIDATION_INCOMPLETE,
                stage=ErrorStage.OCTX_PUBLISH,
            )
            error.report = report
            error.issues = issues
            raise error
        return BuiltPackage(
            output=Path(result["output"]),
            workspace=Path(result["workspace"]),
            asset_id=result["asset_id"],
            version=result["version"],
            package_digest=result["package_digest"],
            report=report,
        )
