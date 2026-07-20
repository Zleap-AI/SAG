"""洞察领域逻辑：事件—实体图谱读取（供未来图谱视图与 get_entity 工具）。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.db.models import Document, Source
from sag_api.sag import EngineManager, EntityInfo
from sag_api.schemas.insight import (
    EntityOut,
    GraphCountsOut,
    GraphDocumentOut,
    GraphEventOut,
    GraphRelationOut,
    SourceGraphOut,
)


async def list_entities(
    engine_manager: EngineManager, source: Source, *, types: list[str] | None = None, limit: int = 100
) -> list[EntityInfo]:
    return await engine_manager.list_entities(source.sag_source_config_id, source=source, types=types, limit=limit)


async def get_source_graph(
    session: AsyncSession,
    engine_manager: EngineManager,
    source: Source,
    *,
    document_limit: int = 1_000,
    event_limit: int = 1_000,
    entity_limit: int = 1_000,
    document_ids: list[str] | None = None,
) -> SourceGraphOut:
    """拼装 Web 文档与引擎事件/实体，按调用方给出的性能预算返回图谱。"""
    scoped_document_ids = (
        list(dict.fromkeys(document_id.strip() for document_id in document_ids if document_id.strip()))
        if document_ids is not None
        else None
    )
    document_scope = [Document.source_id == source.id]
    if scoped_document_ids is not None:
        document_scope.append(Document.id.in_(scoped_document_ids))

    scoped_document_count, scoped_event_count = (
        await session.execute(
            select(
                func.count(Document.id),
                func.coalesce(func.sum(Document.event_count), 0),
            ).where(*document_scope)
        )
    ).one()
    scoped_document_count = int(scoped_document_count or 0)
    scoped_event_count = int(scoped_event_count or 0)
    documents = list(
        (
            await session.execute(
                select(Document)
                .where(*document_scope)
                .order_by(Document.created_at.desc(), Document.id.desc())
                .limit(document_limit)
            )
        )
        .scalars()
        .all()
    )
    source_id_to_document_id = {document.sag_source_id: document.id for document in documents if document.sag_source_id}
    shown_document_event_count = sum(max(0, int(document.event_count or 0)) for document in documents)
    graph = await engine_manager.source_graph(
        source.sag_source_config_id,
        list(source_id_to_document_id),
        source=source,
        event_limit=event_limit,
        entity_limit=entity_limit,
        expected_event_count=shown_document_event_count,
    )

    document_nodes = [
        GraphDocumentOut(
            id=document.id,
            filename=document.filename,
            status=document.status.value,
            chunk_count=document.chunk_count,
            event_count=document.event_count,
            created_at=document.created_at,
        )
        for document in documents
    ]
    event_nodes = [
        GraphEventOut(
            id=event.id,
            document_id=source_id_to_document_id.get(event.source_id),
            title=event.title,
            summary=event.summary,
            category=event.category,
            rank=event.rank,
            parent_id=event.parent_id,
            chunk_id=event.chunk_id,
            start_time=event.start_time,
        )
        for event in graph.events
    ]
    entity_nodes = [EntityOut(**entity.model_dump()) for entity in graph.entities]

    selected_event_ids = {event.id for event in event_nodes}
    relations: list[GraphRelationOut] = []
    relation_keys: set[tuple[str, str, str]] = set()

    def add_relation(relation: GraphRelationOut) -> None:
        key = (relation.source_id, relation.target_id, relation.kind)
        if key not in relation_keys:
            relations.append(relation)
            relation_keys.add(key)

    for event in event_nodes:
        if event.parent_id and event.parent_id in selected_event_ids:
            add_relation(
                GraphRelationOut(
                    source_id=event.parent_id,
                    source_kind="event",
                    target_id=event.id,
                    target_kind="event",
                    kind="subevent",
                )
            )
        elif event.document_id:
            add_relation(
                GraphRelationOut(
                    source_id=event.document_id,
                    source_kind="document",
                    target_id=event.id,
                    target_kind="event",
                    kind="contains",
                )
            )
    for association in graph.associations:
        add_relation(
            GraphRelationOut(
                source_id=association.event_id,
                source_kind="event",
                target_id=association.entity_id,
                target_kind="entity",
                kind="mentions",
                weight=association.weight,
                description=association.description,
            )
        )

    counts = GraphCountsOut(
        documents=(
            scoped_document_count
            if scoped_document_ids is not None
            else max(source.document_count, scoped_document_count, len(document_nodes))
        ),
        # Document checkpoints advance while extraction is still running;
        # Source.event_count is committed only after the whole document. Use
        # the strongest available total so a live graph reports 3 / 73 rather
        # than claiming its current three-node slice is the entire dataset.
        events=(
            max(scoped_event_count, len(event_nodes))
            if scoped_document_ids is not None
            else max(source.event_count, scoped_event_count, len(event_nodes))
        ),
        entities=max(graph.total_entities, len(entity_nodes)),
        shown_documents=len(document_nodes),
        shown_events=len(event_nodes),
        shown_entities=len(entity_nodes),
        shown_relations=len(relations),
    )
    return SourceGraphOut(
        documents=document_nodes,
        events=event_nodes,
        entities=entity_nodes,
        relations=relations,
        counts=counts,
        truncated=(
            counts.documents > counts.shown_documents
            or counts.events > counts.shown_events
            or counts.entities > counts.shown_entities
        ),
    )
