from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any

from octx import ArchiveLimits, open_octx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sag_api.sag.octx_ids import relation_local_id
from sag_api.sag.octx_plan_store import OctxPlanError, OctxPlanStore


@dataclass(frozen=True, slots=True)
class StructuredPlanStats:
    counts: dict[str, int]
    document_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ImportStats:
    counts: dict[str, int]


async def import_knowledge_package(
    package_path: str | Path,
    controlled_dir: str | Path,
    *,
    source_config_id: str,
    engine_manager: Any,
    checkpoint: dict,
    on_checkpoint: Callable[[dict], Awaitable[None]] | None = None,
    limits: ArchiveLimits | None = None,
) -> ImportStats:
    """Rebuild a knowledge-only package through the existing document pipeline."""
    from sag_api.core.config import settings
    from sag_api.sag.dto import ProcessCheckpoint

    root = Path(controlled_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    states = checkpoint.setdefault("documents", {})
    counts = {"documents": 0, "chunks": 0, "events": 0}

    async def persist() -> None:
        if on_checkpoint is not None:
            await on_checkpoint(checkpoint)

    with open_octx(package_path, limits=limits, validate=False) as package:
        for position, document in enumerate(package.iter_documents()):
            state = states.setdefault(document.path, {})
            if state.get("status") == "ready":
                counts["documents"] += 1
                counts["chunks"] += int(state.get("chunk_count") or 0)
                counts["events"] += int(state.get("event_count") or 0)
                continue
            target = root / f"{position:08d}.md"
            namespace = document.metadata.get("octx")
            document_id = namespace.get("document_id") if isinstance(namespace, dict) else None
            if target.exists():
                if target.read_bytes() != document.raw:
                    raise OctxPlanError(f"knowledge checkpoint content changed: {document.path}")
            else:
                target.write_bytes(document.raw)
                target.chmod(0o600)
            process_checkpoint = ProcessCheckpoint.model_validate(state.get("process_checkpoint") or {})

            async def save_process(value: ProcessCheckpoint, _state: dict = state) -> None:
                _state["status"] = "processing"
                _state["process_checkpoint"] = value.model_dump()
                await persist()

            outcome = await engine_manager.process_document(
                source_config_id,
                str(target),
                checkpoint=process_checkpoint,
                on_checkpoint=save_process,
                max_concurrency=settings.document_extract_concurrency,
                document_title=Path(document.path).stem,
            )
            if outcome.paused:
                raise RuntimeError(f"knowledge-only OCTX import paused: {document.path}")
            state.update(
                {
                    "status": "ready",
                    "logical_path": document.path,
                    "octx_document_id": document_id,
                    "controlled_path": str(target),
                    "sag_source_id": outcome.source_id,
                    "chunk_count": outcome.chunk_count,
                    "event_count": outcome.event_count,
                    "token_usage": outcome.token_usage,
                    "process_checkpoint": process_checkpoint.model_dump(),
                }
            )
            counts["documents"] += 1
            counts["chunks"] += outcome.chunk_count
            counts["events"] += outcome.event_count
            await persist()
    return ImportStats(counts=counts)


def _extra(record: dict[str, Any], **octx: Any) -> dict[str, Any]:
    value = dict(record.get("extra_data") or {})
    value["octx"] = {**dict(value.get("octx") or {}), **octx}
    return value


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as error:
        raise OctxPlanError(f"invalid OCTX datetime: {value}") from error


def _weight(value: object) -> Decimal:
    try:
        weight = Decimal(str(1 if value is None else value))
    except (InvalidOperation, ValueError) as error:
        raise OctxPlanError(f"invalid event-entity weight: {value!r}") from error
    if not Decimal("0") <= weight <= Decimal("9.99"):
        raise OctxPlanError(f"event-entity weight exceeds SAG Numeric(3,2): {value!r}")
    return weight.quantize(Decimal("0.01"))


def _title(document: dict[str, Any]) -> str:
    metadata = document.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("title"), str):
        value = metadata["title"].strip()
        if value:
            return value[:500]
    body = str(document.get("body") or "")
    for line in body.splitlines():
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip()[:500]
    return Path(str(document.get("path") or "document.md")).stem[:500]


