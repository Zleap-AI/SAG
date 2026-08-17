"""任务处理器 —— 按 JobType 分发。

处理器只关心「做什么」；状态机（queued/running/succeeded/failed）由队列 worker 统一维护。
处理器内部负责领域对象（Document/Source）的阶段状态与计数更新。
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal
from sag_api.core.error_taxonomy import ErrorLayer, ErrorStage
from sag_api.core.errors import ApiError, NotFoundError, ValidationError
from sag_api.core.logging import get_logger
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs.control import JobPaused, JobYielded
from sag_api.jobs.scheduling import SOURCE_MAINTENANCE
from sag_api.parsing import ParsePaused, prepare_document
from sag_api.sag import EngineManager
from sag_api.sag.dto import ProcessCheckpoint

log = get_logger("jobs")

TaskHandler = Callable[[AsyncSession, Job], Awaitable[None]]

_NAS_TERMINAL_STATES = {"created", "updated", "skipped", "failed"}
_NAS_FAILURE_REASONS = {
    "nas_folder_revoked": "authorization_revoked",
    "nas_file_unreadable": "file_unreadable",
    "nas_file_changed": "file_changed",
    "document_busy": "document_busy",
    "validation_error": "unsafe_or_unsupported",
}

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


def _fallback_reason(state: dict[str, Any]) -> str | None:
    fallback = state.get("fallback")
    if isinstance(fallback, dict):
        return _redact_parser_reason(fallback.get("mineru_error") or fallback.get("markitdown_error"))
    return _redact_parser_reason(state.get("error") or state.get("message"))


def _parser_state_values(state: dict[str, Any]) -> dict[str, str | None]:
    raw_provider = state.get("provider")
    provider = raw_provider if raw_provider in {"mineru", "markitdown", "original"} else None
    raw_status = str(state.get("status") or "").lower()
    fallback = isinstance(state.get("fallback"), dict)
    if raw_status.startswith("fallback_") or fallback:
        status = "failed" if raw_status == "fallback_failed" else "fallback"
        parser_provider = "markitdown" if raw_status != "fallback_failed" else provider
        fallback_from = "mineru"
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
    if provider == "mineru" or fallback_from == "mineru":
        mineru_provider, mineru_model = _mineru_details(state)
    return {
        "parser_provider": parser_provider,
        "mineru_provider": mineru_provider,
        "mineru_model": mineru_model,
        "parser_status": status,
        "fallback_from": fallback_from,
        "fallback_reason": _fallback_reason(state) if fallback_from else None,
    }


def _prepared_parser_values(prepared, state: dict[str, Any] | None) -> dict[str, str | None]:  # noqa: ANN001
    mineru_provider = mineru_model = None
    if prepared.provider == "mineru" or prepared.fallback_from == "mineru":
        mineru_provider, mineru_model = _mineru_details(state)
    return {
        "parser_provider": prepared.provider,
        "mineru_provider": mineru_provider,
        "mineru_model": mineru_model,
        "parser_status": "fallback" if prepared.fallback_from else "done",
        "fallback_from": prepared.fallback_from,
        "fallback_reason": _redact_parser_reason(prepared.fallback_error),
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


def _classify_document_failure(
    e: Exception, current_status: DocumentStatus
) -> tuple[ErrorLayer, ErrorStage]:
    """推断失败的责任层与链路环节。

    优先信任领域异常自带的 layer/stage（LLM 分类、引擎翻译层都会填）；
    否则退化为「按文档当前状态猜环节」，责任层归 engine（zleap-sag 抽取/入库
    过程中逃逸的裸异常，如 jsonschema.ValidationError，几乎都发生在引擎侧）。
    """
    if isinstance(e, ApiError) and e.layer is not None and e.stage is not None:
        return e.layer, e.stage
    stage = _STATUS_TO_STAGE.get(current_status, ErrorStage.EXTRACT)
    return ErrorLayer.ENGINE, stage


async def process_document(
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
        for field, value in _parser_state_values(state).items():
            setattr(document, field, value)
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
            if current_job.status == JobStatus.PAUSED or (
                current_job.payload or {}
            ).get("pause_requested"):
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

    try:
        prepared = None
        parser_stage = False
        if not checkpoint.chunk_ids:
            parser_stage = True
            try:
                prepared = await prepare_document(
                    document.storage_path,
                    settings,
                    state=(job.payload or {}).get("document_parser"),
                    on_state=on_parser_state,
                    should_pause=should_pause,
                )
            except ParsePaused:
                await _pause_or_yield()
            parser_stage = False
            parser_state = (job.payload or {}).get("document_parser")
            for field, value in _prepared_parser_values(prepared, parser_state).items():
                setattr(document, field, value)
            await session.commit()
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
            await _pause_or_yield()
    except (JobPaused, JobYielded):
        raise
    except Exception as e:  # noqa: BLE001 - 记录到文档后再上抛给 worker
        await session.refresh(document)
        if (
            document.status in _CONTROL_TRANSITION_STATES
            or document.status == DocumentStatus.PAUSED
        ):
            await _yield_after_document_transition_lost(session, document)
        layer, stage = _classify_document_failure(e, document.status)
        expected_status = document.status
        message = getattr(e, "message", None) or str(e)
        public_message = message
        parser_failure_values: dict[str, str | None] = {}
        parser_state = (job.payload or {}).get("document_parser")
        parser_failed = parser_stage
        if parser_failed:
            public_message = _redact_parser_reason(message) or "文档解析失败"
        if (
            parser_failed
            and isinstance(parser_state, dict)
            and str(parser_state.get("status") or "").lower() == "fallback_failed"
        ):
            parser_failure_values = _parser_state_values(parser_state)
            parser_failure_values["parser_status"] = "failed"
            parser_failure_values["fallback_reason"] = _redact_parser_reason(message)
        failed = await session.execute(
            update(Document)
            .where(
                Document.id == document.id,
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
            await _yield_after_document_transition_lost(session, document)
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
    """在信源维护窗口内清理旧派生数据，再排入普通文档处理任务。"""
    target_document_id = job.document_id or str(
        (job.payload or {}).get("target_document_id") or ""
    )
    if not target_document_id:
        raise NotFoundError("重新处理任务缺少文档")
    document = await session.get(Document, target_document_id)
    if document is None:
        raise NotFoundError("文档不存在")
    source = await session.get(Source, document.source_id)
    if source is None:
        raise NotFoundError("信源不存在")

    payload = dict(job.payload or {})
    replacement = payload.get("replacement")
    if isinstance(replacement, dict):
        final_path = Path(str(replacement.get("final_path") or ""))
        staged_path = Path(str(replacement.get("staged_path") or ""))
        expected_sha256 = str(replacement.get("sha256") or "")
        expected_size = replacement.get("size_bytes")
        if final_path != Path(document.storage_path) or not expected_sha256 or type(expected_size) is not int:
            raise NotFoundError("替换文件检查点无效")
        if replacement.get("state") == "staged":
            backup_path = Path(
                str(replacement.get("backup_path") or f"{final_path}.nas-backup-{job.id}")
            )
            if staged_path.exists():
                if _file_digest(staged_path) != (expected_sha256, expected_size):
                    raise NotFoundError("替换文件检查点已损坏")
                if final_path.exists() and not backup_path.exists():
                    os.link(final_path, backup_path)
                os.replace(staged_path, final_path)
            if not final_path.exists() or _file_digest(final_path) != (expected_sha256, expected_size):
                raise NotFoundError("替换文件检查点已丢失")
            document.size_bytes = expected_size
            document.origin_size_bytes = expected_size
            document.origin_mtime_ns = replacement.get("mtime_ns")
            document.origin_sha256 = expected_sha256
            document.origin_path = replacement.get("origin_path")
            document.origin_display_path = replacement.get("origin_display_path")
            replacement = {
                **replacement,
                "state": "installed",
                "backup_path": str(backup_path),
            }
            payload = {**payload, "replacement": replacement}
            job.payload = payload
            await session.commit()
    derived_source_ids = sorted(
        {
            value.strip()
            for value in payload.get("derived_source_ids", [])
            if isinstance(value, str) and value.strip()
        }
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
    process_job = (
        await session.get(Job, process_job_id)
        if isinstance(process_job_id, str) and process_job_id
        else None
    )
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
    if isinstance(replacement, dict):
        from sag_api.parsing.service import parsed_sidecar_paths

        final_path = str(replacement.get("final_path") or "")
        backup_path = str(replacement.get("backup_path") or "")
        for candidate in [*parsed_sidecar_paths(final_path), backup_path]:
            if not candidate:
                continue
            try:
                os.remove(candidate)
            except FileNotFoundError:
                pass
            except OSError:
                log.warning("替换文档旧文件清理失败 job=%s", job.id)
    if job_queue is not None:
        from sag_api.services.document_service import _enqueue_persisted_job

        await _enqueue_persisted_job(job_queue, process_job.id)


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


async def sync_source(session: AsyncSession, job: Job, *, engine_manager=None, job_queue=None) -> None:
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


async def index_universe(
    session: AsyncSession, job: Job, *, engine_manager: EngineManager, job_queue=None
) -> None:
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


def _build_fnos_nas_importer(job_queue):
    """Build the fnOS importer lazily so non-fnOS deployments stay decoupled."""
    from sag_api.fnos.open_api import FnOSOpenAPIClient
    from sag_api.services.fnos_nas_access import FnOSNasAccessService
    from sag_api.services.fnos_nas_import import FnOSNasImporter

    open_api = FnOSOpenAPIClient()
    access = FnOSNasAccessService(
        open_api,
        secret_file=Path(settings.fnos_internal_secret_file),
    )
    return FnOSNasImporter(
        access,
        open_api,
        upload_dir=settings.upload_dir,
        job_queue=job_queue,
    )


def _persisted_nas_entry(value: object):
    from sag_api.fnos.nas_registry import NasScanEntry

    if not isinstance(value, dict):
        raise ValueError("invalid NAS import entry")
    strings = {
        key: value.get(key)
        for key in (
            "canonical_root",
            "canonical_path",
            "display_path",
            "folder_source",
        )
    }
    if (
        any(not isinstance(item, str) or not item for item in strings.values())
        or strings["folder_source"] not in {"host_api", "legacy_manual"}
        or type(value.get("size_bytes")) is not int
        or value["size_bytes"] < 0
        or type(value.get("mtime_ns")) is not int
        or value["mtime_ns"] < 0
    ):
        raise ValueError("invalid NAS import entry")
    return NasScanEntry(
        canonical_root=strings["canonical_root"],
        canonical_path=strings["canonical_path"],
        display_path=strings["display_path"],
        size_bytes=value["size_bytes"],
        mtime_ns=value["mtime_ns"],
        folder_source=strings["folder_source"],
    )


def _nas_failure_reason(error: Exception) -> str:
    if isinstance(error, ApiError):
        return _NAS_FAILURE_REASONS.get(str(error.code), "import_failed")
    return "copy_failed"


def _nas_result(entry: dict) -> dict[str, object | None]:
    display_path = entry.get("display_path")
    return {
        "display_path": display_path if isinstance(display_path, str) else "",
        "outcome": entry.get("state")
        if entry.get("state") in _NAS_TERMINAL_STATES
        else "failed",
        "document_id": entry.get("document_id")
        if isinstance(entry.get("document_id"), str)
        else None,
        "reason": entry.get("reason") if isinstance(entry.get("reason"), str) else None,
    }


async def _checkpoint_nas_import(session: AsyncSession, job: Job, entries: list[dict]) -> None:
    counts = {state: 0 for state in _NAS_TERMINAL_STATES}
    for entry in entries:
        state = entry.get("state")
        if state in counts:
            counts[state] += 1
    completed = sum(counts.values())
    total = len(entries)
    results = [_nas_result(entry) for entry in entries if entry.get("state") in counts]
    job.payload = {
        **(job.payload or {}),
        "entries": entries,
        "summary": {
            "total": total,
            "completed": completed,
            "created": counts["created"],
            "updated": counts["updated"],
            "skipped": counts["skipped"],
            "failed": counts["failed"],
        },
        "results": results,
    }
    job.progress = max(job.progress, completed / total if total else 1.0)
    await session.commit()


async def import_nas_documents(
    session: AsyncSession,
    job: Job,
    *,
    engine_manager=None,
    job_queue=None,
) -> None:
    """Import one persisted NAS selection at a time with durable item checkpoints."""
    del engine_manager
    payload = job.payload or {}
    owner_uid = payload.get("owner_uid")
    raw_entries = payload.get("entries")
    if type(owner_uid) is not int or owner_uid < 1 or not isinstance(raw_entries, list):
        raise ValidationError("NAS 导入任务数据无效")
    if job_queue is None:
        raise RuntimeError("NAS import job requires a queue")

    from sag_api.fnos.identity import GatewayIdentity

    identity = GatewayIdentity(owner_uid, "", True)
    importer = _build_fnos_nas_importer(job_queue)
    entries = [dict(value) if isinstance(value, dict) else {} for value in raw_entries]
    for index, stored in enumerate(entries):
        if stored.get("state") in _NAS_TERMINAL_STATES:
            continue
        stored["state"] = "copying"
        stored.pop("document_id", None)
        stored.pop("reason", None)
        await _checkpoint_nas_import(session, job, entries)
        try:
            entry = _persisted_nas_entry(stored)
        except (ValueError, TypeError, KeyError):
            stored["state"] = "failed"
            stored["document_id"] = None
            stored["reason"] = "unsafe_or_unsupported"
            log.warning(
                "NAS 导入条目失败 job=%s index=%d reason=%s",
                job.id,
                index,
                stored["reason"],
            )
            await _checkpoint_nas_import(session, job, entries)
            continue
        try:
            outcome = await importer.import_one(
                session,
                job,
                entry,
                identity=identity,
            )
        except (ApiError, OSError) as error:
            stored["state"] = "failed"
            stored["document_id"] = None
            stored["reason"] = _nas_failure_reason(error)
            log.warning(
                "NAS 导入条目失败 job=%s index=%d reason=%s",
                job.id,
                index,
                stored["reason"],
            )
        else:
            if outcome.outcome not in _NAS_TERMINAL_STATES - {"failed"}:
                raise RuntimeError("NAS importer returned an invalid outcome")
            stored["state"] = outcome.outcome
            stored["document_id"] = outcome.document_id
            stored["reason"] = outcome.reason
        await _checkpoint_nas_import(session, job, entries)

    await _checkpoint_nas_import(session, job, entries)
    log.info(
        "NAS 导入批次完成 job=%s total=%d failed=%d",
        job.id,
        len(entries),
        sum(entry.get("state") == "failed" for entry in entries),
    )


TASK_HANDLERS: dict[JobType, TaskHandler] = {
    JobType.PROCESS_DOCUMENT: process_document,
    JobType.REPROCESS_DOCUMENT: reprocess_document_task,
    JobType.DELETE_DOCUMENT: delete_document_task,
    JobType.SYNC_SOURCE: sync_source,
    JobType.INDEX_UNIVERSE: index_universe,
    JobType.IMPORT_NAS_DOCUMENTS: import_nas_documents,
}
