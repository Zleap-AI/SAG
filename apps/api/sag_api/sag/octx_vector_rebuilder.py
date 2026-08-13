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
        if reuse_reader is not None:
            try:
                reused_by_id = reuse_reader.get_many(role, [str(record_id) for record_id in record_ids])
            except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as error:
                logger.warning(
                    "OCTX vector reuse failed; rebuilding role=%s error=%s",
                    role,
                    error,
                )
                reused_by_id = {}
            if len(reused_by_id) == len(record_ids):
                return [reused_by_id[str(record_id)] for record_id in record_ids]
        elif plan_path is not None:
            from sag_api.sag.octx_vector_reuse import ArrowVectorReuseReader

            with ArrowVectorReuseReader(plan_path) as reader:
                reused_by_id = reader.get_many(role, [str(record_id) for record_id in record_ids])
            if len(reused_by_id) == len(record_ids):
                return [reused_by_id[str(record_id)] for record_id in record_ids]
    return await _generate(embedding_client, texts)


async def _write(
    vector_store: Any,
    index: str,
    documents: list[dict[str, Any]],
    source_config_id: str,
) -> int:
    result = await vector_store.bulk_index(
        index=index,
        documents=documents,
        return_details=True,
        routing=source_config_id,
    )
    if not isinstance(result, dict) or int(result.get("error_count", 0)):
        raise RuntimeError(f"OCTX vector batch failed for {index}: {result!r}")
    written = int(result.get("success_count", 0))
    if written != len(documents):
        raise RuntimeError(f"OCTX vector batch incomplete for {index}: expected={len(documents)} actual={written}")
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
    from zleap.sag.core.ai.factory import get_embedding_client
    from zleap.sag.core.storage.client import get_vector_client
    from zleap.sag.db import get_session_factory
    from zleap.sag.db.models import Entity, EventEntity, SourceChunk, SourceEvent

    embedding = embedding_client or await get_embedding_client(scenario="general")
    vector_store = vector_store or get_vector_client()
    sessions = session_factory or get_session_factory()
    reusable: set[str] = set()
    reuse_reader = None
    if enable_vector_reuse and package_path is not None and plan_path is not None:
        from sag_api.sag.octx_vector_reuse import ArrowVectorReuseReader, prepare_vector_reuse

        reusable = await asyncio.to_thread(
            prepare_vector_reuse,
            package_path,
            plan_path,
            embedding,
            prevalidated_vector_valid=prevalidated_vector_valid,
        )
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
                        SourceEvent.source_config_id == source_config_id
                    )
                else:
                    statement = statement.where(model.source_config_id == source_config_id)
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
        counts["chunks"] += await _write(vector_store, "source_chunks", documents, source_config_id)

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
        counts["events"] += await _write(vector_store, "event_vectors", documents, source_config_id)

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
        counts["entities"] += await _write(vector_store, "entity_vectors", documents, source_config_id)

    async for records in batches(
        EventEntity,
        kind="event_entities",
        size=kind_batch_size(("event_entity.relation",)),
        join_event=True,
    ):
        relation_ids = [record.id for record in records]
        async with sessions() as session:
            relation_context = {
                str(relation_id): (str(event_title), str(entity_name))
                for relation_id, event_title, entity_name in (
                    await session.execute(
                        select(EventEntity.id, SourceEvent.title, Entity.name)
                        .join(SourceEvent, SourceEvent.id == EventEntity.event_id)
                        .join(Entity, Entity.id == EventEntity.entity_id)
                        .where(EventEntity.id.in_(relation_ids))
                    )
                ).all()
            }
        relation_texts = [
            str(record.description) if record.description else "\n\n".join(relation_context[str(record.id)])
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
                "description": record.description or "",
                "vector": vector,
                "created_time": (record.created_time.isoformat() if record.created_time else None),
                "is_delete": False,
            }
            for record, vector in zip(records, vectors, strict=True)
        ]
        counts["event_entities"] += await _write(vector_store, "event_entity_vectors", documents, source_config_id)

    if reuse_reader is not None:
        reuse_reader.close()
    return counts
