"""信源领域逻辑（单用户，扁平）。"""

from __future__ import annotations

import os
import shutil

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.connectors import registry
from sag_api.core.config import settings
from sag_api.core.error_taxonomy import ErrorCode
from sag_api.core.errors import ApiError, NotFoundError, ValidationError
from sag_api.core.logging import get_logger
from sag_api.db.base import new_id
from sag_api.db.models import AgentBinding, Job, Source
from sag_api.enums import CONNECTOR_SOURCE_TYPE, BindingTargetType, JobStatus, JobType, SourceType
from sag_api.jobs import JobQueue
from sag_api.sag import EngineManager
from sag_api.schemas.source import SourceCreate, SourceUpdate

log = get_logger("services.source")


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
        rows = await session.execute(select(Source).where(Source.id.in_(ordered_ids)))
        by_id = {source.id: source for source in rows.scalars().all()}
        return [by_id[source_id] for source_id in ordered_ids if source_id in by_id]

    rows = await session.execute(
        select(Source)
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
        config=data.config or {},
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)

    # 预建引擎 schema（幂等）；失败不阻断创建，处理文档时会重试
    try:
        await engine_manager.provision(source.sag_source_config_id, source)
    except ApiError as e:
        log.warning("信源引擎预建失败 %s：%s", source.sag_source_config_id, e.message)
    return source


async def update_source(
    session: AsyncSession,
    source_id: str,
    data: SourceUpdate,
    *,
    job_queue: JobQueue | None = None,
) -> Source:
    source = await get_source(session, source_id)
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
    engine_manager: EngineManager,
    upload_dir: str,
    job_queue: JobQueue | None = None,
) -> None:
    """删除信源并收尾：移除悬挂绑定、关闭引擎槽、清理上传文件。"""
    source = await get_source(session, source_id)
    sag_id = source.sag_source_config_id

    # 悬挂绑定清理（target_id 为普通字符串，无 FK 级联）
    await session.execute(
        AgentBinding.__table__.delete().where(
            AgentBinding.target_type == BindingTargetType.SOURCE,
            AgentBinding.target_id == source.id,
        )
    )
    await session.delete(source)
    await session.commit()

    # 引擎槽关闭 + 上传目录清理（尽力而为，不阻断删除）
    await engine_manager.release(sag_id)
    shutil.rmtree(os.path.join(upload_dir, source_id), ignore_errors=True)
    from sag_api.services.universe_service import schedule_universe_refresh

    await schedule_universe_refresh(
        session,
        job_queue,
        source_id=None,
        reason="source_deleted",
    )


async def sync_source(session: AsyncSession, source_id: str, *, job_queue: JobQueue) -> Job:
    """触发一次动态连接器同步（如网页抓取）。"""
    source = await get_source(session, source_id)
    connector = registry.get(source.connector_kind)
    if not connector.meta.supports_sync:
        raise ValidationError("该连接器不支持同步")
    job = Job(type=JobType.SYNC_SOURCE, source_id=source.id, status=JobStatus.QUEUED)
    session.add(job)
    await session.commit()
    await session.refresh(job)
    await job_queue.enqueue(job.id)
    return job
