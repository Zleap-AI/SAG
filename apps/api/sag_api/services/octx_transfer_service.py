from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import UploadFile
from packaging.version import InvalidVersion, Version
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.error_taxonomy import ErrorCode, ErrorLayer, ErrorStage
from sag_api.core.errors import ConflictError, NotFoundError, ValidationError
from sag_api.db.base import new_id
from sag_api.db.models import (
    Document,
    Job,
    OctxAsset,
    OctxDocumentBinding,
    OctxInstallation,
    OctxRelease,
    OctxSourceBinding,
    OctxTransfer,
    Source,
)
from sag_api.db.models.octx import transition_installation, transition_transfer
from sag_api.enums import (
    ConnectorKind,
    DocumentStatus,
    JobStatus,
    JobType,
    OctxAssetOwnership,
    OctxExportAction,
    OctxImportAction,
    OctxInstallationStatus,
    OctxReleaseOrigin,
    OctxTransferDirection,
    OctxTransferStatus,
    SourceStatus,
    SourceType,
)
from sag_api.octx.decision_token import (
    DecisionTokenClaims,
    DecisionTokenError,
    ExportDecisionTokenClaims,
    issue_decision_token,
    issue_export_decision_token,
    verify_export_decision_token,
)
from sag_api.octx.runner import BuildPackageRequest, OctxRunner
from sag_api.octx.semver import bump_semver_patch, parse_semver, validate_semver
from sag_api.octx.storage import FileSignature, OctxStorage, StoredUpload
from sag_api.sag.octx_importer import (
    build_structured_plan,
    document_display_metadata,
    import_knowledge_package,
    import_structured_plan,
)
from sag_api.sag.octx_plan_store import OctxPlanError, OctxPlanStore
from sag_api.sag.octx_smoke_test import smoke_test_installation
from sag_api.sag.octx_snapshot import export_snapshot
from sag_api.services.octx_conflict_service import (
    ImportDecision,
    confirm_import_decision,
    resolve_import_conflict,
)
from sag_api.services.octx_diagnostics_service import append_octx_trace

if TYPE_CHECKING:
    from sag_api.jobs import JobQueue


_EXPORT_SNAPSHOT_RANGES = {
    "documents": (0.10, 0.15),
    "chunks": (0.15, 0.23),
    "events": (0.23, 0.30),
    "entities": (0.30, 0.38),
    "event_entities": (0.38, 0.45),
}
_EXPORT_VECTOR_ROLES = (
    "chunk.heading",
    "chunk.content",
    "event.title",
    "event.content",
    "entity.name",
    "event_entity.relation",
)


@dataclass(slots=True)
class _VectorProgressGate:
    total: int
    interval_seconds: float
    last_completed: int = -1
    last_persisted_at: float | None = None
    last_stage: tuple[str, str] | None = None

    def should_persist(
        self,
        kind: str,
        mode: str,
        completed: int,
        *,
        now: float | None = None,
    ) -> bool:
        timestamp = time.monotonic() if now is None else now
        stage = (kind, mode)
        threshold = max(1, math.ceil(self.total * 0.01))
        should_write = (
            self.last_persisted_at is None
            or stage != self.last_stage
            or completed - self.last_completed >= threshold
            or timestamp - self.last_persisted_at >= self.interval_seconds
        )
        if should_write:
            self.last_completed = completed
            self.last_persisted_at = timestamp
            self.last_stage = stage
        return should_write


def _export_progress(detail: dict[str, Any]) -> float:
    phase = str(detail.get("phase") or "")
    if phase == "snapshot_complete":
        return 0.59
    completed = max(0, int(detail.get("completed") or 0))
    total = max(1, int(detail.get("total") or 0))
    fraction = min(1.0, completed / total)
    kind = str(detail.get("kind") or "")
    if phase == "vectors":
        try:
            index = _EXPORT_VECTOR_ROLES.index(kind)
        except ValueError:
            index = 0
        width = 0.14 / len(_EXPORT_VECTOR_ROLES)
        return 0.45 + width * (index + fraction)
    start, end = _EXPORT_SNAPSHOT_RANGES.get(kind, (0.10, 0.45))
    return start + (end - start) * fraction


def default_octx_storage() -> OctxStorage:
    return OctxStorage(
        Path(settings.data_dir) / "octx",
        max_upload_bytes=settings.octx_max_upload_mb * 1024 * 1024,
    )


async def _create_job(
    session: AsyncSession,
    transfer: OctxTransfer,
    job_type: JobType,
    *,
    source_id: str | None = None,
) -> Job:
    job = Job(
        type=job_type,
        status=JobStatus.QUEUED,
        source_id=source_id,
        payload={"transfer_id": transfer.id},
    )
    session.add(job)
    await session.flush()
    return job


async def create_import_transfer(
    session: AsyncSession,
    upload: UploadFile,
    *,
    storage: OctxStorage,
    job_queue: JobQueue,
    transfer_id: str | None = None,
    requested_by_user_id: str | None = None,
) -> OctxTransfer:
    filename = str(upload.filename or "")
    if not filename.casefold().endswith(".octx"):
        raise ValidationError("OCTX import requires a .octx file")
    if transfer_id is not None:
        try:
            normalized_transfer_id = uuid.UUID(hex=transfer_id).hex
        except ValueError as error:
            raise ValidationError("invalid OCTX transfer id") from error
        existing = await session.get(OctxTransfer, normalized_transfer_id)
        if existing is not None:
            if existing.direction is not OctxTransferDirection.IMPORT:
                raise ConflictError("OCTX transfer id already belongs to another operation")
            existing_owner = str(
                (existing.checkpoint or {}).get("requested_by_user_id") or ""
            )
            if requested_by_user_id and existing_owner and existing_owner != requested_by_user_id:
                raise ConflictError("OCTX transfer id already belongs to another user")
            return existing
    else:
        normalized_transfer_id = new_id()
    transfer = OctxTransfer(
        id=normalized_transfer_id,
        direction=OctxTransferDirection.IMPORT,
        status=OctxTransferStatus.UPLOADED,
        progress=0.0,
        checkpoint=(
            {"requested_by_user_id": requested_by_user_id}
            if requested_by_user_id
            else {}
        ),
        expires_at=datetime.now(UTC) + timedelta(hours=settings.octx_transfer_ttl_hours),
    )
    stored = await storage.stream_upload(upload, transfer.id)
    transfer.upload_sha256 = stored.sha256
    transfer.input_signature = stored.signature.to_dict()
    transfer.staging_key = stored.key
    transition_transfer(transfer, OctxTransferStatus.VALIDATING)
    transfer.progress = 0.02
    append_octx_trace(
        transfer,
        stage="upload",
        state="completed",
        details={"size_bytes": stored.signature.size},
    )
    session.add(transfer)
    job = await _create_job(session, transfer, JobType.OCTX_PREFLIGHT)
    await session.commit()
    await job_queue.enqueue(job.id)
    return transfer


def _stored_upload(transfer: OctxTransfer, storage: OctxStorage) -> StoredUpload:
    signature = dict(transfer.input_signature or {})
    if not transfer.staging_key or not transfer.upload_sha256 or not signature:
        raise ValidationError("OCTX transfer has no immutable staged upload")
    file_signature = FileSignature(
        device=int(signature["device"]),
        inode=int(signature["inode"]),
        size=int(signature["size"]),
        modified_ns=int(signature["modified_ns"]),
    )
    return StoredUpload(
        path=storage.resolve_key(transfer.staging_key),
        key=transfer.staging_key,
        sha256=transfer.upload_sha256,
        size_bytes=file_signature.size,
        signature=file_signature,
    )


async def _persist_import_release(
    session: AsyncSession,
    transfer: OctxTransfer,
    validated: Any,
    storage: OctxStorage,
) -> OctxRelease:
    manifest = validated.manifest
    asset_data = manifest["asset"]
    release_data = manifest["release"]
    asset_id = str(asset_data["id"])
    version = str(release_data["version"])
    digest = str(release_data["package_digest"])
    asset = await session.get(OctxAsset, asset_id)
    if asset is None:
        asset = OctxAsset(
            id=asset_id,
            name=str(asset_data.get("name") or "Imported OCTX")[:200],
            ownership=OctxAssetOwnership.IMPORTED,
        )
        session.add(asset)
        await session.flush()
    release = await session.scalar(
        select(OctxRelease).where(
            OctxRelease.asset_id == asset_id,
            OctxRelease.version == version,
        )
    )
    if release is not None:
        if release.package_digest != digest:
            raise ValidationError("OCTX release digest changed after conflict resolution")
        return release
    upload_path = storage.resolve_key(str(transfer.staging_key))
    artifact_key = storage.publish_release(upload_path, asset_id, version, digest)
    release = OctxRelease(
        asset_id=asset_id,
        version=version,
        package_digest=digest,
        manifest=dict(manifest),
        artifact_key=artifact_key,
        created_by=OctxReleaseOrigin.IMPORT,
    )
    session.add(release)
    await session.flush()
    return release


