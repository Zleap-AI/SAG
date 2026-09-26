"""OCTX 传输服务 —— 导入侧实现与对外门面。

测试会替换本模块的模块级绑定（`execute_structured_import`、
`import_knowledge_package`、`smoke_test_installation`、`settings`）。
被替换的名字只有在调用点同属本模块命名空间时才生效，因此导入侧实现与
`default_octx_storage` 保留在此；导出侧与共享辅助已外迁，并在末尾重新
导出，对外接口保持不变。
"""

from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import UploadFile
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.error_taxonomy import ErrorCode, ErrorLayer, ErrorStage
from sag_api.core.errors import ConflictError, ValidationError
from sag_api.db.base import new_id
from sag_api.db.models import (
    Document,
    Job,
    OctxAsset,
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
    JobType,
    OctxAssetOwnership,
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
    issue_decision_token,
)
from sag_api.octx.runner import OctxRunner
from sag_api.octx.storage import OctxStorage
from sag_api.sag.octx_importer import (
    build_structured_plan,
    document_display_metadata,
    import_knowledge_package,
    import_structured_plan,
)
from sag_api.sag.octx_plan_store import OctxPlanError, OctxPlanStore
from sag_api.sag.octx_smoke_test import smoke_test_installation
from sag_api.sag.octx_vector_protocol import (
    configured_embedding_identity,
    replace_vector_identity_record,
)
from sag_api.services.octx_checkpoint import merge_checkpoint
from sag_api.services.octx_conflict_service import (
    ImportDecision,
    confirm_import_decision,
    resolve_import_conflict,
)
from sag_api.services.octx_diagnostics_service import append_octx_trace

if TYPE_CHECKING:
    from sag_api.jobs import JobQueue

from sag_api.services.octx_export import (
    _commit_export_ready,
    _prepare_export_attempt,
    create_document_export_transfer,
    create_export_transfer,
    execute_export,
    submit_export_decision,
)
from sag_api.services.octx_transfer_common import (  # noqa: E402
    _create_job,
    _duration_seconds,
    _ensure_shadow_identity,
    _ensure_transfer_active,
    _export_progress,
    _import_started_at,
    _promote_knowledge_documents,
    _stored_upload,
    _VectorProgressGate,
)

logger = logging.getLogger(__name__)


