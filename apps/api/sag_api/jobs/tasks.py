"""任务处理器 —— 按 JobType 分发。

处理器只关心「做什么」；状态机（queued/running/succeeded/failed）由队列 worker 统一维护。
处理器内部负责领域对象（Document/Source）的阶段状态与计数更新。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal
from sag_api.core.error_taxonomy import ErrorLayer, ErrorStage
from sag_api.core.errors import ApiError, NotFoundError
from sag_api.core.logging import get_logger
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs.control import JobPaused, JobYielded
from sag_api.jobs.octx_tasks import (
    export_octx,
    gc_octx_installation,
    gc_octx_transfers,
    import_octx,
    preflight_octx,
)
from sag_api.jobs.scheduling import SOURCE_MAINTENANCE
from sag_api.parsing import prepare_document
from sag_api.sag import EngineManager
from sag_api.sag.dto import ProcessCheckpoint
from sag_api.services.source_operation_service import (
    acquire_operation_lease,
    acquire_source_exclusive_lease,
    acquire_source_processing_lease,
    touch_source_revision,
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


async def _yield_after_document_transition_lost(
    session: AsyncSession,
    document: Document,
) -> None:
    """Converge a winning pause/delete intent without overwriting it."""
    document_id = document.id
    await session.rollback()
    current = await session.get(Document, document_id, populate_existing=True)
    if current is not None and current.status == DocumentStatus.PAUSING:
        paused = await session.execute(
            update(Document)
            .where(
                Document.id == document_id,
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
    async with acquire_source_processing_lease(SessionLocal, document.source_id, job.id):
        await _process_document_unlocked(
            session,
            job,
            engine_manager=engine_manager,
            job_queue=job_queue,
        )


async def _process_document_unlocked(
    session: AsyncSession, job: Job, *, engine_manager: EngineManager, job_queue=None
) -> None:
    """解析、入库并按 chunk 并发抽取；每个 chunk 完成即保存断点。"""
    document = await session.get(Document, job.document_id) if job.document_id else None
    if document is None:
        raise NotFoundError("文档不存在")
    source = await session.get(Source, document.source_id)
    if source is None:
        raise NotFoundError("信源不存在")
    checkpoint = ProcessCheckpoint.from_payload(job.payload)
    scheduler_yield_reason: str | None = None

    # A worker retry reuses the document row. Clear the previous attempt's
    # failure before parsing can block for a long time, so active processing
    # never carries a stale terminal error.
    if document.error is not None and document.status not in _CONTROL_TRANSITION_STATES:
        document.error = None
        await session.commit()

    async def refresh_payload() -> dict:
        await session.refresh(job, attribute_names=["payload"])
        return dict(job.payload or {})

    async def on_stage(stage: str) -> None:
        await session.refresh(document)
        if document.status in _CONTROL_TRANSITION_STATES:
            return
        if stage == "loading":
            document.status = DocumentStatus.LOADING
            document.progress = max(document.progress, 5)
            job.progress = document.progress / 100
        elif stage == "extracting":
            document.status = DocumentStatus.EXTRACTING
            completed = len(checkpoint.processed_chunk_ids)
            total = len(checkpoint.chunk_ids)
            document.progress = 20 + round(80 * completed / total) if total else 20
            job.progress = document.progress / 100
        await session.commit()

    async def on_parser_state(state: dict) -> None:
        await session.refresh(document)
        if document.status in _CONTROL_TRANSITION_STATES:
            return
        document.status = DocumentStatus.LOADING
        document.progress = max(document.progress, 10)
        job.progress = document.progress / 100
        job.payload = {**(await refresh_payload()), "document_parser": state}
        await session.commit()

    async def on_checkpoint(value: ProcessCheckpoint) -> None:
        nonlocal checkpoint
        checkpoint = value
        await session.refresh(document)
        job.payload = value.merge_payload(await refresh_payload())
        document.chunk_count = len(value.chunk_ids)
        document.event_count = value.event_count
        document.sag_source_id = value.source_id
        document.token_usage = value.token_usage
        if document.status not in _CONTROL_TRANSITION_STATES:
            total = len(value.chunk_ids)
            completed = len(value.processed_chunk_ids)
            document.progress = 20 + round(80 * completed / total) if total else 20
            job.progress = document.progress / 100
        await session.commit()

    async def should_pause() -> bool:
        nonlocal scheduler_yield_reason
        async with SessionLocal() as control_session:
            current_job = await control_session.get(Job, job.id)
            if current_job is None:
                return True
            if current_job.status == JobStatus.PAUSED or (current_job.payload or {}).get("pause_requested"):
                scheduler_yield_reason = None
                return True
        if job_queue is not None and job_queue.source_maintenance_requested(source.id):
            scheduler_yield_reason = SOURCE_MAINTENANCE
            return True
        return False

    try:
        prepared = None
        if not checkpoint.chunk_ids:
            prepared = await prepare_document(
                document.storage_path,
                settings,
                state=(job.payload or {}).get("document_parser"),
                on_state=on_parser_state,
            )
            if prepared.fallback_from:
                log.warning(
                    "文档解析已降级 doc=%s job=%s from=%s to=%s cached=%s error=%s",
                    document.id,
                    getattr(job, "id", None),
                    prepared.fallback_from,
                    prepared.provider,
                    prepared.cached,
                    prepared.fallback_error,
                )
        outcome = await engine_manager.process_document(
            source.sag_source_config_id,
            str(prepared.path) if prepared is not None else None,
            source=source,
            on_stage=on_stage,
            checkpoint=checkpoint,
            on_checkpoint=on_checkpoint,
            should_pause=should_pause,
            max_concurrency=settings.document_extract_concurrency,
            document_title=Path(document.filename).stem.strip(),
        )
        if outcome.paused:
            await session.refresh(document)
            if scheduler_yield_reason == SOURCE_MAINTENANCE and document.status not in _CONTROL_TRANSITION_STATES:
                raise JobYielded(SOURCE_MAINTENANCE)
            if document.status in _DELETE_CONTROL_STATES:
                raise JobPaused()
            expected_status = document.status
            paused = await session.execute(
                update(Document)
                .where(
                    Document.id == document.id,
                    Document.status == expected_status,
                )
                .values(status=DocumentStatus.PAUSED, error=None)
                .execution_options(synchronize_session=False)
            )
            if paused.rowcount != 1:
                await _yield_after_document_transition_lost(session, document)
            await session.commit()
            raise JobPaused()
    except (JobPaused, JobYielded):
        raise
    except Exception as e:  # noqa: BLE001 - 记录到文档后再上抛给 worker
        await session.refresh(document)
        if document.status in _CONTROL_TRANSITION_STATES or document.status == DocumentStatus.PAUSED:
            await _yield_after_document_transition_lost(session, document)
        layer, stage = _classify_document_failure(e, document.status)
        expected_status = document.status
        message = getattr(e, "message", None) or str(e)
        failed = await session.execute(
            update(Document)
            .where(
                Document.id == document.id,
                Document.status == expected_status,
            )
            .values(
                status=DocumentStatus.FAILED,
                error=message,
                error_layer=layer.value,
                error_stage=stage.value,
            )
            .execution_options(synchronize_session=False)
        )
        if failed.rowcount != 1:
            await _yield_after_document_transition_lost(session, document)
        log.warning(
            "文档处理失败 doc=%s layer=%s stage=%s error=%s",
            document.id,
            layer.value,
            stage.value,
            message,
        )
        await session.commit()
        raise

    await session.refresh(document)
    if document.status in _DELETE_CONTROL_STATES:
        raise JobPaused()
    if document.status == DocumentStatus.PAUSING:
        document.status = DocumentStatus.PAUSED
        await session.commit()
        raise JobPaused()
    completed = await session.execute(
        update(Document)
        .where(
            Document.id == document.id,
            Document.status.in_(
                [
                    DocumentStatus.PENDING,
                    DocumentStatus.LOADING,
                    DocumentStatus.EXTRACTING,
                ]
            ),
        )
        .values(
            status=DocumentStatus.READY,
            chunk_count=outcome.chunk_count,
            event_count=outcome.event_count,
            sag_source_id=outcome.source_id,
            progress=100,
            token_usage=outcome.token_usage,
            error=None,
        )
        .execution_options(synchronize_session=False)
    )
    if completed.rowcount != 1:
        await _yield_after_document_transition_lost(session, document)
    # 信源聚合计数用原子 SQL 更新，避免并发读改写丢失
    await session.execute(
        update(Source)
        .where(Source.id == source.id)
        .values(
            chunk_count=Source.chunk_count + outcome.chunk_count,
            event_count=Source.event_count + outcome.event_count,
        )
    )
    await touch_source_revision(session, source.id)
    await session.commit()
    log.info(
        "文档处理完成 doc=%s parser=%s cached=%s chunks=%d events=%d tokens=%d",
        document.id,
        prepared.provider if prepared is not None else "checkpoint",
        prepared.cached if prepared is not None else True,
        outcome.chunk_count,
        outcome.event_count,
        outcome.token_usage,
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
    async with acquire_source_exclusive_lease(SessionLocal, job.source_id, f"document-delete:{job.id}"):
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
    derived_source_ids = {
        value.strip()
        for value in (job.payload or {}).get("derived_source_ids", [])
        if isinstance(value, str) and value.strip()
    }
    if document.sag_source_id:
        derived_source_ids.add(document.sag_source_id)
    for derived_source_id in sorted(derived_source_ids):
        await engine_manager.delete_document_data(
            source.sag_source_config_id,
            derived_source_id,
            source=source,
        )
    path = document.storage_path
    job.payload = {**(job.payload or {}), "target_document_id": document.id}
    job.document_id = None
    await session.flush()
    await session.delete(document)
    await session.flush()
    from sag_api.services.document_service import _refresh_source_counts

    await _refresh_source_counts(session, source)
    await session.commit()
    if path:
        from sag_api.parsing.service import parsed_sidecar_paths

        for candidate in [path, *parsed_sidecar_paths(path)]:
            try:
                if os.path.exists(candidate):
                    os.remove(candidate)
            except OSError:
                pass
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


async def reprocess_document_task(
    session: AsyncSession, job: Job, *, engine_manager: EngineManager, job_queue=None
) -> None:
    if not job.source_id:
        raise NotFoundError("重新处理任务缺少信源")
    async with acquire_source_exclusive_lease(SessionLocal, job.source_id, f"document-reprocess:{job.id}"):
        await _reprocess_document_task_unlocked(session, job, engine_manager=engine_manager, job_queue=job_queue)


async def _reprocess_document_task_unlocked(
    session: AsyncSession, job: Job, *, engine_manager: EngineManager, job_queue=None
) -> None:
    """在信源维护窗口内清理旧派生数据，再排入普通文档处理任务。"""
    target_document_id = job.document_id or str((job.payload or {}).get("target_document_id") or "")
    if not target_document_id:
        raise NotFoundError("重新处理任务缺少文档")
    document = await session.get(Document, target_document_id)
    if document is None:
        raise NotFoundError("文档不存在")
    source = await session.get(Source, document.source_id)
    if source is None:
        raise NotFoundError("信源不存在")

    payload = dict(job.payload or {})
    derived_source_ids = sorted(
        {value.strip() for value in payload.get("derived_source_ids", []) if isinstance(value, str) and value.strip()}
    )
    if not payload.get("cleanup_completed"):
        for derived_source_id in derived_source_ids:
            await engine_manager.delete_document_data(
                source.sag_source_config_id,
                derived_source_id,
                source=source,
            )

    await session.refresh(document)
    if document.status in _DELETE_CONTROL_STATES:
        job.payload = {
            **payload,
            "cleanup_completed": True,
        }
        await session.commit()
        return

    process_job_id = payload.get("process_job_id")
    process_job = await session.get(Job, process_job_id) if isinstance(process_job_id, str) and process_job_id else None
    if process_job is None:
        process_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.QUEUED,
        )
        session.add(process_job)
        await session.flush()
    job.payload = {
        **payload,
        "cleanup_completed": True,
        "process_job_id": process_job.id,
    }
    await session.commit()
    await session.refresh(process_job)
    if job_queue is not None:
        from sag_api.services.document_service import _enqueue_persisted_job

        await _enqueue_persisted_job(job_queue, process_job.id)


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
    JobType.REPROCESS_DOCUMENT: reprocess_document_task,
    JobType.DELETE_DOCUMENT: delete_document_task,
    JobType.SYNC_SOURCE: sync_source,
    JobType.INDEX_UNIVERSE: index_universe,
    JobType.OCTX_PREFLIGHT: preflight_octx,
    JobType.OCTX_IMPORT: import_octx,
    JobType.OCTX_EXPORT: export_octx,
    JobType.OCTX_GC_INSTALLATION: gc_octx_installation,
    JobType.OCTX_GC_TRANSFER: gc_octx_transfers,
}