async def preflight_import(
    session: AsyncSession,
    transfer: OctxTransfer,
    *,
    storage: OctxStorage,
    runner: OctxRunner,
    job_queue: JobQueue,
    decision_secret: str | None = None,
) -> OctxTransfer:
    if transfer.status is not OctxTransferStatus.VALIDATING:
        raise ValidationError("OCTX transfer is not validating")
    await _ensure_transfer_active(session, transfer, stage="before_validation")
    validated = await runner.validate_package(_stored_upload(transfer, storage))
    await _ensure_transfer_active(session, transfer, stage="after_validation")
    structured = validated.capabilities.get("sag-structured")
    structured_version = structured.get("version") if isinstance(structured, dict) else structured
    if structured_version is None and (not settings.llm_configured or not settings.effective_embedding_api_key):
        raise ValidationError(
            "knowledge-only OCTX import requires configured LLM and embedding",
            code=ErrorCode.OCTX_REBUILD_CONFIGURATION_MISSING,
            layer=ErrorLayer.API,
            stage=ErrorStage.OCTX_VALIDATE,
        )
    resolution = await resolve_import_conflict(session, validated)
    release = await _persist_import_release(session, transfer, validated, storage)
    transfer.asset_id = release.asset_id
    transfer.release_id = release.id
    transfer.package_version = release.version
    transfer.package_digest = release.package_digest
    transfer.validation_report = dict(validated.report)
    transfer.checkpoint = {
        **dict(transfer.checkpoint or {}),
        "asset_name": str(validated.manifest["asset"].get("name") or "Imported OCTX"),
        "record_counts": dict(validated.record_counts),
        "capabilities": dict(validated.capabilities),
    }
    transfer.progress = 0.1

    job: Job | None = None
    if resolution.kind in {"new", "idempotent"}:
        transfer.selected_action = OctxImportAction.NEW
        if resolution.kind == "idempotent":
            transfer.target_source_id = resolution.source_id
        transition_transfer(transfer, OctxTransferStatus.QUEUED)
        job = await _create_job(
            session,
            transfer,
            JobType.OCTX_IMPORT,
            source_id=transfer.target_source_id,
        )
    else:
        expires_at = datetime.now(UTC) + timedelta(minutes=15)
        token = issue_decision_token(
            DecisionTokenClaims(
                transfer_id=transfer.id,
                asset_id=release.asset_id,
                source_revisions={
                    candidate.source_id: candidate.content_revision for candidate in resolution.conflicts
                },
                highest_version=resolution.highest_version,
                expires_at=expires_at,
            ),
            secret=decision_secret or settings.secret_key,
        )
        transfer.decision_expires_at = expires_at
        transfer.checkpoint = {
            **dict(transfer.checkpoint or {}),
            "allowed_actions": list(resolution.allowed_actions),
            "decision_token": token,
            "conflicts": [
                {
                    "source_id": item.source_id,
                    "source_name": item.source_name,
                    "active_version": item.active_version,
                    "local_changes": item.local_changes,
                }
                for item in resolution.conflicts
            ],
        }
        transition_transfer(transfer, OctxTransferStatus.DECISION_REQUIRED)
    await session.commit()
    if job is not None:
        await job_queue.enqueue(job.id)
    return transfer


async def submit_import_decision(
    session: AsyncSession,
    transfer_id: str,
    decision: ImportDecision,
    *,
    job_queue: JobQueue,
) -> OctxTransfer:
    transfer = await confirm_import_decision(session, transfer_id, decision)
    job: Job | None = None
    if transfer.status is OctxTransferStatus.QUEUED:
        job = await _create_job(
            session,
            transfer,
            JobType.OCTX_IMPORT,
            source_id=transfer.target_source_id,
        )
    await session.commit()
    # updated_at is generated by the database on UPDATE and SQLAlchemy expires
    # that attribute even when expire_on_commit=False. Refresh before returning
    # the ORM object so response serialization never performs implicit async IO.
    await session.refresh(transfer)
    if job is not None:
        await job_queue.enqueue(job.id)
    return transfer


def _ensure_shadow_identity(transfer: OctxTransfer) -> tuple[str, str]:
    checkpoint = dict(transfer.checkpoint or {})
    id_namespace = checkpoint.get("id_namespace")
    source_config_id = checkpoint.get("source_config_id")
    if not isinstance(id_namespace, str) or not id_namespace:
        id_namespace = str(uuid.uuid4())
    if not isinstance(source_config_id, str) or not source_config_id:
        source_config_id = f"octx_{new_id()[:24]}"
    transfer.checkpoint = {
        **checkpoint,
        "id_namespace": id_namespace,
        "source_config_id": source_config_id,
    }
    return id_namespace, source_config_id


def _import_started_at(transfer: OctxTransfer) -> datetime:
    checkpoint = dict(transfer.checkpoint or {})
    raw = checkpoint.get("import_started_at")
    try:
        started_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        started_at = datetime.now(UTC)
        transfer.checkpoint = {
            **checkpoint,
            "import_started_at": started_at.isoformat(),
        }
    return started_at


def _duration_seconds(started_at: datetime) -> int:
    return max(0, int((datetime.now(UTC) - started_at).total_seconds()))


def _promote_knowledge_documents(states: dict[str, Any], final_dir: str | Path) -> None:
    destination = Path(final_dir)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    ready_states = [
        state for _, state in sorted(states.items()) if isinstance(state, dict) and state.get("status") == "ready"
    ]
    for position, state in enumerate(ready_states):
        source = Path(str(state["controlled_path"]))
        target = destination / f"{position:08d}.md"
        if source.resolve() != target.resolve():
            temporary = target.with_name(f".{target.name}.{new_id()}.tmp")
            shutil.copyfile(source, temporary)
            temporary.chmod(0o600)
            temporary.replace(target)
        elif not target.is_file():
            raise FileNotFoundError(f"promoted OCTX document is missing: {target}")
        state["controlled_path"] = str(target)


async def _ensure_transfer_active(session: AsyncSession, transfer: OctxTransfer, *, stage: str) -> None:
    await session.refresh(transfer, attribute_names=["cancellation_requested", "status"])
    if transfer.cancellation_requested or transfer.status is OctxTransferStatus.CANCELLED:
        raise ConflictError(
            f"OCTX transfer cancelled at {stage}",
            code=ErrorCode.OCTX_TRANSFER_CANCELLED,
            layer=ErrorLayer.API,
            stage=ErrorStage.OCTX_RESOLVE,
            retryable=False,
        )


