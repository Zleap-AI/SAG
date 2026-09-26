"""知识宇宙（universe）读侧 —— 从 `EngineManager` 拆出的聚合查询。

承接概览统计、时间轴、邻域展开与节点详情。这些方法彼此协作（时间轴与展开
复用 `_universe_event_bundles` / `_universe_entity_event_counts`），但与引擎
生命周期无关，只经由 `_EngineAccess` 取引擎槽与会话工厂。

游标编解码复用 `sag.universe_cursor`，与写侧共用同一套签名协议。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sag_api.sag.dto import (
    UniverseExpansionInfo,
    UniverseSourceStatsInfo,
    UniverseTimeBucketInfo,
    UniverseTimelineBundleInfo,
    UniverseTimelineInfo,
)
from sag_api.sag.universe_cursor import (
    cursor_datetime,
    cursor_float,
    cursor_int,
    database_time,
    decode_universe_cursor,
    encode_universe_cursor,
    universe_cursor_scope,
    utc_time,
    weight,
)

if TYPE_CHECKING:
    from sag_api.db.models import Source
    from sag_api.sag.engine_access import EngineAccess


class UniverseReader:
    """知识宇宙的只读聚合视图。"""

    def __init__(self, access: EngineAccess) -> None:
        self._access = access

    async def universe_overview_stats(
        self,
        source_config_id: str,
        *,
        source: Source | None = None,
        bucket_count: int = 8,
        category_limit: int = 8,
    ) -> UniverseSourceStatsInfo:
        """Return aggregate-only statistics; never materialize event/entity rows."""
        await self._access.slot(source_config_id, source)
        from sqlalchemy import case, func, select
        from zleap.sag.db.models import Entity, EventEntity, SourceEvent

        buckets = max(1, min(int(bucket_count), 24))
        categories = max(0, min(int(category_limit), 16))
        event_time = func.coalesce(SourceEvent.start_time, SourceEvent.created_time)
        active_event = SourceEvent.status.is_(None) | (SourceEvent.status != "DELETED")
        sf = await self._access.relational_session_factory(source_config_id)
        async with sf() as session:
            event_count, min_time, max_time = (
                await session.execute(
                    select(
                        func.count(SourceEvent.id),
                        func.min(event_time),
                        func.max(event_time),
                    ).where(
                        SourceEvent.data_source_id == source_config_id,
                        active_event,
                    )
                )
            ).one()
            entity_count = int(
                (
                    await session.execute(
                        select(func.count(Entity.id)).where(Entity.data_source_id == source_config_id)
                    )
                ).scalar_one()
                or 0
            )
            unique_relation_rows = (
                select(EventEntity.event_id, EventEntity.entity_id)
                .join(SourceEvent, SourceEvent.id == EventEntity.event_id)
                .join(Entity, Entity.id == EventEntity.entity_id)
                .where(
                    SourceEvent.data_source_id == source_config_id,
                    Entity.data_source_id == source_config_id,
                    active_event,
                )
                .distinct()
                .subquery()
            )
            relation_count = int(
                (await session.execute(select(func.count()).select_from(unique_relation_rows))).scalar_one() or 0
            )
            category_rows = []
            if categories:
                category = func.coalesce(func.nullif(SourceEvent.category, ""), "未分类")
                category_rows = (
                    await session.execute(
                        select(category, func.count(SourceEvent.id).label("count"))
                        .where(
                            SourceEvent.data_source_id == source_config_id,
                            active_event,
                        )
                        .group_by(category)
                        .order_by(func.count(SourceEvent.id).desc(), category.asc())
                        .limit(categories)
                    )
                ).all()

            time_buckets: list[UniverseTimeBucketInfo] = []
            if min_time is not None and max_time is not None:
                if max_time <= min_time:
                    time_buckets.append(
                        UniverseTimeBucketInfo(
                            start=min_time,
                            end=max_time,
                            count=int(event_count or 0),
                        )
                    )
                else:
                    step = (max_time - min_time) / buckets
                    boundaries = [min_time + step * index for index in range(buckets + 1)]
                    count_columns = []
                    for index in range(buckets):
                        lower = boundaries[index]
                        upper = boundaries[index + 1]
                        condition = event_time >= lower
                        condition &= event_time <= upper if index == buckets - 1 else event_time < upper
                        count_columns.append(func.sum(case((condition, 1), else_=0)).label(f"bucket_{index}"))
                    bucket_values = (
                        await session.execute(
                            select(*count_columns).where(
                                SourceEvent.data_source_id == source_config_id,
                                active_event,
                            )
                        )
                    ).one()
                    time_buckets = [
                        UniverseTimeBucketInfo(
                            start=boundaries[index],
                            end=boundaries[index + 1],
                            count=int(bucket_values[index] or 0),
                        )
                        for index in range(buckets)
                    ]

        return UniverseSourceStatsInfo(
            event_count=int(event_count or 0),
            entity_count=entity_count,
            relation_count=relation_count,
            category_counts={str(label or "未分类"): int(count or 0) for label, count in category_rows},
            time_buckets=time_buckets,
        )

    async def _universe_entity_event_counts(
        self,
        session: Any,
        source_config_id: str,
        entity_ids: list[str],
        *,
        as_of_db: datetime,
    ) -> dict[str, int]:
        """Count factual source events for a batch of entities at one snapshot."""
        if not entity_ids:
            return {}
        from sqlalchemy import func, select
        from zleap.sag.db.models import EventEntity, SourceEvent

        event_time = func.coalesce(SourceEvent.start_time, SourceEvent.created_time)
        return {
            str(entity_id): int(count or 0)
            for entity_id, count in (
                await session.execute(
                    select(
                        EventEntity.entity_id,
                        func.count(func.distinct(EventEntity.event_id)),
                    )
                    .join(SourceEvent, SourceEvent.id == EventEntity.event_id)
                    .where(
                        EventEntity.entity_id.in_(entity_ids),
                        EventEntity.created_time <= as_of_db,
                        SourceEvent.data_source_id == source_config_id,
                        event_time <= as_of_db,
                        SourceEvent.created_time <= as_of_db,
                        (SourceEvent.status.is_(None) | (SourceEvent.status != "DELETED")),
                    )
                    .group_by(EventEntity.entity_id)
                )
            ).all()
        }

    async def _universe_event_bundles(
        self,
        session: Any,
        source_config_id: str,
        events: list[dict[str, Any]],
        *,
        as_of_db: datetime,
        entity_limit: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Hydrate event rows and their factual entity relations without N+1 reads."""
        from sqlalchemy import func, select
        from zleap.sag.db.models import Entity, EventEntity

        event_ids = [str(event["id"]) for event in events]
        if not event_ids:
            return [], []

        bounded_entities = max(4, min(int(entity_limit), 8))
        # The API unit is one factual event/entity pair. Normalize at the query
        # boundary so totals, ranking and returned relationships cannot diverge.
        unique_relations = (
            select(
                EventEntity.event_id.label("event_id"),
                EventEntity.entity_id.label("entity_id"),
                func.max(EventEntity.weight).label("weight"),
                func.max(EventEntity.description).label("relation_description"),
            )
            .select_from(EventEntity)
            .join(Entity, Entity.id == EventEntity.entity_id)
            .where(
                EventEntity.event_id.in_(event_ids),
                EventEntity.created_time <= as_of_db,
                Entity.data_source_id == source_config_id,
                Entity.created_time <= as_of_db,
            )
            .group_by(EventEntity.event_id, EventEntity.entity_id)
            .subquery()
        )
        ranked_relations = select(
            unique_relations.c.event_id,
            unique_relations.c.entity_id,
            unique_relations.c.weight,
            unique_relations.c.relation_description,
            func.count().over(partition_by=unique_relations.c.event_id).label("relation_total"),
            func.row_number()
            .over(
                partition_by=unique_relations.c.event_id,
                order_by=(
                    unique_relations.c.weight.desc(),
                    unique_relations.c.entity_id.asc(),
                ),
            )
            .label("relation_rank"),
        ).subquery()
        relation_rows = (
            await session.execute(
                select(
                    ranked_relations.c.event_id,
                    ranked_relations.c.entity_id,
                    ranked_relations.c.weight,
                    ranked_relations.c.relation_description,
                    ranked_relations.c.relation_total,
                    ranked_relations.c.relation_rank,
                    Entity.name,
                    Entity.type,
                    Entity.description.label("entity_description"),
                )
                .join(Entity, Entity.id == ranked_relations.c.entity_id)
                .where(
                    ranked_relations.c.relation_rank <= bounded_entities,
                    Entity.data_source_id == source_config_id,
                )
                .order_by(
                    ranked_relations.c.event_id,
                    ranked_relations.c.relation_rank,
                )
            )
        ).all()

        entity_ids = sorted({str(row.entity_id) for row in relation_rows})
        entity_counts = await self._access.universe_entity_event_counts(
            session,
            source_config_id,
            entity_ids,
            as_of_db=as_of_db,
        )

        event_counts: dict[str, int] = {}
        entity_nodes: dict[str, dict[str, Any]] = {}
        relations: list[dict[str, Any]] = []
        for row in relation_rows:
            event_id = str(row.event_id)
            entity_id = str(row.entity_id)
            event_counts[event_id] = int(row.relation_total or 0)
            entity_nodes.setdefault(
                entity_id,
                {
                    "id": entity_id,
                    "kind": "entity",
                    "label": row.name or "未命名实体",
                    "description": str(row.entity_description or "")[:800],
                    "category": row.type or "实体",
                    "chunk_id": None,
                    "start_time": None,
                    "importance": max(
                        0.3,
                        min(1.0, 0.42 + weight(row.weight) * 0.08),
                    ),
                    "related_count": entity_counts.get(entity_id, 0),
                    "state": "active",
                },
            )
            relations.append(
                {
                    "from_id": event_id,
                    "to_id": entity_id,
                    "kind": "mentions",
                    "weight": weight(row.weight),
                    "description": str(row.relation_description or "")[:240],
                }
            )

        event_nodes = [
            {
                "id": str(event["id"]),
                "kind": "event",
                "label": event.get("title") or "未命名事件",
                "description": str(event.get("summary") or "")[:800],
                "category": event.get("category") or "事件",
                "chunk_id": event.get("chunk_id"),
                "start_time": utc_time(event.get("start_time") or event.get("event_time")),
                "importance": max(
                    0.4,
                    min(
                        1.0,
                        0.5 + math.log1p(event_counts.get(str(event["id"]), 0)) * 0.08,
                    ),
                ),
                "related_count": event_counts.get(str(event["id"]), 0),
                "state": "active",
            }
            for event in events
        ]
        return [*event_nodes, *entity_nodes.values()], relations

    async def universe_timeline(
        self,
        source_config_id: str,
        *,
        source_revision: str,
        source: Source | None = None,
        limit: int = 6,
        entity_limit: int = 8,
        direction: str = "older",
        cursor: str | None = None,
        snapshot_id: str | None = None,
    ) -> UniverseTimelineInfo:
        """Return a bidirectional, snapshot-stable event-time page.

        Results always use canonical newest-to-oldest order. ``direction`` only
        selects which adjacent page to read from the supplied boundary cursor.
        """
        await self._access.slot(source_config_id, source)
        from sqlalchemy import and_, func, or_, select
        from zleap.sag.db.models import SourceEvent

        bounded_limit = max(1, min(int(limit), 24))
        bounded_entities = max(4, min(int(entity_limit), 8))
        if direction not in {"older", "newer"}:
            raise ValueError("invalid universe timeline direction")
        if direction == "newer" and cursor is None:
            raise ValueError("newer universe timeline reads require a cursor")
        if not source_revision:
            raise ValueError("universe source revision is required")
        cursor_payload = decode_universe_cursor(cursor, self._access.settings.secret_key) if cursor else None
        snapshot_payload = (
            decode_universe_cursor(snapshot_id, self._access.settings.secret_key) if snapshot_id else None
        )
        if cursor_payload and snapshot_payload is None:
            raise ValueError("universe timeline cursor requires a snapshot")
        if cursor_payload and (
            cursor_payload.get("scope") != universe_cursor_scope(source_config_id)
            or cursor_payload.get("kind") != "source-timeline"
        ):
            raise ValueError("universe cursor does not match its source timeline")
        if snapshot_payload and (
            snapshot_payload.get("scope") != universe_cursor_scope(source_config_id)
            or snapshot_payload.get("kind") != "source-read-snapshot"
        ):
            raise ValueError("universe snapshot does not match its source")
        if cursor_payload and cursor_payload.get("revision") != source_revision:
            raise ValueError("universe timeline revision changed")
        if snapshot_payload and snapshot_payload.get("revision") != source_revision:
            raise ValueError("universe timeline revision changed")

        if cursor_payload:
            as_of = cursor_datetime(cursor_payload, "as_of")
        elif snapshot_payload:
            as_of = cursor_datetime(snapshot_payload, "as_of")
        else:
            as_of = datetime.now(UTC)
        if snapshot_payload and cursor_datetime(snapshot_payload, "as_of") != as_of:
            raise ValueError("universe cursor does not belong to this snapshot")
        stable_snapshot_id = snapshot_id or encode_universe_cursor(
            {
                "v": 2,
                "scope": universe_cursor_scope(source_config_id),
                "kind": "source-read-snapshot",
                "as_of": utc_time(as_of).isoformat(),
                "revision": source_revision,
            },
            self._access.settings.secret_key,
        )
        as_of_db = database_time(as_of)
        event_time = func.coalesce(SourceEvent.start_time, SourceEvent.created_time)
        filters = [
            SourceEvent.data_source_id == source_config_id,
            (SourceEvent.status.is_(None) | (SourceEvent.status != "DELETED")),
            event_time <= as_of_db,
            SourceEvent.created_time <= as_of_db,
        ]
        # Canonical exploration order: newest first, then the extractor's
        # source-wide narrative rank, then id. Rank is what makes a source whose
        # events all share one instant (an imported book) explorable in reading
        # order instead of uuid order — the counting axis keys on this sequence.
        if cursor_payload:
            boundary_time = database_time(cursor_datetime(cursor_payload, "time"))
            boundary_rank = cursor_int(cursor_payload, "rank")
            boundary_id = str(cursor_payload.get("id") or "")
            if boundary_time is None or not boundary_id:
                raise ValueError("invalid universe cursor")
            if direction == "older":
                filters.append(
                    or_(
                        event_time < boundary_time,
                        and_(event_time == boundary_time, SourceEvent.rank > boundary_rank),
                        and_(
                            event_time == boundary_time,
                            SourceEvent.rank == boundary_rank,
                            SourceEvent.id > boundary_id,
                        ),
                    )
                )
            else:
                filters.append(
                    or_(
                        event_time > boundary_time,
                        and_(event_time == boundary_time, SourceEvent.rank < boundary_rank),
                        and_(
                            event_time == boundary_time,
                            SourceEvent.rank == boundary_rank,
                            SourceEvent.id < boundary_id,
                        ),
                    )
                )

        ordering = (
            (event_time.desc(), SourceEvent.rank.asc(), SourceEvent.id.asc())
            if direction == "older"
            else (event_time.asc(), SourceEvent.rank.desc(), SourceEvent.id.desc())
        )

        sf = await self._access.relational_session_factory(source_config_id)
        async with sf() as session:
            event_rows = (
                await session.execute(
                    select(
                        SourceEvent.id,
                        SourceEvent.title,
                        SourceEvent.summary,
                        SourceEvent.category,
                        SourceEvent.chunk_id,
                        SourceEvent.start_time,
                        SourceEvent.rank,
                        event_time.label("event_time"),
                    )
                    .where(*filters)
                    .order_by(*ordering)
                    .limit(bounded_limit + 1)
                )
            ).all()
            has_directional_more = len(event_rows) > bounded_limit
            selected_rows = event_rows[:bounded_limit]
            page = selected_rows if direction == "older" else list(reversed(selected_rows))

            # Ordinals anchor the client's counting axis: within this snapshot,
            # an event's depth is its position in the canonical order. One count
            # for the page head; the page itself is contiguous in that order.
            base_filters = [
                SourceEvent.data_source_id == source_config_id,
                (SourceEvent.status.is_(None) | (SourceEvent.status != "DELETED")),
                event_time <= as_of_db,
                SourceEvent.created_time <= as_of_db,
            ]
            total_events = int(
                await session.scalar(select(func.count()).select_from(SourceEvent).where(*base_filters)) or 0
            )
            first_ordinal = 0
            head = page[0] if page else None
            if head is not None:
                head_time = head.event_time
                head_rank = int(head.rank or 0)
                head_id = str(head.id)
                first_ordinal = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(SourceEvent)
                        .where(
                            *base_filters,
                            or_(
                                event_time > head_time,
                                and_(event_time == head_time, SourceEvent.rank < head_rank),
                                and_(
                                    event_time == head_time,
                                    SourceEvent.rank == head_rank,
                                    SourceEvent.id < head_id,
                                ),
                            ),
                        )
                    )
                    or 0
                )

            bundle_nodes, bundle_relations = await self._access.universe_event_bundles(
                session,
                source_config_id,
                [
                    {
                        "id": str(row.id),
                        "title": row.title,
                        "summary": row.summary,
                        "category": row.category,
                        "chunk_id": row.chunk_id,
                        "start_time": row.start_time,
                        "event_time": row.event_time,
                    }
                    for row in page
                ],
                as_of_db=as_of_db,
                entity_limit=bounded_entities,
            )

        nodes_by_key = {(str(node.get("kind") or ""), str(node.get("id") or "")): node for node in bundle_nodes}
        relations_by_event: dict[str, list[dict[str, Any]]] = {}
        for relation in bundle_relations:
            relations_by_event.setdefault(str(relation.get("from_id") or ""), []).append(relation)

        def boundary_cursor(row: Any) -> str:
            return encode_universe_cursor(
                {
                    "v": 2,
                    "scope": universe_cursor_scope(source_config_id),
                    "kind": "source-timeline",
                    "as_of": utc_time(as_of).isoformat(),
                    "revision": source_revision,
                    "time": utc_time(row.event_time).isoformat(),
                    "rank": int(row.rank or 0),
                    "id": row.id,
                },
                self._access.settings.secret_key,
            )

        has_newer = has_directional_more if direction == "newer" else cursor is not None
        has_older = has_directional_more if direction == "older" else cursor is not None
        bundles: list[UniverseTimelineBundleInfo] = []
        for index, row in enumerate(page):
            event_id = str(row.id)
            event_node = nodes_by_key.get(("event", event_id))
            if event_node is None:
                continue
            relations = relations_by_event.get(event_id, [])
            seen_entity_ids: set[str] = set()
            entity_nodes: list[dict[str, Any]] = []
            for relation in relations:
                entity_id = str(relation.get("to_id") or "")
                if not entity_id or entity_id in seen_entity_ids:
                    continue
                entity_node = nodes_by_key.get(("entity", entity_id))
                if entity_node is None:
                    continue
                seen_entity_ids.add(entity_id)
                entity_nodes.append(entity_node)
            row_cursor = boundary_cursor(row)
            cursor_before = row_cursor if index > 0 or has_newer else None
            cursor_after = row_cursor if index < len(page) - 1 or has_older else None
            neighbor_total = max(0, int(event_node.get("related_count") or 0))
            neighbor_complete = len(entity_nodes) == neighbor_total
            neighbor_next_cursor = None
            if not neighbor_complete and relations:
                last_relation = relations[-1]
                neighbor_next_cursor = encode_universe_cursor(
                    {
                        "v": 2,
                        "scope": universe_cursor_scope(source_config_id),
                        "kind": "source-expand",
                        "node_kind": "event",
                        "node": event_id,
                        "as_of": utc_time(as_of).isoformat(),
                        "revision": source_revision,
                        "weight": format(
                            weight(last_relation.get("weight")),
                            ".17g",
                        ),
                        "id": str(last_relation.get("to_id") or ""),
                    },
                    self._access.settings.secret_key,
                )
            bundles.append(
                UniverseTimelineBundleInfo(
                    bundle_id=f"event:{event_id}",
                    ordinal=first_ordinal + index,
                    event=event_node,
                    nodes=entity_nodes,
                    relations=relations,
                    neighbor_total=neighbor_total,
                    neighbor_returned=len(entity_nodes),
                    complete=neighbor_complete,
                    neighbor_next_cursor=neighbor_next_cursor,
                    cursor_before=cursor_before,
                    cursor_after=cursor_after,
                )
            )

        newer_cursor = bundles[0].cursor_before if bundles else cursor if direction == "older" else None
        older_cursor = bundles[-1].cursor_after if bundles else cursor if direction == "newer" else None
        next_cursor = older_cursor if direction == "older" else newer_cursor

        return UniverseTimelineInfo(
            bundles=bundles,
            total_events=total_events,
            snapshot_id=stable_snapshot_id,
            direction=direction,
            has_newer=newer_cursor is not None,
            newer_cursor=newer_cursor,
            has_older=older_cursor is not None,
            older_cursor=older_cursor,
            has_more=next_cursor is not None,
            next_cursor=next_cursor,
            as_of=utc_time(as_of),
        )

    async def universe_expand(
        self,
        source_config_id: str,
        node_kind: str,
        node_id: str,
        *,
        source_revision: str,
        source: Source | None = None,
        limit: int = 4,
        cursor: str | None = None,
        snapshot_id: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
    ) -> UniverseExpansionInfo | None:
        """Read one explicit hop with a hard cap and stable keyset cursor."""
        await self._access.slot(source_config_id, source)
        from sqlalchemy import and_, func, or_, select
        from zleap.sag.db.models import Entity, EventEntity, SourceEvent

        hard_limit = 8 if node_kind == "event" else 4
        bounded_limit = max(1, min(int(limit), hard_limit))
        if not source_revision:
            raise ValueError("universe source revision is required")
        cursor_payload = decode_universe_cursor(cursor, self._access.settings.secret_key) if cursor else None
        snapshot_payload = (
            decode_universe_cursor(snapshot_id, self._access.settings.secret_key) if snapshot_id else None
        )
        if cursor_payload and snapshot_payload is None:
            raise ValueError("universe expansion cursor requires a snapshot")
        if cursor_payload and (
            cursor_payload.get("scope") != universe_cursor_scope(source_config_id)
            or cursor_payload.get("kind") != "source-expand"
            or cursor_payload.get("node_kind") != node_kind
            or cursor_payload.get("node") != node_id
        ):
            raise ValueError("universe cursor does not match its anchor")
        if snapshot_payload and (
            snapshot_payload.get("scope") != universe_cursor_scope(source_config_id)
            or snapshot_payload.get("kind") != "source-read-snapshot"
        ):
            raise ValueError("universe snapshot does not match its source")
        if cursor_payload and cursor_payload.get("revision") != source_revision:
            raise ValueError("universe expansion revision changed")
        if snapshot_payload and snapshot_payload.get("revision") != source_revision:
            raise ValueError("universe expansion revision changed")

        if snapshot_payload:
            as_of = cursor_datetime(snapshot_payload, "as_of")
        elif cursor_payload:
            as_of = cursor_datetime(cursor_payload, "as_of")
        else:
            as_of = datetime.now(UTC)
        if cursor_payload and cursor_datetime(cursor_payload, "as_of") != as_of:
            raise ValueError("universe cursor does not belong to this snapshot")
        stable_snapshot_id = snapshot_id or encode_universe_cursor(
            {
                "v": 2,
                "scope": universe_cursor_scope(source_config_id),
                "kind": "source-read-snapshot",
                "as_of": utc_time(as_of).isoformat(),
                "revision": source_revision,
            },
            self._access.settings.secret_key,
        )

        sf = await self._access.relational_session_factory(source_config_id)
        async with sf() as session:
            as_of_db = database_time(as_of)
            if node_kind == "event":
                event_time = func.coalesce(
                    SourceEvent.start_time,
                    SourceEvent.created_time,
                )
                anchor = (
                    await session.execute(
                        select(
                            SourceEvent.id,
                            SourceEvent.title,
                            SourceEvent.summary,
                            SourceEvent.category,
                            SourceEvent.chunk_id,
                            SourceEvent.start_time,
                        ).where(
                            SourceEvent.id == node_id,
                            SourceEvent.data_source_id == source_config_id,
                            (SourceEvent.status.is_(None) | (SourceEvent.status != "DELETED")),
                            event_time <= as_of_db,
                            SourceEvent.created_time <= as_of_db,
                        )
                    )
                ).one_or_none()
                if anchor is None:
                    return None
                unique_event_relations = (
                    select(
                        EventEntity.entity_id.label("entity_id"),
                        func.max(EventEntity.weight).label("weight"),
                        func.max(EventEntity.description).label("relation_description"),
                    )
                    .where(
                        EventEntity.event_id == node_id,
                        EventEntity.created_time <= as_of_db,
                    )
                    .group_by(EventEntity.entity_id)
                    .subquery()
                )
                related_count = int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(unique_event_relations)
                            .join(Entity, Entity.id == unique_event_relations.c.entity_id)
                            .where(
                                Entity.data_source_id == source_config_id,
                                Entity.created_time <= as_of_db,
                            )
                        )
                    ).scalar_one()
                    or 0
                )
                filters = []
                if cursor_payload:
                    last_weight = cursor_float(cursor_payload, "weight")
                    last_id = str(cursor_payload.get("id") or "")
                    if not last_id:
                        raise ValueError("invalid universe cursor")
                    filters.append(
                        or_(
                            unique_event_relations.c.weight < last_weight,
                            and_(
                                unique_event_relations.c.weight == last_weight,
                                unique_event_relations.c.entity_id > last_id,
                            ),
                        )
                    )
                rows = (
                    await session.execute(
                        select(
                            unique_event_relations.c.entity_id,
                            unique_event_relations.c.weight,
                            unique_event_relations.c.relation_description,
                            Entity.name,
                            Entity.type,
                            Entity.description.label("entity_description"),
                        )
                        .select_from(unique_event_relations)
                        .join(Entity, Entity.id == unique_event_relations.c.entity_id)
                        .where(
                            *filters,
                            Entity.data_source_id == source_config_id,
                            Entity.created_time <= as_of_db,
                        )
                        .order_by(
                            unique_event_relations.c.weight.desc(),
                            unique_event_relations.c.entity_id.asc(),
                        )
                        .limit(bounded_limit + 1)
                    )
                ).all()
                has_more = len(rows) > bounded_limit
                page = rows[:bounded_limit]
                entity_counts = await self._access.universe_entity_event_counts(
                    session,
                    source_config_id,
                    [str(row.entity_id) for row in page],
                    as_of_db=as_of_db,
                )
                next_cursor = None
                if has_more and page:
                    next_cursor = encode_universe_cursor(
                        {
                            "v": 2,
                            "scope": universe_cursor_scope(source_config_id),
                            "kind": "source-expand",
                            "node_kind": node_kind,
                            "node": node_id,
                            "as_of": as_of.isoformat(),
                            "revision": source_revision,
                            "weight": format(weight(page[-1].weight), ".17g"),
                            "id": page[-1].entity_id,
                        },
                        self._access.settings.secret_key,
                    )
                return UniverseExpansionInfo(
                    anchor={
                        "id": anchor.id,
                        "kind": "event",
                        "label": anchor.title or "未命名事件",
                        "description": str(anchor.summary or "")[:800],
                        "category": anchor.category or "事件",
                        "chunk_id": anchor.chunk_id,
                        "start_time": anchor.start_time,
                        "related_count": related_count,
                    },
                    neighbors=[
                        {
                            "id": row.entity_id,
                            "kind": "entity",
                            "label": row.name or "未命名实体",
                            "description": str(row.entity_description or "")[:800],
                            "category": row.type or "实体",
                            "weight": weight(row.weight),
                            "related_count": entity_counts.get(str(row.entity_id), 0),
                            "relation_description": str(row.relation_description or "")[:240],
                        }
                        for row in page
                    ],
                    relations=[
                        {
                            "from_id": str(anchor.id),
                            "to_id": str(row.entity_id),
                            "kind": "mentions",
                            "weight": weight(row.weight),
                            "description": str(row.relation_description or "")[:240],
                        }
                        for row in page
                    ],
                    returned=len(page),
                    has_more=has_more,
                    next_cursor=next_cursor,
                    snapshot_id=stable_snapshot_id,
                    as_of=as_of,
                )

            if node_kind != "entity":
                return None
            anchor = (
                await session.execute(
                    select(Entity.id, Entity.name, Entity.type, Entity.description).where(
                        Entity.id == node_id,
                        Entity.data_source_id == source_config_id,
                        Entity.created_time <= as_of_db,
                    )
                )
            ).one_or_none()
            if anchor is None:
                return None

            event_time = func.coalesce(SourceEvent.start_time, SourceEvent.created_time)
            unique_entity_relations = (
                select(
                    EventEntity.event_id.label("event_id"),
                    func.max(EventEntity.weight).label("weight"),
                    func.max(EventEntity.description).label("relation_description"),
                )
                .where(
                    EventEntity.entity_id == node_id,
                    EventEntity.created_time <= as_of_db,
                )
                .group_by(EventEntity.event_id)
                .subquery()
            )
            effective_after = after
            base_filters = [
                SourceEvent.data_source_id == source_config_id,
                (SourceEvent.status.is_(None) | (SourceEvent.status != "DELETED")),
                event_time <= as_of_db,
                SourceEvent.created_time <= as_of_db,
            ]
            after_db = database_time(effective_after)
            before_db = database_time(before)
            expected_after = after_db.isoformat() if after_db is not None else None
            expected_before = before_db.isoformat() if before_db is not None else None
            if cursor_payload and (
                cursor_payload.get("after") != expected_after or cursor_payload.get("before") != expected_before
            ):
                raise ValueError("universe cursor does not match its time range")
            if after_db is not None:
                base_filters.append(event_time >= after_db)
            if before_db is not None:
                base_filters.append(event_time <= before_db)
            related_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(unique_entity_relations)
                        .join(
                            SourceEvent,
                            SourceEvent.id == unique_entity_relations.c.event_id,
                        )
                        .where(*base_filters)
                    )
                ).scalar_one()
                or 0
            )
            filters = list(base_filters)
            if cursor_payload:
                last_time = cursor_datetime(cursor_payload, "time")
                last_time_db = database_time(last_time)
                last_weight = cursor_float(cursor_payload, "weight")
                last_id = str(cursor_payload.get("id") or "")
                if not last_id:
                    raise ValueError("invalid universe cursor")
                filters.append(
                    or_(
                        event_time < last_time_db,
                        and_(
                            event_time == last_time_db,
                            unique_entity_relations.c.weight < last_weight,
                        ),
                        and_(
                            event_time == last_time_db,
                            unique_entity_relations.c.weight == last_weight,
                            unique_entity_relations.c.event_id < last_id,
                        ),
                    )
                )
            rows = (
                await session.execute(
                    select(
                        unique_entity_relations.c.event_id,
                        unique_entity_relations.c.weight,
                        unique_entity_relations.c.relation_description,
                        SourceEvent.title,
                        SourceEvent.summary,
                        SourceEvent.category,
                        SourceEvent.chunk_id,
                        SourceEvent.start_time,
                        event_time.label("event_time"),
                    )
                    .select_from(unique_entity_relations)
                    .join(
                        SourceEvent,
                        SourceEvent.id == unique_entity_relations.c.event_id,
                    )
                    .where(*filters)
                    .order_by(
                        event_time.desc(),
                        unique_entity_relations.c.weight.desc(),
                        unique_entity_relations.c.event_id.desc(),
                    )
                    .limit(bounded_limit + 1)
                )
            ).all()
            has_more = len(rows) > bounded_limit
            page = rows[:bounded_limit]
            next_cursor = None
            if has_more and page:
                next_cursor = encode_universe_cursor(
                    {
                        "v": 2,
                        "scope": universe_cursor_scope(source_config_id),
                        "kind": "source-expand",
                        "node_kind": node_kind,
                        "node": node_id,
                        "as_of": as_of.isoformat(),
                        "revision": source_revision,
                        "after": expected_after,
                        "before": expected_before,
                        "time": page[-1].event_time.isoformat(),
                        "weight": format(weight(page[-1].weight), ".17g"),
                        "id": page[-1].event_id,
                    },
                    self._access.settings.secret_key,
                )
            bundle_nodes, bundle_relations = await self._access.universe_event_bundles(
                session,
                source_config_id,
                [
                    {
                        "id": str(row.event_id),
                        "title": row.title,
                        "summary": row.summary,
                        "category": row.category,
                        "chunk_id": row.chunk_id,
                        "start_time": row.start_time,
                        "event_time": row.event_time,
                    }
                    for row in page
                ],
                as_of_db=as_of_db,
                entity_limit=self._access.settings.universe_event_entity_limit,
            )
            direct_event_ids = {
                str(relation.get("from_id") or "")
                for relation in bundle_relations
                if str(relation.get("to_id") or "") == node_id
            }
            for row in page:
                event_id = str(row.event_id)
                if event_id in direct_event_ids:
                    continue
                bundle_relations.append(
                    {
                        "from_id": event_id,
                        "to_id": node_id,
                        "kind": "mentions",
                        "weight": weight(row.weight),
                        "description": str(row.relation_description or "")[:240],
                    }
                )
            return UniverseExpansionInfo(
                anchor={
                    "id": anchor.id,
                    "kind": "entity",
                    "label": anchor.name or "未命名实体",
                    "description": str(anchor.description or "")[:800],
                    "category": anchor.type or "实体",
                    "related_count": related_count,
                },
                neighbors=[
                    node for node in bundle_nodes if not (node.get("kind") == "entity" and node.get("id") == node_id)
                ],
                relations=bundle_relations,
                returned=len(page),
                has_more=has_more,
                next_cursor=next_cursor,
                snapshot_id=stable_snapshot_id,
                as_of=as_of,
            )

    async def universe_node_detail(
        self,
        source_config_id: str,
        node_kind: str,
        node_id: str,
        *,
        source: Source | None = None,
    ) -> dict[str, Any] | None:
        """Read node metadata only; graph neighborhoods are served by universe_expand."""
        await self._access.slot(source_config_id, source)
        from sqlalchemy import select
        from zleap.sag.db.models import Entity, SourceEvent

        sf = await self._access.relational_session_factory(source_config_id)
        async with sf() as session:
            if node_kind == "event":
                event = (
                    await session.execute(
                        select(
                            SourceEvent.id,
                            SourceEvent.source_id,
                            SourceEvent.title,
                            SourceEvent.summary,
                            SourceEvent.content,
                            SourceEvent.category,
                            SourceEvent.chunk_id,
                            SourceEvent.start_time,
                        ).where(
                            SourceEvent.id == node_id,
                            SourceEvent.data_source_id == source_config_id,
                            (SourceEvent.status.is_(None) | (SourceEvent.status != "DELETED")),
                        )
                    )
                ).one_or_none()
                if event is None:
                    return None
                summary = str(event.summary or "").strip()
                content = str(event.content or "").strip()
                return {
                    "id": event.id,
                    "kind": "event",
                    "source_ref_id": event.source_id,
                    "label": event.title or "未命名事件",
                    # summary is the compact graph-card copy; content is the
                    # extracted event detail shown after opening the node.
                    "description": (content or summary)[:4000],
                    "category": event.category or "",
                    "chunk_id": event.chunk_id,
                    "start_time": event.start_time,
                }

            if node_kind != "entity":
                return None
            entity = (
                await session.execute(
                    select(Entity.id, Entity.name, Entity.type, Entity.description).where(
                        Entity.id == node_id,
                        Entity.data_source_id == source_config_id,
                    )
                )
            ).one_or_none()
            if entity is None:
                return None
            return {
                "id": entity.id,
                "kind": "entity",
                "label": entity.name or "未命名实体",
                "description": str(entity.description or "")[:4000],
                "category": entity.type or "实体",
            }
