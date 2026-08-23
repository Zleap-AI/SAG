from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select

logger = logging.getLogger(__name__)

_REUSE_BATCH_MEMORY_BUDGET_BYTES = 16 * 1024 * 1024


def _effective_reuse_batch_size(
    configured: int,
    *,
    dimensions: int,
    role_count: int,
) -> int:
    if configured < 1 or dimensions < 1 or role_count < 1:
        raise ValueError("OCTX reuse batch inputs must be positive")
    bytes_per_record = dimensions * role_count * 4
    memory_limited = max(1, _REUSE_BATCH_MEMORY_BUDGET_BYTES // bytes_per_record)
    return min(configured, memory_limited)


async def _generate(client: Any, texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    vectors = await client.batch_generate(texts)
    if len(vectors) != len(texts):
        raise RuntimeError(f"embedding batch size mismatch: expected={len(texts)} actual={len(vectors)}")
    return vectors


def _exchange_record_id(role: str, record: Any) -> str | None:
    extra = record.extra_data if isinstance(getattr(record, "extra_data", None), dict) else {}
    octx = extra.get("octx") if isinstance(extra.get("octx"), dict) else {}
    if role == "event_entity.relation":
        event_id = octx.get("event_id")
        entity_id = octx.get("entity_id")
        if isinstance(event_id, str) and isinstance(entity_id, str):
            return f"event_entity:{event_id}:{entity_id}"
        return None
    value = octx.get("record_id")
    return value if isinstance(value, str) else None


async def _vectors_for_role(
    role: str,
    records: Sequence[Any],
    texts: list[str],
    embedding_client: Any,
    *,
    plan_path: str | Path | None = None,
    reuse_reader: Any = None,
) -> list[list[float]]:
    record_ids = [_exchange_record_id(role, record) for record in records]
    if all(record_id is not None for record_id in record_ids):
        reused_by_id: dict[str, list[float]] = {}
        if reuse_reader is not None:
            try:
                reused_by_id = reuse_reader.get_many(role, [str(record_id) for record_id in record_ids])
            except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as error:
                logger.warning(
                    "OCTX vector reuse failed; rebuilding role=%s error=%s",
                    role,
                    error,
                )
        elif plan_path is not None:
            from sag_api.sag.octx_vector_reuse import ArrowVectorReuseReader

            try:
                with ArrowVectorReuseReader(plan_path) as reader:
                    reused_by_id = reader.get_many(role, [str(record_id) for record_id in record_ids])
            except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as error:
                logger.warning(
                    "OCTX vector reuse failed; rebuilding role=%s error=%s",
                    role,
                    error,
                )

        if len(reused_by_id) == len(record_ids):
            return [reused_by_id[str(record_id)] for record_id in record_ids]
        if reused_by_id:
            missing_indices = [
                index for index, record_id in enumerate(record_ids) if str(record_id) not in reused_by_id
            ]
            generated = await _generate(embedding_client, [texts[index] for index in missing_indices])
            generated_by_index = dict(zip(missing_indices, generated, strict=True))
            return [
                reused_by_id[str(record_id)] if index not in generated_by_index else generated_by_index[index]
                for index, record_id in enumerate(record_ids)
            ]
    return await _generate(embedding_client, texts)


async def _write(
    vector_store: Any,
    index: str,
    documents: list[dict[str, Any]],
) -> int:
    from zleap.sag.core.adapters.models import VectorRecord

    collection = "event_vectors_wide" if index == "event_vectors" else index
    records: list[VectorRecord] = []
    for document in documents:
        item = dict(document)
        record_id = str(item.pop("id"))
        vectors = {
            key: [float(value) for value in item.pop(key)]
            for key in tuple(item)
            if (key == "vector" or key.endswith("_vector")) and item[key] is not None
        }
        source_config_id = item.pop("source_config_id", None)
        if source_config_id is not None:
            item["data_source_id"] = str(source_config_id)
            if collection in {"entity_vectors", "event_entity_vectors"}:
                item["source_config_id"] = str(source_config_id)
        records.append(VectorRecord(id=record_id, payload=item, vectors=vectors))

    result = await vector_store.upsert(collection, records)
    if result.failure_count:
        failed = ", ".join(item.id for item in result.failed_items[:5])
        raise RuntimeError(f"OCTX vector batch failed for {collection}: {failed}")
    written = result.success_count
    if written != len(documents):
        raise RuntimeError(
            f"OCTX vector batch incomplete for {collection}: "
            f"expected={len(documents)} actual={written}"
        )
    return written


async def rebuild_vectors(
    source_config_id: str,
    checkpoint: dict,
    *,
    batch_size: int = 50,
    reuse_batch_size: int = 500,
    enable_vector_reuse: bool = True,
    session_factory: Any = None,
    embedding_client: Any = None,
    vector_store: Any = None,
    on_checkpoint: Callable[[dict], Awaitable[None]] | None = None,
    package_path: str | Path | None = None,
    plan_path: str | Path | None = None,
    prevalidated_vector_valid: bool | None = None,
) -> dict[str, int]:
    """Rebuild all SAG vector kinds from one isolated relational partition."""
    if batch_size < 1:
        raise ValueError("OCTX vector batch size must be positive")
    if reuse_batch_size < 1:
        raise ValueError("OCTX reused vector batch size must be positive")
    from zleap.sag.db.models import Entity, EventEntity, SourceChunk, SourceEvent

    # 0.8.2 无全局客户端/会话工厂:必须由调用方(engine_manager)注入引擎级资源。
    if embedding_client is None or vector_store is None or session_factory is None:
        raise RuntimeError(
            "0.8.2 无全局资源:rebuild_vectors 必须注入 session_factory / embedding_client / vector_store"
        )
    embedding = embedding_client
    sessions = session_factory
    reusable: set[str] = set()
    reuse_reader = None
    if enable_vector_reuse and package_path is not None and plan_path is not None:
        from sag_api.sag.octx_vector_reuse import ArrowVectorReuseReader, prepare_vector_reuse

        try:
            reusable = await asyncio.to_thread(
                prepare_vector_reuse,
                package_path,
                plan_path,
                embedding,
                prevalidated_vector_valid=prevalidated_vector_valid,
            )
        except Exception:
            # Vector reuse is an acceleration layer. Any failure while preparing
            # it must degrade to a full rebuild, never fail the import task.
            logger.exception("OCTX vector reuse preparation failed; rebuilding all vectors")
            reusable = set()
        checkpoint["reusable_roles"] = sorted(reusable)
        if reusable:
            reuse_reader = ArrowVectorReuseReader(plan_path)
    counts: dict[str, int] = dict(checkpoint.get("counts") or {})
    for kind in ("chunks", "events", "entities", "event_entities"):
        counts.setdefault(kind, 0)

    async def persist_checkpoint() -> None:
        if on_checkpoint is None:
            return
        checkpoint["counts"] = dict(counts)
        await on_checkpoint(checkpoint)

    dimensions = int(getattr(embedding, "dimensions", 0) or 0)

    def kind_batch_size(roles: tuple[str, ...]) -> int:
        if dimensions and all(role in reusable for role in roles):
            checkpoint["current_mode"] = "reuse"
            return _effective_reuse_batch_size(
                reuse_batch_size,
                dimensions=dimensions,
                role_count=len(roles),
            )
        checkpoint["current_mode"] = "generate" if not reusable else "mixed"
        return batch_size

    async def batches(model: Any, *, kind: str, size: int, join_event: bool = False):
        last_id = str(checkpoint.get(model.__tablename__) or "")
        while True:
            async with sessions() as session:
                statement = select(model)
                if join_event:
                    statement = statement.join(SourceEvent, SourceEvent.id == EventEntity.event_id).where(
                        SourceEvent.data_source_id == source_config_id
                    )
                else:
                    statement = statement.where(model.data_source_id == source_config_id)
                if last_id:
                    statement = statement.where(model.id > last_id)
                records: Sequence[Any] = (
                    (await session.execute(statement.order_by(model.id).limit(size))).scalars().all()
                )
            if not records:
                return
            checkpoint["current_kind"] = kind
            checkpoint["current_batch_size"] = len(records)
            checkpoint["batch_state"] = "started"
            await persist_checkpoint()
            yield records
            last_id = str(records[-1].id)
            checkpoint[model.__tablename__] = last_id
            checkpoint["batch_state"] = "completed"
            await persist_checkpoint()

    async for records in batches(
        SourceChunk,
        kind="chunks",
        size=kind_batch_size(("chunk.heading", "chunk.content")),
    ):
        content_vectors = await _vectors_for_role(
            "chunk.content",
            records,
            [f"{record.heading or ''}\n\n{record.content or ''}" for record in records],
            embedding,
            plan_path=plan_path,
            reuse_reader=reuse_reader,
        )
        heading_vectors = await _vectors_for_role(
            "chunk.heading",
            records,
            [str(record.heading or record.content or "") for record in records],
            embedding,
            plan_path=plan_path,
            reuse_reader=reuse_reader,
        )
        documents = [
            {
                "id": record.id,
                "chunk_id": record.id,
                "source_id": record.source_id,
                "source_config_id": source_config_id,
                "generation_id": record.generation_id,
                "rank": record.rank,
                "heading": record.heading,
                "content": record.content or "",
                "heading_vector": heading_vector,
                "content_vector": content_vector,
                "references": record.references or [],
                "chunk_type": "TEXT",
                "content_length": record.chunk_length,
            }
            for record, heading_vector, content_vector in zip(records, heading_vectors, content_vectors, strict=True)
        ]
        counts["chunks"] += await _write(vector_store, "source_chunks", documents)

    async for records in batches(
        SourceEvent,
        kind="events",
        size=kind_batch_size(("event.title", "event.content")),
    ):
        title_vectors = await _vectors_for_role(
            "event.title",
            records,
            [record.title for record in records],
            embedding,
            plan_path=plan_path,
            reuse_reader=reuse_reader,
        )
        content_vectors = await _vectors_for_role(
            "event.content",
            records,
            [f"{record.title}\n\n{record.content or ''}" for record in records],
            embedding,
            plan_path=plan_path,
            reuse_reader=reuse_reader,
        )
        documents = [
            {
                "id": record.id,
                "event_id": record.id,
                "source_config_id": source_config_id,
                "source_id": record.source_id,
                "source_type": record.source_type,
                "generation_id": record.generation_id,
                "article_id": record.article_id,
                "conversation_id": record.conversation_id,
                "chunk_id": record.chunk_id,
                "title": record.title,
                "summary": record.summary,
                "content": record.content,
                "category": record.category or "",
                "entity_ids": [],
                "title_vector": title_vector,
                "content_vector": content_vector,
                "created_time": (record.created_time.isoformat() if record.created_time else None),
            }
            for record, title_vector, content_vector in zip(records, title_vectors, content_vectors, strict=True)
        ]
        counts["events"] += await _write(vector_store, "event_vectors", documents)

    async for records in batches(
        Entity,
        kind="entities",
        size=kind_batch_size(("entity.name",)),
    ):
        vectors = await _vectors_for_role(
            "entity.name",
            records,
            [record.name for record in records],
            embedding,
            plan_path=plan_path,
            reuse_reader=reuse_reader,
        )
        documents = [
            {
                "id": record.id,
                "entity_id": record.id,
                "source_config_id": source_config_id,
                "type": record.type,
                "name": record.name,
                "normalized_name": record.normalized_name,
                "description": record.description or "",
                "vector": vector,
                "created_time": (record.created_time.isoformat() if record.created_time else None),
            }
            for record, vector in zip(records, vectors, strict=True)
        ]
        counts["entities"] += await _write(vector_store, "entity_vectors", documents)

    async for records in batches(
        EventEntity,
        kind="event_entities",
        size=kind_batch_size(("event_entity.relation",)),
        join_event=True,
    ):
        relation_ids = [record.id for record in records]
        async with sessions() as session:
            relation_context = {
                str(relation_id): (
                    str(event_title),
                    str(entity_name),
                    str(source_id),
                    str(generation_id) if generation_id is not None else None,
                    str(source_type),
                )
                for relation_id, event_title, entity_name, source_id, generation_id, source_type in (
                    await session.execute(
                        select(
                            EventEntity.id,
                            SourceEvent.title,
                            Entity.name,
                            SourceEvent.source_id,
                            SourceEvent.generation_id,
                            SourceEvent.source_type,
                        )
                        .join(SourceEvent, SourceEvent.id == EventEntity.event_id)
                        .join(Entity, Entity.id == EventEntity.entity_id)
                        .where(EventEntity.id.in_(relation_ids))
                    )
                ).all()
            }
        relation_texts = [
            str(record.description)
            if record.description
            else "\n\n".join(relation_context[str(record.id)][:2])
            for record in records
        ]
        vectors = await _vectors_for_role(
            "event_entity.relation",
            records,
            relation_texts,
            embedding,
            plan_path=plan_path,
            reuse_reader=reuse_reader,
        )
        documents = [
            {
                "id": record.id,
                "event_id": record.event_id,
                "entity_id": record.entity_id,
                "source_config_id": source_config_id,
                "source_id": relation_context[str(record.id)][2],
                "generation_id": relation_context[str(record.id)][3],
                "source_type": relation_context[str(record.id)][4],
                "description": record.description or "",
                "vector": vector,
                "created_time": (record.created_time.isoformat() if record.created_time else None),
                "is_delete": False,
            }
            for record, vector in zip(records, vectors, strict=True)
        ]
        counts["event_entities"] += await _write(vector_store, "event_entity_vectors", documents)

    collections = tuple(
        collection
        for kind, collection in (
            ("chunks", "source_chunks"),
            ("events", "event_vectors_wide"),
            ("entities", "entity_vectors"),
            ("event_entities", "event_entity_vectors"),
        )
        if counts[kind]
    )
    try:
        if collections:
            await vector_store.publish(collections)
    finally:
        if reuse_reader is not None:
            reuse_reader.close()
    return counts
