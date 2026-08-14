from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from sag_api.octx.errors import OctxSourceReextractRequiredError
from sag_api.sag.octx_ids import ProducerIdMap
from sag_api.sag.octx_vector_manifest import VectorExportManifest
from sag_api.sag.octx_vector_protocol import input_sha256, render_role_input


@dataclass(frozen=True, slots=True)
class SnapshotStats:
    counts: dict[str, int]
    vector_roles: frozenset[str] = frozenset()


def _json_default(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"cannot serialize {type(value).__name__} to OCTX JSON")


def _write_jsonl(handle, record: dict[str, Any]) -> None:  # noqa: ANN001
    handle.write(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )
        + "\n"
    )


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").casefold()
    return slug[:80] or "document"


_SENSITIVE_EXTRA_KEY_TOKENS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "passwd",
    "credential",
    "authorization",
    "auth_header",
    "private_key",
    "access_key",
)


def _is_sensitive_extra_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.casefold()
    return any(token in lowered for token in _SENSITIVE_EXTRA_KEY_TOKENS)


def _sanitize_extra(value: object) -> Any:
    if isinstance(value, dict):
        return {key: _sanitize_extra(item) for key, item in value.items() if not _is_sensitive_extra_key(key)}
    if isinstance(value, list):
        return [_sanitize_extra(item) for item in value]
    return value