def _raw_title(document: dict[str, Any]) -> str:
    metadata = document.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("title"), str):
        value = metadata["title"].strip()
        if value:
            return value
    body = str(document.get("body") or "")
    for line in body.splitlines():
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip()
    return Path(str(document.get("path") or "document.md")).stem


# SAG column bounds — mirrors zleap.sag.db.models. Fields without a reversible
# adapter are rejected before opening the SAG session. Event titles are the one
# exception: SAG stores a 255-character display value and preserves the complete
# OCTX title in extra_data so a later export remains lossless.
_SAG_LIMITS = {
    "article_title": 500,
    "article_source_id": 100,
    "section_heading": 500,
    "chunk_source_id": 100,
    "chunk_heading": 500,
    "entity_type": 50,
    "entity_name": 500,
    "entity_type_name": 100,
    "event_title": 255,
    "event_category": 50,
    "event_source_id": 100,
}


def _validate_sag_compatibility(plan: OctxPlanStore) -> None:
    """Reject plans whose values would overflow SAG columns or leave events unlinked.

    Runs before the SAG session opens so ``OctxPlanError`` never surfaces mid-
    transaction. Callers must map ``OctxPlanError`` to
    ``OCTX_SAG_MAPPING_CONFLICT`` at ``OCTX_IMPORT``.
    """

    def _require(actual: int, limit: int, label: str, identifier: str) -> None:
        if actual > limit:
            raise OctxPlanError(f"{label} exceeds SAG column bound ({actual} > {limit}): {identifier}")

    for document in plan.iter_records("document"):
        document_id = str(document.get("id") or "")
        raw_title = _raw_title(document)
        _require(len(raw_title), _SAG_LIMITS["article_title"], "document title", document_id)
        _require(len(document_id), _SAG_LIMITS["article_source_id"], "document id", document_id)

    for chunk in plan.iter_records("chunk"):
        chunk_id = str(chunk.get("id") or "")
        heading = str(chunk.get("heading") or "")
        _require(len(heading), _SAG_LIMITS["section_heading"], "chunk heading", chunk_id)
        _require(len(chunk_id), _SAG_LIMITS["chunk_source_id"], "chunk id", chunk_id)

    for entity in plan.iter_records("entity"):
        entity_id = str(entity.get("id") or "")
        entity_type = str(entity.get("type") or "")
        entity_name = str(entity.get("name") or "")
        if not entity_type:
            raise OctxPlanError(f"entity has empty type: {entity_id}")
        if not entity_name:
            raise OctxPlanError(f"entity has empty name: {entity_id}")
        _require(len(entity_type), _SAG_LIMITS["entity_type"], "entity type", entity_id)
        _require(len(entity_name), _SAG_LIMITS["entity_name"], "entity name", entity_id)

    for type_key, _local_id in plan.iter_entity_types():
        _require(len(type_key), _SAG_LIMITS["entity_type"], "entity_type type", type_key)
        _require(
            len(type_key),
            _SAG_LIMITS["entity_type_name"],
            "entity_type name",
            type_key,
        )

    for event in plan.iter_records("event"):
        event_id = str(event.get("id") or "")
        title = str(event.get("title") or "")
        if not title.strip():
            raise OctxPlanError(f"event has empty title: {event_id}")
        category = str(event.get("category") or "")
        _require(len(category), _SAG_LIMITS["event_category"], "event category", event_id)
        if not plan.chunk_ids_for_event(event_id):
            raise OctxPlanError(f"event has no chunk relation: {event_id}")

    for relation in plan.iter_relations("event_entity"):
        _weight(relation.get("weight"))


def _document_id(metadata: object, path: str) -> str:
    if not isinstance(metadata, dict):
        raise OctxPlanError(f"Concept Document metadata is invalid: {path}")
    namespace = metadata.get("octx")
    document_id = namespace.get("document_id") if isinstance(namespace, dict) else None
    if not isinstance(document_id, str):
        raise OctxPlanError(f"Concept Document is missing octx.document_id: {path}")
    return document_id