async def execute_structured_import(
    session: AsyncSession,
    transfer: OctxTransfer,
    *,
    storage: OctxStorage,
    engine_manager: Any,
    sag_session_factory: Any = None,
    vector_rebuilder: Callable[[str, dict], Awaitable[dict]] | None = None,
    attempt: int = 1,
) -> OctxTransfer:
    """Build, index, and atomically activate one structured shadow partition."""
    if transfer.status is not OctxTransferStatus.QUEUED:
        raise ValidationError("OCTX import transfer is not queued")
    if not transfer.release_id or not transfer.asset_id:
        raise ValidationError("OCTX import transfer has no release identity")
    binding = await session.get(OctxSourceBinding, transfer.target_source_id) if transfer.target_source_id else None
    if binding is not None and binding.active_release_id == transfer.release_id:
        transition_transfer(transfer, OctxTransferStatus.IMPORTING)
        transition_transfer(transfer, OctxTransferStatus.INDEXING)
        transition_transfer(transfer, OctxTransferStatus.SWITCHING)
        transition_transfer(transfer, OctxTransferStatus.READY)
        transfer.progress = 1.0
        await session.commit()
        return transfer

    capabilities = dict((transfer.checkpoint or {}).get("capabilities") or {})
    structured = capabilities.get("sag-structured")
    version = structured.get("version") if isinstance(structured, dict) else structured
    if version != "0.1":
        raise ValidationError("OCTX knowledge-only import requires the rebuild pipeline")
    release = await session.get(OctxRelease, transfer.release_id)
    if release is None:
        raise ValidationError("OCTX import release is missing")

    id_namespace, source_config_id = _ensure_shadow_identity(transfer)
    started_at = _import_started_at(transfer)
    if sag_session_factory is None:
        sag_session_factory = await engine_manager.get_sag_session_factory(source_config_id)
    transition_transfer(transfer, OctxTransferStatus.IMPORTING)
    transfer.progress = 0.2
    transfer.checkpoint = {
        **dict(transfer.checkpoint or {}),
        "progress_detail": {"phase": "building_shadow"},
    }
    await session.commit()
    await _ensure_transfer_active(session, transfer, stage="before_structured_import")
    attempt_dir = storage.staging_dir(transfer.id) / f"import-{max(1, attempt)}"
    attempt_dir.mkdir(parents=False, exist_ok=False, mode=0o700)
    plan_path = attempt_dir / "plan.sqlite3"
    package_path = storage.resolve_key(release.artifact_key)
    await asyncio.to_thread(
        build_structured_plan,
        package_path,
        plan_path,
        id_namespace,
        validate=False,
    )
    try:
        imported = await import_structured_plan(
            plan_path,
            id_namespace,
            source_config_id=source_config_id,
            source_name=str((transfer.checkpoint or {}).get("asset_name") or "Imported OCTX"),
            session_factory=sag_session_factory,
        )
    except OctxPlanError as error:
        raise ValidationError(
            str(error),
            code=ErrorCode.OCTX_SAG_MAPPING_CONFLICT,
            layer=ErrorLayer.ENGINE,
            stage=ErrorStage.OCTX_IMPORT,
            retryable=False,
        ) from error

    transition_transfer(transfer, OctxTransferStatus.INDEXING)
    transfer.progress = 0.7
    vector_totals = {
        "chunks": imported.counts["chunks"],
        "events": imported.counts["events"],
        "entities": imported.counts["entities"],
        "event_entities": imported.counts["event_entities"],
    }
    total_vectors = sum(vector_totals.values())
    transfer.checkpoint = {
        **dict(transfer.checkpoint or {}),
        "progress_detail": {
            "phase": "vectorizing",
            "completed_vectors": 0,
            "total_vectors": total_vectors,
        },
    }
    await session.commit()
    await _ensure_transfer_active(session, transfer, stage="before_vector_rebuild")
    if vector_rebuilder is None:
        from sag_api.sag.octx_vector_rebuilder import rebuild_vectors

        await engine_manager.provision(source_config_id)
        vector_rebuilder = rebuild_vectors

    vector_checkpoint = dict((transfer.checkpoint or {}).get("vector_progress") or {})
    report_capabilities = (
        (transfer.validation_report or {}).get("capabilities") if isinstance(transfer.validation_report, dict) else None
    )
    vector_layer_report = report_capabilities.get("vectors") if isinstance(report_capabilities, dict) else None
    prevalidated_vector_valid = (
        vector_layer_report.get("valid") is True if isinstance(vector_layer_report, dict) else False
    )
    progress_gate = _VectorProgressGate(
        total=total_vectors,
        interval_seconds=settings.octx_vector_progress_interval_seconds,
    )

    async def save_vector_checkpoint(value: dict) -> None:
        counts = dict(value.get("counts") or {})
        completed_vectors = min(
            total_vectors,
            sum(int(counts.get(kind) or 0) for kind in vector_totals),
        )
        ratio = completed_vectors / total_vectors if total_vectors else 1.0
        current_kind = str(value.get("current_kind") or "")
        vector_mode = str(value.get("current_mode") or "generate")
        if not progress_gate.should_persist(
            current_kind,
            vector_mode,
            completed_vectors,
        ):
            return
        written_records = int(counts.get(current_kind) or 0)
        current_total = int(vector_totals.get(current_kind) or 0)
        transfer.progress = min(0.88, 0.7 + 0.18 * ratio)
        transfer.checkpoint = {
            **dict(transfer.checkpoint or {}),
            "vector_progress": dict(value),
            "source_config_id": source_config_id,
            "progress_detail": {
                "phase": "vectorizing",
                "current_kind": current_kind,
                "current_batch_size": int(value.get("current_batch_size") or 0),
                "completed_vectors": completed_vectors,
                "total_vectors": total_vectors,
                "batch_state": value.get("batch_state"),
                "vector_mode": vector_mode,
                "written_records": written_records,
                "role_total_records": current_total,
                "reused_records": written_records if vector_mode in {"reuse", "mixed"} else 0,
                "generated_records": written_records if vector_mode in {"generate", "mixed"} else 0,
                "reusable_vector_roles": list(value.get("reusable_roles") or ()),
            },
        }
        await session.commit()

    try:
        vector_stats = await vector_rebuilder(
            source_config_id,
            vector_checkpoint,
            reuse_batch_size=settings.octx_reused_vector_batch_size,
            enable_vector_reuse=settings.octx_arrow_vector_reuse_enabled,
            session_factory=sag_session_factory,
            on_checkpoint=save_vector_checkpoint,
            package_path=package_path,
            plan_path=plan_path,
            prevalidated_vector_valid=prevalidated_vector_valid,
        )
    except TypeError:
        try:
            # Older rebuild adapters accept checkpoints but not an injected session.
            vector_stats = await vector_rebuilder(
                source_config_id,
                vector_checkpoint,
                on_checkpoint=save_vector_checkpoint,
            )
        except TypeError:
            # Minimal test doubles may accept neither optional argument.
            vector_stats = await vector_rebuilder(source_config_id, vector_checkpoint)
    expected_vectors = {
        "chunks": imported.counts["chunks"],
        "events": imported.counts["events"],
        "entities": imported.counts["entities"],
        "event_entities": imported.counts["event_entities"],
    }
    if any(int(vector_stats.get(kind, -1)) != count for kind, count in expected_vectors.items()):
        raise ValidationError(
            "OCTX shadow vector rebuild is incomplete",
            code=ErrorCode.OCTX_SHADOW_VALIDATION_FAILED,
            layer=ErrorLayer.ENGINE,
            stage=ErrorStage.OCTX_INDEX,
            retryable=True,
        )

    transfer.progress = 0.88
    transfer.checkpoint = {
        **dict(transfer.checkpoint or {}),
        "progress_detail": {
            "phase": "validating_shadow",
            "completed_vectors": total_vectors,
            "total_vectors": total_vectors,
        },
    }
    await session.commit()
    await _ensure_transfer_active(session, transfer, stage="before_shadow_smoke_test")
    smoke_stats = await smoke_test_installation(
        source_config_id,
        expected_counts=expected_vectors,
        engine_manager=engine_manager,
        sag_session_factory=sag_session_factory,
    )

    await _ensure_transfer_active(session, transfer, stage="before_atomic_switch")
    transition_transfer(transfer, OctxTransferStatus.SWITCHING)
    transfer.progress = 0.9
    transfer.checkpoint = {
        **dict(transfer.checkpoint or {}),
        "progress_detail": {"phase": "switching"},
    }
    await session.commit()
    source = await session.get(Source, transfer.target_source_id) if transfer.target_source_id else None
    old_source_config_id = source.sag_source_config_id if source is not None else None
    source_id = source.id if source is not None else new_id()
    installation = OctxInstallation(
        id=new_id(),
        source_id=source_id,
        release_id=release.id,
        sag_source_config_id=source_config_id,
        id_namespace=id_namespace,
        status=OctxInstallationStatus.SHADOW,
        counts=dict(imported.counts),
    )
    transition_installation(installation, OctxInstallationStatus.ACTIVE)

    document_dir = Path(settings.upload_dir) / source_id / f"octx-{installation.id}"
    document_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    document_models: list[Document] = []
    with OctxPlanStore(plan_path, id_namespace, create=False) as plan:
        for position, document in enumerate(plan.iter_records("document")):
            document_id = str(document["id"])
            body = str(document.get("body") or "")
            path = document_dir / f"{position:08d}-{document_id}.md"
            path.write_text(body, encoding="utf-8")
            path.chmod(0o600)
            chunk_count, event_count = plan.document_counts(document_id)
            filename, content_type = document_display_metadata(document)
            document_models.append(
                Document(
                    source_id=source_id,
                    filename=filename,
                    content_type=content_type,
                    size_bytes=len(body.encode("utf-8")),
                    storage_path=str(path),
                    status=DocumentStatus.READY,
                    chunk_count=chunk_count,
                    event_count=event_count,
                    progress=100,
                    sag_source_id=plan.local_id("document", document_id),
                    octx_installation_id=installation.id,
                    octx_document_id=document_id,
                    is_active=True,
                )
            )

    if source is None:
        source = Source(
            id=source_id,
            name=str((transfer.checkpoint or {}).get("asset_name") or "Imported OCTX")[:200],
            description="Imported from OCTX",
            source_type=SourceType.DOCUMENT,
            connector_kind=ConnectorKind.FILE_UPLOAD,
            sag_source_config_id=source_config_id,
            config={"octx": {"asset_id": transfer.asset_id}},
            status=SourceStatus.ACTIVE,
        )
        session.add(source)
        await session.flush()
    else:
        await session.execute(
            update(Document)
            .where(Document.source_id == source.id, Document.is_active.is_(True))
            .values(is_active=False)
        )
        old_installation = await session.scalar(
            select(OctxInstallation).where(
                OctxInstallation.source_id == source.id,
                OctxInstallation.status == OctxInstallationStatus.ACTIVE,
            )
        )
        if old_installation is not None:
            transition_installation(old_installation, OctxInstallationStatus.RETAINED)
            old_installation.retain_until = datetime.now(UTC) + timedelta(days=settings.octx_rollback_retention_days)
        source.sag_source_config_id = source_config_id
        source.status = SourceStatus.ACTIVE

    source.document_count = imported.counts["documents"]
    source.chunk_count = imported.counts["chunks"]
    source.event_count = imported.counts["events"]
    session.add(installation)
    await session.flush()
    session.add_all(document_models)
    with session.no_autoflush:
        binding = await session.get(OctxSourceBinding, source_id)
    if binding is None:
        binding = OctxSourceBinding(
            source_id=source_id,
            asset_id=transfer.asset_id,
            active_release_id=release.id,
            content_revision=1,
            released_revision=1,
        )
        session.add(binding)
    else:
        binding.asset_id = transfer.asset_id
        binding.active_release_id = release.id
        binding.content_revision += 1
        binding.released_revision = binding.content_revision
    installation.activated_at = datetime.now(UTC)
    transfer.target_source_id = source_id
    transfer.installation_id = installation.id
    transfer.checkpoint = {
        **dict(transfer.checkpoint or {}),
        "plan_path": str(plan_path),
        "id_namespace": id_namespace,
        "source_config_id": source_config_id,
        "vector_stats": dict(vector_stats),
        "smoke_test": {
            "sample_chunk_id": smoke_stats.get("sample_chunk_id"),
            "search_stats": smoke_stats.get("search_stats"),
        },
        "progress_detail": {
            "phase": "complete",
            "duration_seconds": _duration_seconds(started_at),
        },
    }
    transition_transfer(transfer, OctxTransferStatus.READY)
    transfer.progress = 1.0
    await session.commit()
    if old_source_config_id and old_source_config_id != source_config_id:
        await engine_manager.release(old_source_config_id)
    return transfer


