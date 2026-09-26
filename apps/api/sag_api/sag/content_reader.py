"""图谱与分块读侧 —— 从 `EngineManager` 拆出的查询。

含事件-实体图构建（`graph_for_sections` / `source_graph`）、实体列举与上下文、
以及分块标题、文档 Markdown、分块检索与单块读取。经由 `_EngineAccess` 取引擎
槽与关系会话工厂。

按读取路径设计，唯一例外是 `source_graph`：当引擎里的事件数证明旧版断点抽取
把先前的块隐藏了时，它会就地修复这些事件的可见状态（该修复逻辑在拆分前即存在
于 `EngineManager`，此处原样保留）。
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any

from sag_api.core.logging import get_logger
from sag_api.sag.dto import (
    ChunkInfo,
    EntityInfo,
    GraphAssociationInfo,
    GraphEventInfo,
    RetrievedSection,
    SourceGraphInfo,
)

if TYPE_CHECKING:
    from sag_api.db.models import Source
    from sag_api.sag.engine_access import EngineAccess

log = get_logger("sag")


class ContentReader:
    """图谱与分块内容的查询（唯一写入路径见 `source_graph` 的就地修复）。"""

    def __init__(self, access: EngineAccess) -> None:
        self._access = access

    async def graph_for_sections(
        self,
        sections: list[RetrievedSection],
        sources_by_config: dict[str, Source | None],
        *,
        event_limit: int = 50,
        entity_limit: int = 48,
        edge_limit: int = 96,
        event_scores: dict[tuple[str, str], float] | None = None,
    ) -> SourceGraphInfo:
        """Build a graph from direct event hits plus events attached to evidence chunks."""
        bounded_event_limit = max(1, min(int(event_limit), 100))
        bounded_entity_limit = max(1, min(int(entity_limit), 100))
        bounded_edge_limit = max(1, min(int(edge_limit), 300))
        direct_event_scores: dict[tuple[str, str], float] = {}
        for (raw_source_config_id, raw_event_id), raw_score in (event_scores or {}).items():
            source_config_id = raw_source_config_id.strip()
            event_id = raw_event_id.strip()
            if not source_config_id or not event_id:
                continue
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                continue
            if math.isfinite(score):
                direct_event_scores[(source_config_id, event_id)] = max(0.0, min(1.0, score))
        direct_event_ids_by_config: dict[str, set[str]] = {}
        for source_config_id, event_id in direct_event_scores:
            direct_event_ids_by_config.setdefault(source_config_id, set()).add(event_id)
        chunk_scores: dict[tuple[str, str], float] = {}
        chunk_ids_by_config: dict[str, set[str]] = {}
        for section in sections:
            source_config_id = (section.source_config_id or "").strip()
            chunk_id = (section.chunk_id or "").strip()
            if not source_config_id or not chunk_id:
                continue
            key = (source_config_id, chunk_id)
            chunk_scores[key] = max(chunk_scores.get(key, 0.0), section.score)
            chunk_ids_by_config.setdefault(source_config_id, set()).add(chunk_id)

        requested_config_ids = set(chunk_ids_by_config) | set(direct_event_ids_by_config)
        if not requested_config_ids:
            return SourceGraphInfo()

        await self._access.ensure_read_runtime(
            {source_config_id: sources_by_config.get(source_config_id) for source_config_id in requested_config_ids}
        )

        from sqlalchemy import and_, func, or_, select
        from zleap.sag.db.models import Entity, EventEntity, SourceEvent

        section_filters = [
            and_(
                SourceEvent.data_source_id == source_config_id,
                SourceEvent.chunk_id.in_(chunk_ids),
            )
            for source_config_id, chunk_ids in chunk_ids_by_config.items()
        ]
        event_filters = [
            and_(
                SourceEvent.data_source_id == source_config_id,
                SourceEvent.id.in_(event_ids),
            )
            for source_config_id, event_ids in direct_event_ids_by_config.items()
        ]
        candidate_filters = [*section_filters, *event_filters]
        candidate_limit = min(
            200,
            max(
                bounded_event_limit * 4,
                bounded_event_limit,
                len(direct_event_scores) * 2,
                len(chunk_scores) * 8 + len(direct_event_scores),
            ),
        )
        per_chunk_limit = max(
            1,
            min(8, math.ceil(candidate_limit / max(1, len(chunk_scores)))),
        )
        chunk_rank = (
            func.row_number()
            .over(
                partition_by=(SourceEvent.data_source_id, SourceEvent.chunk_id),
                order_by=(SourceEvent.rank.asc(), SourceEvent.id.asc()),
            )
            .label("chunk_rank")
        )
        ranked_events = (
            select(
                SourceEvent.id.label("id"),
                SourceEvent.data_source_id.label("source_config_id"),
                SourceEvent.source_id.label("source_id"),
                SourceEvent.title.label("title"),
                SourceEvent.summary.label("summary"),
                SourceEvent.content.label("content"),
                SourceEvent.category.label("category"),
                SourceEvent.rank.label("rank"),
                SourceEvent.parent_id.label("parent_id"),
                SourceEvent.chunk_id.label("chunk_id"),
                SourceEvent.start_time.label("start_time"),
                chunk_rank,
            )
            .where(
                or_(*candidate_filters),
                (SourceEvent.status.is_(None) | (SourceEvent.status != "DELETED")),
            )
            .subquery()
        )
        direct_event_ids = {event_id for _source_config_id, event_id in direct_event_scores}
        rank_condition = ranked_events.c.chunk_rank <= per_chunk_limit
        if direct_event_ids:
            rank_condition = or_(
                rank_condition,
                ranked_events.c.id.in_(direct_event_ids),
            )
        sf = await self._access.relational_session_factory(source_config_id)
        async with sf() as session:
            event_rows = (
                (
                    await session.execute(
                        select(ranked_events)
                        .where(rank_condition)
                        .order_by(
                            ranked_events.c.chunk_rank.asc(),
                            ranked_events.c.rank.asc(),
                            ranked_events.c.id.asc(),
                        )
                        .limit(candidate_limit)
                    )
                )
                .mappings()
                .all()
            )
            events_by_chunk: dict[tuple[str, str], list[Any]] = {}
            for row in event_rows:
                key = (row["source_config_id"], row["chunk_id"] or "")
                events_by_chunk.setdefault(key, []).append(row)
            for rows in events_by_chunk.values():
                rows.sort(key=lambda row: (int(row["rank"] or 0), row["id"]))

            direct_rows = sorted(
                (row for row in event_rows if (row["source_config_id"], row["id"]) in direct_event_scores),
                key=lambda row: (
                    -direct_event_scores[(row["source_config_id"], row["id"])],
                    int(row["rank"] or 0),
                    row["id"],
                ),
            )
            selected_direct_keys = {(row["source_config_id"], row["id"]) for row in direct_rows}

            # 每个高相关分块先贡献一个事件，再进入下一轮，避免单个长分块占满结果。
            ordered_chunk_keys = sorted(
                chunk_scores,
                key=lambda key: (-chunk_scores[key], key[0], key[1]),
            )
            balanced_rows: list[Any] = []
            depth = 0
            while True:
                added = False
                for key in ordered_chunk_keys:
                    rows = events_by_chunk.get(key, [])
                    if depth >= len(rows):
                        continue
                    balanced_rows.append(rows[depth])
                    added = True
                if not added:
                    break
                depth += 1

            # 重复上传或重叠分块会抽取出同名事件；同一信源只保留相关度最高的一条。
            seen_titles: set[tuple[str, str]] = set()
            event_rows = []
            for row in [
                *direct_rows,
                *(row for row in balanced_rows if (row["source_config_id"], row["id"]) not in selected_direct_keys),
            ]:
                normalized_title = re.sub(r"\s+", "", str(row["title"] or "")).casefold()
                title_key = (row["source_config_id"], normalized_title or row["id"])
                if title_key in seen_titles:
                    continue
                seen_titles.add(title_key)
                event_rows.append(row)
                if len(event_rows) >= bounded_event_limit:
                    break
            events = [
                GraphEventInfo(
                    id=row["id"],
                    source_config_id=row["source_config_id"],
                    source_id=row["source_id"],
                    title=row["title"] or "未命名事件",
                    summary=str(row["summary"] or "")[:800],
                    content=str(row["content"] or "")[:4000],
                    category=row["category"] or "",
                    rank=int(row["rank"] or 0),
                    parent_id=row["parent_id"],
                    chunk_id=row["chunk_id"],
                    start_time=row["start_time"],
                    score=direct_event_scores.get(
                        (row["source_config_id"], row["id"]),
                        chunk_scores.get(
                            (row["source_config_id"], row["chunk_id"] or ""),
                            0.0,
                        ),
                    ),
                )
                for row in event_rows
            ]
            event_ids = [event.id for event in events]
            if not event_ids:
                return SourceGraphInfo(events=events)

            association_limit = min(
                bounded_edge_limit,
                max(bounded_event_limit * 4, bounded_entity_limit * 3),
            )
            association_rows = (
                await session.execute(
                    select(
                        EventEntity.event_id,
                        EventEntity.entity_id,
                        EventEntity.weight,
                        EventEntity.description,
                        Entity.name,
                        Entity.type,
                        Entity.description.label("entity_description"),
                    )
                    .join(Entity, Entity.id == EventEntity.entity_id)
                    .where(
                        EventEntity.event_id.in_(event_ids),
                        Entity.data_source_id.in_(requested_config_ids),
                    )
                    .order_by(EventEntity.weight.desc(), EventEntity.created_time.asc())
                    .limit(association_limit)
                )
            ).all()

        heat: dict[str, int] = {}
        entity_rows: dict[str, tuple[str, str, str]] = {}
        for _event_id, entity_id, _weight, _description, name, kind, description in association_rows:
            heat[entity_id] = heat.get(entity_id, 0) + 1
            entity_rows[entity_id] = (
                str(name or "")[:500],
                str(kind or "")[:50],
                str(description or "")[:500],
            )
        selected_entity_ids = {
            entity_id
            for entity_id, _count in sorted(
                heat.items(),
                key=lambda item: (-item[1], entity_rows[item[0]][0], item[0]),
            )[:bounded_entity_limit]
        }
        entities = [
            EntityInfo(
                id=entity_id,
                name=entity_rows[entity_id][0],
                type=entity_rows[entity_id][1],
                description=entity_rows[entity_id][2],
                heat=heat[entity_id],
            )
            for entity_id in selected_entity_ids
        ]
        entities.sort(key=lambda entity: (-entity.heat, entity.name, entity.id))
        associations = [
            GraphAssociationInfo(
                event_id=event_id,
                entity_id=entity_id,
                weight=float(weight or 1.0),
                description=str(description or "")[:240],
            )
            for event_id, entity_id, weight, description, *_rest in association_rows
            if entity_id in selected_entity_ids
        ][:bounded_edge_limit]
        return SourceGraphInfo(
            events=events,
            entities=entities,
            associations=associations,
            total_entities=len(entities),
        )

    async def list_entities(
        self,
        source_config_id: str,
        *,
        source: Source | None = None,
        types: list[str] | None = None,
        limit: int = 100,
    ) -> list[EntityInfo]:
        """读取该源的事件—实体图谱，按热度（关联事件数）排序。extract 后才有数据。"""
        await self._access.slot(source_config_id, source)  # 确保引擎 / DB 已初始化
        from sqlalchemy import func, select
        from zleap.sag.db.models import Entity, EventEntity

        heat = func.count(EventEntity.id)
        conds = [Entity.data_source_id == source_config_id]
        if types:
            conds.append(Entity.type.in_(list(types)))
        stmt = (
            select(Entity, heat.label("heat"))
            .outerjoin(EventEntity, EventEntity.entity_id == Entity.id)
            .where(*conds)
            .group_by(Entity.id)
            .order_by(heat.desc())
            .limit(limit)
        )
        sf = await self._access.relational_session_factory(source_config_id)
        async with sf() as s:
            rows = (await s.execute(stmt)).all()
        return [
            EntityInfo(
                id=e.id,
                name=e.name or "",
                type=e.type or "",
                description=e.description or "",
                heat=int(h or 0),
            )
            for e, h in rows
        ]

    async def source_graph(
        self,
        source_config_id: str,
        source_ids: list[str],
        *,
        source: Source | None = None,
        event_limit: int = 1_000,
        entity_limit: int = 1_000,
        expected_event_count: int | None = None,
    ) -> SourceGraphInfo:
        """按展示预算读取一个按文档均衡的事件—实体图谱。

        图谱只读取本次展示文档对应的引擎 source_id。事件使用窗口排名轮询各文档，
        避免单篇长文占满配额；关联边覆盖优先并限制密度，避免高基数图谱拖垮浏览器。
        """
        await self._access.slot(source_config_id, source)
        from sqlalchemy import func, select, update
        from zleap.sag.db.models import Entity, EventEntity, SourceEvent

        sf = await self._access.relational_session_factory(source_config_id)
        async with sf() as s:
            if not source_ids:
                return SourceGraphInfo()

            event_scope = (
                SourceEvent.data_source_id == source_config_id,
                SourceEvent.source_id.in_(source_ids),
            )
            visible_event = SourceEvent.status.is_(None) | (SourceEvent.status != "DELETED")

            # 旧版断点抽取逐块保存，上游却把每次保存都视为整篇替换，导致先前块被隐藏。
            # 只有 Web 完成数能证明引擎中的全部事件属于本次抽取时才修复，避免恢复旧版本。
            if expected_event_count and expected_event_count > 0:
                total_event_count, visible_event_count = (
                    await s.execute(
                        select(
                            func.count(SourceEvent.id),
                            func.count(SourceEvent.id).filter(visible_event),
                        ).where(*event_scope)
                    )
                ).one()
                if (
                    int(visible_event_count or 0) < expected_event_count
                    and int(total_event_count or 0) == expected_event_count
                ):
                    repaired = await s.execute(
                        update(SourceEvent)
                        .where(*event_scope, SourceEvent.status == "DELETED")
                        .values(status="COMPLETED")
                    )
                    await s.commit()
                    log.warning(
                        "已恢复分块抽取中被上游隐藏的事件 source_config_id=%s count=%d",
                        source_config_id,
                        int(repaired.rowcount or 0),
                    )

            # 实体总量必须与当前文档范围一致；否则单文档筛选会把整个信源的
            # 实体数当成分母，错误显示为“已截断”。
            total_entities = int(
                (
                    await s.execute(
                        select(func.count(func.distinct(EventEntity.entity_id)))
                        .join(SourceEvent, SourceEvent.id == EventEntity.event_id)
                        .where(*event_scope, visible_event)
                    )
                ).scalar_one()
                or 0
            )

            # 每个文档先取 rank 较小的事件，再在文档之间轮询，兼顾层级根节点与覆盖面。
            source_rank = (
                func.row_number()
                .over(
                    partition_by=SourceEvent.source_id,
                    order_by=(SourceEvent.rank.asc(), SourceEvent.created_time.desc()),
                )
                .label("source_rank")
            )
            ranked = (
                select(
                    SourceEvent.id.label("id"),
                    SourceEvent.source_id.label("source_id"),
                    SourceEvent.title.label("title"),
                    SourceEvent.summary.label("summary"),
                    SourceEvent.category.label("category"),
                    SourceEvent.rank.label("rank"),
                    SourceEvent.parent_id.label("parent_id"),
                    SourceEvent.chunk_id.label("chunk_id"),
                    SourceEvent.start_time.label("start_time"),
                    SourceEvent.created_time.label("created_time"),
                    source_rank,
                )
                .where(
                    *event_scope,
                    visible_event,
                )
                .subquery()
            )
            event_rows = (
                (
                    await s.execute(
                        select(ranked)
                        .order_by(ranked.c.source_rank.asc(), ranked.c.created_time.desc())
                        .limit(event_limit)
                    )
                )
                .mappings()
                .all()
            )
            events = [
                GraphEventInfo(
                    id=row["id"],
                    source_config_id=source_config_id,
                    source_id=row["source_id"],
                    title=row["title"] or "未命名事件",
                    summary=str(row["summary"] or "")[:800],
                    category=row["category"] or "",
                    rank=int(row["rank"] or 0),
                    parent_id=row["parent_id"],
                    chunk_id=row["chunk_id"],
                    start_time=row["start_time"],
                )
                for row in event_rows
            ]
            event_ids = [event.id for event in events]
            if not event_ids:
                return SourceGraphInfo(events=events, total_entities=total_entities)

            relation_limit = min(12_000, max(300, event_limit * 2, entity_limit * 2))
            association_limit = min(24_000, relation_limit * 4)
            association_rows = (
                await s.execute(
                    select(
                        EventEntity.event_id,
                        EventEntity.entity_id,
                        EventEntity.weight,
                        EventEntity.description,
                        Entity.name,
                        Entity.type,
                        Entity.description.label("entity_description"),
                    )
                    .join(Entity, Entity.id == EventEntity.entity_id)
                    .where(
                        EventEntity.event_id.in_(event_ids),
                        Entity.data_source_id == source_config_id,
                    )
                    .order_by(EventEntity.weight.desc(), EventEntity.created_time.asc())
                    .limit(association_limit)
                )
            ).all()

        # 热度在当前图谱切片中计算；优先保留跨事件出现的实体。
        heat: dict[str, int] = {}
        entity_rows: dict[str, tuple[str, str, str]] = {}
        for _event_id, entity_id, _weight, _description, name, kind, entity_description in association_rows:
            heat[entity_id] = heat.get(entity_id, 0) + 1
            entity_rows[entity_id] = (
                str(name or "")[:500],
                str(kind or "")[:50],
                str(entity_description or "")[:500],
            )
        selected_entity_ids = {
            entity_id
            for entity_id, _count in sorted(
                heat.items(),
                key=lambda item: (-item[1], entity_rows[item[0]][0], item[0]),
            )[:entity_limit]
        }
        entities = [
            EntityInfo(
                id=entity_id,
                name=entity_rows[entity_id][0],
                type=entity_rows[entity_id][1],
                description=entity_rows[entity_id][2],
                heat=heat[entity_id],
            )
            for entity_id in selected_entity_ids
        ]
        entities.sort(key=lambda entity: (-entity.heat, entity.name, entity.id))

        eligible_rows = [row for row in association_rows if row[1] in selected_entity_ids]
        covering_rows = []
        remaining_rows = []
        covered_events: set[str] = set()
        covered_entities: set[str] = set()
        for row in eligible_rows:
            event_id, entity_id = row[0], row[1]
            if event_id not in covered_events or entity_id not in covered_entities:
                covering_rows.append(row)
                covered_events.add(event_id)
                covered_entities.add(entity_id)
            else:
                remaining_rows.append(row)
        selected_rows = [*covering_rows, *remaining_rows][:relation_limit]

        associations = [
            GraphAssociationInfo(
                event_id=event_id,
                entity_id=entity_id,
                weight=float(weight or 1.0),
                description=str(description or "")[:240],
            )
            for event_id, entity_id, weight, description, *_rest in selected_rows
        ]
        return SourceGraphInfo(
            events=events,
            entities=entities,
            associations=associations,
            total_entities=total_entities,
        )

    async def entity_context(
        self,
        source_config_id: str,
        entity_id: str,
        *,
        source: Source | None = None,
        limit: int = 20,
    ) -> list[str]:
        """某实体关联事件的文本片段（用于生成人格）。"""
        await self._access.slot(source_config_id, source)
        from sqlalchemy import select
        from zleap.sag.db.models import EventEntity, SourceEvent

        stmt = (
            select(SourceEvent.title, SourceEvent.summary, SourceEvent.content)
            .join(EventEntity, EventEntity.event_id == SourceEvent.id)
            .where(EventEntity.entity_id == entity_id)
            .limit(limit)
        )
        sf = await self._access.relational_session_factory(source_config_id)
        snippets: list[str] = []
        async with sf() as s:
            for title, summary, content in (await s.execute(stmt)).all():
                text = summary or content or title
                if text:
                    snippets.append(str(text)[:500])
        return snippets

    async def list_chunk_headings(
        self,
        source_config_id: str,
        *,
        source: Source | None = None,
        doc_sag_id: str | None = None,
        limit: int = 300,
    ) -> list[dict]:
        """分块大纲：heading + rank（可限定单文档），供 MCP outline。"""
        await self._access.slot(source_config_id, source)
        from sqlalchemy import select
        from zleap.sag.db.models import SourceChunk

        conds = [SourceChunk.data_source_id == source_config_id]
        if doc_sag_id:
            conds.append(SourceChunk.source_id == doc_sag_id)
        stmt = (
            select(SourceChunk.id, SourceChunk.heading, SourceChunk.rank)
            .where(*conds)
            .order_by(SourceChunk.rank)
            .limit(limit)
        )
        sf = await self._access.relational_session_factory(source_config_id)
        async with sf() as s:
            rows = (await s.execute(stmt)).all()
        return [{"chunk_id": cid, "heading": (h or "").strip(), "rank": int(r or 0)} for cid, h, r in rows]

    async def get_document_markdown(
        self,
        source_config_id: str,
        article_id: str,
        *,
        source: Source | None = None,
    ) -> str | None:
        """读取成功入库时保存的整篇 Markdown；不存在或内容为空时返回 None。"""
        await self._access.slot(source_config_id, source)
        from sqlalchemy import select
        from zleap.sag.db.models import Article

        sf = await self._access.relational_session_factory(source_config_id)
        async with sf() as session:
            content = await session.scalar(
                select(Article.content).where(
                    Article.id == article_id,
                    Article.data_source_id == source_config_id,
                )
            )
        return str(content) if content else None

    async def grep_chunks(
        self,
        source_config_id: str,
        pattern: str,
        *,
        source: Source | None = None,
        limit: int = 20,
        exclude_source_ids: tuple[str, ...] = (),
    ) -> list[dict]:
        """精确文本匹配（LIKE，大小写不敏感）：语义检索之外的确定性查找。"""
        await self._access.ensure_read_runtime({source_config_id: source})
        from sqlalchemy import select
        from zleap.sag.db.models import SourceChunk

        needle = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        excluded = tuple(
            sorted({value.strip() for value in exclude_source_ids if isinstance(value, str) and value.strip()})
        )
        conditions = [
            SourceChunk.data_source_id == source_config_id,
            SourceChunk.content.ilike(f"%{needle}%", escape="\\"),
        ]
        if excluded:
            conditions.append(SourceChunk.source_id.not_in(excluded))
        stmt = (
            select(
                SourceChunk.id,
                SourceChunk.heading,
                SourceChunk.content,
                SourceChunk.source_id,
            )
            .where(*conditions)
            .order_by(SourceChunk.rank)
            .limit(limit)
        )
        sf = await self._access.relational_session_factory(source_config_id)
        async with sf() as s:
            rows = (await s.execute(stmt)).all()
        out = []
        for cid, heading, content, document_source_id in rows:
            text = content or ""
            lowered = text.lower()
            needle = pattern.lower()
            positions: list[int] = []
            start = 0
            while (position := lowered.find(needle, start)) >= 0:
                positions.append(position)
                start = position + max(1, len(needle))

            def quality(position: int, content: str = text) -> int:
                window = content[max(0, position - 40) : position + 700]
                cjk = sum("\u3400" <= char <= "\u9fff" for char in window)
                return cjk - window.count("http") * 40 - window.count("](") * 15

            best = max(positions, key=quality) if positions else 0
            prefix_start = max(0, best - 500)
            prefix = text[prefix_start:best]
            dates = list(
                re.finditer(
                    r"20\d{2}(?:年\d{1,2}月\d{1,2}日|[-/]\d{1,2}[-/]\d{1,2})",
                    prefix,
                )
            )
            lo = prefix_start + dates[-1].start() if dates else max(0, best - 80)
            snippet = text[lo : best + 700]
            snippet = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", snippet)
            snippet = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", snippet)
            snippet = re.sub(r"[ \t]+", " ", snippet).strip()
            display_heading = (heading or "").strip()
            for line in text.splitlines():
                candidate = re.sub(r"!\[[^]]*\]\([^)]*\)", "", line)
                candidate = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", candidate)
                candidate = candidate.lstrip("#* -").strip()
                if needle in candidate.lower() and 2 <= len(candidate) <= 160:
                    display_heading = candidate
                    break
            out.append(
                {
                    "chunk_id": cid,
                    "heading": display_heading,
                    "snippet": snippet,
                    "source_id": document_source_id,
                }
            )
        return out

    async def get_chunk(
        self,
        source_config_id: str,
        chunk_id: str,
        *,
        source: Source | None = None,
    ):
        """读取某分块的完整原文（引用/搜索溯源）。不存在返回 None。"""

        await self._access.slot(source_config_id, source)
        from sqlalchemy import select
        from zleap.sag.db.models import SourceChunk

        sf = await self._access.relational_session_factory(source_config_id)
        async with sf() as s:
            row = (
                await s.execute(
                    select(SourceChunk).where(
                        SourceChunk.id == chunk_id,
                        SourceChunk.data_source_id == source_config_id,
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        return ChunkInfo(
            chunk_id=row.id,
            heading=(row.heading or "").strip(),
            content=(row.content or row.raw_content or "").strip(),
            rank=int(row.rank or 0),
        )