def _octx_extra(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    sanitized = _sanitize_extra(value)
    return sanitized if isinstance(sanitized, dict) else None


def _event_export_title(event: Any) -> str:
    """Restore a complete OCTX title when SAG stores only its display prefix."""
    extra = _octx_extra(event.extra_data) or {}
    octx = extra.get("octx")
    original = octx.get("original_title") if isinstance(octx, dict) else None
    return original if isinstance(original, str) and original.strip() else str(event.title)


async def export_snapshot(
    source: Any,
    documents: Sequence[Any],
    workspace: str | Path,
    *,
    selected_article_ids: Sequence[str] | None = None,
    producer_state_path: str | Path,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    vector_store: Any = None,
    embedding_client: Any = None,
    on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> SnapshotStats:
    """Stream one explicit source_config partition into an OCTX workspace."""
    from zleap.sag.db import get_session_factory
    from zleap.sag.db.models import Article, Entity, EventEntity, SourceChunk, SourceEvent

    root = Path(workspace)
    if root.exists():
        raise FileExistsError(f"OCTX snapshot workspace already exists: {root}")
    knowledge = root / "knowledge"
    concepts = knowledge / "documents"
    data = root / "data"
    relations = root / "relations"
    concepts.mkdir(parents=True)
    data.mkdir()
    relations.mkdir()

    counts = {
        "documents": 0,
        "chunks": 0,
        "events": 0,
        "entities": 0,
        "chunk_events": 0,
        "event_entities": 0,
    }
    vector_manifest_path = root / ".octx-vector-export.sqlite3"
    filenames = {
        str(document.sag_source_id): str(document.filename)
        for document in documents
        if getattr(document, "is_active", True) and getattr(document, "sag_source_id", None)
    }
    document_metadata = {
        str(document.sag_source_id): {
            "id": str(getattr(document, "id", "") or document.sag_source_id),
            "filename": str(document.filename),
            "content_type": str(getattr(document, "content_type", "") or "application/octet-stream"),
        }
        for document in documents
        if getattr(document, "is_active", True) and getattr(document, "sag_source_id", None)
    }
    sessions = session_factory or get_session_factory()
    source_config_id = str(source.sag_source_config_id)
    selected_ids = tuple(
        dict.fromkeys(
            str(value)
            for value in (selected_article_ids if selected_article_ids is not None else filenames.keys())
            if value
        )
    )
    if not selected_ids:
        raise ValueError("OCTX snapshot requires at least one selected Article")

    async def report(kind: str, completed: int, total: int, *, phase: str = "snapshot") -> None:
        if on_progress is not None:
            await on_progress(
                {
                    "phase": phase,
                    "kind": kind,
                    "completed": completed,
                    "total": total,
                }
            )

    with ProducerIdMap(producer_state_path) as ids, VectorExportManifest(vector_manifest_path) as vector_manifest:
        with (knowledge / "index.md").open("x", encoding="utf-8") as index:
            index.write('---\nokf_version: "1.0"\n---\n# ' + str(source.name) + "\n\n")
            async with sessions() as session:
                articles = await session.stream_scalars(
                    select(Article)
                    .where(
                        Article.source_config_id == source_config_id,
                        Article.id.in_(selected_ids),
                    )
                    .order_by(Article.id)
                )
                async for article in articles:
                    document_id = ids.exchange_id("document", article.id, extra_data=_octx_extra(article.extra_data))
                    title = str(article.title or filenames.get(article.id) or "Document")
                    logical_name = f"{_slug(title)}-{document_id[-8:]}.md"
                    index.write(f"- [{title}](documents/{logical_name})\n")
                    body = str(article.content or "")
                    with (concepts / logical_name).open("x", encoding="utf-8") as output:
                        sag_metadata = document_metadata.get(article.id, {})
                        filename = PurePosixPath(
                            str(sag_metadata.get("filename") or logical_name).replace("\\", "/")
                        ).name[:512]
                        content_type = str(sag_metadata.get("content_type") or "application/octet-stream")[:128]
                        output.write(
                            "---\n"
                            "octx:\n"
                            f"  document_id: {json.dumps(document_id, ensure_ascii=False)}\n"
                            "sag:\n"
                            f"  filename: {json.dumps(filename, ensure_ascii=False)}\n"
                            f"  content_type: {json.dumps(content_type, ensure_ascii=False)}\n"
                            "---\n"
                        )
                        if not body.lstrip().startswith("#"):
                            output.write(f"# {title}\n\n")
                        output.write(body)
                        if body and not body.endswith("\n"):
                            output.write("\n")
                    counts["documents"] += 1
                    await report("documents", counts["documents"], len(selected_ids))

        async with sessions() as session:
            chunk_total = int(
                await session.scalar(
                    select(func.count(SourceChunk.id)).where(
                        SourceChunk.source_config_id == source_config_id,
                        SourceChunk.article_id.in_(selected_ids),
                    )
                )
                or 0
            )
            with (data / "chunks.jsonl").open("x", encoding="utf-8") as output:
                rows = await session.stream_scalars(
                    select(SourceChunk)
                    .where(
                        SourceChunk.source_config_id == source_config_id,
                        SourceChunk.article_id.in_(selected_ids),
                    )
                    .order_by(SourceChunk.article_id, SourceChunk.rank, SourceChunk.id)
                    .execution_options(yield_per=500)
                )
                async for chunk in rows:
                    if not chunk.article_id:
                        raise ValueError(f"OCTX export does not support unbound chunk: {chunk.id}")
                    chunk_exchange_id = ids.exchange_id("chunk", chunk.id, extra_data=_octx_extra(chunk.extra_data))
                    record = {
                        "id": chunk_exchange_id,
                        "document_id": ids.exchange_id("document", chunk.article_id),
                        "ordinal": int(chunk.rank),
                        "text": str(chunk.content or chunk.raw_content or ""),
                    }
                    if chunk.heading:
                        record["heading"] = str(chunk.heading)
                    _write_jsonl(output, record)
                    for role in ("chunk.heading", "chunk.content"):
                        vector_manifest.add(
                            role,
                            str(chunk.id),
                            chunk_exchange_id,
                            input_sha256(render_role_input(role, record)),
                        )
                    counts["chunks"] += 1
                    if counts["chunks"] % 100 == 0 or counts["chunks"] == chunk_total:
                        await report("chunks", counts["chunks"], chunk_total)

            selected_event_filter = (
                SourceEvent.source_config_id == source_config_id,
                SourceEvent.article_id.in_(selected_ids),
                SourceEvent.not_deleted(),
            )
            missing_relation_rows = (
                await session.execute(
                    select(SourceEvent.article_id, func.count(SourceEvent.id))
                    .where(
                        *selected_event_filter,
                        ~exists(select(EventEntity.id).where(EventEntity.event_id == SourceEvent.id)),
                    )
                    .group_by(SourceEvent.article_id)
                    .order_by(SourceEvent.article_id)
                )
            ).all()
            if missing_relation_rows:
                missing_count = sum(int(count) for _, count in missing_relation_rows)
                raise OctxSourceReextractRequiredError(
                    [
                        {
                            "id": document_metadata.get(article_id, {}).get("id", article_id),
                            "filename": document_metadata.get(article_id, {}).get(
                                "filename", filenames.get(article_id, article_id)
                            ),
                            "event_count": count,
                        }
                        for raw_article_id, count in missing_relation_rows
                        for article_id in (str(raw_article_id),)
                    ],
                    event_count=missing_count,
                )
            missing_entities = (
                await session.scalars(
                    select(EventEntity.entity_id)
                    .join(SourceEvent, SourceEvent.id == EventEntity.event_id)
                    .outerjoin(
                        Entity,
                        (Entity.id == EventEntity.entity_id) & (Entity.source_config_id == source_config_id),
                    )
                    .where(*selected_event_filter, Entity.id.is_(None))
                    .distinct()
                    .order_by(EventEntity.entity_id)
                    .limit(20)
                )
            ).all()
            if missing_entities:
                raise ValueError(
                    "OCTX event relation references missing or cross-source entity: " + ", ".join(missing_entities[:20])
                )

            event_total = int(
                await session.scalar(select(func.count(SourceEvent.id)).where(*selected_event_filter)) or 0
            )
            relation_total = int(
                await session.scalar(
                    select(func.count(EventEntity.id))
                    .join(SourceEvent, SourceEvent.id == EventEntity.event_id)
                    .where(*selected_event_filter)
                )
                or 0
            )
            entity_total = int(
                await session.scalar(
                    select(func.count(func.distinct(Entity.id)))
                    .join(EventEntity, EventEntity.entity_id == Entity.id)
                    .join(SourceEvent, SourceEvent.id == EventEntity.event_id)
                    .where(*selected_event_filter, Entity.source_config_id == source_config_id)
                )
                or 0
            )

            with (data / "entities.jsonl").open("x", encoding="utf-8") as output:
                entity_rows = await session.stream_scalars(
                    select(Entity)
                    .join(EventEntity, EventEntity.entity_id == Entity.id)
                    .join(SourceEvent, SourceEvent.id == EventEntity.event_id)
                    .where(*selected_event_filter, Entity.source_config_id == source_config_id)
                    .distinct()
                    .order_by(Entity.id)
                    .execution_options(yield_per=500)
                )
                async for entity in entity_rows:
                    entity_exchange_id = ids.exchange_id("entity", entity.id, extra_data=_octx_extra(entity.extra_data))
                    record = {
                        "id": entity_exchange_id,
                        "type": str(entity.type),
                        "name": str(entity.name),
                    }
                    if entity.description:
                        record["description"] = str(entity.description)
                    _write_jsonl(output, record)
                    vector_manifest.add(
                        "entity.name",
                        str(entity.id),
                        entity_exchange_id,
                        input_sha256(render_role_input("entity.name", record)),
                    )
                    counts["entities"] += 1
                    if counts["entities"] % 100 == 0 or counts["entities"] == entity_total:
                        await report("entities", counts["entities"], entity_total)

            with (
                (data / "events.jsonl").open("x", encoding="utf-8") as event_output,
                (relations / "chunk-events.jsonl").open("x", encoding="utf-8") as relation_output,
            ):
                parent_event = aliased(SourceEvent)
                event_rows = await session.stream(
                    select(SourceEvent, parent_event)
                    .outerjoin(
                        parent_event,
                        and_(
                            parent_event.id == SourceEvent.parent_id,
                            parent_event.source_config_id == source_config_id,
                            parent_event.article_id.in_(selected_ids),
                            parent_event.not_deleted(),
                        ),
                    )
                    .where(*selected_event_filter)
                    .order_by(SourceEvent.id)
                    .execution_options(yield_per=500)
                )
                async for event, selected_parent in event_rows:
                    event_id = ids.exchange_id("event", event.id, extra_data=_octx_extra(event.extra_data))
                    record = {
                        "id": event_id,
                        "title": _event_export_title(event),
                        "content": str(event.content or event.summary or event.title).strip(),
                    }
                    optional = {
                        "summary": event.summary,
                        "category": event.category,
                        "start_time": event.start_time,
                        "end_time": event.end_time,
                    }
                    record.update({key: value for key, value in optional.items() if value})
                    if selected_parent is not None:
                        record["parent_id"] = ids.exchange_id(
                            "event",
                            selected_parent.id,
                            extra_data=_octx_extra(selected_parent.extra_data),
                        )
                        record["level"] = int(event.level)
                    _write_jsonl(event_output, record)
                    for role in ("event.title", "event.content"):
                        vector_manifest.add(
                            role,
                            str(event.id),
                            event_id,
                            input_sha256(render_role_input(role, record)),
                        )
                    counts["events"] += 1
                    if counts["events"] % 100 == 0 or counts["events"] == event_total:
                        await report("events", counts["events"], event_total)

                    if not event.chunk_id:
                        raise ValueError(f"OCTX event has no chunk relation: {event.id}")
                    _write_jsonl(
                        relation_output,
                        {
                            "chunk_id": ids.exchange_id("chunk", event.chunk_id),
                            "event_id": event_id,
                        },
                    )
                    counts["chunk_events"] += 1

            with (relations / "event-entities.jsonl").open("x", encoding="utf-8") as output:
                relation_rows = await session.stream(
                    select(EventEntity, SourceEvent, Entity)
                    .join(SourceEvent, SourceEvent.id == EventEntity.event_id)
                    .join(
                        Entity,
                        (Entity.id == EventEntity.entity_id) & (Entity.source_config_id == source_config_id),
                    )
                    .where(*selected_event_filter)
                    .order_by(EventEntity.event_id, EventEntity.entity_id, EventEntity.id)
                    .execution_options(yield_per=500)
                )
                async for relation, event, entity in relation_rows:
                    event_exchange_id = ids.exchange_id("event", relation.event_id)
                    entity_exchange_id = ids.exchange_id("entity", relation.entity_id)
                    record = {
                        "event_id": event_exchange_id,
                        "entity_id": entity_exchange_id,
                        "weight": relation.weight,
                    }
                    if relation.description:
                        record["description"] = str(relation.description)
                    _write_jsonl(output, record)
                    relation_exchange_id = f"event_entity:{event_exchange_id}:{entity_exchange_id}"
                    relation_vector_record = {
                        **record,
                        "event": {"title": _event_export_title(event)},
                        "entity": {"name": entity.name},
                    }
                    vector_manifest.add(
                        "event_entity.relation",
                        str(relation.id),
                        relation_exchange_id,
                        input_sha256(render_role_input("event_entity.relation", relation_vector_record)),
                    )
                    counts["event_entities"] += 1
                    if counts["event_entities"] % 100 == 0 or counts["event_entities"] == relation_total:
                        await report("event_entities", counts["event_entities"], relation_total)

    vector_roles: set[str] = set()
    try:
        if vector_store is not None and embedding_client is not None:
            from sag_api.sag.octx_vector_protocol import write_existing_vector_payload

            vector_roles = await write_existing_vector_payload(
                root,
                vector_store,
                embedding_client,
                manifest_path=vector_manifest_path,
                routing=source_config_id,
                on_progress=on_progress,
            )
    finally:
        vector_manifest_path.unlink(missing_ok=True)
    await report("complete", sum(counts.values()), sum(counts.values()), phase="snapshot_complete")
    return SnapshotStats(counts=counts, vector_roles=frozenset(vector_roles))
