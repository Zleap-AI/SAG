"""知识宇宙的聚合、重建与调度 —— 同时作为对外门面。

测试会替换本模块的模块级绑定（`_cleanup_old_overviews`、
`schedule_universe_refresh`、`save_exploration`）。被替换的名字只有在调用点
同属本模块命名空间时才生效，因此重建与调度机制保留在此；查询侧已外迁至
`universe_query` 并在下方重新导出，对外接口保持不变。
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.errors import (
    NotFoundError,
)
from sag_api.core.logging import get_logger
from sag_api.db.base import new_id
from sag_api.db.models import (
    ExplorationSession,
    ExplorationStep,
    Job,
    Source,
    UniverseDirtySource,
    UniverseOverview,
    UniversePartition,
    User,
)
from sag_api.enums import JobStatus, JobType
from sag_api.jobs import JobQueue
from sag_api.sag import EngineManager
from sag_api.services.universe_query import (
    _stable_unit,
    active_overview,
    overview_is_stale,
    universe_expand,
    universe_manifest,
    universe_node_detail,
    universe_rebuild_is_pending,
    universe_timeline,
)

_GOLDEN_ANGLE = math.pi * (3 - math.sqrt(5))
_UNIVERSE_SCHEDULE_LOCKS: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
log = get_logger("services.universe")


def _universe_schedule_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    return _UNIVERSE_SCHEDULE_LOCKS.setdefault(loop, asyncio.Lock())


async def _previous_partition_positions(
    session: AsyncSession, user_id: str
) -> dict[tuple[str, str, str], tuple[float, float, float]]:
    active = (
        await session.execute(
            select(UniverseOverview.id, UniverseOverview.schema_version)
            .where(
                UniverseOverview.user_id == user_id,
                UniverseOverview.is_active.is_(True),
                UniverseOverview.status == "ready",
            )
            .order_by(UniverseOverview.created_at.desc())
        )
    ).first()
    if active is None or int(active.schema_version or 1) < 3:
        return {}
    rows = (
        await session.execute(
            select(
                UniversePartition.source_id,
                UniversePartition.kind,
                UniversePartition.key,
                UniversePartition.x,
                UniversePartition.y,
                UniversePartition.z,
            ).where(UniversePartition.overview_id == active.id)
        )
    ).all()
    return {(source_id, kind, key): (float(x), float(y), float(z or 0.0)) for source_id, kind, key, x, y, z in rows}


async def _previous_source_stats(session: AsyncSession, user_id: str) -> dict[str, dict[str, Any]]:
    active = (
        await session.execute(
            select(UniverseOverview.id, UniverseOverview.schema_version)
            .where(
                UniverseOverview.user_id == user_id,
                UniverseOverview.is_active.is_(True),
                UniverseOverview.status == "ready",
            )
            .order_by(UniverseOverview.created_at.desc())
        )
    ).first()
    if active is None or int(active.schema_version or 1) < 3:
        return {}
    rows = list(
        (
            await session.execute(
                select(UniversePartition).where(
                    UniversePartition.overview_id == active.id,
                    UniversePartition.kind == "source",
                )
            )
        ).scalars()
    )
    return {
        row.source_id: {
            "event_count": row.event_count,
            "entity_count": row.entity_count,
            "relation_count": row.relation_count,
            "time_buckets": list(row.time_buckets or []),
        }
        for row in rows
    }


async def _cleanup_old_overviews(
    session: AsyncSession,
    user_id: str,
    active_overview_id: str,
) -> None:
    """Keep one rollback snapshot; cleanup must never affect the active snapshot."""
    old_overviews = list(
        (
            await session.execute(
                select(UniverseOverview)
                .where(
                    UniverseOverview.user_id == user_id,
                    UniverseOverview.id != active_overview_id,
                )
                .order_by(UniverseOverview.created_at.desc())
                .offset(1)
            )
        ).scalars()
    )
    for old in old_overviews:
        await session.delete(old)
    if old_overviews:
        await session.commit()


async def rebuild_universe_overview(
    session: AsyncSession,
    engine_manager: EngineManager,
    user_id: str,
) -> UniverseOverview:
    """Build aggregate virtual partitions, then atomically make them active."""
    sources = list((await session.execute(select(Source).order_by(Source.created_at))).scalars())
    previous_positions = await _previous_partition_positions(session, user_id)
    previous_stats = await _previous_source_stats(session, user_id)
    dirty_snapshot = list(
        (
            await session.execute(
                select(
                    UniverseDirtySource.id,
                    UniverseDirtySource.source_id,
                    UniverseDirtySource.revision,
                ).where(UniverseDirtySource.user_id == user_id)
            )
        ).all()
    )
    overview = UniverseOverview(id=new_id(), user_id=user_id, status="building")
    overview_id = overview.id
    session.add(overview)
    await session.commit()

    partitions: list[UniversePartition] = []
    min_x = min_y = min_z = math.inf
    max_x = max_y = max_z = -math.inf

    try:
        dirty_source_ids = {source_id for _dirty_id, source_id, _revision in dirty_snapshot}
        recompute_all = not previous_stats or not dirty_snapshot
        stats_by_source: dict[str, Any] = {
            source.id: previous_stats[source.id]
            for source in sources
            if not recompute_all and source.id not in dirty_source_ids and source.id in previous_stats
        }

        semaphore = asyncio.Semaphore(max(1, min(4, settings.job_concurrency * 2)))

        async def load_stats(source: Source) -> tuple[str, Any]:
            async with semaphore:
                stats = await engine_manager.universe_overview_stats(
                    source.sag_source_config_id,
                    source=source,
                    category_limit=0,
                )
                return source.id, stats

        to_recompute = [source for source in sources if source.id not in stats_by_source]
        if to_recompute:
            computed = await asyncio.gather(*(load_stats(source) for source in to_recompute))
            stats_by_source.update(computed)

        def stat_value(stats: Any, key: str, default: Any = 0) -> Any:
            return stats.get(key, default) if isinstance(stats, dict) else getattr(stats, key, default)

        total_events = total_entities = total_relations = 0

        for source_index, source in enumerate(sources):
            stats = stats_by_source[source.id]
            event_count = int(stat_value(stats, "event_count"))
            entity_count = int(stat_value(stats, "entity_count"))
            relation_count = int(stat_value(stats, "relation_count"))
            time_buckets_raw = list(stat_value(stats, "time_buckets", []) or [])
            total_events += event_count
            total_entities += entity_count
            total_relations += relation_count

            density = max(0.0, min(1.0, math.log10(event_count + 1) / 6.0))
            source_radius = max(
                settings.universe_planet_radius_min,
                min(
                    settings.universe_planet_radius_max,
                    settings.universe_planet_radius_min
                    + settings.universe_planet_radius_scale * math.log10(event_count + 1),
                ),
            )
            source_key = (source.id, "source", source.id)
            if source_key in previous_positions:
                source_x, source_y, source_z = previous_positions[source_key]
            elif len(sources) <= 1:
                source_x = source_y = source_z = 0.0
            else:
                angle = source_index * _GOLDEN_ANGLE
                orbit = 320.0 + math.sqrt(source_index) * 360.0
                source_x = math.cos(angle) * orbit
                source_y = math.sin(angle) * orbit
                source_z = (_stable_unit(f"source:{source.id}:z") - 0.5) * min(360.0, orbit * 0.5)

            serialized_buckets = [
                {
                    "start": bucket.start.isoformat(),
                    "end": bucket.end.isoformat(),
                    "count": int(bucket.count),
                }
                if not isinstance(bucket, dict)
                else bucket
                for bucket in time_buckets_raw
            ]
            source_partition = UniversePartition(
                id=new_id(),
                overview_id=overview_id,
                user_id=user_id,
                source_id=source.id,
                kind="source",
                key=source.id,
                label=source.name,
                x=source_x,
                y=source_y,
                z=source_z,
                radius=source_radius,
                node_count=event_count + entity_count,
                event_count=event_count,
                entity_count=entity_count,
                relation_count=relation_count,
                density=density,
                seed=int(_stable_unit(f"partition:{source.id}") * (2**31 - 1)),
                time_range={
                    "start": serialized_buckets[0]["start"],
                    "end": serialized_buckets[-1]["end"],
                }
                if serialized_buckets
                else {},
                time_buckets=serialized_buckets,
                importance=float(event_count),
            )
            partitions.append(source_partition)

            min_x = min(min_x, source_x - source_radius)
            max_x = max(max_x, source_x + source_radius)
            min_y = min(min_y, source_y - source_radius)
            max_y = max(max_y, source_y + source_radius)
            min_z = min(min_z, source_z - source_radius)
            max_z = max(max_z, source_z + source_radius)

        if not math.isfinite(min_x):
            min_x = min_y = min_z = -600.0
            max_x = max_y = max_z = 600.0
        padding = 140.0
        bounds = {
            "min_x": min_x - padding,
            "min_y": min_y - padding,
            "min_z": min_z - padding,
            "max_x": max_x + padding,
            "max_y": max_y + padding,
            "max_z": max_z + padding,
        }

        session.add_all(partitions)
        await session.flush()
        await session.execute(
            update(UniverseOverview)
            .where(
                UniverseOverview.user_id == user_id,
                UniverseOverview.id != overview_id,
                UniverseOverview.is_active.is_(True),
            )
            .values(is_active=False)
        )
        completed_at = datetime.now(UTC)
        overview.status = "ready"
        overview.is_active = True
        overview.source_count = len(sources)
        overview.partition_count = len(partitions)
        overview.event_count = total_events
        overview.entity_count = total_entities
        overview.node_count = total_events + total_entities
        overview.relation_count = total_relations
        overview.bounds = bounds
        overview.schema_version = 3
        overview.as_of = completed_at
        overview.completed_at = completed_at
        overview.error = None
        for dirty_id, _source_id, revision in dirty_snapshot:
            await session.execute(
                delete(UniverseDirtySource).where(
                    UniverseDirtySource.id == dirty_id,
                    UniverseDirtySource.user_id == user_id,
                    UniverseDirtySource.revision == revision,
                )
            )
        await session.commit()
    except Exception as error:
        await session.rollback()
        try:
            failed = await session.get(UniverseOverview, overview_id)
            if failed is not None:
                failed.status = "failed"
                failed.error = str(error)[:2000]
                failed.is_active = False
                await session.commit()
        except Exception:  # noqa: BLE001 - preserve the original build failure
            await session.rollback()
            log.exception("记录知识宇宙构建失败状态时再次失败 overview=%s", overview_id)
        raise

    # Activation is already committed. Retention cleanup is deliberately
    # best-effort and must not invalidate the newly active snapshot.
    try:
        await _cleanup_old_overviews(session, user_id, overview_id)
    except Exception:  # noqa: BLE001 - a valid active overview remains usable
        await session.rollback()
        log.exception("清理旧知识宇宙快照失败 active_overview=%s", overview_id)
    return overview


async def _prepare_universe_refresh(
    session: AsyncSession,
    *,
    user_id: str,
    source_id: str | None,
    reason: str,
    mark_dirty: bool,
) -> tuple[Job, bool]:
    """Prepare at most one queued refresh for a user while the caller holds the lock."""
    if mark_dirty and source_id is not None:
        dirty = await session.scalar(
            select(UniverseDirtySource).where(
                UniverseDirtySource.user_id == user_id,
                UniverseDirtySource.source_id == source_id,
            )
        )
        if dirty is None:
            session.add(
                UniverseDirtySource(
                    user_id=user_id,
                    source_id=source_id,
                    reason=reason[:64],
                    revision=1,
                )
            )
        else:
            dirty.reason = reason[:64]
            dirty.revision = int(dirty.revision or 0) + 1
            dirty.updated_at = datetime.now(UTC)

    queued_jobs = list(
        (
            await session.execute(
                select(Job).where(
                    Job.type == JobType.INDEX_UNIVERSE,
                    Job.status == JobStatus.QUEUED,
                )
            )
        ).scalars()
    )
    pending = next(
        (job for job in queued_jobs if str((job.payload or {}).get("user_id") or "") == user_id),
        None,
    )
    if pending is not None:
        return pending, False

    pending = Job(
        type=JobType.INDEX_UNIVERSE,
        # A universe refresh spans the whole workspace. Keeping this nullable
        # also prevents one source deletion from cascading away the refresh.
        source_id=None,
        status=JobStatus.QUEUED,
        payload={"user_id": user_id, "reason": reason},
    )
    session.add(pending)
    await session.flush()
    return pending, True


async def enqueue_universe_rebuild(
    session: AsyncSession,
    job_queue: JobQueue,
    *,
    user_id: str,
    reason: str = "manual_rebuild",
) -> Job:
    """Enqueue, or return, the one queued rebuild for the current user."""
    async with _universe_schedule_lock():
        try:
            job, created = await _prepare_universe_refresh(
                session,
                user_id=user_id,
                source_id=None,
                reason=reason,
                mark_dirty=False,
            )
            await session.commit()
            if created:
                await session.refresh(job)
        except Exception:
            await session.rollback()
            raise
    if created:
        await job_queue.enqueue(job.id)
    return job


async def schedule_universe_refresh(
    session: AsyncSession,
    job_queue: JobQueue | None,
    *,
    source_id: str | None,
    reason: str,
) -> list[Job]:
    """Mark data dirty and coalesce one queued follow-up rebuild per local user."""
    created_jobs: list[Job] = []
    scheduled_jobs: list[Job] = []
    async with _universe_schedule_lock():
        try:
            users = list((await session.execute(select(User))).scalars())
            for user in users:
                job, created = await _prepare_universe_refresh(
                    session,
                    user_id=user.id,
                    source_id=source_id,
                    reason=reason,
                    mark_dirty=True,
                )
                scheduled_jobs.append(job)
                if created:
                    created_jobs.append(job)
            await session.commit()
            for job in created_jobs:
                await session.refresh(job)
        except Exception:
            await session.rollback()
            raise
    if job_queue is not None:
        for job in created_jobs:
            await job_queue.enqueue(job.id)
    return scheduled_jobs


async def save_exploration(
    session: AsyncSession,
    *,
    user_id: str,
    query: str,
    source_ids: list[str],
    summary: str,
    events: list[dict],
    entities: list[dict],
    relations: list[dict],
    evidence: list[dict],
) -> tuple[ExplorationSession, ExplorationStep]:
    title = query.strip()[:80] or "新探索"
    exploration = ExplorationSession(
        user_id=user_id,
        title=title,
        source_ids=source_ids,
    )
    session.add(exploration)
    await session.flush()
    step = ExplorationStep(
        session_id=exploration.id,
        query=query,
        summary=summary,
        source_ids=source_ids,
        event_refs=events,
        entity_refs=entities,
        relation_refs=relations,
        evidence_refs=evidence,
    )
    session.add(step)
    await session.commit()
    await session.refresh(exploration)
    await session.refresh(step)
    return exploration, step


async def list_explorations(
    session: AsyncSession, user_id: str, *, limit: int = 20
) -> list[tuple[ExplorationSession, int]]:
    count = func.count(ExplorationStep.id)
    rows = (
        await session.execute(
            select(ExplorationSession, count.label("step_count"))
            .outerjoin(ExplorationStep, ExplorationStep.session_id == ExplorationSession.id)
            .where(ExplorationSession.user_id == user_id)
            .group_by(ExplorationSession.id)
            .order_by(ExplorationSession.updated_at.desc())
            .limit(max(1, min(limit, 100)))
        )
    ).all()
    return [(exploration, int(step_count or 0)) for exploration, step_count in rows]


async def get_exploration(
    session: AsyncSession, user_id: str, exploration_id: str
) -> tuple[ExplorationSession, list[ExplorationStep]]:
    exploration = await session.get(ExplorationSession, exploration_id)
    if exploration is None or exploration.user_id != user_id:
        raise NotFoundError("探索记录不存在")
    steps = list(
        (
            await session.execute(
                select(ExplorationStep)
                .where(ExplorationStep.session_id == exploration.id)
                .order_by(ExplorationStep.created_at)
            )
        ).scalars()
    )
    return exploration, steps


__all__ = [
    "active_overview",
    "enqueue_universe_rebuild",
    "get_exploration",
    "list_explorations",
    "overview_is_stale",
    "rebuild_universe_overview",
    "save_exploration",
    "schedule_universe_refresh",
    "universe_expand",
    "universe_manifest",
    "universe_node_detail",
    "universe_rebuild_is_pending",
    "universe_timeline",
]
