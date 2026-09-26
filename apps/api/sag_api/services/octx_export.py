"""OCTX 导出侧 —— 从 `octx_transfer_service` 抽出。

含导出 transfer 的创建、决策提交与执行。共享辅助从 `octx_transfer_common` 取。

本模块读取模块级的 `settings` 绑定；当前没有测试对它打桩，但若将来需要
（`test_octx_runtime` 即以此方式替换 facade 的 `settings`），在此模块上做
`monkeypatch.setattr(octx_export, "settings", ...)` 会生效。
"""

from __future__ import annotations

import logging
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    OctxRelease,
    OctxSourceBinding,
    OctxTransfer,
    Source,
)
from sag_api.db.models.octx import transition_transfer
from sag_api.enums import (
    DocumentStatus,
    JobType,
    OctxAssetOwnership,
    OctxExportAction,
    OctxReleaseOrigin,
    OctxTransferDirection,
    OctxTransferStatus,
)
from sag_api.octx.decision_token import (
    DecisionTokenError,
    ExportDecisionTokenClaims,
    issue_export_decision_token,
    verify_export_decision_token,
)
from sag_api.octx.runner import BuildPackageRequest, OctxRunner
from sag_api.octx.semver import bump_semver_patch, parse_semver, validate_semver
from sag_api.octx.storage import OctxStorage
from sag_api.sag.octx_snapshot import export_snapshot
from sag_api.services.octx_checkpoint import merge_checkpoint
from sag_api.services.octx_diagnostics_service import append_octx_trace

if TYPE_CHECKING:
    from sag_api.jobs import JobQueue

from sag_api.services.octx_transfer_common import (
    _create_job,
    _ensure_transfer_active,
    _export_checkpoint,
    _export_document_state,
    _export_progress,
    _export_selection_fingerprint,
)

logger = logging.getLogger(__name__)


def _default_storage() -> OctxStorage:
    """惰性取 facade 的 `default_octx_storage`。

    该函数留在 facade 是有意的：`test_octx_runtime` 会替换 facade 的模块级
    `settings` 对象再调用它，因此不能搬进 common；此处函数体内导入以避开
    facade -> export 的循环。
    """
    from sag_api.services.octx_transfer_service import default_octx_storage

    return default_octx_storage()


async def create_export_transfer(
    session: AsyncSession,
    source_id: str,
    *,
    version: str | None,
    job_queue: JobQueue,
    requested_by_user_id: str | None = None,
    storage: OctxStorage | None = None,
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
    reusable_original = bool(
        not excluded
        and binding is not None
        and active_release is not None
        and active_asset is not None
        and active_asset.ownership is OctxAssetOwnership.IMPORTED
        and binding.content_revision == binding.released_revision
    )
    if reusable_original:
        artifact_storage = storage or _default_storage()
        reusable_original = artifact_storage.resolve_release(
            active_release.artifact_key,
            active_release.package_digest,
        ).is_file()
    if reusable_original:
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
                **({"requested_by_user_id": requested_by_user_id} if requested_by_user_id else {}),
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
        merge_checkpoint(
            transfer,
            allowed_actions=[OctxExportAction.EXPORT_READY_ONLY.value, OctxExportAction.CANCEL.value],
            decision_token=token,
        )
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
                and (requested_version is None or requested_version == checkpoint.get("selected_version"))
                and (not requested_by_user_id or not existing_owner or requested_by_user_id == existing_owner)
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
    if active_release is not None and parse_semver(selected_version) <= parse_semver(active_release.version):
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
    merge_checkpoint(
        transfer,
        progress_detail={"phase": "snapshot", "kind": "documents", "completed": 0, "total": len(selected_documents)},
    )
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

    # Vector export is a portable data layer, not a reuse decision. Always try
    # to carry complete vectors from the source partition. The stored identity
    # controls whether the profile is compatible or rebuild_required; importers
    # make the final reuse decision and export never calls the embedding provider.
    # 迁移注记:0.8.2 起 DataSource 无 target_config,向量身份改存业务库 Source.config。
    source_config = source.config if isinstance(source.config, dict) else {}
    stored_vector_identity = source_config.get("octx_vector_identity")
    if not isinstance(stored_vector_identity, dict):
        stored_vector_identity = None
    if vector_store is None and engine_manager is not None:
        try:
            vector_store = await engine_manager._vector_store(source.sag_source_config_id, source)
        except Exception:
            # Missing vector storage must not block a valid structured export,
            # but the degradation must stay diagnosable in task logs.
            logger.warning("OCTX export vector storage unavailable; exporting structured data only", exc_info=True)
            vector_store = None

    async def save_export_progress(detail: dict[str, Any]) -> None:
        await _ensure_transfer_active(session, transfer, stage=str(detail.get("phase") or "export"))
        transfer.progress = max(float(transfer.progress or 0), _export_progress(detail))
        merge_checkpoint(
            transfer,
            progress_detail=dict(detail),
        )
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
            vector_store=vector_store,
            embedding_client=None,
            vector_identity=stored_vector_identity,
            on_progress=save_export_progress,
        )
    await _ensure_transfer_active(session, transfer, stage="after_snapshot")
    previous_state = persistent_workspace / ".octx" / "state.json"
    if previous_state.is_file():
        (workspace / ".octx").mkdir(mode=0o700)
        shutil.copyfile(previous_state, workspace / ".octx" / "state.json")

    transition_transfer(transfer, OctxTransferStatus.PACKAGING)
    transfer.progress = 0.6
    merge_checkpoint(
        transfer,
        snapshot_counts=dict(stats.counts),
        vector_roles=sorted(stats.vector_roles),
        progress_detail={"phase": "packaging", "kind": "validate_package"},
    )
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
    merge_checkpoint(
        transfer,
        progress_detail={"phase": "publishing", "kind": "artifact"},
    )
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
