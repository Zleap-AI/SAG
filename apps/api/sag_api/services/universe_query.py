"""知识宇宙的查询侧 —— 从 `universe_service` 抽出。

含概览清单、邻域展开、时间轴与节点详情，以及它们共用的策略与稳定排序辅助。
重建和调度机制留在 `universe_service`（测试会在那里打桩）。

本模块读取模块级的 `settings` 与 `SessionLocal` 绑定；当前没有测试对它们打桩，
但若将来需要（`test_octx_runtime` 即以此方式替换 facade 的 `settings`），在本模块
上做 `monkeypatch.setattr(universe_query, "settings", ...)` 会生效。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal
from sag_api.core.error_taxonomy import ErrorCode
from sag_api.core.errors import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from sag_api.core.logging import get_logger
from sag_api.db.models import (
    Document,
    Job,
    Source,
    UniverseDirtySource,
    UniverseOverview,
    UniversePartition,
)
from sag_api.enums import JobStatus, JobType
from sag_api.sag import EngineManager

_GOLDEN_ANGLE = math.pi * (3 - math.sqrt(5))
log = get_logger("services.universe")


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


async def active_overview(session: AsyncSession, user_id: str) -> UniverseOverview | None:
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
                    + settings.universe_planet_radius_scale * math.log10((source.event_count or 0) + 1),
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
                code=ErrorCode.SNAPSHOT_CHANGED,
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
            code=ErrorCode.SNAPSHOT_CHANGED,
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
                code=ErrorCode.SNAPSHOT_CHANGED,
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
            code=ErrorCode.SNAPSHOT_CHANGED,
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
        chunk = await engine_manager.get_chunk(source.sag_source_config_id, chunk_id, source=source)
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