def default_octx_storage() -> OctxStorage:
    engine = Path(settings.effective_data_dir).expanduser().resolve()
    upgrade_root = engine.parent / ".storage-upgrades"
    migration_id = "zleap-sag-0.7.1-to-0.8.2"
    return OctxStorage(
        engine / "octx",
        max_upload_bytes=settings.octx_max_upload_mb * 1024 * 1024,
        recovery_roots=(
            upgrade_root / migration_id / "original-engine" / "octx",
            upgrade_root / "backups" / migration_id / "engine" / "octx",
        ),
    )


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
            existing_owner = str((existing.checkpoint or {}).get("requested_by_user_id") or "")
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
        checkpoint=({"requested_by_user_id": requested_by_user_id} if requested_by_user_id else {}),
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
    merge_checkpoint(
        transfer,
        asset_name=str(validated.manifest["asset"].get("name") or "Imported OCTX"),
        record_counts=dict(validated.record_counts),
        capabilities=dict(validated.capabilities),
    )
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
        merge_checkpoint(
            transfer,
            allowed_actions=list(resolution.allowed_actions),
            decision_token=token,
            conflicts=[
                {
                    "source_id": item.source_id,
                    "source_name": item.source_name,
                    "active_version": item.active_version,
                    "local_changes": item.local_changes,
                }
                for item in resolution.conflicts
            ],
        )
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
    merge_checkpoint(
        transfer,
        progress_detail={"phase": "building_shadow"},
    )
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
    merge_checkpoint(
        transfer,
        progress_detail={"phase": "vectorizing", "completed_vectors": 0, "total_vectors": total_vectors},
    )
    await session.commit()
    await _ensure_transfer_active(session, transfer, stage="before_vector_rebuild")
    if vector_rebuilder is None:
        from sag_api.sag.octx_vector_rebuilder import rebuild_vectors

        await engine_manager.provision(source_config_id)
        vector_rebuilder = rebuild_vectors

    # 复用判定必须对比*当前配置*的 embedding 身份。引擎资源对象
    # （LimitedEmbeddingAdapter）不代理 model/base_url/维度，从它取值只会静默
    # 拿到默认值并永久禁用复用，所以身份在这里由配置解析后显式注入。
    local_embedding_identity = configured_embedding_identity(settings)

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
        merge_checkpoint(
            transfer,
            vector_progress=dict(value),
            source_config_id=source_config_id,
            progress_detail={
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
        )
        await session.commit()

    try:
        vector_stats = await vector_rebuilder(
            source_config_id,
            vector_checkpoint,
            reuse_batch_size=settings.octx_reused_vector_batch_size,
            enable_vector_reuse=settings.octx_arrow_vector_reuse_enabled,
            session_factory=sag_session_factory,
            embedding_client=await engine_manager.get_sag_embedding(source_config_id),
            vector_store=await engine_manager._vector_store(source_config_id),
            on_checkpoint=save_vector_checkpoint,
            package_path=package_path,
            plan_path=plan_path,
            prevalidated_vector_valid=prevalidated_vector_valid,
            local_embedding_identity=local_embedding_identity,
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
    merge_checkpoint(
        transfer,
        progress_detail={
            "phase": "validating_shadow",
            "completed_vectors": total_vectors,
            "total_vectors": total_vectors,
        },
    )
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
    merge_checkpoint(
        transfer,
        progress_detail={"phase": "switching"},
    )
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
                    vector_identity=(
                        dict(local_embedding_identity) if isinstance(local_embedding_identity, dict) else None
                    ),
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
    replace_vector_identity_record(source, local_embedding_identity)
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
    merge_checkpoint(
        transfer,
        plan_path=str(plan_path),
        id_namespace=id_namespace,
        source_config_id=source_config_id,
        vector_stats=dict(vector_stats),
        smoke_test={
            "sample_chunk_id": smoke_stats.get("sample_chunk_id"),
            "search_stats": smoke_stats.get("search_stats"),
        },
        progress_detail={"phase": "complete", "duration_seconds": _duration_seconds(started_at)},
    )
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
    local_embedding_identity = configured_embedding_identity(settings)
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
        merge_checkpoint(
            transfer,
            knowledge=snapshot,
            source_config_id=source_config_id,
            progress_detail={
                "phase": "rebuilding_documents",
                "completed_documents": completed_documents,
                "total_documents": total_documents,
                "current_document": current_document,
            },
        )
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
    merge_checkpoint(
        transfer,
        progress_detail={
            **dict((transfer.checkpoint or {}).get("progress_detail") or {}),
            "phase": "indexing",
            "current_document": None,
        },
    )
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
    merge_checkpoint(
        transfer,
        progress_detail={**dict((transfer.checkpoint or {}).get("progress_detail") or {}), "phase": "switching"},
    )
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
            vector_identity=(dict(local_embedding_identity) if isinstance(local_embedding_identity, dict) else None),
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
    replace_vector_identity_record(source, local_embedding_identity)
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
    merge_checkpoint(
        transfer,
        knowledge=knowledge_checkpoint,
        id_namespace=id_namespace,
        source_config_id=source_config_id,
        smoke_test={
            "sample_chunk_id": knowledge_smoke.get("sample_chunk_id"),
            "search_stats": knowledge_smoke.get("search_stats"),
        },
        progress_detail={
            **dict((transfer.checkpoint or {}).get("progress_detail") or {}),
            "phase": "complete",
            "current_document": None,
            "duration_seconds": _duration_seconds(started_at),
        },
    )
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
        binding = await session.get(OctxSourceBinding, transfer.target_source_id) if transfer.target_source_id else None
        if binding is None or expected_revision is None or binding.content_revision != int(expected_revision):
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


# --- 对外接口：导入侧实现见上；导出侧与共享辅助在此重新导出 ---

__all__ = [
    "_VectorProgressGate",
    "_commit_export_ready",
    "_export_progress",
    "_prepare_export_attempt",
    "create_document_export_transfer",
    "create_export_transfer",
    "create_import_transfer",
    "default_octx_storage",
    "execute_export",
    "execute_import",
    "execute_knowledge_import",
    "execute_structured_import",
    "preflight_import",
    "submit_export_decision",
    "submit_import_decision",
]
