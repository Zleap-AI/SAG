from __future__ import annotations

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_export_snapshot_creates_fully_validated_structured_workspace(tmp_path):
    from octx import create_octx, open_octx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from zleap.sag.db.base import Base
    from zleap.sag.db.models import (
        Article,
        ArticleParseStatus,
        Entity,
        EntityType,
        EventEntity,
        SourceChunk,
        SourceConfig,
        SourceEvent,
    )

    from sag_api.sag.octx_snapshot import export_snapshot

    source_config_id = "src_snapshot"
    article_id = str(uuid.uuid4())
    chunk_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    entity_id = str(uuid.uuid4())
    entity_type_id = str(uuid.uuid4())
    relation_id = str(uuid.uuid4())
    original_event_title = "Full imported OCTX title " + ("x" * 300)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(SourceConfig(id=source_config_id, name="Snapshot", target_config={}))
        session.add(
            Article(
                id=article_id,
                source_config_id=source_config_id,
                title="Snapshot document",
                source_id="external-doc",
                content="# Snapshot document\n\nA production export.",
                status="COMPLETED",
                parse_status=ArticleParseStatus.COMPLETED,
            )
        )
        session.add(
            EntityType(
                id=entity_type_id,
                scope="source",
                source_config_id=source_config_id,
                type="topic",
                name="Topic",
                weight=Decimal("1.00"),
                similarity_threshold=Decimal("0.800"),
            )
        )
        await session.flush()
        session.add(
            SourceChunk(
                id=chunk_id,
                source_config_id=source_config_id,
                source_type="ARTICLE",
                source_id=article_id,
                article_id=article_id,
                content="A production export.",
                raw_content="A production export.",
                rank=0,
                chunk_length=20,
            )
        )
        session.add(
            Entity(
                id=entity_id,
                source_config_id=source_config_id,
                entity_type_id=entity_type_id,
                type="topic",
                name="OCTX",
                normalized_name="octx",
            )
        )
        session.add(
            SourceEvent(
                id=event_id,
                source_config_id=source_config_id,
                source_type="ARTICLE",
                source_id=article_id,
                article_id=article_id,
                title="Exported",
                summary="Exported event",
                content="",
                rank=0,
                level=0,
                chunk_id=chunk_id,
                extra_data={"octx": {"original_title": original_event_title}},
            )
        )
        await session.flush()
        session.add(
            EventEntity(
                id=relation_id,
                event_id=event_id,
                entity_id=entity_id,
                weight=Decimal("1.25"),
            )
        )
        await session.commit()

    source = SimpleNamespace(
        id="source-local",
        name="Snapshot",
        description="Exported source",
        sag_source_config_id=source_config_id,
    )
    documents = [
        SimpleNamespace(
            sag_source_id=article_id,
            filename="DeepSeekV3 技术报告.pdf",
            content_type="application/pdf",
            is_active=True,
        )
    ]
    progress_updates: list[dict] = []

    async def capture_progress(value: dict) -> None:
        progress_updates.append(dict(value))

    try:
        stats = await export_snapshot(
            source,
            documents,
            tmp_path / "workspace",
            producer_state_path=tmp_path / "producer-ids.json",
            session_factory=sessions,
            on_progress=capture_progress,
        )
    finally:
        await engine.dispose()

    assert stats.counts == {
        "documents": 1,
        "chunks": 1,
        "events": 1,
        "entities": 1,
        "chunk_events": 1,
        "event_entities": 1,
    }
    assert progress_updates
    assert progress_updates[-1]["phase"] == "snapshot_complete"
    assert progress_updates[-1]["completed"] == progress_updates[-1]["total"]
    assert {update["kind"] for update in progress_updates} >= {
        "documents",
        "chunks",
        "events",
        "entities",
        "event_entities",
    }
    package_path = tmp_path / "snapshot.octx"
    result = create_octx(
        tmp_path / "workspace",
        output=package_path,
        name="Snapshot",
        capabilities={"sag-structured": "0.1"},
    )
    assert result.report.valid and result.report.fully_validated
    with open_octx(package_path) as package:
        exported_documents = list(package.iter_documents())
        assert len(exported_documents) == 1
        assert exported_documents[0].metadata["sag"] == {
            "filename": "DeepSeekV3 技术报告.pdf",
            "content_type": "application/pdf",
        }
        assert len(list(package.iter_chunks())) == 1
        exported_events = list(package.iter_events())
        assert len(exported_events) == 1
        assert exported_events[0]["title"] == original_event_title
        assert exported_events[0]["content"] == "Exported event"
        assert len(list(package.iter_entities())) == 1


