"""文档领域逻辑：上传落盘 → 登记 → 入队处理。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from zleap.sag.operations import OperationResult, ProcessSourceRequest
from zleap.sag.pipeline import ChunkSetRef, EventSetRef

from sag_api.core.errors import ConflictError, DocumentRestartRequiredError, NotFoundError
from sag_api.db.base import new_id
from sag_api.db.models import Document, Job, Source
from sag_api.db.models.job import job_payload_matches
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs import JobQueue
from sag_api.jobs.scheduling import DELETE_PRIORITY, MANUAL_RECOVERY, RESUME_PRIORITY, set_scheduler
from sag_api.sag.document_identity import processing_identity_restart_reason
from sag_api.sag.document_vector_identity import record_document_vector_identity
from sag_api.services.source_operation_service import touch_source_revision

if TYPE_CHECKING:
    from sag_api.sag import EngineManager

async def _enqueue_persisted_job(job_queue: JobQueue, job_id: str) -> None:
    """Dispatch a committed job with queue-level retry supervision when available."""
    durable = getattr(job_queue, "enqueue_durably", None)
    if callable(durable):
        await durable(job_id)
        return
    await job_queue.enqueue(job_id)


async def list_documents(session: AsyncSession, source_id: str) -> list[Document]:
    rows = await session.execute(
        select(Document)
        .where(
            Document.source_id == source_id,
            Document.is_active.is_(True),
        )
        .order_by(Document.created_at.desc())
    )
    return list(rows.scalars().all())


async def get_document(session: AsyncSession, source: Source, document_id: str) -> Document:
    doc = await session.get(Document, document_id)
    if doc is None or doc.source_id != source.id or not doc.is_active:
        raise NotFoundError("文档不存在")
    return doc


async def get_public_document(
    session: AsyncSession,
    source: Source,
    document_id: str,
) -> Document:
    """Resolve a document for public reads after the logical-delete barrier."""
    deleting = await session.scalar(select(Source.is_deleting).where(Source.id == source.id))
    if deleting is not False:
        raise NotFoundError("文档不存在")
    document = await get_document(session, source, document_id)
    if document.status in {
        DocumentStatus.DELETING,
        DocumentStatus.DELETE_FAILED,
    }:
        raise NotFoundError("文档不存在")
    return document


async def read_document_lines(
    document: Document,
    source: Source,
    engine_manager: EngineManager,
) -> list[str] | None:
    """Read the committed body; uploads and candidates have separate preview paths."""
    if not document.has_publication:
        return None
    # Active OCTX installations own immutable published Markdown artifacts.
    path = document.storage_path
    if document.octx_installation_id and path and os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as file:
            return file.readlines()
    markdown = await engine_manager.get_document_markdown(
        source.sag_source_config_id,
        document.sag_source_id,
        source=source,
    )
    return markdown.splitlines(keepends=True) if markdown else None


async def create_document_from_upload(
    session: AsyncSession,
    source: Source,
    *,
    filename: str,
    content_type: str,
    data: bytes,
    upload_dir: str,
    job_queue: JobQueue,
) -> tuple[Document, Job]:
    claimed = await session.execute(
        update(Source).where(Source.id == source.id, Source.is_deleting.is_(False))
        .values(document_count=Source.document_count + 1)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        raise ConflictError("信源正在删除或已不存在，无法上传")
    doc_id = new_id()
    safe_name = os.path.basename(filename) or "upload"
    dest_dir = os.path.join(upload_dir, source.id)
    os.makedirs(dest_dir, exist_ok=True)
    storage_path = os.path.join(dest_dir, f"{doc_id}_{safe_name}")
    with open(storage_path, "wb") as f:
        f.write(data)

    document = Document(
        id=doc_id,
        source_id=source.id,
        filename=safe_name,
        content_type=content_type or "application/octet-stream",
        size_bytes=len(data),
        storage_path=storage_path,
        status=DocumentStatus.PENDING,
        processing_stage="queued",
    )
    session.add(document)
    await session.flush()
    job = Job(
        type=JobType.PROCESS_DOCUMENT,
        source_id=source.id,
        document_id=doc_id,
        status=JobStatus.QUEUED,
        payload={"control_revision": document.control_revision},
    )
    session.add(job)
    await touch_source_revision(session, source.id)
    await session.commit()
    await session.refresh(document)
    await session.refresh(job)

    await _enqueue_persisted_job(job_queue, job.id)
    return document, job


def _format_messages(messages: list[dict]) -> str:
    lines = ["# 消息", ""]
    for m in messages:
        who = m.get("author") or m.get("role") or "消息"
        ts = f"（{m['ts']}）" if m.get("ts") else ""
        lines.append(f"**{who}**{ts}：{m.get('text') or ''}")
    return "\n\n".join(lines)


async def ingest_content(
    session: AsyncSession,
    source: Source,
    *,
    text: str | None = None,
    title: str | None = None,
    messages: list[dict] | None = None,
    upload_dir: str,
    job_queue: JobQueue,
) -> Document:
    """统一写入：把文本 / 一批消息归一为文档 → 复用 ingest/extract 管线（持续写入）。"""
    from sag_api.core.errors import ValidationError

    if messages:
        content = _format_messages(messages)
        filename = f"{title or f'消息-{len(messages)}条'}.md"
    elif text:
        content = (f"# {title}\n\n" if title else "") + text
        filename = f"{title or '文本'}.md"
    else:
        raise ValidationError("请提供 text 或 messages")

    document, _job = await create_document_from_upload(
        session,
        source,
        filename=filename,
        content_type="text/markdown",
        data=content.encode("utf-8"),
        upload_dir=upload_dir,
        job_queue=job_queue,
    )
    return document


def _document_candidate_root(document: Document) -> Path:
    from sag_api.core.config import settings

    return Path(settings.upload_dir) / document.source_id / ".document-candidates" / document.id


def _snapshot_document_input(document: Document, job_id: str, *, input_path: str | None = None) -> dict:
    """Keep a new attempt's raw bytes outside every prior parser cache."""
    directory = _document_candidate_root(document) / job_id
    target = directory / (Path(document.filename).name or "upload")
    directory.mkdir(parents=True)
    digest = hashlib.sha256()
    size = 0
    original_path = input_path or document.storage_path
    try:
        with open(original_path, "rb") as source, target.open("xb") as candidate:
            while block := source.read(64 * 1024):
                candidate.write(block)
                digest.update(block)
                size += len(block)
            candidate.flush()
            os.fsync(candidate.fileno())
        if target.suffix.lower() in {".md", ".markdown"}:
            from zleap.sag.modules.load.chunking import BlockType, MarkdownBlockParser, MarkdownInputNormalizer

            raw_root = Path(original_path).resolve().parent
            candidate_root = directory.resolve()
            markdown = MarkdownInputNormalizer().normalize(target.read_text(encoding="utf-8-sig"))
            for block in MarkdownBlockParser(parse_images=True).parse_blocks(markdown):
                if block.block_type != BlockType.IMAGE:
                    continue
                reference = Path(block.metadata["image_src"])
                if reference.is_absolute() or urlsplit(str(reference)).scheme:
                    continue
                original_image = (raw_root / reference).resolve()
                candidate_image = (candidate_root / reference).resolve()
                if not original_image.is_relative_to(raw_root) or not candidate_image.is_relative_to(candidate_root):
                    continue
                if original_image.is_file() and candidate_image != target.resolve():
                    candidate_image.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original_image, candidate_image)
        return {"path": str(target), "directory": str(directory), "sha256": digest.hexdigest(), "size_bytes": size}
    except BaseException:
        shutil.rmtree(directory)
        raise