async def execute_knowledge_import(
    session: AsyncSession,
    transfer: OctxTransfer,
    *,
    storage: OctxStorage,
    engine_manager: Any,
    sag_session_factory: Any = None,
    attempt: int = 1,
) -> OctxTransfer:
    """Rebuild and atomically activate a knowledge-only OCTX package."""
    if transfer.status is not OctxTransferStatus.QUEUED:
        raise ValidationError("OCTX import transfer is not queued")
    release = await session.get(OctxRelease, transfer.release_id) if transfer.release_id else None
    if release is None or not transfer.asset_id:
        raise ValidationError("OCTX import release is missing")
    id_namespace, source_config_id = _ensure_shadow_identity(transfer)
    started_at = _import_started_at(transfer)
    if sag_session_factory is None:
        sag_session_factory = await engine_manager.get_sag_session_factory(source_config_id)
    transition_transfer(transfer, OctxTransferStatus.IMPORTING)
    transfer.progress = 0.2
    await session.commit()
    await _ensure_transfer_active(session, transfer, stage="before_knowledge_import")
    controlled_dir = storage.staging_dir(transfer.id) / f"knowledge-{max(1, attempt)}"
    knowledge_checkpoint = copy.deepcopy((transfer.checkpoint or {}).get("knowledge") or {})

    async def save_checkpoint(value: dict) -> None:
        snapshot = copy.deepcopy(value)
        document_states = dict(snapshot.get("documents") or {})
        completed_documents = sum(
            1 for state in document_states.values() if isinstance(state, dict) and state.get("status") == "ready"
        )
        total_documents = max(
            completed_documents,
            int(((transfer.checkpoint or {}).get("record_counts") or {}).get("documents") or 0),
        )
        current_document = next(
            (
                path
                for path, state in document_states.items()
                if isinstance(state, dict) and state.get("status") == "processing"
            ),
            None,
        )
        ratio = completed_documents / total_documents if total_documents else 0
        transfer.progress = min(0.8, 0.2 + 0.6 * ratio)
        transfer.checkpoint = {
            **dict(transfer.checkpoint or {}),
            "knowledge": snapshot,
            "source_config_id": source_config_id,
            "progress_detail": {
                "phase": "rebuilding_documents",
                "completed_documents": completed_documents,
                "total_documents": total_documents,
                "current_document": current_document,
            },
        }
        await session.commit()

    imported = await import_knowledge_package(
        storage.resolve_key(release.artifact_key),
        controlled_dir,
        source_config_id=source_config_id,
        engine_manager=engine_manager,
        checkpoint=knowledge_checkpoint,
        on_checkpoint=save_checkpoint,
    )
    transition_transfer(transfer, OctxTransferStatus.INDEXING)
    transfer.progress = 0.8
    transfer.checkpoint = {
        **dict(transfer.checkpoint or {}),
        "progress_detail": {
            **dict((transfer.checkpoint or {}).get("progress_detail") or {}),
            "phase": "indexing",
            "current_document": None,
        },
    }
    await session.commit()
    await _ensure_transfer_active(session, transfer, stage="before_knowledge_smoke_test")
    knowledge_smoke = await smoke_test_installation(
        source_config_id,
        expected_counts={},
        engine_manager=engine_manager,
        sag_session_factory=sag_session_factory,
    )
    await _ensure_transfer_active(session, transfer, stage="before_atomic_switch")
    transition_transfer(transfer, OctxTransferStatus.SWITCHING)
    transfer.progress = 0.9
    transfer.checkpoint = {
        **dict(transfer.checkpoint or {}),
        "progress_detail": {
            **dict((transfer.checkpoint or {}).get("progress_detail") or {}),
            "phase": "switching",
        },
    }
    await session.commit()

    source = await session.get(Source, transfer.target_source_id) if transfer.target_source_id else None
    old_source_config_id = source.sag_source_config_id if source is not None else None
    source_id = source.id if source is not None else new_id()
    installation = OctxInstallation(
        id=new_id(),
        source_id=source_id,
        release_id=release.id,
        sag_source_config_id=source_config_id,
        id_namespace=id_namespace,
        status=OctxInstallationStatus.SHADOW,
        counts=dict(imported.counts),
    )
    transition_installation(installation, OctxInstallationStatus.ACTIVE)
    states = knowledge_checkpoint.get("documents") or {}
    _promote_knowledge_documents(
        states,
        Path(settings.upload_dir) / source_id / f"octx-{installation.id}",
    )
    document_models = [
        Document(
            source_id=source_id,
            filename=Path(str(state.get("logical_path") or "document.md")).name[:512],
            content_type="text/markdown",
            size_bytes=Path(str(state["controlled_path"])).stat().st_size,
            storage_path=str(state["controlled_path"]),
            status=DocumentStatus.READY,
            chunk_count=int(state.get("chunk_count") or 0),
            event_count=int(state.get("event_count") or 0),
            progress=100,
            token_usage=int(state.get("token_usage") or 0),
            sag_source_id=state.get("sag_source_id"),
            octx_installation_id=installation.id,
            octx_document_id=state.get("octx_document_id"),
            is_active=True,
        )
        for state in states.values()
        if state.get("status") == "ready"
    ]
    if source is None:
        source = Source(
            id=source_id,
            name=str((transfer.checkpoint or {}).get("asset_name") or "Imported OCTX")[:200],
            description="Imported from OCTX knowledge documents",
            source_type=SourceType.DOCUMENT,
            connector_kind=ConnectorKind.FILE_UPLOAD,
            sag_source_config_id=source_config_id,
            config={"octx": {"asset_id": transfer.asset_id}},
            status=SourceStatus.ACTIVE,
        )
        session.add(source)
        await session.flush()
    else:
        await session.execute(
            update(Document)
            .where(Document.source_id == source.id, Document.is_active.is_(True))
            .values(is_active=False)
        )
        old_installation = await session.scalar(
            select(OctxInstallation).where(
                OctxInstallation.source_id == source.id,
                OctxInstallation.status == OctxInstallationStatus.ACTIVE,
            )
        )
        if old_installation is not None:
            transition_installation(old_installation, OctxInstallationStatus.RETAINED)
            old_installation.retain_until = datetime.now(UTC) + timedelta(days=settings.octx_rollback_retention_days)
        source.sag_source_config_id = source_config_id
        source.status = SourceStatus.ACTIVE
    source.document_count = imported.counts["documents"]
    source.chunk_count = imported.counts["chunks"]
    source.event_count = imported.counts["events"]
    session.add(installation)
    await session.flush()
    session.add_all(document_models)
    with session.no_autoflush:
        binding = await session.get(OctxSourceBinding, source_id)
    if binding is None:
        binding = OctxSourceBinding(
            source_id=source_id,
            asset_id=transfer.asset_id,
            active_release_id=release.id,
            content_revision=1,
            released_revision=1,
        )
        session.add(binding)
    else:
        binding.asset_id = transfer.asset_id
        binding.active_release_id = release.id
        binding.content_revision += 1
        binding.released_revision = binding.content_revision
    installation.activated_at = datetime.now(UTC)
    transfer.target_source_id = source_id
    transfer.installation_id = installation.id
    transfer.checkpoint = {
        **dict(transfer.checkpoint or {}),
        "knowledge": knowledge_checkpoint,
        "id_namespace": id_namespace,
        "source_config_id": source_config_id,
        "smoke_test": {
            "sample_chunk_id": knowledge_smoke.get("sample_chunk_id"),
            "search_stats": knowledge_smoke.get("search_stats"),
        },
        "progress_detail": {
            **dict((transfer.checkpoint or {}).get("progress_detail") or {}),
            "phase": "complete",
            "current_document": None,
            "duration_seconds": _duration_seconds(started_at),
        },
    }
    transition_transfer(transfer, OctxTransferStatus.READY)
    transfer.progress = 1.0
    await session.commit()
    if old_source_config_id and old_source_config_id != source_config_id:
        await engine_manager.release(old_source_config_id)
    return transfer