def document_display_metadata(document: dict[str, Any]) -> tuple[str, str]:
    """Return bounded, path-safe SAG display metadata for one OCTX document."""
    metadata = document.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    sag = metadata.get("sag")
    sag = sag if isinstance(sag, dict) else {}

    def safe_name(value: object) -> str:
        if not isinstance(value, str):
            return ""
        name = PurePosixPath(value.replace("\\", "/")).name.strip()
        return name[:512] if name not in {"", ".", ".."} else ""

    filename = safe_name(sag.get("filename"))
    if not filename:
        title = metadata.get("title")
        title_name = safe_name(title)
        if title_name:
            filename = title_name if Path(title_name).suffix else f"{title_name}.md"
    if not filename:
        filename = safe_name(document.get("path")) or "document.md"

    raw_content_type = sag.get("content_type")
    content_type = (
        raw_content_type.strip()[:128]
        if isinstance(raw_content_type, str) and raw_content_type.strip()
        else "text/markdown"
    )
    return filename, content_type


def build_structured_plan(
    package_path: str | Path,
    plan_path: str | Path,
    id_namespace: str,
    *,
    limits: ArchiveLimits | None = None,
    validate: bool = True,
) -> StructuredPlanStats:
    """Stream a validated sag-structured package into a bounded SQLite plan."""
    counts = {
        "chunks": 0,
        "events": 0,
        "entities": 0,
        "chunk_events": 0,
        "event_entities": 0,
    }
    document_ids: list[str] = []
    with open_octx(package_path, limits=limits, validate=validate) as package:
        capabilities = dict(package.manifest.get("capabilities") or {})
        structured = capabilities.get("sag-structured")
        structured_version = structured.get("version") if isinstance(structured, dict) else structured
        if structured_version != "0.1":
            raise OctxPlanError("package does not declare sag-structured/0.1")
        available = set(package.available_paths)
        required = {
            "data/chunks.jsonl",
            "data/events.jsonl",
            "data/entities.jsonl",
            "relations/chunk-events.jsonl",
            "relations/event-entities.jsonl",
        }
        missing = sorted(required - available)
        if missing:
            raise OctxPlanError("sag-structured package is missing required paths: " + ", ".join(missing))

        with OctxPlanStore(plan_path, id_namespace) as store:
            for document in package.iter_documents():
                document_id = _document_id(dict(document.metadata), document.path)
                store.add_record(
                    "document",
                    {
                        "id": document_id,
                        "path": document.path,
                        "metadata": dict(document.metadata),
                        "body": document.body,
                    },
                )
                document_ids.append(document_id)
            for record in package.iter_chunks():
                store.add_record("chunk", record)
                counts["chunks"] += 1
            for record in package.iter_events():
                store.add_record("event", record)
                counts["events"] += 1
            for record in package.iter_entities():
                store.add_record("entity", record)
                store.ensure_entity_type(str(record.get("type") or ""))
                counts["entities"] += 1
            for relation in package.iter_chunk_events():
                store.add_relation("chunk_event", relation)
                counts["chunk_events"] += 1
            for relation in package.iter_event_entities():
                store.add_relation("event_entity", relation)
                counts["event_entities"] += 1

    return StructuredPlanStats(counts=counts, document_ids=tuple(document_ids))


