"""Aggregate universe overview, bounded expansion, and exploration history."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal
from sag_api.core.errors import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from sag_api.core.logging import get_logger
from sag_api.db.base import new_id
from sag_api.db.models import (
    Document,
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

_GOLDEN_ANGLE = math.pi * (3 - math.sqrt(5))
_UNIVERSE_SCHEDULE_LOCKS: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
log = get_logger("services.universe")


def _universe_policy() -> dict[str, int]:
    return {
        "source_limit": settings.universe_manifest_source_limit,
        "timeline_event_page_size": settings.universe_timeline_event_page_size,
        "event_entity_limit": settings.universe_event_entity_limit,
        "lod_orbit_px": settings.universe_lod_orbit_px,
        "lod_near_px": settings.universe_lod_near_px,
        "lod_deep_px": settings.universe_lod_deep_px,
        "lod_hysteresis_px": settings.universe_lod_hysteresis_px,
        "lod_debounce_ms": settings.universe_lod_debounce_ms,
        "proxy_budget_desktop": settings.universe_proxy_budget_desktop,
        "proxy_budget_mobile": settings.universe_proxy_budget_mobile,
        "node_budget_desktop": settings.universe_node_budget_desktop,
        "node_budget_mobile": settings.universe_node_budget_mobile,
        "edge_budget_desktop": settings.universe_edge_budget_desktop,
        "edge_budget_mobile": settings.universe_edge_budget_mobile,
    }


async def _source_graph_revision(
    *,
    user_id: str,
    source_id: str,
) -> str:
    """Read a graph fence in a fresh short transaction, bypassing ORM identity caches."""
    async with SessionLocal() as revision_session:
        source_state = (
            await revision_session.execute(
                select(
                    Source.updated_at,
                    Source.event_count,
                    Source.chunk_count,
                ).where(Source.id == source_id)
            )
        ).one_or_none()
        if source_state is None:
            raise NotFoundError("信息源不存在")
        overview_id = await revision_session.scalar(
            select(UniverseOverview.id)
        .where(
            UniverseOverview.user_id == user_id,
            UniverseOverview.is_active.is_(True),
            UniverseOverview.status == "ready",
        )
        .order_by(UniverseOverview.created_at.desc())
        )
        dirty_revision = await revision_session.scalar(
            select(UniverseDirtySource.revision).where(
                UniverseDirtySource.user_id == user_id,
                UniverseDirtySource.source_id == source_id,
            )
        )
    raw = "|".join(
        [
            str(overview_id or "none"),
            str(int(dirty_revision or 0)),
            source_state.updated_at.isoformat(),
            str(int(source_state.event_count or 0)),
            str(int(source_state.chunk_count or 0)),
        ]
    )
    return hashlib.blake2b(raw.encode("utf-8"), digest_size=12).hexdigest()


def _stable_unit(value: str) -> float:
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") / float(2**64 - 1)


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
    return {
        (source_id, kind, key): (float(x), float(y), float(z or 0.0))
        for source_id, kind, key, x, y, z in rows
    }


async def _previous_source_stats(
    session: AsyncSession, user_id: str
) -> dict[str, dict[str, Any]]:
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
            if not recompute_all
            and source.id not in dirty_source_ids
            and source.id in previous_stats
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


async def active_overview(
    session: AsyncSession, user_id: str
) -> UniverseOverview | None:
    return await session.scalar(
        select(UniverseOverview)
        .where(
            UniverseOverview.user_id == user_id,
            UniverseOverview.is_active.is_(True),
            UniverseOverview.status == "ready",
        )
        .order_by(UniverseOverview.created_at.desc())
    )


async def overview_is_stale(
    session: AsyncSession,
    user_id: str,
    overview: UniverseOverview | None = None,
) -> bool:
    count = await session.scalar(
        select(func.count(UniverseDirtySource.id)).where(UniverseDirtySource.user_id == user_id)
    )
    if count or overview is None:
        return bool(count)

    source_count = await session.scalar(select(func.count(Source.id)))
    if int(overview.source_count or 0) != int(source_count or 0):
        return True
    undercounted_sources = await session.scalar(
        select(func.count(UniversePartition.id))
        .join(Source, Source.id == UniversePartition.source_id)
        .where(
            UniversePartition.overview_id == overview.id,
            UniversePartition.kind == "source",
            UniversePartition.event_count < Source.event_count,
        )
    )
    return bool(undercounted_sources)


async def universe_rebuild_is_pending(session: AsyncSession, user_id: str) -> bool:
    return bool(
        await session.scalar(
            select(func.count(Job.id)).where(
                Job.type == JobType.INDEX_UNIVERSE,
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                Job.payload["user_id"].as_string() == user_id,
            )
        )
    )


async def universe_manifest(
    session: AsyncSession,
    user_id: str,
) -> dict[str, Any]:
    overview = await active_overview(session, user_id)
    stale = await overview_is_stale(session, user_id, overview)
    if overview is None:
        rebuilding = await universe_rebuild_is_pending(session, user_id)
        latest_failed = await session.scalar(
            select(UniverseOverview.id)
            .where(
                UniverseOverview.user_id == user_id,
                UniverseOverview.status == "failed",
            )
            .order_by(UniverseOverview.created_at.desc())
            .limit(1)
        )
        source_count, event_count = (
            await session.execute(
                select(
                    func.count(Source.id),
                    func.coalesce(func.sum(Source.event_count), 0),
                )
            )
        ).one()
        source_count = int(source_count or 0)
        event_count = int(event_count or 0)
        visible_sources = list(
            (
                await session.execute(
                    select(Source)
                    .order_by(Source.event_count.desc(), Source.created_at, Source.id)
                    .limit(settings.universe_manifest_source_limit)
                )
            ).scalars()
        )
        placeholders: list[dict[str, Any]] = []
        min_x = min_y = min_z = math.inf
        max_x = max_y = max_z = -math.inf
        for index, source in enumerate(visible_sources):
            if len(visible_sources) <= 1:
                x = y = z = 0.0
            else:
                angle = index * _GOLDEN_ANGLE
                orbit = 320.0 + math.sqrt(index) * 360.0
                x = math.cos(angle) * orbit
                y = math.sin(angle) * orbit
                z = (_stable_unit(f"source:{source.id}:z") - 0.5) * min(360.0, orbit * 0.5)
            density = max(0.0, min(1.0, math.log10((source.event_count or 0) + 1) / 6.0))
            radius = max(
                settings.universe_planet_radius_min,
                min(
                    settings.universe_planet_radius_max,
                    settings.universe_planet_radius_min
                    + settings.universe_planet_radius_scale
                    * math.log10((source.event_count or 0) + 1),
                ),
            )
            placeholders.append(
                {
                    "id": f"source:{source.id}",
                    "source_id": source.id,
                    "parent_id": None,
                    "kind": "source",
                    "key": source.id,
                    "label": source.name,
                    "x": x,
                    "y": y,
                    "z": z,
                    "radius": radius,
                    "node_count": int(source.event_count or 0),
                    "event_count": int(source.event_count or 0),
                    "entity_count": 0,
                    "relation_count": 0,
                    "density": density,
                    "time_buckets": [],
                    "importance": float(source.event_count or 0),
                }
            )
            min_x, max_x = min(min_x, x - radius), max(max_x, x + radius)
            min_y, max_y = min(min_y, y - radius), max(max_y, y + radius)
            min_z, max_z = min(min_z, z - radius), max(max_z, z + radius)
        if not math.isfinite(min_x):
            min_x = min_y = min_z = -600.0
            max_x = max_y = max_z = 600.0
        return {
            "version": None,
            "status": (
                "empty"
                if source_count == 0
                else "building"
                if rebuilding
                else "failed"
                if latest_failed is not None
                else "stale"
            ),
            "stale": source_count > 0,
            "as_of": None,
            "bounds": {
                "min_x": min_x,
                "min_y": min_y,
                "min_z": min_z,
                "max_x": max_x,
                "max_y": max_y,
                "max_z": max_z,
            },
            "partitions": placeholders,
            "counts": {
                "sources": source_count,
                "partitions": len(placeholders),
                "events": event_count,
                "entities": 0,
                "nodes": event_count,
                "relations": 0,
            },
            "policy": _universe_policy(),
        }
    partitions = list(
        (
            await session.execute(
                select(UniversePartition)
                .where(
                    UniversePartition.overview_id == overview.id,
                    UniversePartition.kind == "source",
                )
                .order_by(UniversePartition.importance.desc(), UniversePartition.source_id)
                .limit(settings.universe_manifest_source_limit)
            )
        ).scalars()
    )
    stale = stale or int(overview.schema_version or 1) < 3
    rebuilding = stale and await universe_rebuild_is_pending(session, user_id)
    return {
        "version": overview.id,
        "status": "building" if rebuilding else "stale" if stale else "ready",
        "stale": stale,
        "as_of": overview.as_of or overview.completed_at,
        "bounds": overview.bounds or {},
        "partitions": partitions,
        "counts": {
            "sources": overview.source_count,
            "partitions": len(partitions),
            "events": overview.event_count,
            "entities": overview.entity_count,
            "nodes": overview.node_count,
            "relations": overview.relation_count,
        },
        "policy": _universe_policy(),
    }


async def universe_expand(
    session: AsyncSession,
    engine_manager: EngineManager,
    *,
    user_id: str,
    source_id: str,
    node_kind: str,
    node_id: str,
    limit: int,
    cursor: str | None,
    snapshot_id: str | None,
    after: datetime | None,
    before: datetime | None,
) -> dict[str, Any]:
    """Resolve a source-qualified node and return one bounded factual hop."""
    source = await session.get(Source, source_id)
    if source is None:
        raise NotFoundError("信息源不存在")
    source_revision = await _source_graph_revision(
        user_id=user_id,
        source_id=source.id,
    )
    hard_limit = 8 if node_kind == "event" else 4
    try:
        async with asyncio.timeout(8.0):
            expansion = await engine_manager.universe_expand(
                source.sag_source_config_id,
                node_kind,
                node_id,
                source=source,
                limit=max(1, min(int(limit), hard_limit)),
                cursor=cursor,
                snapshot_id=snapshot_id,
                source_revision=source_revision,
                after=after,
                before=before,
            )
    except TimeoutError as error:
        raise ServiceUnavailableError("知识邻域查询超时，请缩小时间范围后重试") from error
    except ValueError as error:
        if "revision" in str(error):
            raise ConflictError(
                "知识图谱数据已更新，请重新开始当前探索",
                code="snapshot_changed",
            ) from error
        raise ValidationError("无效或不匹配的知识宇宙游标") from error
    except TypeError as error:
        raise ValidationError("无效或不匹配的知识宇宙游标") from error
    if expansion is None:
        raise NotFoundError("知识星点已不存在")
    committed_revision = await _source_graph_revision(
        user_id=user_id,
        source_id=source.id,
    )
    if committed_revision != source_revision:
        raise ConflictError(
            "知识图谱数据已更新，请重新开始当前探索",
            code="snapshot_changed",
        )

    anchor = {
        **expansion.anchor,
        "source_id": source.id,
        "importance": 1.0,
    }
    nodes = [
        {
            **neighbor,
            "source_id": source.id,
            "importance": max(
                0.2,
                min(
                    1.0,
                    float(neighbor.get("importance", neighbor.get("weight", 0.5))),
                ),
            ),
        }
        for neighbor in expansion.neighbors
    ]
    relations = [
        {
            **relation,
            "source_id": source.id,
            "weight": float(relation.get("weight", 1.0)),
            "description": str(relation.get("description", "")),
        }
        for relation in expansion.relations
    ]
    page_signature = json.dumps(
        {
            "source_id": source.id,
            "source_revision": source_revision,
            "as_of": expansion.as_of.isoformat(),
            "node_kind": node_kind,
            "node_id": node_id,
            "request_cursor": cursor,
            "limit": limit,
            "anchor": anchor,
            "nodes": nodes,
            "relations": relations,
            "page": {
                "returned": expansion.returned,
                "has_more": expansion.has_more,
                "next_cursor": expansion.next_cursor,
            },
        },
        ensure_ascii=True,
        allow_nan=False,
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    )
    page_id = hashlib.blake2b(
        page_signature.encode("utf-8"),
        digest_size=16,
    ).hexdigest()
    return {
        "source_id": source.id,
        "source_revision": source_revision,
        "snapshot_id": expansion.snapshot_id,
        "request_cursor": cursor,
        "page_id": page_id,
        "bundle_id": f"{source.id}:{node_kind}:{node_id}:{page_id}",
        "anchor": anchor,
        "nodes": nodes,
        "relations": relations,
        "page": {
            "returned": expansion.returned,
            "has_more": expansion.has_more,
            "next_cursor": expansion.next_cursor,
        },
        "as_of": expansion.as_of,
    }


async def universe_timeline(
    session: AsyncSession,
    engine_manager: EngineManager,
    *,
    user_id: str,
    source_id: str,
    limit: int,
    direction: str,
    cursor: str | None,
    snapshot_id: str | None,
) -> dict[str, Any]:
    """Load one stable, recent-to-old source timeline page with bounded context."""
    source = await session.get(Source, source_id)
    if source is None:
        raise NotFoundError("信息源不存在")
    source_revision = await _source_graph_revision(
        user_id=user_id,
        source_id=source.id,
    )
    try:
        async with asyncio.timeout(8.0):
            page = await engine_manager.universe_timeline(
                source.sag_source_config_id,
                source=source,
                limit=max(1, min(int(limit), 24)),
                entity_limit=settings.universe_event_entity_limit,
                direction=direction,
                cursor=cursor,
                snapshot_id=snapshot_id,
                source_revision=source_revision,
            )
    except TimeoutError as error:
        raise ServiceUnavailableError("知识时间轴查询超时，请稍后重试") from error
    except ValueError as error:
        if "revision" in str(error):
            raise ConflictError(
                "知识图谱数据已更新，请刷新时间轴后继续",
                code="snapshot_changed",
            ) from error
        raise ValidationError("无效或不匹配的知识时间轴游标") from error
    except TypeError as error:
        raise ValidationError("无效或不匹配的知识时间轴游标") from error
    committed_revision = await _source_graph_revision(
        user_id=user_id,
        source_id=source.id,
    )
    if committed_revision != source_revision:
        raise ConflictError(
            "知识图谱数据已更新，请刷新时间轴后继续",
            code="snapshot_changed",
        )
    page_signature = "|".join(
        [
            source.id,
            source_revision,
            page.as_of.isoformat(),
            direction,
            cursor or "root",
            str(limit),
            *(bundle.bundle_id for bundle in page.bundles),
        ]
    )
    page_id = hashlib.blake2b(
        page_signature.encode("utf-8"),
        digest_size=16,
    ).hexdigest()
    bundles = [
        {
            "bundle_id": f"{source.id}:{bundle.bundle_id}",
            "ordinal": bundle.ordinal,
            "event": {**bundle.event, "source_id": source.id},
            "nodes": [{**node, "source_id": source.id} for node in bundle.nodes],
            "relations": [{**relation, "source_id": source.id} for relation in bundle.relations],
            "neighbor_page": {
                "total_unique": bundle.neighbor_total,
                "returned_unique": bundle.neighbor_returned,
                "complete": bundle.complete,
                "next_cursor": bundle.neighbor_next_cursor,
            },
            "cursor_before": bundle.cursor_before,
            "cursor_after": bundle.cursor_after,
        }
        for bundle in page.bundles
    ]
    returned_node_keys = {(bundle["event"]["kind"], bundle["event"]["id"]) for bundle in bundles}
    returned_node_keys.update((node["kind"], node["id"]) for bundle in bundles for node in bundle["nodes"])
    return {
        "source_id": source.id,
        "source_revision": source_revision,
        "snapshot_id": page.snapshot_id,
        "request_direction": direction,
        "request_cursor": cursor,
        "page_id": page_id,
        "bundles": bundles,
        "total_events": page.total_events,
        "page": {
            "returned_bundles": len(page.bundles),
            "returned_unique_nodes": len(returned_node_keys),
            "returned_relations": sum(len(bundle["relations"]) for bundle in bundles),
            "direction": page.direction,
            "has_newer": page.has_newer,
            "newer_cursor": page.newer_cursor,
            "has_older": page.has_older,
            "older_cursor": page.older_cursor,
            "has_more": page.has_more,
            "next_cursor": page.next_cursor,
        },
        "as_of": page.as_of,
    }


async def universe_node_detail(
    session: AsyncSession,
    engine_manager: EngineManager,
    node_kind: str,
    node_id: str,
    *,
    source_id: str,
) -> dict[str, Any]:
    source = await session.get(Source, source_id)
    if source is None:
        raise NotFoundError("信息源不存在")
    try:
        async with asyncio.timeout(8.0):
            detail = await engine_manager.universe_node_detail(
                source.sag_source_config_id,
                node_kind,
                node_id,
                source=source,
            )
    except TimeoutError as error:
        raise ServiceUnavailableError("知识星点读取超时，请稍后重试") from error
    if detail is None:
        raise NotFoundError("知识星点已不存在")

    evidence = None
    chunk_id = detail.get("chunk_id")
    if chunk_id:
        chunk = await engine_manager.get_chunk(
            source.sag_source_config_id, chunk_id, source=source
        )
        if chunk is not None:
            source_ref_id = detail.get("source_ref_id")
            document = await session.scalar(
                select(Document).where(
                    Document.source_id == source.id,
                    Document.sag_source_id == source_ref_id,
                )
            )
            evidence = {
                "source_id": source.id,
                "source_name": source.name,
                "document_id": document.id if document else None,
                "document_name": document.filename if document else None,
                "chunk_id": chunk.chunk_id,
                "heading": chunk.heading,
                "content": chunk.content,
            }

    return {
        "id": node_id,
        "kind": node_kind,
        "source_id": source.id,
        "source_name": source.name,
        "label": detail.get("label") or "未命名星点",
        "description": detail.get("description") or "",
        "category": detail.get("category", ""),
        "start_time": detail.get("start_time"),
        "evidence": evidence,
    }


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
        (
            job
            for job in queued_jobs
            if str((job.payload or {}).get("user_id") or "") == user_id
        ),
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