async def execute_import(
    session: AsyncSession,
    transfer: OctxTransfer,
    **kwargs: Any,
) -> OctxTransfer:
    if transfer.selected_action is OctxImportAction.UPDATE:
        expected_revision = (transfer.checkpoint or {}).get("expected_source_revision")
        binding = (
            await session.get(OctxSourceBinding, transfer.target_source_id)
            if transfer.target_source_id
            else None
        )
        if (
            binding is None
            or expected_revision is None
            or binding.content_revision != int(expected_revision)
        ):
            raise ConflictError(
                "OCTX source content revision changed after confirmation",
                code=ErrorCode.OCTX_DECISION_STALE,
                layer=ErrorLayer.API,
                stage=ErrorStage.OCTX_RESOLVE,
                retryable=False,
            )
    capabilities = dict((transfer.checkpoint or {}).get("capabilities") or {})
    if "sag-structured" in capabilities:
        return await execute_structured_import(session, transfer, **kwargs)
    supported = {
        "storage",
        "engine_manager",
        "sag_session_factory",
        "attempt",
    }
    return await execute_knowledge_import(
        session,
        transfer,
        **{key: value for key, value in kwargs.items() if key in supported},
    )


def _export_document_state(documents: list[Document]) -> tuple[list[dict], list[dict]]:
    ready: list[dict] = []
    excluded: list[dict] = []
    for document in sorted(documents, key=lambda item: item.id):
        if document.status is DocumentStatus.READY and document.sag_source_id:
            ready.append(
                {
                    "id": document.id,
                    "article_id": str(document.sag_source_id),
                    "status": document.status.value,
                }
            )
            continue
        excluded.append(
            {
                "id": document.id,
                "filename": document.filename,
                "status": document.status.value,
                "error": str(document.error or "")[:500] or None,
            }
        )
    return ready, excluded