async def import_structured_plan(
    plan_path: str | Path,
    id_namespace: str,
    *,
    source_config_id: str,
    source_name: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> ImportStats:
    """Atomically materialize one plan into a new isolated zleap-sag partition."""
    from zleap.sag.db import get_session_factory
    from zleap.sag.db.models import (
        Article,
        ArticleParseStatus,
        ArticleSection,
        Entity,
        EntityType,
        EventEntity,
        SourceChunk,
        SourceConfig,
        SourceEvent,
    )

    from sag_api.core.config import settings
    from sag_api.sag.octx_vector_protocol import configured_embedding_identity

    sessions = session_factory or get_session_factory()
    counts = {
        "documents": 0,
        "chunks": 0,
        "events": 0,
        "entities": 0,
        "event_entities": 0,
    }
    target_config: dict[str, Any] = {"octx": {"id_namespace": id_namespace}}
    vector_identity = configured_embedding_identity(settings)
    if vector_identity is not None:
        target_config["octx_vector_identity"] = vector_identity
    with OctxPlanStore(plan_path, id_namespace, create=False) as plan:
        _validate_sag_compatibility(plan)
        async with sessions() as session:
            source_config = await session.get(SourceConfig, source_config_id)
            if source_config is not None:
                expected = {
                    "documents": plan.count("document"),
                    "chunks": plan.count("chunk"),
                    "events": plan.count("event"),
                    "entities": plan.count("entity"),
                    "event_entities": plan.count("event_entity"),
                }
                actual = {
                    "documents": int(
                        await session.scalar(
                            select(func.count())
                            .select_from(Article)
                            .where(Article.source_config_id == source_config_id)
                        )
                        or 0
                    ),
                    "chunks": int(
                        await session.scalar(
                            select(func.count())
                            .select_from(SourceChunk)
                            .where(SourceChunk.source_config_id == source_config_id)
                        )
                        or 0
                    ),
                    "events": int(
                        await session.scalar(
                            select(func.count())
                            .select_from(SourceEvent)
                            .where(SourceEvent.source_config_id == source_config_id)
                        )
                        or 0
                    ),
                    "entities": int(
                        await session.scalar(
                            select(func.count()).select_from(Entity).where(Entity.source_config_id == source_config_id)
                        )
                        or 0
                    ),
                    "event_entities": int(
                        await session.scalar(
                            select(func.count())
                            .select_from(EventEntity)
                            .join(SourceEvent, SourceEvent.id == EventEntity.event_id)
                            .where(SourceEvent.source_config_id == source_config_id)
                        )
                        or 0
                    ),
                }
                derived_count = sum(actual.values())
                partition_namespace = dict((source_config.target_config or {}).get("octx") or {}).get("id_namespace")
                if derived_count and actual == expected and partition_namespace == id_namespace:
                    return ImportStats(counts=expected)
                if derived_count:
                    raise OctxPlanError(f"refusing to overwrite existing SAG partition: {source_config_id}")
                source_config.name = source_name[:100]
                source_config.description = "OCTX shadow installation"
                source_config.target_config = target_config
            else:
                session.add(
                    SourceConfig(
                        id=source_config_id,
                        name=source_name[:100],
                        description="OCTX shadow installation",
                        target_config=target_config,
                    )
                )
            await session.flush()

            for document in plan.iter_records("document"):
                document_id = str(document["id"])
                local_article_id = plan.local_id("document", document_id)
                metadata = document.get("metadata")
                description = (
                    metadata.get("description")
                    if isinstance(metadata, dict) and isinstance(metadata.get("description"), str)
                    else None
                )
                session.add(
                    Article(
                        id=local_article_id,
                        source_config_id=source_config_id,
                        title=_title(document),
                        source_id=document_id,
                        summary=description,
                        content=str(document.get("body") or ""),
                        status="COMPLETED",
                        parse_status=ArticleParseStatus.COMPLETED,
                        extra_data={
                            "octx": {
                                "record_id": document_id,
                                "logical_path": document.get("path"),
                            }
                        },
                    )
                )
                counts["documents"] += 1
            await session.flush()

            for chunk in plan.iter_records("chunk"):
                chunk_id = str(chunk["id"])
                document_id = str(chunk["document_id"])
                local_chunk_id = plan.local_id("chunk", chunk_id)
                local_article_id = plan.local_id("document", document_id)
                ordinal = int(chunk.get("ordinal") or 0)
                text = str(chunk.get("text") or "")
                heading = str(chunk.get("heading") or "")
                section_id = relation_local_id(id_namespace, "article_section", document_id, chunk_id)
                session.add(
                    ArticleSection(
                        id=section_id,
                        article_id=local_article_id,
                        order_index=ordinal,
                        render_group_index=ordinal,
                        type="TEXT",
                        rank=ordinal,
                        heading=heading,
                        content=text,
                        raw_content=text,
                        length=len(text),
                        extra_data=_extra(chunk, record_id=chunk_id),
                    )
                )
                session.add(
                    SourceChunk(
                        id=local_chunk_id,
                        source_config_id=source_config_id,
                        source_type="ARTICLE",
                        source_id=local_article_id,
                        article_id=local_article_id,
                        content=text,
                        raw_content=text,
                        heading=heading or None,
                        rank=ordinal,
                        chunk_length=len(text),
                        extra_data=_extra(chunk, record_id=chunk_id),
                    )
                )
                counts["chunks"] += 1
            await session.flush()

            for type_key, local_type_id in plan.iter_entity_types():
                session.add(
                    EntityType(
                        id=local_type_id,
                        scope="source",
                        source_config_id=source_config_id,
                        article_id=None,
                        type=type_key,
                        name=type_key,
                        description="Imported from OCTX",
                        weight=Decimal("1.00"),
                        similarity_threshold=Decimal("0.800"),
                        is_active=True,
                        is_default=False,
                        extra_data={"octx": {"type": type_key}},
                    )
                )
            await session.flush()

            type_ids = dict(plan.iter_entity_types())
            for entity in plan.iter_records("entity"):
                entity_id = str(entity["id"])
                type_key = str(entity["type"]).strip().casefold()
                name = str(entity["name"])
                session.add(
                    Entity(
                        id=plan.local_id("entity", entity_id),
                        source_config_id=source_config_id,
                        entity_type_id=type_ids[type_key],
                        type=type_key,
                        name=name,
                        normalized_name=name.casefold(),
                        description=entity.get("description"),
                        extra_data=_extra(entity, record_id=entity_id),
                    )
                )
                counts["entities"] += 1
            await session.flush()

            imported_events: dict[str, SourceEvent] = {}
            for event in plan.iter_records("event"):
                event_id = str(event["id"])
                title = str(event["title"])
                chunk_ids = plan.chunk_ids_for_event(event_id)
                if not chunk_ids:
                    raise OctxPlanError(f"event has no chunk relation: {event_id}")
                primary_chunk = plan.get_record("chunk", chunk_ids[0])
                document_id = str(primary_chunk["document_id"])
                local_article_id = plan.local_id("document", document_id)
                content = str(event.get("content") or "")
                summary = str(event.get("summary") or content[:2000] or event["title"])
                model = SourceEvent(
                    id=plan.local_id("event", event_id),
                    source_config_id=source_config_id,
                    source_type="ARTICLE",
                    source_id=local_article_id,
                    article_id=local_article_id,
                    title=title[:255],
                    summary=summary,
                    content=content,
                    category=str(event.get("category") or "")[:50],
                    keywords=event.get("keywords"),
                    rank=int(event.get("rank") or 0),
                    level=int(event.get("level") or 0),
                    parent_id=None,
                    start_time=_datetime(event.get("start_time")),
                    end_time=_datetime(event.get("end_time")),
                    chunk_id=plan.local_id("chunk", chunk_ids[0]),
                    extra_data=_extra(
                        event,
                        record_id=event_id,
                        chunk_ids=chunk_ids,
                        **({"original_title": title} if len(title) > 255 else {}),
                    ),
                )
                session.add(model)
                imported_events[event_id] = model
                counts["events"] += 1
            await session.flush()

            for event in plan.iter_records("event"):
                parent_id = event.get("parent_id")
                if isinstance(parent_id, str):
                    imported_events[str(event["id"])].parent_id = plan.local_id("event", parent_id)
            await session.flush()

            for relation in plan.iter_relations("event_entity"):
                event_id = str(relation["event_id"])
                entity_id = str(relation["entity_id"])
                session.add(
                    EventEntity(
                        id=relation_local_id(id_namespace, "event_entity", event_id, entity_id),
                        event_id=plan.local_id("event", event_id),
                        entity_id=plan.local_id("entity", entity_id),
                        weight=_weight(relation.get("weight")),
                        description=relation.get("description"),
                        extra_data=_extra(relation, event_id=event_id, entity_id=entity_id),
                    )
                )
                counts["event_entities"] += 1
            await session.commit()

    return ImportStats(counts=counts)