@pytest.mark.asyncio
async def test_export_snapshot_preserves_selected_parent_event_identity(tmp_path):
    """A selected child must retain its selected parent's stored OCTX identity."""
    from octx import create_octx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from zleap.sag.db.base import Base
    from zleap.sag.db.models import (
        Article,
        ArticleParseStatus,
        Entity,
        EntityType,
        EventEntity,
        SourceChunk,
        SourceConfig,
        SourceEvent,
    )

    from sag_api.sag.octx_importer import build_structured_plan
    from sag_api.sag.octx_snapshot import export_snapshot

    source_config_id = "parent-identity-source"
    article_id = str(uuid.uuid4())
    chunk_id = str(uuid.uuid4())
    entity_id = str(uuid.uuid4())
    entity_type_id = str(uuid.uuid4())
    first_id, second_id = sorted(
        str(uuid.uuid5(uuid.NAMESPACE_URL, name)) for name in ("selected-child", "selected-parent")
    )
    child_event_id = first_id
    parent_event_id = second_id
    child_record_id = "018f5f7e-89ab-7def-8123-0123456789c1"
    parent_record_id = "018f5f7e-89ab-7def-8123-0123456789c2"
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'parent-identity.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(SourceConfig(id=source_config_id, name="Parent identity", target_config={}))
        session.add(
            Article(
                id=article_id,
                source_config_id=source_config_id,
                title="Selected document",
                content="Selected document content.",
                status="COMPLETED",
                parse_status=ArticleParseStatus.COMPLETED,
            )
        )
        session.add(
            SourceChunk(
                id=chunk_id,
                source_config_id=source_config_id,
                source_type="ARTICLE",
                source_id=article_id,
                article_id=article_id,
                content="Selected document content.",
                rank=0,
                chunk_length=26,
            )
        )
        session.add(
            EntityType(
                id=entity_type_id,
                scope="source",
                source_config_id=source_config_id,
                type="topic",
                name="Topic",
                weight=Decimal("1.00"),
                similarity_threshold=Decimal("0.800"),
            )
        )
        session.add(
            Entity(
                id=entity_id,
                source_config_id=source_config_id,
                entity_type_id=entity_type_id,
                type="topic",
                name="Parent identity",
                normalized_name="parent identity",
            )
        )
        session.add_all(
            [
                SourceEvent(
                    id=parent_event_id,
                    source_config_id=source_config_id,
                    source_type="ARTICLE",
                    source_id=article_id,
                    article_id=article_id,
                    title="Parent event",
                    summary="Parent event",
                    content="Parent event content.",
                    rank=0,
                    level=0,
                    chunk_id=chunk_id,
                    extra_data={"octx": {"record_id": parent_record_id}},
                ),
                SourceEvent(
                    id=child_event_id,
                    source_config_id=source_config_id,
                    source_type="ARTICLE",
                    source_id=article_id,
                    article_id=article_id,
                    parent_id=parent_event_id,
                    title="Child event",
                    summary="Child event",
                    content="Child event content.",
                    rank=1,
                    level=1,
                    chunk_id=chunk_id,
                    extra_data={"octx": {"record_id": child_record_id}},
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                EventEntity(id=str(uuid.uuid4()), event_id=parent_event_id, entity_id=entity_id, weight=Decimal("1")),
                EventEntity(id=str(uuid.uuid4()), event_id=child_event_id, entity_id=entity_id, weight=Decimal("1")),
            ]
        )
        await session.commit()

    try:
        await export_snapshot(
            SimpleNamespace(id="source-local", name="Parent identity", sag_source_config_id=source_config_id),
            [SimpleNamespace(sag_source_id=article_id, filename="selected.md", is_active=True)],
            tmp_path / "parent-identity-workspace",
            selected_article_ids=[article_id],
            producer_state_path=tmp_path / "parent-identity-producer-ids.json",
            session_factory=sessions,
        )
    finally:
        await engine.dispose()

    event_records = [
        json.loads(line)
        for line in (tmp_path / "parent-identity-workspace/data/events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    child_record = next(record for record in event_records if record["id"] == child_record_id)
    assert child_record["parent_id"] == parent_record_id
    assert parent_record_id in {record["id"] for record in event_records}

    package_path = create_octx(
        tmp_path / "parent-identity-workspace",
        output=tmp_path / "parent-identity.octx",
        name="Parent identity",
        capabilities={"sag-structured": "0.1"},
    ).output
    build_structured_plan(package_path, tmp_path / "parent-identity-plan.sqlite3", str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_document_snapshot_detaches_parent_outside_selected_article(tmp_path):
    """A child whose parent is outside the selected documents becomes a root Event."""
    from octx import create_octx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from zleap.sag.db.base import Base
    from zleap.sag.db.models import (
        Article,
        ArticleParseStatus,
        Entity,
        EntityType,
        EventEntity,
        SourceChunk,
        SourceConfig,
        SourceEvent,
    )

    from sag_api.sag.octx_snapshot import export_snapshot

    source_config_id = "cross-document-parent-source"
    parent_article_id = str(uuid.uuid4())
    child_article_id = str(uuid.uuid4())
    parent_chunk_id = str(uuid.uuid4())
    child_chunk_id = str(uuid.uuid4())
    parent_event_id = str(uuid.uuid4())
    child_event_id = str(uuid.uuid4())
    entity_id = str(uuid.uuid4())
    entity_type_id = str(uuid.uuid4())
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cross-document-parent.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(SourceConfig(id=source_config_id, name="Cross document parent", target_config={}))
        for article_id, title in ((parent_article_id, "Parent document"), (child_article_id, "Child document")):
            session.add(
                Article(
                    id=article_id,
                    source_config_id=source_config_id,
                    title=title,
                    content=f"{title} content.",
                    status="COMPLETED",
                    parse_status=ArticleParseStatus.COMPLETED,
                )
            )
        for chunk_id, article_id, content in (
            (parent_chunk_id, parent_article_id, "Parent document content."),
            (child_chunk_id, child_article_id, "Child document content."),
        ):
            session.add(
                SourceChunk(
                    id=chunk_id,
                    source_config_id=source_config_id,
                    source_type="ARTICLE",
                    source_id=article_id,
                    article_id=article_id,
                    content=content,
                    rank=0,
                    chunk_length=len(content),
                )
            )
        session.add(
            EntityType(
                id=entity_type_id,
                scope="source",
                source_config_id=source_config_id,
                type="topic",
                name="Topic",
                weight=Decimal("1.00"),
                similarity_threshold=Decimal("0.800"),
            )
        )
        session.add(
            Entity(
                id=entity_id,
                source_config_id=source_config_id,
                entity_type_id=entity_type_id,
                type="topic",
                name="Cross document parent",
                normalized_name="cross document parent",
            )
        )
        session.add_all(
            [
                SourceEvent(
                    id=parent_event_id,
                    source_config_id=source_config_id,
                    source_type="ARTICLE",
                    source_id=parent_article_id,
                    article_id=parent_article_id,
                    title="Parent event",
                    summary="Parent event",
                    content="Parent event content.",
                    rank=0,
                    level=0,
                    chunk_id=parent_chunk_id,
                ),
                SourceEvent(
                    id=child_event_id,
                    source_config_id=source_config_id,
                    source_type="ARTICLE",
                    source_id=child_article_id,
                    article_id=child_article_id,
                    parent_id=parent_event_id,
                    title="Child event",
                    summary="Child event",
                    content="Child event content.",
                    rank=0,
                    level=1,
                    chunk_id=child_chunk_id,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                EventEntity(id=str(uuid.uuid4()), event_id=parent_event_id, entity_id=entity_id, weight=Decimal("1")),
                EventEntity(id=str(uuid.uuid4()), event_id=child_event_id, entity_id=entity_id, weight=Decimal("1")),
            ]
        )
        await session.commit()

    try:
        await export_snapshot(
            SimpleNamespace(id="source-local", name="Cross document parent", sag_source_config_id=source_config_id),
            [SimpleNamespace(sag_source_id=child_article_id, filename="child.md", is_active=True)],
            tmp_path / "cross-document-parent-workspace",
            selected_article_ids=[child_article_id],
            producer_state_path=tmp_path / "cross-document-parent-producer-ids.json",
            session_factory=sessions,
        )
    finally:
        await engine.dispose()

    event_records = [
        json.loads(line)
        for line in (tmp_path / "cross-document-parent-workspace/data/events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(event_records) == 1
    child_record = event_records[0]
    assert "parent_id" not in child_record
    assert "level" not in child_record
    event_ids = {record["id"] for record in event_records}
    assert {record["parent_id"] for record in event_records if record.get("parent_id")} <= event_ids

    result = create_octx(
        tmp_path / "cross-document-parent-workspace",
        output=tmp_path / "cross-document-parent.octx",
        name="Cross document parent",
        capabilities={"sag-structured": "0.1"},
    )
    assert result.report.valid is True


@pytest.mark.asyncio
async def test_export_snapshot_projects_only_frozen_ready_articles(tmp_path):
    """Selecting one READY document must not leak another document's graph into the package."""
    from octx import create_octx, open_octx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from zleap.sag.db.base import Base
    from zleap.sag.db.models import (
        Article,
        ArticleParseStatus,
        SourceChunk,
        SourceConfig,
    )

    from sag_api.sag.octx_snapshot import export_snapshot

    source_config_id = "frozen-source"
    selected_article = str(uuid.uuid4())
    excluded_article = str(uuid.uuid4())
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'frozen.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(SourceConfig(id=source_config_id, name="Frozen", target_config={}))
        for rank, article_id in enumerate((selected_article, excluded_article)):
            session.add(
                Article(
                    id=article_id,
                    source_config_id=source_config_id,
                    title=f"Document {rank}",
                    content=f"Body {rank}",
                    status="COMPLETED",
                    parse_status=ArticleParseStatus.COMPLETED,
                )
            )
            session.add(
                SourceChunk(
                    id=str(uuid.uuid4()),
                    source_config_id=source_config_id,
                    source_type="ARTICLE",
                    source_id=article_id,
                    article_id=article_id,
                    content=f"Body {rank}",
                    rank=rank,
                    chunk_length=6,
                )
            )
        await session.commit()

    source = SimpleNamespace(id="source-local", name="Frozen", sag_source_config_id=source_config_id)
    try:
        stats = await export_snapshot(
            source,
            [
                SimpleNamespace(
                    sag_source_id=selected_article,
                    filename="selected.md",
                    is_active=True,
                )
            ],
            tmp_path / "frozen-workspace",
            selected_article_ids=[selected_article],
            producer_state_path=tmp_path / "frozen-producer-ids.json",
            session_factory=sessions,
        )
    finally:
        await engine.dispose()

    assert stats.counts == {
        "documents": 1,
        "chunks": 1,
        "events": 0,
        "entities": 0,
        "chunk_events": 0,
        "event_entities": 0,
    }
    package_path = tmp_path / "frozen.octx"
    result = create_octx(
        tmp_path / "frozen-workspace",
        output=package_path,
        name="Frozen",
        capabilities={"sag-structured": "0.1"},
    )
    assert result.report.valid and result.report.fully_validated
    with open_octx(package_path) as package:
        documents = list(package.iter_documents())
        chunks = list(package.iter_chunks())
        assert len(documents) == 1
        assert len(chunks) == 1
        assert chunks[0]["text"] == "Body 0"


@pytest.mark.asyncio
async def test_export_snapshot_rejects_stored_event_without_entity(tmp_path):
    """A legacy incomplete graph must identify the document the user can reprocess."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from zleap.sag.db.base import Base
    from zleap.sag.db.models import (
        Article,
        ArticleParseStatus,
        SourceChunk,
        SourceConfig,
        SourceEvent,
    )

    from sag_api.sag.octx_snapshot import export_snapshot

    source_config_id = "broken-event-source"
    article_id = str(uuid.uuid4())
    chunk_id = str(uuid.uuid4())
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'broken.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(SourceConfig(id=source_config_id, name="Broken", target_config={}))
        session.add(
            Article(
                id=article_id,
                source_config_id=source_config_id,
                title="Broken",
                content="Body",
                status="COMPLETED",
                parse_status=ArticleParseStatus.COMPLETED,
            )
        )
        session.add(
            SourceChunk(
                id=chunk_id,
                source_config_id=source_config_id,
                source_type="ARTICLE",
                source_id=article_id,
                article_id=article_id,
                content="Body",
                rank=0,
                chunk_length=4,
            )
        )
        for rank in range(2):
            session.add(
                SourceEvent(
                    id=str(uuid.uuid4()),
                    source_config_id=source_config_id,
                    source_type="ARTICLE",
                    source_id=article_id,
                    article_id=article_id,
                    title=f"Broken Event {rank}",
                    summary="No Entity",
                    content="Body",
                    rank=rank,
                    level=0,
                    chunk_id=chunk_id,
                )
            )
        await session.commit()

    try:
        with pytest.raises(Exception) as caught:
            await export_snapshot(
                SimpleNamespace(
                    id="source-local",
                    name="Broken",
                    sag_source_config_id=source_config_id,
                ),
                [
                    SimpleNamespace(
                        id="document-local",
                        sag_source_id=article_id,
                        filename="broken.md",
                        is_active=True,
                    )
                ],
                tmp_path / "broken-workspace",
                selected_article_ids=[article_id],
                producer_state_path=tmp_path / "broken-producer-ids.json",
                session_factory=sessions,
            )
        error = caught.value
        assert getattr(error, "code", None) == "octx_source_reextract_required"
        assert getattr(error, "retryable", None) is False
        assert getattr(error, "details", None) == {
            "documents": [
                {
                    "id": "document-local",
                    "filename": "broken.md",
                    "event_count": 2,
                }
            ],
            "event_count": 2,
            "recovery_action": "reprocess_documents",
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_vector_rebuilder_requires_all_four_partition_indexes(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from zleap.sag.db.base import Base
    from zleap.sag.db.models import (
        Article,
        ArticleParseStatus,
        Entity,
        EntityType,
        EventEntity,
        SourceChunk,
        SourceConfig,
        SourceEvent,
    )

    from sag_api.sag.octx_vector_rebuilder import rebuild_vectors

    scid = "vector-source"
    article_id = str(uuid.uuid4())
    chunk_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    entity_id = str(uuid.uuid4())
    entity_type_id = str(uuid.uuid4())
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'vectors.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(SourceConfig(id=scid, name="Vectors", target_config={}))
        session.add(
            Article(
                id=article_id,
                source_config_id=scid,
                title="Doc",
                content="Body",
                status="COMPLETED",
                parse_status=ArticleParseStatus.COMPLETED,
            )
        )
        session.add(
            EntityType(
                id=entity_type_id,
                scope="source",
                source_config_id=scid,
                type="topic",
                name="Topic",
                weight=Decimal("1.00"),
                similarity_threshold=Decimal("0.800"),
            )
        )
        await session.flush()
        session.add(
            SourceChunk(
                id=chunk_id,
                source_config_id=scid,
                source_type="ARTICLE",
                source_id=article_id,
                article_id=article_id,
                content="Body",
                rank=0,
                chunk_length=4,
            )
        )
        session.add(
            Entity(
                id=entity_id,
                source_config_id=scid,
                entity_type_id=entity_type_id,
                type="topic",
                name="OCTX",
                normalized_name="octx",
            )
        )
        session.add(
            SourceEvent(
                id=event_id,
                source_config_id=scid,
                source_type="ARTICLE",
                source_id=article_id,
                article_id=article_id,
                title="Event",
                summary="Summary",
                content="Body",
                rank=0,
                level=0,
                chunk_id=chunk_id,
            )
        )
        await session.flush()
        session.add(
            EventEntity(
                id=str(uuid.uuid4()),
                event_id=event_id,
                entity_id=entity_id,
                weight=Decimal("1.00"),
            )
        )
        await session.commit()

    checkpoints: list[dict] = []

    async def capture_checkpoint(value: dict) -> None:
        checkpoints.append(dict(value))

    class Embedding:
        calls: list[list[str]] = []

        async def batch_generate(self, texts):
            assert checkpoints[-1]["current_kind"] in {
                "chunks",
                "events",
                "entities",
                "event_entities",
            }
            assert checkpoints[-1]["current_batch_size"] == 1
            self.calls.append(list(texts))
            return [[0.1, 0.2] for _ in texts]

    class Vectors:
        indexes: list[str] = []

        async def bulk_index(self, *, index, documents, return_details, routing):
            assert routing == scid and return_details is True
            self.indexes.append(index)
            return {
                "success": True,
                "success_count": len(documents),
                "error_count": 0,
            }

    vectors = Vectors()
    embedding = Embedding()
    try:
        stats = await rebuild_vectors(
            scid,
            {},
            batch_size=1,
            session_factory=sessions,
            embedding_client=embedding,
            vector_store=vectors,
            on_checkpoint=capture_checkpoint,
        )
    finally:
        await engine.dispose()
    assert stats == {"chunks": 1, "events": 1, "entities": 1, "event_entities": 1}
    assert vectors.indexes == [
        "source_chunks",
        "event_vectors",
        "entity_vectors",
        "event_entity_vectors",
    ]
    assert [item["current_kind"] for item in checkpoints if item.get("batch_state") == "started"] == [
        "chunks",
        "events",
        "entities",
        "event_entities",
    ]
    assert embedding.calls[-1] == ["Event\n\nOCTX"]


def test_octx_extra_strips_sensitive_source_config_derived_fields():
    from sag_api.sag.octx_snapshot import _octx_extra, _sanitize_extra

    payload = {
        "safe": "keep",
        "api_key": "leak-1",
        "SECRET_TOKEN": "leak-2",
        "provider": {"api_key": "nested", "user": "keep"},
        "history": [
            {"token": "leak-3", "kind": "keep"},
            {"password": "leak-4"},
        ],
        "octx": {"chunk_ids": ["ch-1"]},
    }
    sanitized = _octx_extra(payload)
    assert sanitized == {
        "safe": "keep",
        "provider": {"user": "keep"},
        "history": [{"kind": "keep"}, {}],
        "octx": {"chunk_ids": ["ch-1"]},
    }
    assert _sanitize_extra("plain") == "plain"
    assert _octx_extra("not a dict") is None
