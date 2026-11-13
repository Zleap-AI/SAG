"""任务处理器 —— 按 JobType 分发。

队列负责执行准入和重试；处理器负责领域对象的阶段状态与计数。
需要与发布或记录删除原子确认的成功终态，在同一个业务事务中保存。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from sqlalchemy import case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from zleap.sag.operations import (
    DeleteDataSourceRequest,
    DeleteSourceRequest,
    OperationContext,
    OperationResult,
    ProcessSourceRequest,
)
from zleap.sag.pipeline import ChunkSetRef, EventSetRef

from sag_api.core.db import SessionLocal
from sag_api.core.error_taxonomy import ErrorLayer, ErrorStage
from sag_api.core.errors import (
    ApiError,
    ConflictError,
    DocumentRestartRequiredError,
    NotFoundError,
    UpstreamError,
    ValidationError,
)
from sag_api.core.logging import get_logger
from sag_api.db.base import new_id
from sag_api.db.models import AgentBinding, Document, Job, OctxInstallation, OctxTransfer, Source
from sag_api.db.models.job import job_payload_matches
from sag_api.enums import BindingTargetType, DocumentStatus, JobStatus, JobType
from sag_api.jobs.control import JobPaused, JobYielded
from sag_api.jobs.octx_tasks import (
    export_octx,
    gc_octx_installation,
    gc_octx_transfers,
    import_octx,
    preflight_octx,
)
from sag_api.jobs.scheduling import SOURCE_BUSY, SOURCE_MAINTENANCE
from sag_api.parsing import ParsePaused, prepare_document
from sag_api.sag import EngineManager
from sag_api.sag.document_identity import document_processing_identity, processing_identity_restart_reason
from sag_api.sag.document_vector_identity import refresh_source_vector_identity
from sag_api.sag.dto import extraction_display_percent
from sag_api.sag.octx_vector_protocol import configured_embedding_identity
from sag_api.services.source_operation_service import (
    acquire_operation_lease,
    acquire_source_exclusive_lease,
    acquire_source_processing_lease,
)

log = get_logger("jobs")

TaskHandler = Callable[[AsyncSession, Job], Awaitable[None]]

# 文档失败时的当前状态 → 链路环节。这是「唯一知道 stage 的地方」的兜底映射：
# 当异常本身没带 stage（非 ApiError，例如逃逸的 jsonschema 错误）时，用文档处在
# 哪个状态推断它卡在哪个环节。
_STATUS_TO_STAGE: dict[DocumentStatus, ErrorStage] = {
    DocumentStatus.PENDING: ErrorStage.PARSE,
    DocumentStatus.LOADING: ErrorStage.PARSE,
    DocumentStatus.EXTRACTING: ErrorStage.EXTRACT,
}
_CONTROL_TRANSITION_STATES = {
    DocumentStatus.PAUSING,
    DocumentStatus.DELETING,
    DocumentStatus.DELETE_FAILED,
}
_DELETE_CONTROL_STATES = {
    DocumentStatus.DELETING,
    DocumentStatus.DELETE_FAILED,
}
_PARSER_UPLOAD_STATES = {"uploading", "uploaded"}
_PARSER_QUEUE_STATES = {"created", "pending", "queued", "queueing"}
_PARSER_DONE_STATES = {"done", "success", "succeeded", "completed", "finished"}
_PARSER_FAILED_STATES = {"failed", "failure", "error", "cancelled", "canceled", "fallback_failed"}
_PARSER_SOURCES = {"mineru", "markitdown", "anydoc", "original"}
_SECRET_QUERY_KEYS = re.compile(r"(?i)([?&](?:token|key|signature|credential|authorization|x-amz-[^=]+)=)[^&#\s]+")
_BEARER_TOKEN = re.compile(r"(?i)bearer\s+\S+")
_API_KEY = re.compile(r"(?i)\b(?:sk|ak)-[a-z0-9._-]{6,}\b")
_URL = re.compile(r"https?://[^\s；，,]+")


def _mineru_details(state: dict[str, Any] | None = None) -> tuple[str, str]:
    current = state or {}
    service = current.get("mineru_service")
    if service == "official":
        model = current.get("mineru_model")
        return "official", model if model in {"vlm", "pipeline"} else "vlm"
    if service == "self_hosted":
        tier = current.get("mineru_tier")
        return "self_hosted", tier if isinstance(tier, str) and tier else "default"
    if service == "302":
        version = str(current.get("mineru_version") or "").lower()
        # The public contract predates 302's 2.0 label. "pipeline" is the
        # closest stable representation; importantly, it does not claim 2.5.
        return "302", "2.5" if version in {"2.5", "v2.5"} else "pipeline"
    return "302", "2.5"


def _redact_parser_reason(value: object) -> str | None:
    if value is None:
        return None
    message = " ".join(str(value).split())
    if not message:
        return None
    message = _SECRET_QUERY_KEYS.sub(r"\1[REDACTED]", message)
    message = _BEARER_TOKEN.sub("Bearer [REDACTED]", message)
    message = _API_KEY.sub("[REDACTED]", message)
    message = _URL.sub("[URL REDACTED]", message)
    return message[:300]


def _fallback_reason(state: dict[str, Any], source: str) -> str | None:
    """回退原因按来源解析器取值，回退目标自己的失败作为兜底。"""
    fallback = state.get("fallback")
    if isinstance(fallback, dict):
        return _redact_parser_reason(fallback.get(f"{source}_error") or fallback.get("markitdown_error"))
    return _redact_parser_reason(state.get("error") or state.get("message"))


def _parser_state_values(state: dict[str, Any]) -> dict[str, str | None]:
    raw_provider = state.get("provider")
    provider = raw_provider if raw_provider in _PARSER_SOURCES else None
    raw_status = str(state.get("status") or "").lower()
    fallback = isinstance(state.get("fallback"), dict)
    if raw_status.startswith("fallback_") or fallback:
        failed = raw_status == "fallback_failed"
        # 回退来源取实际来源解析器；旧记录只在顶层 provider 上标记来源。
        source = provider or _legacy_fallback_source(state) or "mineru"
        status = "failed" if failed else "fallback"
        parser_provider = source if failed else "markitdown"
        fallback_from = source
    elif raw_status in _PARSER_UPLOAD_STATES or state.get("upload_url") and not state.get("task_id"):
        status = "uploading"
        parser_provider = provider
        fallback_from = None
    elif raw_status in _PARSER_QUEUE_STATES or state.get("task_id") and not raw_status:
        status = "queued"
        parser_provider = provider
        fallback_from = None
    elif raw_status in _PARSER_DONE_STATES:
        status = "done"
        parser_provider = provider
        fallback_from = None
    elif raw_status in _PARSER_FAILED_STATES:
        status = "failed"
        parser_provider = provider
        fallback_from = None
    else:
        status = "running"
        parser_provider = provider
        fallback_from = None
    mineru_provider = mineru_model = None
    if "mineru" in {parser_provider, fallback_from}:
        mineru_provider, mineru_model = _mineru_details(state)
    return {
        "parser_provider": parser_provider,
        "mineru_provider": mineru_provider,
        "mineru_model": mineru_model,
        "parser_status": status,
        "fallback_from": fallback_from,
        "fallback_reason": _fallback_reason(state, fallback_from) if fallback_from else None,
    }


def _legacy_fallback_source(state: dict[str, Any]) -> str | None:
    """从旧回退记录里读出真实来源解析器。

    历史 MinerU 状态没有 `fallback.fallback_from`，来源在顶层 `provider` 上；
    两者都缺失时无法确认来源，交由调用方按 MinerU 兼容处理。
    """
    fallback = state.get("fallback")
    if isinstance(fallback, dict):
        recorded = fallback.get("fallback_from")
        if isinstance(recorded, str) and recorded in _PARSER_SOURCES:
            return recorded
    recorded = state.get("provider")
    return recorded if isinstance(recorded, str) and recorded in _PARSER_SOURCES else None


def _prepared_parser_values(prepared, state: dict[str, Any] | None) -> dict[str, str | None]:  # noqa: ANN001
    mineru_provider = mineru_model = None
    if "mineru" in {prepared.provider, prepared.fallback_from}:
        mineru_provider, mineru_model = _mineru_details(state)
    return {
        "parser_provider": prepared.provider,
        "mineru_provider": mineru_provider,
        "mineru_model": mineru_model,
        "parser_status": "fallback" if prepared.fallback_from else "done",
        "fallback_from": prepared.fallback_from,
        "fallback_reason": _redact_parser_reason(prepared.fallback_error),
    }


def _cleared_parser_values() -> dict[str, None]:
    """重新处理前清空上一轮的解析器信息，避免旧状态冒充本轮结果。"""
    return {
        "parser_provider": None,
        "mineru_provider": None,
        "mineru_model": None,
        "parser_status": None,
        "fallback_from": None,
        "fallback_reason": None,
    }


async def _yield_after_document_transition_lost(
    session: AsyncSession,
    document_id: str,
    control_revision: int,
) -> None:
    """Converge a winning pause/delete intent without overwriting it."""
    await session.rollback()
    current = await session.get(Document, document_id, populate_existing=True)
    if (
        current is not None
        and current.control_revision == control_revision
        and current.status == DocumentStatus.PAUSING
    ):
        paused = await session.execute(
            update(Document)
            .where(
                Document.id == document_id,
                Document.control_revision == control_revision,
                Document.status == DocumentStatus.PAUSING,
            )
            .values(status=DocumentStatus.PAUSED, error=None)
            .execution_options(synchronize_session=False)
        )
        if paused.rowcount == 1:
            await session.commit()
        else:
            await session.rollback()
    raise JobPaused()


def _classify_document_failure(e: Exception, current_status: DocumentStatus) -> tuple[ErrorLayer, ErrorStage]:
    """推断失败的责任层与链路环节。

    优先信任领域异常自带的 layer/stage（LLM 分类、引擎翻译层都会填）；
    否则退化为「按文档当前状态猜环节」，责任层归 engine（zleap-sag 抽取/入库
    过程中逃逸的裸异常，如 jsonschema.ValidationError，几乎都发生在引擎侧）。
    """
    if isinstance(e, ApiError) and e.layer is not None and e.stage is not None:
        return e.layer, e.stage
    stage = _STATUS_TO_STAGE.get(current_status, ErrorStage.EXTRACT)
    return ErrorLayer.ENGINE, stage


async def process_document(session: AsyncSession, job: Job, *, engine_manager: EngineManager, job_queue=None) -> None:
    document = await session.get(Document, job.document_id) if job.document_id else None
    if document is None:
        raise NotFoundError("文档不存在")
    async with AsyncExitStack() as admission:
        try:
            lease_lost = await admission.enter_async_context(
                acquire_source_processing_lease(
                    SessionLocal,
                    document.source_id,
                    job.id,
                    admission_timeout_seconds=0,
                )
            )
        except ConflictError as error:
            raise JobYielded(SOURCE_BUSY) from error
        await _process_document_unlocked(
            session,
            job,
            engine_manager=engine_manager,
            job_queue=job_queue,
            lease_lost=lease_lost,
        )


async def _process_document_unlocked(
    session: AsyncSession, job: Job, *, engine_manager: EngineManager, lease_lost: asyncio.Event, job_queue=None
) -> None:
    """解析、入库并按 chunk 并发抽取；每个 chunk 完成即保存断点。"""
    document = await session.get(Document, job.document_id) if job.document_id else None
    if document is None:
        raise NotFoundError("文档不存在")
    source = await session.get(Source, document.source_id)
    if source is None:
        raise NotFoundError("信源不存在")
    control_revision = (job.payload or {}).get("control_revision", document.control_revision)
    if document.control_revision != control_revision:
        raise JobPaused()
    saved_request = (job.payload or {}).get("process_request")
    if saved_request is None and (job.payload or {}).get("process_checkpoint"):
        raise ValidationError("旧任务断点不能直接恢复，请确认重新处理文档")
    request = ProcessSourceRequest.model_validate(saved_request) if saved_request is not None else None
    from sag_api.services.document_service import reconcile_document_receipt

    if await reconcile_document_receipt(session, source, document, job, engine_manager):
        await session.commit()
        return
    input_artifact = (job.payload or {}).get("input_artifact")
    input_path = input_artifact["path"] if input_artifact else document.storage_path
    preserve_publication = document.has_publication
    scheduler_yield_reason: str | None = None

    async def refresh_payload() -> dict:
        await session.refresh(job, attribute_names=["payload"])
        return dict(job.payload or {})

    document_id, job_id = document.id, job.id
    run_id = new_id()
    previous_run_id = document.processing_run_id
    active_states = [DocumentStatus.PENDING, DocumentStatus.LOADING, DocumentStatus.EXTRACTING]

    async def update_display(values: dict, *, condition=None, initialize: bool = False) -> bool:
        # Display writes have their own transaction and must never poison the
        # checkpoint session. Run identity also fences callbacks after resume.
        try:
            async with SessionLocal() as progress_session:
                statement = update(Document).where(
                    Document.id == document_id,
                    Document.control_revision == control_revision,
                    Document.processing_run_id == (previous_run_id if initialize else run_id),
                    Document.status.in_(active_states + ([DocumentStatus.FAILED] if initialize else [])),
                )
                if condition is not None:
                    statement = statement.where(condition)
                changed = await progress_session.execute(statement.values(**values).returning(Document.progress))
                percent = changed.scalar_one_or_none()
                if percent is None:
                    return False
                if "progress" in values:
                    await progress_session.execute(update(Job).where(Job.id == job_id).values(progress=percent / 100))
                await progress_session.commit()
                return True
        except Exception as exc:  # noqa: BLE001 - display state is best effort
            log.warning("文档处理状态更新失败 doc=%s error_type=%s", document_id, type(exc).__name__)
            return False

    display_initialized = await update_display(
        {
            "processing_run_id": run_id,
            "processing_stage": "queued",
            "processed_chunks": 0 if request is not None else None,
            "total_chunks": document.total_chunks if request is not None else None,
            "status": DocumentStatus.PENDING if document.status == DocumentStatus.FAILED else document.status,
        },
        initialize=True,
    )

    async def on_stage(stage: str) -> None:
        stage = "parsing" if stage == "loading" else stage
        if stage not in {"parsing", "chunking", "indexing", "waiting_extraction", "extracting", "finalizing"}:
            return
        values: dict = {"processing_stage": stage}
        if stage in {"parsing", "chunking", "indexing"}:
            values["status"] = DocumentStatus.LOADING
            values["progress"] = case((Document.progress < 5, 5), else_=Document.progress)
        else:
            values["status"] = DocumentStatus.EXTRACTING
            if stage == "waiting_extraction":
                values.update(
                    progress=20,
                    processed_chunks=0,
                    total_chunks=document.total_chunks,
                )
            elif stage == "extracting":
                values["progress"] = case(
                    (or_(Document.processed_chunks.is_(None), Document.processed_chunks == 0), 20),
                    else_=Document.progress,
                )
        await update_display(values)

    async def on_progress(completed: int, total: int) -> None:
        if total <= 0 or completed < 0:
            return
        await update_display(
            {
                "processing_stage": "finalizing" if completed >= total else "extracting",
                "processed_chunks": completed,
                "total_chunks": total,
                "progress": extraction_display_percent(completed, total, committed=False),
            },
            condition=(
                (Document.status == DocumentStatus.EXTRACTING)
                & or_(Document.processed_chunks.is_(None), Document.processed_chunks < completed)
            ),
        )

    async def save_parser_result(values: dict, *, state: dict | None = None, parsed_path: str | None = None) -> None:
        for _ in range(4):
            payload = await refresh_payload()
            next_payload = {**payload, "parser_result": values}
            if state is not None:
                next_payload["document_parser"] = state
            if parsed_path is not None:
                next_payload["parsed_artifact"] = {"path": parsed_path}
            saved = await session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    job_payload_matches(session, payload),
                    Job.document_id.in_(
                        select(Document.id).where(
                            Document.id == document_id,
                            Document.control_revision == control_revision,
                        )
                    ),
                )
                .values(payload=next_payload)
                .execution_options(synchronize_session=False)
            )
            if saved.rowcount == 1:
                if not preserve_publication:
                    await session.execute(
                        update(Document)
                        .where(
                            Document.id == document_id,
                            Document.control_revision == control_revision,
                            Document.status.in_(active_states + [DocumentStatus.PAUSING, DocumentStatus.PAUSED]),
                        )
                        .values(**values)
                        .execution_options(synchronize_session=False)
                    )
                await session.commit()
                await session.refresh(job, attribute_names=["payload"])
                return
            await session.rollback()
            await session.refresh(document)
            await session.refresh(source)
            await session.refresh(job)
            if document.control_revision != control_revision:
                raise JobPaused()
        raise ValidationError("任务控制状态已变化，请重新执行恢复操作")

    async def on_parser_state(state: dict) -> None:
        await session.refresh(document)
        if document.status in _CONTROL_TRANSITION_STATES | {DocumentStatus.PAUSED, DocumentStatus.READY}:
            return
        if display_initialized and document.processing_run_id != run_id:
            return
        await update_display(
            {
                "status": DocumentStatus.LOADING,
                "processing_stage": "parsing",
                "progress": case((Document.progress < 10, 10), else_=Document.progress),
            }
        )
        await save_parser_result(_parser_state_values(state), state=state)

    async def should_pause() -> bool:
        nonlocal scheduler_yield_reason
        if lease_lost.is_set():
            scheduler_yield_reason = None
            return True
        async with SessionLocal() as control_session:
            current_job = await control_session.get(Job, job.id)
            if current_job is None:
                return True
            current_document = await control_session.get(Document, document_id)
            if (
                current_document is None
                or current_document.control_revision != control_revision
                or not current_document.is_active
                or (display_initialized and current_document.processing_run_id != run_id)
            ):
                return True
            if current_job.status == JobStatus.PAUSED or (current_job.payload or {}).get("pause_requested"):
                scheduler_yield_reason = None
                return True
        if job_queue is not None and job_queue.source_maintenance_requested(source.id):
            scheduler_yield_reason = SOURCE_MAINTENANCE
            return True
        # 信源正在被删除：请求在途处理任务尽快让路并释放处理租约，使同步删除
        # 能在 HTTP 窗口内完成，而非被动等待解析/抽取自然结束。此处按“暂停”语义
        # 处理（不设置 yield 原因）——文档随后会随信源级联删除。
        if job_queue is not None and job_queue.source_stop_requested(source.id):
            return True
        return False

    async def _pause_or_yield() -> None:
        """把当前文档落到 PAUSED 或让行，供解析/抽取两个阶段共用。"""
        await session.refresh(document)
        if document.control_revision != control_revision:
            raise JobPaused()
        if scheduler_yield_reason == SOURCE_MAINTENANCE and document.status not in _CONTROL_TRANSITION_STATES:
            raise JobYielded(SOURCE_MAINTENANCE)
        if document.status in _DELETE_CONTROL_STATES:
            raise JobPaused()
        expected_status = document.status
        paused = await session.execute(
            update(Document)
            .where(
                Document.id == document.id,
                Document.control_revision == control_revision,
                Document.status == expected_status,
            )
            .values(status=DocumentStatus.PAUSED, error=None)
            .execution_options(synchronize_session=False)
        )
        if paused.rowcount != 1:
            await _yield_after_document_transition_lost(session, document_id, control_revision)
        await session.commit()
        raise JobPaused()

    # A worker retry reuses the document row. Clear the previous attempt's
    # failure before parsing can block for a long time. A fresh parse clears
    # stale provenance; retries retain saved parser tasks and chunk resumes
    # retain their completed parser result. A replacement keeps the published
    # provenance until the completion transaction selects the candidate.
    # A winning pause/delete intent (document-side transition state) is left
    # untouched so concurrent control flows are never overwritten.
    if document.status not in _CONTROL_TRANSITION_STATES:
        saved_parser = (job.payload or {}).get("document_parser")
        resuming_parse = isinstance(saved_parser, dict) and bool(saved_parser)
        if request is None and not resuming_parse and not preserve_publication:
            for field, value in _cleared_parser_values().items():
                setattr(document, field, value)
        if document.error is not None:
            document.error = None
        await session.commit()

    try:
        prepared = None
        parser_stage = False

        async def bind_request(processor, config, runtime_settings) -> ProcessSourceRequest:
            nonlocal request, prepared, parser_stage
            try:
                identity = await document_processing_identity(
                    config,
                    runtime_settings,
                    source.sag_source_config_id,
                    input_path,
                    **({"engine_kind": "sag2"} if getattr(source, "engine_kind", "sag1") == "sag2" else {}),
                )
            except OSError:
                raise DocumentRestartRequiredError("resume_input_unavailable") from None
            for _ in range(4):
                payload = await refresh_payload()
                saved = payload.get("process_request")
                if saved is not None:
                    request = ProcessSourceRequest.model_validate(saved)
                confirmed = payload.get("confirmed_identity")
                if confirmed is not None and confirmed != identity:
                    raise DocumentRestartRequiredError("confirmation_target_changed")
                baseline = payload.get("processing_identity")
                if baseline is not None:
                    reason = processing_identity_restart_reason(baseline, identity)
                    if reason:
                        raise DocumentRestartRequiredError(reason)
                    break
                if request is not None or payload.get("document_parser"):
                    raise DocumentRestartRequiredError("legacy_processing_identity")
                stored = await session.execute(
                    update(Job)
                    .where(
                        Job.id == job.id,
                        job_payload_matches(session, payload),
                        Job.document_id.in_(
                            select(Document.id).where(
                                Document.id == document_id,
                                Document.control_revision == control_revision,
                            )
                        ),
                    )
                    .values(payload={**payload, "processing_identity": identity})
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
                if stored.rowcount == 1:
                    break
            else:
                raise ValidationError("任务控制状态已变化，请重新执行恢复操作")
            if request is not None:
                return request
            parser_stage = True
            try:
                prepared = await prepare_document(
                    input_path,
                    runtime_settings,
                    state=(job.payload or {}).get("document_parser"),
                    on_state=on_parser_state,
                    should_pause=should_pause,
                )
            except ParsePaused:
                await _pause_or_yield()
            parser_stage = False
            parser_state = (job.payload or {}).get("document_parser")
            await save_parser_result(_prepared_parser_values(prepared, parser_state), parsed_path=str(prepared.path))
            if prepared.fallback_from:
                log.warning(
                    "文档解析已降级 doc=%s job=%s from=%s to=%s cached=%s error=%s",
                    document.id,
                    getattr(job, "id", None),
                    prepared.fallback_from,
                    prepared.provider,
                    prepared.cached,
                    _redact_parser_reason(prepared.fallback_error),
                )
            if prepared is None:
                raise RuntimeError("文档解析结果缺失")
            operation_id = new_id()
            candidate = await processor.build_request(
                prepared.path,
                context=OperationContext(
                    operation_id=operation_id,
                    idempotency_key=operation_id,
                    request_digest="0" * 64,
                    fence_scope=source.sag_source_config_id,
                    fence_token=1,
                    owner_id=job.id,
                    isolation="input",
                ),
                input_id=new_id(),
                original_path=input_path,
            )
            current = await document_processing_identity(
                config,
                runtime_settings,
                source.sag_source_config_id,
                input_path,
                **({"engine_kind": "sag2"} if getattr(source, "engine_kind", "sag1") == "sag2" else {}),
            )
            reason = processing_identity_restart_reason(identity, current)
            if reason or candidate.source.original_sha256 != identity["original_sha256"]:
                raise DocumentRestartRequiredError(
                    "confirmation_target_changed"
                    if (job.payload or {}).get("confirmed_identity")
                    else reason or "resume_input_changed",
                )
            for _ in range(4):
                payload = await refresh_payload()
                saved = payload.get("process_request")
                if saved is not None:
                    request = ProcessSourceRequest.model_validate(saved)
                    return request
                bound = await session.execute(
                    update(Job)
                    .where(
                        Job.id == job.id,
                        job_payload_matches(session, payload),
                        Job.document_id.in_(
                            select(Document.id).where(
                                Document.id == document_id,
                                Document.control_revision == control_revision,
                            )
                        ),
                    )
                    .values(
                        payload={
                            **payload,
                            "process_request": candidate.model_dump(mode="json"),
                            "processing_identity": identity,
                            "control_revision": control_revision,
                            "vector_identity": (None if getattr(source, "engine_kind", "sag1") == "sag2"
                                            else configured_embedding_identity(runtime_settings)),
                        }
                    )
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
                if bound.rowcount == 1:
                    request = candidate
                    await session.refresh(job, attribute_names=["payload"])
                    return request
            await session.refresh(document)
            if document.control_revision != control_revision:
                raise JobPaused()
            raise ValidationError("任务控制状态已变化，请重新执行恢复操作")

        result = await engine_manager.process_document(
            source.sag_source_config_id,
            bind_request,
            source=source,
            on_stage=on_stage,
            on_progress=on_progress,
            should_pause=should_pause,
            document_title=Path(document.filename).stem.strip(),
        )
        if result is None or result.failure_code == "cancelled":
            await _pause_or_yield()
        if result.status != "succeeded" or result.checkpoint != "ready":
            from sag_api.sag.errors import operation_failure

            raise operation_failure(result, stage=ErrorStage.EXTRACT)
        chunks = ChunkSetRef.model_validate(result.result["chunk_ref"])
        events = EventSetRef.model_validate(result.result["event_ref"])
        if request is None or result.source_id != request.source.descriptor.source_id:
            raise RuntimeError("文档完成结果与持久请求不一致")
        token_usage = int(((result.extraction_progress or {}).get("token_usage") or {}).get("total_tokens") or 0)
    except (JobPaused, JobYielded, DocumentRestartRequiredError):
        raise
    except Exception as e:  # noqa: BLE001 - 记录到文档后再上抛给 worker
        await session.refresh(document)
        if document.control_revision != control_revision:
            raise JobPaused() from e
        if document.status in _CONTROL_TRANSITION_STATES or document.status == DocumentStatus.PAUSED:
            await _yield_after_document_transition_lost(session, document_id, control_revision)
        layer, stage = _classify_document_failure(e, document.status)
        expected_status = document.status
        message = getattr(e, "message", None) or str(e)
        public_message = message
        parser_failure_values: dict[str, str | None] = {}
        parser_state = (job.payload or {}).get("document_parser")
        parser_failed = parser_stage and isinstance(parser_state, dict)
        if parser_failed:
            public_message = _redact_parser_reason(message) or "文档解析失败"
            # 有解析器上下文的普通解析失败与回退失败都要落 parser_status=failed：
            # 只处理 fallback_failed 会让“AnyDoc 直接失败”在文档上仍显示 done。
            parser_failure_values = _parser_state_values(parser_state)
            parser_failure_values["parser_status"] = "failed"
            parser_failure_values["fallback_reason"] = _redact_parser_reason(message)
            if preserve_publication:
                parser_failure_values = {}
        failed = await session.execute(
            update(Document)
            .where(
                Document.id == document.id,
                Document.control_revision == control_revision,
                Document.status == expected_status,
            )
            .values(
                status=DocumentStatus.FAILED,
                error=public_message,
                error_layer=layer.value,
                error_stage=stage.value,
                **parser_failure_values,
            )
            .execution_options(synchronize_session=False)
        )
        if failed.rowcount != 1:
            await _yield_after_document_transition_lost(session, document_id, control_revision)
        log.warning(
            "文档处理失败 doc=%s layer=%s stage=%s error=%s",
            document.id,
            layer.value,
            stage.value,
            public_message,
        )
        await session.commit()
        raise

    await session.refresh(document)
    if document.status in _DELETE_CONTROL_STATES:
        raise JobPaused()
    from sag_api.services.document_service import publish_document_result

    await refresh_payload()
    if not await publish_document_result(
        session,
        source,
        document,
        job,
        result,
        expected_run_id=run_id if display_initialized else previous_run_id,
    ):
        await _yield_after_document_transition_lost(session, document_id, control_revision)
    await session.commit()
    log.info(
        "文档处理完成 doc=%s parser=%s cached=%s chunks=%d events=%d tokens=%d",
        document.id,
        prepared.provider if prepared is not None else "checkpoint",
        prepared.cached if prepared is not None else True,
        chunks.chunk_count,
        events.event_count,
        token_usage,
    )
    if job_queue is not None:
        from sag_api.services.universe_service import schedule_universe_refresh

        await schedule_universe_refresh(
            session,
            job_queue,
            source_id=source.id,
            reason="document_processed",
        )


async def delete_document_task(
    session: AsyncSession, job: Job, *, engine_manager: EngineManager, job_queue=None
) -> None:
    if not job.source_id:
        raise NotFoundError("删除任务缺少信源")
    async with AsyncExitStack() as admission:
        try:
            await admission.enter_async_context(
                acquire_source_exclusive_lease(SessionLocal, job.source_id, f"document-delete:{job.id}")
            )
        except ConflictError as error:
            raise JobYielded(SOURCE_BUSY) from error
        await _delete_document_task_unlocked(session, job, engine_manager=engine_manager, job_queue=job_queue)


async def _delete_document_task_unlocked(
    session: AsyncSession, job: Job, *, engine_manager: EngineManager, job_queue=None
) -> None:
    """清理已取得信源维护窗口的文档派生数据、文件和记录。"""
    target_document_id = job.document_id or str((job.payload or {}).get("target_document_id") or "")
    if not target_document_id:
        raise NotFoundError("删除任务缺少文档")
    document = await session.get(Document, target_document_id)
    if document is None:
        return
    source = await session.get(Source, document.source_id)
    if source is None:
        raise NotFoundError("信源不存在")

    await session.refresh(document)
    from sag_api.parsing.service import remove_parsed_sidecars
    from sag_api.services.document_service import _document_cleanup_targets

    targets = await _document_cleanup_targets(session, document)
    bound = dict((job.payload or {}).get("delete_requests") or {})
    tokens: dict[str, int] = {}
    histories = (await session.scalars(select(Job).where(Job.document_id == document.id))).all()
    for historical in histories:
        payload = historical.payload or {}
        process = payload.get("process_request")
        if process:
            request = ProcessSourceRequest.model_validate(process)
            input_id = request.source.descriptor.source_id
            if request.source.descriptor.data_source_id == source.sag_source_config_id:
                tokens[input_id] = max(tokens.get(input_id, 0), request.context.fence_token)
        for serialized in (payload.get("delete_requests") or {}).values():
            request = DeleteSourceRequest.model_validate(serialized)
            if request.data_source_id == source.sag_source_config_id:
                tokens[request.source_id] = max(tokens.get(request.source_id, 0), request.context.fence_token)
    for input_id in targets["derived_source_ids"]:
        if input_id not in bound:
            operation_id = new_id()
            digest = sha256(
                json.dumps(
                    {
                        "operation_type": "delete_source",
                        "data_source_id": source.sag_source_config_id,
                        "source_id": input_id,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            bound[input_id] = DeleteSourceRequest(
                context=OperationContext(
                    operation_id=operation_id,
                    idempotency_key=operation_id,
                    request_digest=digest,
                    fence_scope=source.sag_source_config_id,
                    fence_token=tokens.get(input_id, 0) + 1,
                    owner_id=job.id,
                    isolation="input",
                ),
                data_source_id=source.sag_source_config_id,
                source_id=input_id,
            ).model_dump(mode="json")
        request = DeleteSourceRequest.model_validate(bound[input_id])
        if request.data_source_id != source.sag_source_config_id or request.source_id != input_id:
            raise ConflictError("文档删除请求与目标不匹配", layer=ErrorLayer.ENGINE, stage=ErrorStage.PERSIST)
    job.payload = {**(job.payload or {}), **targets, "target_document_id": document.id, "delete_requests": bound}
    await session.commit()
    for derived_source_id in targets["derived_source_ids"]:
        request = DeleteSourceRequest.model_validate(bound[derived_source_id])
        result = await engine_manager.delete_data(request, source=source)
        _require_delete_completion(request, result)
    for path in targets["artifact_paths"]:
        remove_parsed_sidecars(path)
    for directory in targets["candidate_directories"]:
        if Path(directory).exists():
            shutil.rmtree(directory)
    job.document_id = None
    await session.flush()
    await session.delete(document)
    await session.flush()
    from sag_api.services.document_service import _refresh_source_counts

    await _refresh_source_counts(session, source)
    await refresh_source_vector_identity(session, source)
    job.status = JobStatus.SUCCEEDED
    job.progress = 1.0
    job.finished_at = datetime.now(UTC)
    job.error = None
    await session.commit()
    if job_queue is not None:
        from sag_api.services.universe_service import schedule_universe_refresh

        try:
            await schedule_universe_refresh(
                session,
                job_queue,
                source_id=source.id,
                reason="document_deleted",
            )
        except Exception:  # noqa: BLE001 - 派生视图刷新不影响核心删除结果
            log.exception("文档已删除，但知识宇宙刷新调度失败 source=%s", source.id)
            # Core deletion was committed above. Roll back only the optional
            # refresh scheduling transaction so the worker can still persist
            # the delete Job's SUCCEEDED terminal state and release maintenance.
            await session.rollback()


def _require_delete_completion(
    request: DeleteSourceRequest | DeleteDataSourceRequest, result: OperationResult,
) -> None:
    """Only the bound public receipt may authorize application file cleanup."""
    input_delete = isinstance(request, DeleteSourceRequest)
    operation_type = "delete_source" if input_delete else "delete_data_source"
    input_id = request.source_id if input_delete else None
    if (
        not isinstance(result, OperationResult)
        or result.operation_id != request.context.operation_id
        or result.idempotency_key != request.context.idempotency_key
        or result.request_digest != request.context.request_digest
        or result.operation_type != operation_type
        or result.data_source_id != request.data_source_id
        or result.source_id != input_id
        or result.fence_token != request.context.fence_token
    ):
        raise ConflictError("删除回执与请求不匹配", layer=ErrorLayer.ENGINE, stage=ErrorStage.PERSIST)
    if result.status != "succeeded" or result.checkpoint != "ready":
        raise UpstreamError(
            result.failure_message or "持久删除尚未完成", layer=ErrorLayer.ENGINE, stage=ErrorStage.PERSIST,
            retryable=bool(result.retryable) if result.status == "failed" else True,
        )
    if (result.result or {}).get("target") != ("source" if input_delete else "data_source"):
        raise ConflictError("删除回执缺少完成目标", layer=ErrorLayer.ENGINE, stage=ErrorStage.PERSIST)


async def delete_source_task(session: AsyncSession, job: Job, *, engine_manager: EngineManager, job_queue=None) -> None:
    source_id = str((job.payload or {}).get("target_source_id") or "")
    if not source_id:
        raise NotFoundError("删除任务缺少信源")
    async with AsyncExitStack() as admission:
        try:
            await admission.enter_async_context(
                acquire_source_exclusive_lease(SessionLocal, source_id, f"source-delete:{job.id}")
            )
        except ConflictError as error:
            raise JobYielded(SOURCE_BUSY) from error
        await _delete_source_task_unlocked(session, job, engine_manager=engine_manager, job_queue=job_queue)


async def _delete_source_task_unlocked(
    session: AsyncSession, job: Job, *, engine_manager: EngineManager, job_queue=None,
) -> None:
    from sag_api.parsing.service import remove_parsed_sidecars
    from sag_api.services.document_service import _document_cleanup_targets
    from sag_api.services.source_service import _range_delete_request

    payload = dict(job.payload or {})
    source_id = str(payload.get("target_source_id") or "")
    bound = dict(payload.get("delete_requests") or {})
    if not source_id or not bound:
        raise ConflictError("信源删除缺少持久请求", layer=ErrorLayer.API, stage=ErrorStage.PERSIST)
    source = await session.get(Source, source_id, populate_existing=True)
    if source is not None and not source.is_deleting:
        raise ConflictError("信源删除状态已变化")
    # A drained parser may have recorded artifacts after the original delete
    # intent. Capture them before any engine or filesystem cleanup.
    paths = set(payload.get("artifact_paths") or [])
    directories = set(payload.get("candidate_directories") or [])
    documents = await session.scalars(select(Document).where(Document.source_id == source_id))
    for document in documents:
        closure = await _document_cleanup_targets(session, document)
        paths.update(closure["artifact_paths"])
        directories.update(closure["candidate_directories"])
    ranges = set(await session.scalars(select(OctxInstallation.sag_source_config_id).where(
        OctxInstallation.source_id == source_id,
    )))
    transfers = await session.scalars(select(OctxTransfer).where(OctxTransfer.target_source_id == source_id))
    for transfer in transfers:
        range_id = (transfer.checkpoint or {}).get("source_config_id")
        if isinstance(range_id, str) and range_id:
            ranges.add(range_id)
    for range_id in ranges:
        if range_id not in bound:
            bound[range_id] = _range_delete_request(range_id, job.id).model_dump(mode="json")
    job.payload = {
        **payload, "artifact_paths": sorted(paths), "candidate_directories": sorted(directories),
        "delete_requests": bound,
    }
    await session.commit()
    for range_id, serialized in bound.items():
        request = DeleteDataSourceRequest.model_validate(serialized)
        if request.data_source_id != range_id:
            raise ConflictError("信源删除请求与目标不匹配")
        runtime_source = source if source is not None and range_id == source.sag_source_config_id else None
        result = await engine_manager.delete_data(request, source=runtime_source)
        _require_delete_completion(request, result)
    for range_id in bound:
        await engine_manager.release(range_id)
    for path in paths:
        remove_parsed_sidecars(path)
    for directory in directories:
        if Path(directory).exists():
            shutil.rmtree(directory)
    # Keep the request and cleanup intent outside Source's CASCADE boundary.
    job.source_id = None
    await session.flush()
    await session.execute(AgentBinding.__table__.delete().where(
        AgentBinding.target_type == BindingTargetType.SOURCE, AgentBinding.target_id == source_id,
    ))
    if source is not None:
        await session.delete(source)
    job.status = JobStatus.SUCCEEDED
    job.progress = 1.0
    job.finished_at = datetime.now(UTC)
    job.error = None
    await session.commit()
    if job_queue is not None:
        from sag_api.services.universe_service import schedule_universe_refresh

        try:
            await schedule_universe_refresh(session, job_queue, source_id=None, reason="source_deleted")
        except Exception:  # noqa: BLE001 - cleanup was already committed
            log.exception("信源已删除，但知识宇宙刷新调度失败 source=%s", source_id)
            await session.rollback()


async def sync_source(session: AsyncSession, job: Job, *, engine_manager=None, job_queue=None) -> None:
    if not job.source_id:
        raise NotFoundError("信源不存在")
    async with acquire_operation_lease(
        SessionLocal,
        [f"source:{job.source_id}"],
        owner=f"source-sync:{job.id}",
    ):
        await _sync_source_unlocked(session, job, job_queue=job_queue)


async def _sync_source_unlocked(session: AsyncSession, job: Job, *, job_queue=None) -> None:
    """动态连接器同步：discover → fetch → 登记文档并入队处理（复用 ingest→extract 管线）。"""
    # 延迟导入避免与 jobs 包的循环依赖
    from sag_api.connectors import registry
    from sag_api.core.config import settings
    from sag_api.services.document_service import create_document_from_upload

    source = await session.get(Source, job.source_id) if job.source_id else None
    if source is None:
        raise NotFoundError("信源不存在")

    connector = registry.get(source.connector_kind)
    discovered = await connector.discover(source.config or {})
    fetched = 0
    for d in discovered:
        try:
            local = await connector.fetch(source.config or {}, d)
            with open(local.path, "rb") as f:
                data = f.read()
        except Exception as e:  # noqa: BLE001 - 单篇失败不影响整体同步
            log.warning("同步抓取失败 %s：%s", d.external_id, getattr(e, "message", None) or e)
            continue
        await create_document_from_upload(
            session,
            source,
            filename=local.filename,
            content_type=local.content_type,
            data=data,
            upload_dir=settings.upload_dir,
            job_queue=job_queue,
        )
        try:
            os.remove(local.path)
        except OSError:
            pass
        fetched += 1

    job.progress = 1.0
    job.payload = {**(job.payload or {}), "discovered": len(discovered), "fetched": fetched}
    await session.commit()
    log.info("同步完成 source=%s 发现=%d 抓取=%d", source.id, len(discovered), fetched)


async def index_universe(session: AsyncSession, job: Job, *, engine_manager: EngineManager, job_queue=None) -> None:
    """Rebuild one user's aggregate universe overview from authoritative graph data."""
    from sag_api.db.models import User
    from sag_api.services.universe_service import rebuild_universe_overview

    user_id = str((job.payload or {}).get("user_id") or "")
    if not user_id or await session.get(User, user_id) is None:
        raise NotFoundError("知识宇宙所属用户不存在")
    job.progress = 0.1
    await session.commit()
    overview = await rebuild_universe_overview(session, engine_manager, user_id)
    job.progress = 1.0
    job.payload = {**(job.payload or {}), "overview_id": overview.id}
    await session.commit()


TASK_HANDLERS: dict[JobType, TaskHandler] = {
    JobType.PROCESS_DOCUMENT: process_document,
    JobType.DELETE_DOCUMENT: delete_document_task,
    JobType.DELETE_SOURCE: delete_source_task,
    JobType.SYNC_SOURCE: sync_source,
    JobType.INDEX_UNIVERSE: index_universe,
    JobType.OCTX_PREFLIGHT: preflight_octx,
    JobType.OCTX_IMPORT: import_octx,
    JobType.OCTX_EXPORT: export_octx,
    JobType.OCTX_GC_INSTALLATION: gc_octx_installation,
    JobType.OCTX_GC_TRANSFER: gc_octx_transfers,
}
