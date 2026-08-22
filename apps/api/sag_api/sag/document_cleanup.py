"""Delete one document's derived zleap-sag records across relational and vector stores."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, delete, exists, or_, select, text

from sag_api.core.logging import get_logger

log = get_logger("sag.cleanup")


@dataclass(frozen=True)
class DeletedDocumentRecords:
    chunk_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    relation_ids: tuple[str, ...]
    entity_ids: tuple[str, ...]


def _batches(values: tuple[str, ...], size: int = 500) -> Iterator[tuple[str, ...]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _lance_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def _delete_vector_ids(client: Any, index: str, ids: tuple[str, ...]) -> None:
    if not ids:
        return

    # zleap-sag 0.7.1 has no bulk-delete facade. Keep provider details contained here
    # so deleting thousands of derived rows does not become thousands of transactions.
    open_table = getattr(client, "_open_table", None)
    if callable(open_table):
        table = await open_table(index)
        if table is None:
            return
        for batch in _batches(ids):
            values = ", ".join(_lance_literal(value) for value in batch)
            await table.delete(f"id IN ({values})")
        return

    raw_client = getattr(client, "client", client)
    delete_by_query = getattr(raw_client, "delete_by_query", None)
    if callable(delete_by_query):
        for batch in _batches(ids):
            await delete_by_query(
                index=index,
                query={"ids": {"values": list(batch)}},
                conflicts="proceed",
                refresh=True,
            )
        return

    engine_factory = getattr(client, "_engine", None)
    if callable(engine_factory):
        quote = "`" if client.__class__.__name__ == "OceanBaseVectorStore" else '"'
        statement = text(
            f"DELETE FROM {quote}{index}{quote} WHERE id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        for batch in _batches(ids):
            async with engine_factory().begin() as connection:
                await connection.execute(statement, {"ids": list(batch)})
        return

    for record_id in ids:
        await client.delete(index=index, id=record_id)


async def _delete_vectors(records: DeletedDocumentRecords, store: Any) -> None:
    """0.8.2:向量删除经引擎注入的 VectorStore(集合名含 event_vectors_wide)。"""
    groups = (
        ("source_chunks", records.chunk_ids),
        ("event_vectors_wide", records.event_ids),
        ("event_entity_vectors", records.relation_ids),
        ("entity_vectors", records.entity_ids),
    )
    for index, ids in groups:
        if not ids:
            continue
        try:
            await store.delete(index, tuple(ids))
        except Exception as error:  # noqa: BLE001 - relational data remains authoritative
            log.warning("向量派生数据清理失败 index=%s count=%d: %s", index, len(ids), error)


async def delete_document_records(
    source_config_id: str,
    document_source_id: str,
    *,
    session_factory: Any = None,
    vector_store: Any = None,
) -> DeletedDocumentRecords:
    """Delete chunks, events, relations and now-orphaned entities for one document.

    0.8.2 无全局会话工厂/向量客户端:必须由 engine_manager 注入引擎级资源。
    """
    from zleap.sag.db.models import (
        Article,
        ArticleSection,
        Entity,
        EventEntity,
        SourceChunk,
        SourceEvent,
    )

    if session_factory is None:
        raise RuntimeError("0.8.2 无全局会话工厂:delete_document_records 必须注入 session_factory")
    async with session_factory() as session:
        article_ids = tuple(
            (
                await session.scalars(
                    select(Article.id).where(
                        Article.data_source_id == source_config_id,
                        or_(
                            Article.id == document_source_id,
                            Article.document_id == document_source_id,
                        ),
                    )
                )
            ).all()
        )
        source_ids = tuple({document_source_id, *article_ids})
        chunk_conditions = [SourceChunk.source_id.in_(source_ids)]
        if article_ids:
            chunk_conditions.append(SourceChunk.article_id.in_(article_ids))
        chunk_ids = tuple(
            (
                await session.scalars(
                    select(SourceChunk.id).where(
                        SourceChunk.data_source_id == source_config_id,
                        or_(*chunk_conditions),
                    )
                )
            ).all()
        )

        event_conditions = [SourceEvent.source_id.in_(source_ids)]
        if article_ids:
            event_conditions.append(SourceEvent.article_id.in_(article_ids))
        if chunk_ids:
            event_conditions.append(SourceEvent.chunk_id.in_(chunk_ids))
        event_ids = tuple(
            (
                await session.scalars(
                    select(SourceEvent.id).where(
                        SourceEvent.data_source_id == source_config_id,
                        or_(*event_conditions),
                    )
                )
            ).all()
        )

        relation_rows = (
            (
                await session.execute(
                    select(EventEntity.id, EventEntity.entity_id).where(
                        EventEntity.event_id.in_(event_ids)
                    )
                )
            ).all()
            if event_ids
            else []
        )
        relation_ids = tuple(row.id for row in relation_rows)
        related_entity_ids = tuple({row.entity_id for row in relation_rows})

        if relation_ids:
            await session.execute(delete(EventEntity).where(EventEntity.id.in_(relation_ids)))
        if event_ids:
            await session.execute(delete(SourceEvent).where(SourceEvent.id.in_(event_ids)))

        orphan_entity_ids: tuple[str, ...] = ()
        if related_entity_ids:
            orphan_entity_ids = tuple(
                (
                    await session.scalars(
                        select(Entity.id).where(
                            Entity.id.in_(related_entity_ids),
                            ~exists(
                                select(EventEntity.id).where(EventEntity.entity_id == Entity.id)
                            ),
                        )
                    )
                ).all()
            )
            if orphan_entity_ids:
                await session.execute(delete(Entity).where(Entity.id.in_(orphan_entity_ids)))

        if chunk_ids:
            await session.execute(delete(SourceChunk).where(SourceChunk.id.in_(chunk_ids)))
        if article_ids:
            await session.execute(
                delete(ArticleSection).where(ArticleSection.article_id.in_(article_ids))
            )
            await session.execute(delete(Article).where(Article.id.in_(article_ids)))
        await session.commit()

    records = DeletedDocumentRecords(
        chunk_ids=chunk_ids,
        event_ids=event_ids,
        relation_ids=relation_ids,
        entity_ids=orphan_entity_ids,
    )
    if vector_store is not None:
        await _delete_vectors(records, vector_store)
    return records