def _export_selection_fingerprint(ready: list[dict], excluded: list[dict], source_revision: int) -> str:
    encoded = json.dumps(
        {
            "ready": ready,
            "excluded": excluded,
            "source_revision": source_revision,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _export_checkpoint(
    *,
    ready: list[dict],
    excluded: list[dict],
    source_revision: int,
    selected_version: str,
    asset_name: str,
) -> dict[str, Any]:
    return {
        "asset_name": asset_name,
        "selected_version": selected_version,
        "selected_document_ids": [item["id"] for item in ready],
        "selected_article_ids": [item["article_id"] for item in ready],
        "excluded_documents": excluded,
        "source_revision": source_revision,
        "selection_fingerprint": _export_selection_fingerprint(ready, excluded, source_revision),
    }


async def create_export_transfer(
    session: AsyncSession,
    source_id: str,
    *,
    version: str | None,
    job_queue: JobQueue,
    requested_by_user_id: str | None = None,
) -> OctxTransfer:
    active = await session.scalar(
        select(OctxTransfer)
        .where(
            OctxTransfer.direction == OctxTransferDirection.EXPORT,
            OctxTransfer.target_source_id == source_id,
            OctxTransfer.status.not_in(
                [
                    OctxTransferStatus.READY,
                    OctxTransferStatus.FAILED,
                    OctxTransferStatus.CANCELLED,
                    OctxTransferStatus.EXPIRED,
                ]
            ),
        )
        .order_by(OctxTransfer.created_at.desc(), OctxTransfer.id.desc())
        .limit(1)
    )
    if active is not None:
        active_scope = str((active.checkpoint or {}).get("export_scope") or "source")
        if active_scope != "source":
            raise ConflictError("another OCTX export is already active for this source")
        active_version = str((active.checkpoint or {}).get("selected_version") or "")
        if version is not None:
            try:
                requested_version = str(Version(version))
            except InvalidVersion as error:
                raise ValidationError("OCTX export version must be SemVer") from error
            if active_version and requested_version != active_version:
                raise ConflictError(f"OCTX export {active_version} is already active for this source")
        return active

    source = await session.get(Source, source_id)
    if source is None:
        raise NotFoundError("source not found")
    documents = (
        (
            await session.execute(
                select(Document).where(
                    Document.source_id == source_id,
                    Document.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    ready, excluded = _export_document_state(list(documents))
    if not ready:
        raise ConflictError(
            "OCTX source has no READY documents to export",
            code=ErrorCode.OCTX_SOURCE_NOT_EXPORTABLE,
            layer=ErrorLayer.API,
            stage=ErrorStage.OCTX_EXPORT,
            retryable=bool(documents),
        )
    binding = await session.get(OctxSourceBinding, source_id)
    active_release = await session.get(OctxRelease, binding.active_release_id) if binding else None
    active_asset = await session.get(OctxAsset, binding.asset_id) if binding else None
    if (
        not excluded
        and binding is not None
        and active_release is not None
        and active_asset is not None
        and active_asset.ownership is OctxAssetOwnership.IMPORTED
        and binding.content_revision == binding.released_revision
    ):
        transfer = OctxTransfer(
            direction=OctxTransferDirection.EXPORT,
            status=OctxTransferStatus.READY,
            progress=1.0,
            target_source_id=source_id,
            asset_id=active_asset.id,
            release_id=active_release.id,
            package_version=active_release.version,
            package_digest=active_release.package_digest,
            artifact_key=active_release.artifact_key,
            checkpoint={
                "asset_name": active_asset.name,
                "reused_original": True,
                "export_scope": "source",
                **(
                    {"requested_by_user_id": requested_by_user_id}
                    if requested_by_user_id
                    else {}
                ),
            },
        )
        session.add(transfer)
        await session.commit()
        await session.refresh(transfer)
        return transfer

    if version is not None:
        try:
            selected_version = str(Version(version))
        except InvalidVersion as error:
            raise ValidationError("OCTX export version must be SemVer") from error
    elif active_release is None:
        selected_version = "1.0.0"
    else:
        current = Version(active_release.version)
        selected_version = f"{current.major}.{current.minor}.{current.micro + 1}"
    if active_release is not None and Version(selected_version) <= Version(active_release.version):
        raise ConflictError("OCTX export version must be greater than the active release")

    source_revision = binding.content_revision if binding is not None else 0
    checkpoint = _export_checkpoint(
        ready=ready,
        excluded=excluded,
        source_revision=source_revision,
        selected_version=selected_version,
        asset_name=source.name,
    )
    if requested_by_user_id:
        checkpoint["requested_by_user_id"] = requested_by_user_id
    checkpoint["export_scope"] = "source"
    transfer = OctxTransfer(
        direction=OctxTransferDirection.EXPORT,
        status=(OctxTransferStatus.DECISION_REQUIRED if excluded else OctxTransferStatus.QUEUED),
        progress=0.0,
        target_source_id=source_id,
        checkpoint=checkpoint,
        expires_at=datetime.now(UTC) + timedelta(hours=settings.octx_transfer_ttl_hours),
    )
    session.add(transfer)
    await session.flush()
    job: Job | None = None
    if excluded:
        decision_expires_at = datetime.now(UTC) + timedelta(minutes=15)
        token = issue_export_decision_token(
            ExportDecisionTokenClaims(
                transfer_id=transfer.id,
                source_id=source_id,
                selected_document_ids=tuple(checkpoint["selected_document_ids"]),
                selected_article_ids=tuple(checkpoint["selected_article_ids"]),
                selection_fingerprint=str(checkpoint["selection_fingerprint"]),
                source_revision=source_revision,
                nonce=new_id(),
                expires_at=decision_expires_at,
            ),
            secret=settings.secret_key,
        )
        transfer.decision_expires_at = decision_expires_at
        transfer.checkpoint = {
            **checkpoint,
            "allowed_actions": [
                OctxExportAction.EXPORT_READY_ONLY.value,
                OctxExportAction.CANCEL.value,
            ],
            "decision_token": token,
        }
    else:
        job = await _create_job(session, transfer, JobType.OCTX_EXPORT, source_id=source_id)
    await session.commit()
    # The decision-required branch updates server-onupdate columns after the
    # initial INSERT. Refresh before the API serializes the model; otherwise
    # accessing the expired updated_at attribute performs async IO from a sync
    # serializer and raises MissingGreenlet.
    await session.refresh(transfer)
    if job is not None:
        await job_queue.enqueue(job.id)
    return transfer


async def create_document_export_transfer(
    session: AsyncSession,
    source_id: str,
    document_id: str,
    *,
    version: str | None,
    job_queue: JobQueue,
    transfer_id: str | None = None,
    requested_by_user_id: str | None = None,
) -> OctxTransfer:
    try:
        requested_version = validate_semver(version) if version is not None else None
    except ValueError as error:
        raise ValidationError("OCTX export version must be SemVer") from error

    normalized_transfer_id: str | None = None
    if transfer_id is not None:
        try:
            normalized_transfer_id = uuid.UUID(hex=transfer_id).hex
        except ValueError as error:
            raise ValidationError("invalid OCTX transfer id") from error
        existing = await session.get(OctxTransfer, normalized_transfer_id)
        if existing is not None:
            checkpoint = dict(existing.checkpoint or {})
            existing_owner = str(checkpoint.get("requested_by_user_id") or "")
            same_request = (
                existing.direction is OctxTransferDirection.EXPORT
                and existing.target_source_id == source_id
                and str(checkpoint.get("export_scope") or "source") == "document"
                and str(checkpoint.get("document_id") or "") == document_id
                and (
                    requested_version is None
                    or requested_version == checkpoint.get("selected_version")
                )
                and (
                    not requested_by_user_id
                    or not existing_owner
                    or requested_by_user_id == existing_owner
                )
            )
            if not same_request:
                raise ConflictError("OCTX transfer id already belongs to another operation")
            return existing

    active = await session.scalar(
        select(OctxTransfer)
        .where(
            OctxTransfer.direction == OctxTransferDirection.EXPORT,
            OctxTransfer.target_source_id == source_id,
            OctxTransfer.status.not_in(
                [
                    OctxTransferStatus.READY,
                    OctxTransferStatus.FAILED,
                    OctxTransferStatus.CANCELLED,
                    OctxTransferStatus.EXPIRED,
                ]
            ),
        )
        .order_by(OctxTransfer.created_at.desc(), OctxTransfer.id.desc())
        .limit(1)
    )
    if active is not None:
        checkpoint = dict(active.checkpoint or {})
        if (
            str(checkpoint.get("export_scope") or "source") == "document"
            and str(checkpoint.get("document_id") or "") == document_id
        ):
            active_version = str(checkpoint.get("selected_version") or "")
            if requested_version is not None:
                if active_version and requested_version != active_version:
                    raise ConflictError(f"OCTX export {active_version} is already active for this source")
            return active
        raise ConflictError("another OCTX export is already active for this source")

    source = await session.get(Source, source_id)
    if source is None:
        raise NotFoundError("source not found")
    document = await session.get(Document, document_id)
    if document is None or document.source_id != source_id or not document.is_active:
        raise NotFoundError("document not found")
    if document.status is not DocumentStatus.READY or not document.sag_source_id:
        raise ConflictError(
            "only READY documents can be exported as OCTX",
            code=ErrorCode.OCTX_SOURCE_NOT_EXPORTABLE,
            layer=ErrorLayer.API,
            stage=ErrorStage.OCTX_EXPORT,
            retryable=document.status not in {DocumentStatus.FAILED},
        )

    binding = await session.get(OctxDocumentBinding, document.id)
    active_release = await session.get(OctxRelease, binding.active_release_id) if binding else None
    if requested_version is not None:
        selected_version = requested_version
    elif active_release is None:
        selected_version = "1.0.0"
    else:
        selected_version = bump_semver_patch(active_release.version)
    if active_release is not None and parse_semver(selected_version) <= parse_semver(
        active_release.version
    ):
        raise ConflictError("OCTX export version must be greater than the active release")

    ready = [
        {
            "id": document.id,
            "article_id": str(document.sag_source_id),
            "status": document.status.value,
        }
    ]
    content_revision = binding.content_revision if binding is not None else 0
    checkpoint = _export_checkpoint(
        ready=ready,
        excluded=[],
        source_revision=content_revision,
        selected_version=selected_version,
        asset_name=document.filename,
    )
    checkpoint.update(
        {
            "export_scope": "document",
            "document_id": document.id,
            "document_name": document.filename,
        }
    )
    if requested_by_user_id:
        checkpoint["requested_by_user_id"] = requested_by_user_id
    transfer = OctxTransfer(
        id=normalized_transfer_id or new_id(),
        direction=OctxTransferDirection.EXPORT,
        status=OctxTransferStatus.QUEUED,
        progress=0.0,
        target_source_id=source_id,
        checkpoint=checkpoint,
        expires_at=datetime.now(UTC) + timedelta(hours=settings.octx_transfer_ttl_hours),
    )
    session.add(transfer)
    await session.flush()
    job = await _create_job(session, transfer, JobType.OCTX_EXPORT, source_id=source_id)
    await session.commit()
    await session.refresh(transfer)
    await job_queue.enqueue(job.id)
    return transfer


async def submit_export_decision(
    session: AsyncSession,
    transfer_id: str,
    *,
    action: OctxExportAction,
    decision_token: str,
    job_queue: JobQueue,
) -> OctxTransfer:
    transfer = await session.scalar(select(OctxTransfer).where(OctxTransfer.id == transfer_id).with_for_update())
    if transfer is None or transfer.direction is not OctxTransferDirection.EXPORT:
        raise NotFoundError("OCTX export transfer not found")
    try:
        claims = verify_export_decision_token(decision_token, secret=settings.secret_key)
    except DecisionTokenError as error:
        raise ConflictError(
            str(error),
            code=ErrorCode.OCTX_DECISION_STALE,
            layer=ErrorLayer.API,
            stage=ErrorStage.OCTX_EXPORT,
            retryable=True,
        ) from error
    if claims.transfer_id != transfer.id or claims.source_id != transfer.target_source_id:
        raise ConflictError(
            "OCTX export decision does not match this transfer",
            code=ErrorCode.OCTX_DECISION_STALE,
            layer=ErrorLayer.API,
            stage=ErrorStage.OCTX_EXPORT,
            retryable=True,
        )
    if transfer.status is OctxTransferStatus.QUEUED:
        return transfer
    if transfer.status is not OctxTransferStatus.DECISION_REQUIRED:
        raise ConflictError("OCTX export transfer no longer accepts decisions")
    if action is OctxExportAction.CANCEL:
        transfer.cancellation_requested = True
        transition_transfer(transfer, OctxTransferStatus.CANCELLED)
        await session.commit()
        await session.refresh(transfer)
        return transfer

    source_id = str(transfer.target_source_id)
    documents = list(
        (
            await session.execute(
                select(Document).where(
                    Document.source_id == source_id,
                    Document.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    ready, excluded = _export_document_state(documents)
    binding = await session.get(OctxSourceBinding, source_id)
    source_revision = binding.content_revision if binding is not None else 0
    fingerprint = _export_selection_fingerprint(ready, excluded, source_revision)
    if (
        claims.selected_document_ids != tuple(item["id"] for item in ready)
        or claims.selected_article_ids != tuple(item["article_id"] for item in ready)
        or claims.selection_fingerprint != fingerprint
        or claims.source_revision != source_revision
    ):
        source = await session.get(Source, source_id)
        if source is None:
            raise NotFoundError("source not found")
        previous = dict(transfer.checkpoint or {})
        checkpoint = _export_checkpoint(
            ready=ready,
            excluded=excluded,
            source_revision=source_revision,
            selected_version=str(previous.get("selected_version") or "1.0.0"),
            asset_name=source.name,
        )
        checkpoint["export_scope"] = str(previous.get("export_scope") or "source")
        if previous.get("requested_by_user_id"):
            checkpoint["requested_by_user_id"] = previous["requested_by_user_id"]
        if not ready:
            checkpoint["allowed_actions"] = []
            checkpoint["decision_stale"] = True
            transfer.checkpoint = checkpoint
            transfer.cancellation_requested = True
            transfer.decision_expires_at = None
            transition_transfer(transfer, OctxTransferStatus.CANCELLED)
            await session.commit()
            await session.refresh(transfer)
            return transfer
        actions = [OctxExportAction.CANCEL.value]
        actions.insert(0, OctxExportAction.EXPORT_READY_ONLY.value)
        decision_expires_at = datetime.now(UTC) + timedelta(minutes=15)
        checkpoint["decision_token"] = issue_export_decision_token(
            ExportDecisionTokenClaims(
                transfer_id=transfer.id,
                source_id=source_id,
                selected_document_ids=tuple(checkpoint["selected_document_ids"]),
                selected_article_ids=tuple(checkpoint["selected_article_ids"]),
                selection_fingerprint=str(checkpoint["selection_fingerprint"]),
                source_revision=source_revision,
                nonce=new_id(),
                expires_at=decision_expires_at,
            ),
            secret=settings.secret_key,
        )
        transfer.decision_expires_at = decision_expires_at
        checkpoint["allowed_actions"] = actions
        checkpoint["decision_stale"] = True
        transfer.checkpoint = checkpoint
        await session.commit()
        await session.refresh(transfer)
        return transfer
    transition_transfer(transfer, OctxTransferStatus.QUEUED)
    checkpoint = dict(transfer.checkpoint or {})
    checkpoint["confirmed_at"] = datetime.now(UTC).isoformat()
    transfer.checkpoint = checkpoint
    job = await _create_job(session, transfer, JobType.OCTX_EXPORT, source_id=source_id)
    await session.commit()
    await session.refresh(transfer)
    await job_queue.enqueue(job.id)
    return transfer


async def execute_export(
    session: AsyncSession,
    transfer: OctxTransfer,
    *,
    storage: OctxStorage,
    runner: OctxRunner,
    engine_manager: Any,
    sag_session_factory: Any = None,
    embedding_client: Any = None,
    vector_store: Any = None,
    attempt: int = 1,
) -> OctxTransfer:
    if transfer.status is not OctxTransferStatus.QUEUED or not transfer.target_source_id:
        raise ValidationError("OCTX export transfer is not queued")
    source = await session.get(Source, transfer.target_source_id)
    if source is None:
        raise NotFoundError("source not found")
    checkpoint = dict(transfer.checkpoint or {})
    selected_document_ids = tuple(checkpoint.get("selected_document_ids") or ())
    selected_article_ids = tuple(checkpoint.get("selected_article_ids") or ())
    selected_documents = (
        (
            await session.execute(
                select(Document).where(
                    Document.source_id == source.id,
                    Document.is_active.is_(True),
                    Document.id.in_(selected_document_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    selected_documents.sort(key=lambda document: selected_document_ids.index(document.id))
    if (
        not selected_document_ids
        or len(selected_documents) != len(selected_document_ids)
        or any(
            document.status is not DocumentStatus.READY
            or str(document.sag_source_id or "") != selected_article_ids[index]
            for index, document in enumerate(selected_documents)
        )
    ):
        raise ConflictError("OCTX frozen READY selection changed and is no longer exportable")

    transition_transfer(transfer, OctxTransferStatus.EXPORTING)
    transfer.progress = 0.1
    transfer.checkpoint = {
        **dict(transfer.checkpoint or {}),
        "progress_detail": {
            "phase": "snapshot",
            "kind": "documents",
            "completed": 0,
            "total": len(selected_documents),
        },
    }
    append_octx_trace(
        transfer,
        stage="selection_frozen",
        state="completed",
        details={
            "document_count": len(selected_documents),
            "excluded_count": len((transfer.checkpoint or {}).get("excluded_documents") or []),
        },
    )
    await session.commit()
    await _ensure_transfer_active(session, transfer, stage="before_snapshot")
    attempt_dir, workspace = _prepare_export_attempt(storage, transfer.id, attempt=attempt)
    export_scope = str((transfer.checkpoint or {}).get("export_scope") or "source")
    export_document_id = str((transfer.checkpoint or {}).get("document_id") or "")
    if export_scope == "document":
        if not export_document_id or len(selected_documents) != 1 or selected_documents[0].id != export_document_id:
            raise ConflictError("OCTX document export selection is invalid")
        persistent_workspace = storage.document_workspace_dir(export_document_id)
    else:
        persistent_workspace = storage.workspace_dir(source.id)
    producer_ids = persistent_workspace / "producer-ids.json"
    if sag_session_factory is None:
        sag_session_factory = await engine_manager.get_sag_session_factory(
            source.sag_source_config_id,
            source,
        )

    # Vector export is an optional acceleration layer. Only reuse vectors when
    # the source partition records the exact identity of the current embedding
    # configuration. Export never calls the embedding provider: the descriptor
    # below is metadata-only and vectors are read from the existing vector store.
    from zleap.sag.db.models import SourceConfig

    from sag_api.sag.octx_vector_protocol import embedding_identity

    descriptor = embedding_client
    if descriptor is None:
        try:
            from zleap.sag.core.ai.factory import get_embedding_client

            descriptor = await get_embedding_client(scenario="general")
        except Exception:
            # Vector export is optional. Missing local model configuration must
            # not block a valid structured-only OCTX export.
            descriptor = None
    current_vector_identity = embedding_identity(descriptor) if descriptor is not None else None
    trusted_vector_export = False
    if current_vector_identity is not None:
        async with sag_session_factory() as sag_session:
            source_config = await sag_session.get(SourceConfig, source.sag_source_config_id)
        target_config = source_config.target_config if source_config is not None else None
        stored_vector_identity = target_config.get("octx_vector_identity") if isinstance(target_config, dict) else None
        trusted_vector_export = stored_vector_identity == current_vector_identity
    if trusted_vector_export and vector_store is None:
        from zleap.sag.core.storage.client import get_vector_client

        vector_store = get_vector_client()

    async def save_export_progress(detail: dict[str, Any]) -> None:
        await _ensure_transfer_active(session, transfer, stage=str(detail.get("phase") or "export"))
        transfer.progress = max(float(transfer.progress or 0), _export_progress(detail))
        transfer.checkpoint = {
            **dict(transfer.checkpoint or {}),
            "progress_detail": dict(detail),
        }
        trace = list((transfer.checkpoint or {}).get("diagnostic_trace") or [])
        trace_stage = "snapshot_vectors" if detail.get("phase") == "vectors" else "snapshot_structured"
        if not trace or trace[-1].get("stage") != trace_stage:
            append_octx_trace(
                transfer,
                stage=trace_stage,
                state="started",
                details={"kind": detail.get("kind"), "total": detail.get("total")},
            )
        await session.commit()

    async with engine_manager.maintenance(source.sag_source_config_id, source=source):
        stats = await export_snapshot(
            source,
            selected_documents,
            workspace,
            selected_article_ids=selected_article_ids,
            producer_state_path=producer_ids,
            session_factory=sag_session_factory,
            vector_store=vector_store if trusted_vector_export else None,
            embedding_client=descriptor if trusted_vector_export else None,
            on_progress=save_export_progress,
        )
    await _ensure_transfer_active(session, transfer, stage="after_snapshot")
    previous_state = persistent_workspace / ".octx" / "state.json"
    if previous_state.is_file():
        (workspace / ".octx").mkdir(mode=0o700)
        shutil.copyfile(previous_state, workspace / ".octx" / "state.json")

    transition_transfer(transfer, OctxTransferStatus.PACKAGING)
    transfer.progress = 0.6
    transfer.checkpoint = {
        **dict(transfer.checkpoint or {}),
        "snapshot_counts": dict(stats.counts),
        "vector_roles": sorted(stats.vector_roles),
        "progress_detail": {"phase": "packaging", "kind": "validate_package"},
    }
    append_octx_trace(
        transfer,
        stage="package_validation",
        state="started",
        details={"vector_role_count": len(stats.vector_roles)},
    )
    await session.commit()
    output = attempt_dir / "release.octx"
    try:
        built = await runner.build_package(
            BuildPackageRequest(
                workspace=workspace,
                output=output,
                name=str((transfer.checkpoint or {}).get("asset_name") or source.name),
                version=str((transfer.checkpoint or {}).get("selected_version") or "1.0.0"),
                capabilities={
                    "sag-structured": "0.1",
                    **({"vectors": "0.1"} if stats.vector_roles else {}),
                },
            )
        )
    except ValidationError as error:
        report = getattr(error, "report", None)
        issues = getattr(error, "issues", None)
        if isinstance(report, dict):
            transfer.validation_report = dict(report)
        elif isinstance(issues, list):
            transfer.validation_report = {"issues": issues}
        await session.commit()
        raise
    transfer.progress = 0.9
    transfer.checkpoint = {
        **dict(transfer.checkpoint or {}),
        "progress_detail": {"phase": "publishing", "kind": "artifact"},
    }
    append_octx_trace(transfer, stage="artifact_publish", state="started")
    await session.commit()
    await _ensure_transfer_active(session, transfer, stage="before_publish")
    artifact_key = storage.publish_release(built.output, built.asset_id, built.version, built.package_digest)
    await _ensure_transfer_active(session, transfer, stage="after_publish")

    state_source = workspace / ".octx" / "state.json"
    if not state_source.is_file():
        raise RuntimeError("OCTX producer state is missing after package build")
    state_target = persistent_workspace / ".octx" / "state.json"
    state_target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_temporary = state_target.with_name(f".{state_target.name}.{new_id()}.tmp")
    shutil.copyfile(state_source, state_temporary)
    state_temporary.chmod(0o600)
    state_temporary.replace(state_target)

    asset = await session.get(OctxAsset, built.asset_id)
    if asset is None:
        asset = OctxAsset(
            id=built.asset_id,
            name=str((transfer.checkpoint or {}).get("asset_name") or source.name)[:200],
            ownership=OctxAssetOwnership.LOCAL,
            producer_source_id=source.id,
        )
        session.add(asset)
        await session.flush()
    release = await session.scalar(
        select(OctxRelease).where(
            OctxRelease.asset_id == built.asset_id,
            OctxRelease.version == built.version,
        )
    )
    if release is None:
        release = OctxRelease(
            asset_id=built.asset_id,
            version=built.version,
            package_digest=built.package_digest,
            manifest={
                "asset": {
                    "id": built.asset_id,
                    "name": str((transfer.checkpoint or {}).get("asset_name") or source.name),
                },
                "release": {
                    "version": built.version,
                    "package_digest": built.package_digest,
                },
                "capabilities": {"sag-structured": {"version": "0.1"}},
            },
            artifact_key=artifact_key,
            created_by=OctxReleaseOrigin.EXPORT,
        )
        session.add(release)
        await session.flush()
    if export_scope == "document":
        document_binding = await session.get(OctxDocumentBinding, export_document_id)
        if document_binding is None:
            document_binding = OctxDocumentBinding(
                document_id=export_document_id,
                asset_id=asset.id,
                active_release_id=release.id,
                content_revision=1,
                released_revision=1,
                workspace_key=f"document-workspaces/{export_document_id}",
            )
            session.add(document_binding)
        else:
            document_binding.asset_id = asset.id
            document_binding.active_release_id = release.id
            document_binding.released_revision = document_binding.content_revision
            document_binding.workspace_key = f"document-workspaces/{export_document_id}"
    else:
        binding = await session.get(OctxSourceBinding, source.id)
        if binding is None:
            binding = OctxSourceBinding(
                source_id=source.id,
                asset_id=asset.id,
                active_release_id=release.id,
                content_revision=1,
                released_revision=1,
                workspace_key=f"workspaces/{source.id}",
            )
            session.add(binding)
        else:
            binding.asset_id = asset.id
            binding.active_release_id = release.id
            binding.released_revision = binding.content_revision
            binding.workspace_key = f"workspaces/{source.id}"
    transfer.asset_id = asset.id
    transfer.release_id = release.id
    transfer.package_version = release.version
    transfer.package_digest = release.package_digest
    transfer.artifact_key = release.artifact_key
    transfer.validation_report = dict(built.report)
    append_octx_trace(
        transfer,
        stage="ready",
        state="completed",
        details={"package_digest": release.package_digest},
    )
    await _commit_export_ready(session, transfer)
    return transfer


def _prepare_export_attempt(
    storage: OctxStorage,
    transfer_id: str,
    *,
    attempt: int,
) -> tuple[Path, Path]:
    """Reuse a transfer root while keeping every worker attempt immutable."""
    staging = storage.staging_dir(transfer_id)
    staging.mkdir(parents=True, exist_ok=True, mode=0o700)
    attempt_dir = staging / f"export-{max(1, attempt)}"
    attempt_dir.mkdir(mode=0o700)
    return attempt_dir, attempt_dir / "workspace"


async def _commit_export_ready(
    session: AsyncSession,
    transfer: OctxTransfer,
) -> None:
    """Atomically complete an export unless cancellation already won."""
    completed = await session.execute(
        update(OctxTransfer)
        .where(
            OctxTransfer.id == transfer.id,
            OctxTransfer.status == OctxTransferStatus.PACKAGING,
            OctxTransfer.cancellation_requested.is_(False),
        )
        .values(status=OctxTransferStatus.READY, progress=1.0)
        .execution_options(synchronize_session=False)
    )
    if completed.rowcount == 1:
        await session.commit()
        await session.refresh(transfer)
        return

    transfer_id = transfer.id
    await session.rollback()
    current = await session.get(OctxTransfer, transfer_id, populate_existing=True)
    if current is not None and (current.cancellation_requested or current.status is OctxTransferStatus.CANCELLED):
        raise ConflictError(
            "OCTX transfer cancelled before final export commit",
            code=ErrorCode.OCTX_TRANSFER_CANCELLED,
            layer=ErrorLayer.API,
            stage=ErrorStage.OCTX_EXPORT,
            retryable=False,
        )
    raise ConflictError(
        "OCTX export state changed before final commit",
        layer=ErrorLayer.STORE,
        stage=ErrorStage.OCTX_EXPORT,
        retryable=True,
    )