def _processing_input_path(document: Document, payload: dict) -> str:
    request = payload.get("process_request") or {}
    return (
        (payload.get("input_artifact") or {}).get("path")
        or (request.get("source", {}).get("original_file") or {}).get("path")
        or document.storage_path
    )


async def _restart_confirmation_error(
    session: AsyncSession, source: Source, document: Document, job: Job, engine_manager: EngineManager, reason: str,
) -> DocumentRestartRequiredError:
    """Bind confirmation to the current paused operation and exact target."""
    if document.status == DocumentStatus.FAILED and job.status == JobStatus.FAILED:
        if not await hold_document_for_restart(session, job, DocumentRestartRequiredError(reason)):
            raise ConflictError("文档或任务状态已变化，请刷新后重试")
    for _ in range(4):
        payload = dict(job.payload or {})
        try:
            target = await engine_manager.document_processing_identity(
                source.sag_source_config_id, _processing_input_path(document, payload), source=source,
            )
        except OSError:
            return DocumentRestartRequiredError("resume_input_unavailable")
        binding = {
            "job_id": job.id, "control_revision": document.control_revision,
            "operation_id": (payload.get("process_request") or {}).get("context", {}).get("operation_id"),
            **target,
        }
        now = datetime.now(UTC).timestamp()
        confirmation = payload.get("restart_confirmation") or {}
        if (
            all(confirmation.get(key) == value for key, value in binding.items())
            and confirmation.get("expires_at", 0) > now
        ):
            return DocumentRestartRequiredError(reason, confirmation_token=confirmation["token"])
        confirmation = {**binding, "token": secrets.token_urlsafe(32), "expires_at": now + 15 * 60}
        changed = await session.execute(
            update(Job)
            .where(
                Job.id == job.id,
                Job.status == JobStatus.PAUSED,
                job_payload_matches(session, payload),
                Job.document_id.in_(
                    select(Document.id).where(
                        Document.id == document.id,
                        Document.is_active.is_(True),
                        Document.control_revision == document.control_revision,
                        Document.status == DocumentStatus.PAUSED,
                    )
                ),
            )
            .values(payload={**payload, "restart_confirmation": confirmation})
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount == 1:
            await session.commit()
            await session.refresh(job)
            return DocumentRestartRequiredError(reason, confirmation_token=confirmation["token"])
        await session.rollback()
        await session.refresh(document)
        await session.refresh(job)
        if document.status != DocumentStatus.PAUSED or job.status != JobStatus.PAUSED:
            raise ConflictError("文档或任务状态已变化，请刷新后重试")
    raise ConflictError("文档或任务状态已变化，请刷新后重试")


async def reprocess_document(
    session: AsyncSession,
    source: Source,
    document_id: str,
    *,
    job_queue: JobQueue,
    engine_manager: EngineManager,
    confirmation_token: str | None = None,
) -> Job:
    """Queue a separate candidate without withdrawing the current publication."""
    document = await get_document(session, source, document_id)
    if document.status in {DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED}:
        raise ConflictError("文档正在删除或删除失败，无法重新处理")
    confirmed_job = None
    confirmed_identity = None
    if confirmation_token is not None:
        confirmed_job = await session.scalar(select(Job).where(
            Job.document_id == document.id,
            Job.payload["restart_confirmation"]["token"].as_string() == confirmation_token,
        ))
        if confirmed_job is None:
            raise ConflictError("重跑确认已失效，请重新执行恢复操作")
        confirmation = confirmed_job.payload["restart_confirmation"]
        if confirmation["job_id"] != confirmed_job.id:
            raise ConflictError("重跑确认对应的任务已变化，请重新执行恢复操作")
        replacement_id = confirmed_job.payload.get("replacement_job_id")
        if replacement_id:
            replacement = await session.get(Job, replacement_id)
            if replacement is None or replacement.document_id != document.id:
                raise ConflictError("重跑任务已变化，请刷新后重试")
            return replacement
        if (
            confirmed_job.status != JobStatus.PAUSED
            or document.status != DocumentStatus.PAUSED
            or confirmation["control_revision"] != document.control_revision
            or confirmation["operation_id"]
            != (confirmed_job.payload.get("process_request") or {}).get("context", {}).get("operation_id")
        ):
            raise ConflictError("文档或任务状态已变化，请刷新后重试")
        if confirmation["expires_at"] <= datetime.now(UTC).timestamp():
            raise await _restart_confirmation_error(
                session, source, document, confirmed_job, engine_manager, "confirmation_expired",
            )
        confirmed_identity = await engine_manager.document_processing_identity(
            source.sag_source_config_id, _processing_input_path(document, confirmed_job.payload), source=source,
        )
        if any(confirmation.get(key) != value for key, value in confirmed_identity.items()):
            raise await _restart_confirmation_error(
                session, source, document, confirmed_job, engine_manager, "confirmation_target_changed",
            )
    latest = confirmed_job or await session.scalar(
        select(Job).where(Job.document_id == document.id).order_by(
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED]).desc(),
            Job.created_at.desc(), Job.id.desc(),
        )
    )
    if (
        confirmed_job is None
        and document.status == DocumentStatus.FAILED
        and latest is not None
        and latest.type == JobType.PROCESS_DOCUMENT
        and latest.status == JobStatus.FAILED
    ):
        return await _resume_document_job(
            session, source, document, latest, job_queue=job_queue, engine_manager=engine_manager,
        )
    if (
        confirmed_job is None
        and latest is not None
        and latest.status != JobStatus.SUCCEEDED
        and document.status != DocumentStatus.READY
    ):
        payload = latest.payload or {}
        reason = payload.get("restart_required")
        if latest.type == JobType.REPROCESS_DOCUMENT or (
            payload.get("process_checkpoint") and not payload.get("process_request")
        ):
            reason = "legacy_checkpoint"
        if reason:
            raise await _restart_confirmation_error(session, source, document, latest, engine_manager, reason)
    if (
        confirmed_job is None
        and latest is not None
        and latest.status in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED}
    ):
        return latest

    original_status, revision = document.status, document.control_revision
    job_id = new_id()
    artifact = await asyncio.to_thread(
        _snapshot_document_input, document, job_id,
        input_path=_processing_input_path(document, confirmed_job.payload) if confirmed_job else None,
    )
    try:
        if confirmed_identity is not None:
            copied_identity = await engine_manager.document_processing_identity(
                source.sag_source_config_id, artifact["path"], source=source,
            )
            if copied_identity != confirmed_identity:
                raise await _restart_confirmation_error(
                    session, source, document, confirmed_job, engine_manager, "confirmation_target_changed",
                )
        claimed = await session.execute(
            update(Document)
            .where(
                Document.id == document.id,
                Document.is_active.is_(True),
                Document.status == original_status,
                Document.control_revision == revision,
            )
            .values(
                status=DocumentStatus.PENDING,
                processing_stage="queued",
                processing_run_id=None,
                control_revision=revision + 1,
                progress=0,
                processed_chunks=None,
                total_chunks=None,
                error=None,
                error_layer=None,
                error_stage=None,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            await session.rollback()
            current = await session.get(Document, document_id, populate_existing=True)
            if confirmed_job is not None:
                await session.refresh(confirmed_job)
                replacement = await session.get(Job, confirmed_job.payload.get("replacement_job_id"))
                if replacement is not None and replacement.document_id == document_id:
                    shutil.rmtree(Path(artifact["path"]).parent)
                    return replacement
                raise ConflictError("文档或任务状态已变化，请刷新后重试")
            if current is not None and current.status not in {DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED}:
                existing = await session.scalar(
                    select(Job)
                    .where(
                        Job.document_id == document_id,
                        Job.type == JobType.PROCESS_DOCUMENT,
                        Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED]),
                    )
                    .order_by(Job.created_at.desc(), Job.id.desc())
                    .limit(1)
                )
                if existing is not None:
                    shutil.rmtree(Path(artifact["path"]).parent)
                    return existing
            raise ConflictError("文档状态已变化，请刷新后重试")
        job = Job(
            id=job_id,
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document_id,
            status=JobStatus.QUEUED,
            payload={
                "input_artifact": artifact,
                "previous_storage_path": document.storage_path,
                "control_revision": revision + 1,
                **({"confirmed_identity": confirmed_identity} if confirmed_identity is not None else {}),
            },
        )
        if confirmed_job is not None:
            retired = await session.execute(update(Job).where(
                Job.id == confirmed_job.id,
                Job.status == JobStatus.PAUSED,
                job_payload_matches(session, confirmed_job.payload),
            ).values(
                status=JobStatus.FAILED, finished_at=datetime.now(UTC), error="任务已被确认重跑的新任务替代",
                payload={**confirmed_job.payload, "replacement_job_id": job.id},
            ).execution_options(synchronize_session=False))
            if retired.rowcount != 1:
                raise ConflictError("文档或任务状态已变化，请刷新后重试")
        session.add(job)
        await touch_source_revision(session, source.id)
        await session.commit()
    except BaseException:
        await session.rollback()
        shutil.rmtree(Path(artifact["path"]).parent)
        raise
    await session.refresh(document)
    await session.refresh(job)
    await _enqueue_persisted_job(job_queue, job.id)
    return job


