"""信源领域逻辑（单用户，扁平）。"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from zleap.sag.operations import DeleteDataSourceRequest, OperationContext

from sag_api.connectors import registry
from sag_api.core.config import settings
from sag_api.core.error_taxonomy import ErrorCode
from sag_api.core.errors import ApiError, ConflictError, NotFoundError, ValidationError
from sag_api.core.logging import get_logger
from sag_api.db.base import new_id
from sag_api.db.models import Document, Job, OctxInstallation, OctxTransfer, Source
from sag_api.enums import (
    CONNECTOR_SOURCE_TYPE,
    DocumentStatus,
    JobStatus,
    JobType,
    OctxTransferStatus,
    SourceStatus,
    SourceType,
)
from sag_api.jobs import JobQueue
from sag_api.jobs.scheduling import DELETE_PRIORITY, set_scheduler
from sag_api.sag import EngineManager
from sag_api.sag.octx_vector_protocol import initialize_vector_identity_state
from sag_api.schemas.source import SourceCreate, SourceUpdate

log = get_logger("services.source")


def _range_delete_request(range_id: str, owner_id: str) -> DeleteDataSourceRequest:
    operation_id = new_id()
    digest = sha256(json.dumps(
        {"operation_type": "delete_data_source", "data_source_id": range_id},
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return DeleteDataSourceRequest(
        context=OperationContext(
            operation_id=operation_id, idempotency_key=operation_id, request_digest=digest,
            fence_scope=range_id, fence_token=1, owner_id=owner_id, isolation="input",
        ), data_source_id=range_id,
    )


async def list_sources(session: AsyncSession) -> list[Source]:
    rows = await session.execute(select(Source).order_by(Source.created_at.desc()))
    return list(rows.scalars().all())


async def search_source_candidates(
    session: AsyncSession,
    source_ids: list[str] | None = None,
) -> list[Source]:
    """Select a bounded retrieval scope without materializing the source table.

    Explicit `source_ids` preserve the user's @ order. An implicit global search
    uses data density and recency as the cheap partition router until a dedicated
    source-level semantic index is available.
    """
    limit = settings.search_source_candidate_limit
    if source_ids:
        ordered_ids = list(dict.fromkeys(source_ids))
        if len(ordered_ids) > limit:
            raise ValidationError(
                f"单次最多检索 {limit} 个信息源，请通过 @ 缩小范围",
                code=ErrorCode.TOO_MANY_SEARCH_SOURCES,
            )
        rows = await session.execute(select(Source).where(Source.id.in_(ordered_ids), Source.is_deleting.is_(False)))
        by_id = {source.id: source for source in rows.scalars().all()}
        return [by_id[source_id] for source_id in ordered_ids if source_id in by_id]

    rows = await session.execute(
        select(Source)
        .where(Source.is_deleting.is_(False))
        .order_by(
            Source.chunk_count.desc(),
            Source.event_count.desc(),
            Source.updated_at.desc(),
            Source.id,
        )
        .limit(limit)
    )
    return list(rows.scalars().all())


async def get_source(session: AsyncSession, source_id: str) -> Source:
    source = await session.get(Source, source_id)
    if source is None:
        raise NotFoundError("信源不存在")
    return source


async def create_source(
    session: AsyncSession, data: SourceCreate, *, engine_manager: EngineManager
) -> Source:
    connector = registry.get(data.connector_kind)
    connector.validate_config(data.config)
    source_type = CONNECTOR_SOURCE_TYPE.get(data.connector_kind, SourceType.DOCUMENT)

    source = Source(
        name=data.name,
        description=data.description,
        source_type=source_type,
        connector_kind=data.connector_kind,
        sag_source_config_id=f"src_{new_id()[:16]}",
        config=initialize_vector_identity_state(data.config or {}),
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)

    # Source 与知识引擎父记录必须同时可用。预建失败时补偿删除主库记录，
    # 避免接口成功却留下无法上传、检索的半成品信源。
    try:
        await engine_manager.provision(source.sag_source_config_id, source)
    except ApiError as e:
        log.warning("信源引擎预建失败，回滚信源 %s：%s", source.sag_source_config_id, e.message)
        await session.delete(source)
        await session.commit()
        raise
    return source


async def update_source(
    session: AsyncSession,
    source_id: str,
    data: SourceUpdate,
    *,
    job_queue: JobQueue | None = None,
) -> Source:
    source = await get_source(session, source_id)
    if source.is_deleting:
        raise ConflictError("信源正在删除，无法修改")
    if data.status in {SourceStatus.DELETING, SourceStatus.DELETE_FAILED}:
        raise ValidationError("请通过删除操作更改信源删除状态")
    if data.name is not None:
        source.name = data.name
    if data.description is not None:
        source.description = data.description
    if data.status is not None:
        source.status = data.status
    await session.commit()
    await session.refresh(source)
    from sag_api.services.universe_service import schedule_universe_refresh

    await schedule_universe_refresh(
        session,
        job_queue,
        source_id=source.id,
        reason="source_updated",
    )
    return source


async def delete_source(
    session: AsyncSession,
    source_id: str,
    *,
    upload_dir: str,
    job_queue: JobQueue,
) -> Job:
    """Hide the business group and persist one replayable cleanup intent."""
    from sag_api.services.document_service import _document_cleanup_targets, _enqueue_persisted_job
    from sag_api.services.source_operation_service import touch_source_revision

    source = await session.scalar(select(Source).where(Source.id == source_id).with_for_update())
    job = await session.scalar(
        select(Job).where(
            Job.type == JobType.DELETE_SOURCE,
            Job.payload["target_source_id"].as_string() == source_id,
        ).order_by(Job.created_at.desc(), Job.id.desc()).limit(1)
    )
    if source is None and job is None:
        raise NotFoundError("信源不存在")
    if job is not None:
        if job.status == JobStatus.SUCCEEDED:
            return job
        if job.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
            job.status = JobStatus.QUEUED
            job.attempts = 0
            job.error = None
            job.started_at = job.finished_at = None
            job.payload = set_scheduler(job.payload, priority=DELETE_PRIORITY, blocked_reason=None)
            if source is not None:
                source.status = SourceStatus.DELETING
            await session.commit()
            await session.refresh(job)
        await _enqueue_persisted_job(job_queue, job.id)
        return job

    claimed = await session.execute(
        update(Source).where(Source.id == source_id, Source.status == source.status)
        .values(status=SourceStatus.DELETING).execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        return await delete_source(session, source_id, upload_dir=upload_dir, job_queue=job_queue)
    await session.refresh(source)
    documents = list(await session.scalars(select(Document).where(Document.source_id == source_id)))
    paths, directories = set(), {str(Path(upload_dir) / source_id)}
    for document in documents:
        closure = await _document_cleanup_targets(session, document)
        paths.update(closure["artifact_paths"])
        directories.update(closure["candidate_directories"])
        document.control_revision += 1
        document.status = DocumentStatus.DELETING
        document.error = None
    active_jobs = await session.scalars(
        select(Job).where(Job.source_id == source_id, Job.status.in_(
            [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED],
        ))
    )
    for active in active_jobs:
        active.payload = {**(active.payload or {}), "pause_requested": True}
        if active.status == JobStatus.QUEUED:
            active.status = JobStatus.PAUSED
    range_ids = set(await session.scalars(
        select(OctxInstallation.sag_source_config_id).where(OctxInstallation.source_id == source_id)
    ))
    range_ids.add(source.sag_source_config_id)
    transfers = list(await session.scalars(select(OctxTransfer).where(OctxTransfer.target_source_id == source_id)))
    for transfer in transfers:
        if transfer.status not in {OctxTransferStatus.READY, OctxTransferStatus.CANCELLED, OctxTransferStatus.EXPIRED}:
            transfer.cancellation_requested = True
        shadow_range = (transfer.checkpoint or {}).get("source_config_id")
        if isinstance(shadow_range, str) and shadow_range:
            range_ids.add(shadow_range)
    job = Job(type=JobType.DELETE_SOURCE, source_id=source_id, status=JobStatus.QUEUED)
    session.add(job)
    await session.flush()
    requests = {}
    for range_id in sorted(range_ids):
        requests[range_id] = _range_delete_request(range_id, job.id).model_dump(mode="json")
    job.payload = set_scheduler({
        "target_source_id": source_id, "delete_requests": requests,
        "artifact_paths": sorted(paths), "candidate_directories": sorted(directories),
    }, priority=DELETE_PRIORITY)
    await touch_source_revision(session, source_id)
    await session.commit()
    await session.refresh(job)
    await _enqueue_persisted_job(job_queue, job.id)
    return job


async def sync_source(session: AsyncSession, source_id: str, *, job_queue: JobQueue) -> Job:
    """触发一次动态连接器同步（如网页抓取）。"""
    source = await get_source(session, source_id)
    if source.is_deleting:
        raise ConflictError("信源正在删除，无法同步")
    connector = registry.get(source.connector_kind)
    if not connector.meta.supports_sync:
        raise ValidationError("该连接器不支持同步")
    job = Job(type=JobType.SYNC_SOURCE, source_id=source.id, status=JobStatus.QUEUED)
    session.add(job)
    await session.commit()
    await session.refresh(job)
    from sag_api.services.document_service import _enqueue_persisted_job

    await _enqueue_persisted_job(job_queue, job.id)
    return job