def _job_input_id(payload: dict | None) -> str | None:
    """Read the bound input, retaining historical unfinished IDs for cleanup only."""
    request = (payload or {}).get("process_request")
    if isinstance(request, dict):
        descriptor = (request.get("source") or {}).get("descriptor") or {}
        value = descriptor.get("source_id")
    else:
        checkpoint = (payload or {}).get("process_checkpoint")
        value = checkpoint.get("source_id") if isinstance(checkpoint, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


async def _document_cleanup_targets(session: AsyncSession, document: Document) -> dict:
    """Persist the input and artifact closure before document Jobs are removed."""
    inputs = {document.sag_source_id} if document.sag_source_id else set()
    paths = {document.storage_path} if document.storage_path else set()
    # A killed snapshot may precede its Job commit. The document owns this
    # complete subtree, including attempts whose bindings never reached SQL.
    directories = {str(_document_candidate_root(document))}
    jobs = (await session.scalars(select(Job).where(Job.document_id == document.id))).all()
    for candidate in jobs:
        payload = candidate.payload or {}
        input_id = _job_input_id(payload)
        if input_id:
            inputs.add(input_id)
        inputs.update(
            value.strip() for value in payload.get("derived_source_ids", []) if isinstance(value, str) and value.strip()
        )
        paths.update(value for value in payload.get("artifact_paths", []) if isinstance(value, str) and value)
        directories.update(
            value for value in payload.get("candidate_directories", []) if isinstance(value, str) and value
        )
        artifact = payload.get("input_artifact") or {}
        request = payload.get("process_request") or {}
        original = (request.get("source") or {}).get("original_file") or {}
        for value in (payload.get("previous_storage_path"), artifact.get("path"), original.get("path")):
            if isinstance(value, str) and value:
                paths.add(value)
        if artifact.get("directory"):
            directories.add(artifact["directory"])
    return {
        "derived_source_ids": sorted(inputs),
        "artifact_paths": sorted(paths),
        "candidate_directories": sorted(directories),
    }


def _source_document_filter(source_id: str):
    return (
        Document.source_id == source_id,
        Document.is_active.is_(True),
        Document.status.not_in([DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED]),
    )


def _source_sum_clause(source_id: str, column):
    """Aggregate committed counts even while a replacement is paused or failed."""
    return (
        select(func.coalesce(func.sum(column), 0))
        .where(Document.source_id == source_id, Document.has_publication)
        .scalar_subquery()
    )


async def _refresh_source_counts(session: AsyncSession, source: Source) -> None:
    """原子重算信源聚合计数。

    删除任务与文档级变更（上传/删除等）可并发提交；用单条 UPDATE 在数据库
    层串行化计数写入，避免「先 SELECT 后赋值」在两者交错时丢失计数。
    """
    document_count = select(func.count(Document.id)).where(*_source_document_filter(source.id)).scalar_subquery()
    await session.execute(
        update(Source)
        .where(Source.id == source.id)
        .values(
            document_count=document_count,
            chunk_count=_source_sum_clause(source.id, Document.chunk_count),
            event_count=_source_sum_clause(source.id, Document.event_count),
        )
        .execution_options(synchronize_session=False)
    )
    await session.refresh(
        source,
        attribute_names=["document_count", "chunk_count", "event_count"],
    )


async def qualify_historical_publications(session: AsyncSession) -> None:
    """Classify pre-upgrade references once, without executing or removing data."""
    documents = list((await session.scalars(select(Document).where(Document.control_revision == 0))).all())
    affected_sources = set()
    for document in documents:
        old_input = document.sag_source_id
        published = document.status == DocumentStatus.READY and bool(old_input)
        values = {"control_revision": 1}
        if not published:
            values.update(sag_source_id=None, chunk_count=0, event_count=0)
        claimed = await session.execute(
            update(Document)
            .where(Document.id == document.id, Document.control_revision == 0)
            .values(**values)
        )
        if claimed.rowcount != 1:
            continue
        affected_sources.add(document.source_id)
        if published:
            continue
        deleting = document.status in {DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED}
        if not deleting:
            document.status = DocumentStatus.PAUSED
            document.processing_stage = "waiting_manual"
            document.processing_run_id = None
        job = await session.scalar(
            select(Job)
            .where(
                Job.document_id == document.id,
                Job.type.in_([JobType.PROCESS_DOCUMENT, JobType.REPROCESS_DOCUMENT, JobType.DELETE_DOCUMENT]),
            )
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(1)
        )
        if job is None:
            job = Job(
                type=JobType.DELETE_DOCUMENT if deleting else JobType.PROCESS_DOCUMENT,
                source_id=document.source_id,
                document_id=document.id,
                status=JobStatus.PAUSED,
                payload={"target_document_id": document.id} if deleting else {},
            )
            session.add(job)
            if not deleting:
                document.error = "历史任务缺少完成记录，请确认后重新处理；原始数据已保留"
        payload = dict(job.payload or {})
        payload["restart_required"] = "legacy_checkpoint"
        if old_input:
            payload["derived_source_ids"] = sorted({old_input, *(payload.get("derived_source_ids") or [])})
        job.payload = set_scheduler(payload, blocked_reason=MANUAL_RECOVERY)
        job.status = JobStatus.PAUSED
    await session.flush()
    for source_id in affected_sources:
        source = await session.get(Source, source_id)
        if source is not None:
            await _refresh_source_counts(session, source)


async def publish_document_result(
    session: AsyncSession,
    source: Source,
    document: Document,
    job: Job,
    result: OperationResult,
    *,
    expected_run_id: str | None,
    expected_job_status: JobStatus | None = None,
) -> bool:
    """Choose one complete input and reconcile business records in this transaction."""
    revision = (job.payload or {}).get("control_revision")
    if type(revision) is not int or revision != document.control_revision:
        return False
    request = ProcessSourceRequest.model_validate((job.payload or {}).get("process_request"))
    if (
        result.status != "succeeded"
        or result.checkpoint != "ready"
        or result.operation_id != request.context.operation_id
        or result.data_source_id != source.sag_source_config_id
        or result.source_id != request.source.descriptor.source_id
    ):
        raise ConflictError("文档完成结果与持久请求不一致")
    if document.published_operation_id == result.operation_id:
        return True
    chunks = ChunkSetRef.model_validate(result.result["chunk_ref"])
    events = EventSetRef.model_validate(result.result["event_ref"])
    if chunks.source_id != result.source_id or events.source_id != result.source_id:
        raise ConflictError("文档结果引用与输入不一致")
    claimed_job = await session.execute(
        update(Job)
        .where(
            Job.id == job.id,
            Job.status == expected_job_status if expected_job_status is not None
            else Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED]),
            job_payload_matches(session, job.payload),
        )
        .values(
            status=JobStatus.SUCCEEDED,
            payload={key: value for key, value in (job.payload or {}).items() if key != "pause_requested"},
            progress=1.0,
            finished_at=datetime.now(UTC),
            error=None,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed_job.rowcount != 1:
        await session.rollback()
        return False
    artifact = (job.payload or {}).get("input_artifact")
    artifact_values = {}
    if artifact:
        if request.source.original_file is None or request.source.original_file.path != artifact["path"]:
            raise ConflictError("文档候选工件与持久请求不一致")
        artifact_values = {"storage_path": artifact["path"], "size_bytes": artifact["size_bytes"]}
    parser_result = (job.payload or {}).get("parser_result") or {}
    artifact_values.update({
        key: parser_result[key]
        for key in (
            "parser_provider", "mineru_provider", "mineru_model", "parser_status", "fallback_from", "fallback_reason",
        )
        if key in parser_result
    })
    published = await session.execute(
        update(Document)
        .where(
            Document.id == document.id,
            Document.control_revision == revision,
            Document.processing_run_id == expected_run_id,
            Document.status.not_in([DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED, DocumentStatus.READY]),
        )
        .values(
            status=DocumentStatus.READY,
            processing_stage="ready",
            sag_source_id=result.source_id,
            published_operation_id=result.operation_id,
            chunk_count=chunks.chunk_count,
            event_count=events.event_count,
            processed_chunks=chunks.chunk_count,
            total_chunks=chunks.chunk_count,
            progress=100,
            token_usage=int(((result.extraction_progress or {}).get("token_usage") or {}).get("total_tokens") or 0),
            error=None,
            **artifact_values,
        )
        .execution_options(synchronize_session=False)
    )
    if published.rowcount != 1:
        await session.rollback()
        return False
    await session.refresh(document)
    await session.refresh(job)
    await _refresh_source_counts(session, source)
    await record_document_vector_identity(session, source, document, (job.payload or {}).get("vector_identity"))
    await touch_source_revision(session, source.id)
    return True


async def reconcile_document_receipt(
    session: AsyncSession, source: Source, document: Document, job: Job, engine_manager: EngineManager,
) -> bool:
    """Publish a completed operation without reading inputs or advancing engine work."""
    saved = (job.payload or {}).get("process_request")
    if saved is None or document.status in {DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED}:
        return False
    request = ProcessSourceRequest.model_validate(saved)
    async with engine_manager.use_concurrently(source.sag_source_config_id, source) as engine:
        result = await engine.get_operation_status(request.context.operation_id)
    if result is None or result.status != "succeeded" or result.checkpoint != "ready":
        return False
    return await publish_document_result(
        session, source, document, job, result, expected_run_id=document.processing_run_id,
    )


async def _commit_document_job_transition(
    session: AsyncSession,
    document: Document,
    job: Job,
    *,
    expected_document_status: DocumentStatus,
    document_values: dict,
    expected_job_status: JobStatus,
    job_values: dict,
) -> bool:
    """Atomically claim one document control transition and its process job."""
    if (job.payload or {}).get("control_revision", document.control_revision) != document.control_revision:
        return False
    claimed_document = await session.execute(
        update(Document)
        .where(
            Document.id == document.id,
            Document.control_revision == document.control_revision,
            Document.status == expected_document_status,
        )
        .values(**document_values)
        .execution_options(synchronize_session=False)
    )
    if claimed_document.rowcount != 1:
        await session.rollback()
        return False

    job_transition = update(Job).where(
        Job.id == job.id,
        Job.document_id == document.id,
        Job.type == job.type,
        Job.status == expected_job_status,
        Job.started_at == job.started_at,
    )
    if "payload" in job_values:
        job_transition = job_transition.where(job_payload_matches(session, job.payload))
    claimed_job = await session.execute(
        job_transition.values(**job_values).execution_options(synchronize_session=False)
    )
    if claimed_job.rowcount != 1:
        await session.rollback()
        return False

    await session.commit()
    await session.refresh(document)
    await session.refresh(job)
    return True


async def hold_document_for_restart(session: AsyncSession, job: Job, error: DocumentRestartRequiredError) -> bool:
    """Persist a confirmation gate without replacing the request or publication."""
    document = await session.get(Document, job.document_id, populate_existing=True) if job.document_id else None
    if (
        job.type not in {JobType.PROCESS_DOCUMENT, JobType.REPROCESS_DOCUMENT}
        or document is None
        or document.status not in {
            DocumentStatus.PENDING, DocumentStatus.LOADING, DocumentStatus.EXTRACTING,
            DocumentStatus.PAUSING, DocumentStatus.PAUSED,
            DocumentStatus.FAILED,
        }
    ):
        return False
    return await _commit_document_job_transition(
        session,
        document,
        job,
        expected_document_status=document.status,
        document_values={
            "status": DocumentStatus.PAUSED,
            "processing_stage": "waiting_confirmation",
            "processing_run_id": None,
            "error": error.message,
            "error_layer": error.layer.value,
            "error_stage": error.stage.value,
        },
        expected_job_status=job.status,
        job_values={
            "status": JobStatus.PAUSED,
            "finished_at": None,
            "error": error.message,
            "payload": set_scheduler(
                {**(job.payload or {}), "restart_required": error.reason},
                blocked_reason=MANUAL_RECOVERY,
            ),
        },
    )


async def _raise_document_control_conflict(
    session: AsyncSession,
    document_id: str,
    *,
    action: str,
    fallback: str,
) -> None:
    """Report the winning concurrent transition after a failed control CAS."""
    await session.rollback()
    current = await session.get(Document, document_id, populate_existing=True)
    if current is None:
        raise ConflictError("文档已删除，请刷新后重试")
    if current.status in {DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED}:
        raise ConflictError(f"文档正在删除或删除失败，无法{action}")
    raise ConflictError(fallback)


async def pause_document(session: AsyncSession, source: Source, document_id: str) -> Job:
    """协作式暂停：已开始的分块跑完并保存断点，不再领取新分块。"""
    document = await get_document(session, source, document_id)
    if document.status in {DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED}:
        raise ConflictError("文档正在删除或删除失败，无法停止抽取")
    if document.status not in {
        DocumentStatus.PENDING,
        DocumentStatus.LOADING,
        DocumentStatus.EXTRACTING,
    }:
        raise ConflictError("抽取任务已经结束或状态已变化，无法停止")
    document_record_id = document.id
    expected_document_status = document.status
    job = await session.scalar(
        select(Job)
        .where(
            Job.document_id == document_record_id,
            Job.type == JobType.PROCESS_DOCUMENT,
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    if job is None:
        await _raise_document_control_conflict(
            session,
            document_record_id,
            action="停止抽取",
            fallback="当前文档没有可停止的抽取任务",
        )

    if job.status == JobStatus.QUEUED:
        if await _commit_document_job_transition(
            session,
            document,
            job,
            expected_document_status=expected_document_status,
            document_values={"status": DocumentStatus.PAUSED},
            expected_job_status=JobStatus.QUEUED,
            job_values={"status": JobStatus.PAUSED},
        ):
            return job
        await _raise_document_control_conflict(
            session,
            document_record_id,
            action="停止抽取",
            fallback="文档或抽取任务状态已变化，请刷新后重试",
        )

    if job.status != JobStatus.RUNNING:
        raise ConflictError("抽取任务已经结束，无法停止")
    if await _commit_document_job_transition(
        session,
        document,
        job,
        expected_document_status=expected_document_status,
        document_values={"status": DocumentStatus.PAUSING},
        expected_job_status=JobStatus.RUNNING,
        job_values={"payload": {**(job.payload or {}), "pause_requested": True}},
    ):
        return job
    await _raise_document_control_conflict(
        session,
        document_record_id,
        action="停止抽取",
        fallback="文档或抽取任务状态已变化，请刷新后重试",
    )


async def resume_document(
    session: AsyncSession,
    source: Source,
    document_id: str,
    *,
    job_queue: JobQueue,
    engine_manager: EngineManager,
) -> Job:
    """把暂停任务原样重新入队，处理器会跳过断点中已完成的分块。"""
    document = await get_document(session, source, document_id)
    if document.status in {DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED}:
        raise ConflictError("文档正在删除或删除失败，无法继续")
    if document.status != DocumentStatus.PAUSED:
        raise ConflictError("当前文档不是已暂停状态，无法继续")
    document_record_id = document.id
    job = await session.scalar(
        select(Job)
        .where(
            Job.document_id == document_record_id,
            Job.type.in_([JobType.PROCESS_DOCUMENT, JobType.REPROCESS_DOCUMENT]),
            Job.status == JobStatus.PAUSED,
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    if job is None:
        await _raise_document_control_conflict(
            session,
            document_record_id,
            action="继续",
            fallback="当前文档没有可继续的暂停任务",
        )
    return await _resume_document_job(
        session, source, document, job, job_queue=job_queue, engine_manager=engine_manager,
    )


async def _resume_document_job(
    session: AsyncSession,
    source: Source,
    document: Document,
    job: Job,
    *,
    job_queue: JobQueue,
    engine_manager: EngineManager,
) -> Job:
    """Requeue the same compatible attempt; the SDK owns durable recovery."""
    document_record_id = document.id
    expected_document_status, expected_job_status = document.status, job.status
    retrying_failed = expected_document_status == DocumentStatus.FAILED
    if retrying_failed:
        payload = dict(job.payload or {})
        receipt = None
        if payload.get("process_request"):
            request = ProcessSourceRequest.model_validate(payload["process_request"])
            async with engine_manager.use_concurrently(source.sag_source_config_id, source) as engine:
                receipt = await engine.get_operation_status(request.context.operation_id)
            if receipt is not None and receipt.status == "succeeded" and receipt.checkpoint == "ready":
                if not await publish_document_result(
                    session, source, document, job, receipt,
                    expected_run_id=document.processing_run_id, expected_job_status=JobStatus.FAILED,
                ):
                    raise ConflictError("文档或任务状态已变化，请刷新后重试")
                await session.commit()
                return job
        can_retry = payload.get("failure_retryable") is True or (
            "failure_retryable" not in payload and receipt is not None
            and (receipt.status in {"accepted", "running"} or receipt.retryable is True)
        )
        if not can_retry or (receipt is not None and receipt.status == "failed" and receipt.retryable is not True):
            raise await _restart_confirmation_error(
                session, source, document, job, engine_manager, "operation_not_retryable",
            )

    elif await reconcile_document_receipt(session, source, document, job, engine_manager):
        await session.commit()
        return job

    payload = dict(job.payload or {})
    if (
        job.type == JobType.REPROCESS_DOCUMENT
        or payload.get("restart_required")
        or (payload.get("process_checkpoint") and not payload.get("process_request"))
    ):
        current = await session.get(Document, document_record_id, populate_existing=True)
        if current is None or current.status != expected_document_status:
            raise ConflictError("文档状态已变化，请刷新后重试")
        raise await _restart_confirmation_error(
            session, source, document, job, engine_manager, payload.get("restart_required") or "legacy_checkpoint",
        )
    baseline = payload.get("processing_identity")
    if baseline is None and (payload.get("process_request") or payload.get("document_parser")):
        raise await _restart_confirmation_error(
            session, source, document, job, engine_manager, "legacy_processing_identity",
        )
    if baseline is not None:
        try:
            target = await engine_manager.document_processing_identity(
                source.sag_source_config_id, _processing_input_path(document, payload), source=source,
            )
        except OSError:
            raise DocumentRestartRequiredError("resume_input_unavailable") from None
        reason = processing_identity_restart_reason(baseline, target)
        if reason:
            raise await _restart_confirmation_error(session, source, document, job, engine_manager, reason)
    payload.pop("pause_requested", None)
    payload.pop("failure_retryable", None)
    payload["resume_requested"] = True
    resumed_payload = set_scheduler(
        payload,
        priority=RESUME_PRIORITY,
        blocked_reason=None,
    )
    resumed_status = DocumentStatus.EXTRACTING if payload.get("process_request") else DocumentStatus.PENDING
    if not await _commit_document_job_transition(
        session,
        document,
        job,
        expected_document_status=expected_document_status,
        document_values={
            "status": resumed_status,
            "error": None,
            "error_layer": None,
            "error_stage": None,
            "processing_stage": "queued",
            "processing_run_id": None,
        },
        expected_job_status=expected_job_status,
        job_values={
            "payload": resumed_payload,
            "status": JobStatus.QUEUED,
            "started_at": None,
            "finished_at": None,
            "error": None,
            **({"attempts": 0} if retrying_failed else {}),
        },
    ):
        await _raise_document_control_conflict(
            session,
            document_record_id,
            action="继续",
            fallback="文档或抽取任务状态已变化，请刷新后重试",
        )
    await _enqueue_persisted_job(job_queue, job.id)
    return job


async def delete_document(
    session: AsyncSession,
    source: Source,
    document_id: str,
    *,
    job_queue: JobQueue | None = None,
) -> Job:
    document = await get_document(session, source, document_id)
    source_record_id = source.id
    cleanup_targets = await _document_cleanup_targets(session, document)
    derived_source_ids = set(cleanup_targets["derived_source_ids"])
    existing = await session.scalar(
        select(Job)
        .where(
            Job.document_id == document.id,
            Job.type == JobType.DELETE_DOCUMENT,
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    active_jobs = list(
        (
            await session.scalars(
                select(Job).where(
                    Job.document_id == document.id,
                    Job.type == JobType.PROCESS_DOCUMENT,
                    Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                )
            )
        ).all()
    )

    # A document whose processor has never started cannot have written any
    # engine-side records. Delete it inside this request so it is never held up
    # by unrelated long-running ingestion or cleanup work.
    metadata_only = (
        document.status == DocumentStatus.PENDING
        and not document.sag_source_id
        and not derived_source_ids
        and all(
            active_job.status == JobStatus.QUEUED
            and active_job.started_at is None
            and active_job.attempts == 0
            and not _job_input_id(active_job.payload)
            for active_job in active_jobs
        )
    )
    if metadata_only:
        target_document_id = document.id
        candidate_job_ids = [candidate.id for candidate in active_jobs]
        if candidate_job_ids:
            fenced_jobs = await session.execute(
                update(Job)
                .where(
                    Job.id.in_(candidate_job_ids),
                    Job.document_id == target_document_id,
                    Job.type == JobType.PROCESS_DOCUMENT,
                    Job.status == JobStatus.QUEUED,
                    Job.started_at.is_(None),
                    Job.attempts == 0,
                )
                .values(status=JobStatus.PAUSED)
                .execution_options(synchronize_session=False)
            )
            if fenced_jobs.rowcount != len(candidate_job_ids):
                # A worker claimed the processor after the optimistic read.
                # Roll back any partial fence and use cooperative deletion.
                await session.rollback()
                completed_jobs = list(
                    (
                        await session.scalars(
                            select(Job)
                            .where(
                                Job.source_id == source_record_id,
                                Job.type == JobType.DELETE_DOCUMENT,
                                Job.status == JobStatus.SUCCEEDED,
                            )
                            .order_by(Job.created_at.desc(), Job.id.desc())
                            .limit(100)
                        )
                    ).all()
                )
                completed = next(
                    (
                        candidate
                        for candidate in completed_jobs
                        if (candidate.payload or {}).get("target_document_id") == target_document_id
                    ),
                    None,
                )
                if completed is not None:
                    return completed
                current_source = await session.get(
                    Source,
                    source_record_id,
                    populate_existing=True,
                )
                if current_source is None:
                    raise NotFoundError("信源不存在")
                return await delete_document(
                    session,
                    current_source,
                    target_document_id,
                    job_queue=job_queue,
                )
        claimed = await session.execute(
            update(Document)
            .where(
                Document.id == target_document_id,
                Document.status == DocumentStatus.PENDING,
                Document.sag_source_id.is_(None),
            )
            .values(status=DocumentStatus.DELETING, error=None)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            await session.rollback()
            completed_jobs = list(
                (
                    await session.scalars(
                        select(Job)
                        .where(
                            Job.source_id == source_record_id,
                            Job.type == JobType.DELETE_DOCUMENT,
                            Job.status == JobStatus.SUCCEEDED,
                        )
                        .order_by(Job.created_at.desc(), Job.id.desc())
                        .limit(100)
                    )
                ).all()
            )
            completed = next(
                (
                    candidate
                    for candidate in completed_jobs
                    if (candidate.payload or {}).get("target_document_id") == target_document_id
                ),
                None,
            )
            if completed is not None:
                return completed
            raise ConflictError("文档状态已变化，请刷新后重试")
        completed_at = datetime.now(UTC)
        completed = Job(
            type=JobType.DELETE_DOCUMENT,
            source_id=source.id,
            document_id=None,
            status=JobStatus.SUCCEEDED,
            progress=1.0,
            attempts=1,
            payload=set_scheduler(
                {"target_document_id": target_document_id, "cleanup_mode": "metadata_only", **cleanup_targets},
                priority=DELETE_PRIORITY,
            ),
            started_at=completed_at,
            finished_at=completed_at,
        )
        session.add(completed)
        await session.delete(document)
        await session.flush()
        await _refresh_source_counts(session, source)
        await touch_source_revision(session, source.id)
        await session.commit()
        await session.refresh(completed)
        from sag_api.parsing.service import remove_parsed_sidecars

        for path in cleanup_targets["artifact_paths"]:
            remove_parsed_sidecars(path)
        for directory in cleanup_targets["candidate_directories"]:
            if Path(directory).exists():
                shutil.rmtree(directory)
        return completed

    if existing is None:
        original_status = document.status
        claimed = await session.execute(
            update(Document)
            .where(
                Document.id == document.id, Document.status == original_status,
                Document.control_revision == document.control_revision,
            )
            .values(status=DocumentStatus.DELETING, error=None, control_revision=document.control_revision + 1)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            await session.rollback()
            existing = await session.scalar(
                select(Job)
                .where(
                    Job.document_id == document_id,
                    Job.type == JobType.DELETE_DOCUMENT,
                    Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                )
                .order_by(Job.created_at.desc(), Job.id.desc())
                .limit(1)
            )
            if existing is None:
                raise ConflictError("文档状态已变化，请刷新后重试")
            if job_queue is not None:
                job_queue.begin_source_maintenance(source_record_id, existing.id)
                await _enqueue_persisted_job(job_queue, existing.id)
            return existing
        await session.refresh(document)

    for job in active_jobs:
        job.payload = {**(job.payload or {}), "pause_requested": True}
        if job.status == JobStatus.QUEUED:
            job.status = JobStatus.PAUSED
    document.status = DocumentStatus.DELETING
    document.error = None
    if existing is not None:
        if existing.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
            resumed = await session.execute(
                update(Job).where(Job.id == existing.id, Job.status == existing.status).values(
                    status=JobStatus.QUEUED, attempts=0, error=None, started_at=None, finished_at=None,
                    payload=set_scheduler(existing.payload, priority=DELETE_PRIORITY, blocked_reason=None),
                ).execution_options(synchronize_session=False)
            )
            if resumed.rowcount != 1:
                await session.rollback()
                raise ConflictError("删除任务状态已变化，请刷新后重试")
        await _refresh_source_counts(session, source)
        await session.commit()
        await session.refresh(existing)
        if job_queue is not None:
            job_queue.begin_source_maintenance(source.id, existing.id)
            await _enqueue_persisted_job(job_queue, existing.id)
        return existing

    delete_job = Job(
        type=JobType.DELETE_DOCUMENT,
        source_id=source.id,
        document_id=document.id,
        status=JobStatus.QUEUED,
        payload=set_scheduler(
            {
                "target_document_id": document.id,
                **cleanup_targets,
            },
            priority=DELETE_PRIORITY,
        ),
    )
    session.add(delete_job)
    await _refresh_source_counts(session, source)
    await touch_source_revision(session, source.id)
    await session.commit()
    await session.refresh(delete_job)
    if job_queue is not None:
        job_queue.begin_source_maintenance(source.id, delete_job.id)
        await _enqueue_persisted_job(job_queue, delete_job.id)
    return delete_job
