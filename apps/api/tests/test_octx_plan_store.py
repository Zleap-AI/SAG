from __future__ import annotations

import uuid

import pytest


def test_installation_local_ids_are_deterministic_and_kind_isolated():
    from sag_api.sag.octx_ids import installation_local_id, relation_local_id

    namespace = uuid.UUID("9f38a52a-bceb-4d19-9889-7874cd049b43")
    octx_id = "018f5f7e-89ab-7def-8123-0123456789ab"

    chunk_id = installation_local_id(namespace, "chunk", octx_id)
    assert chunk_id == installation_local_id(str(namespace), "chunk", octx_id)
    assert chunk_id != installation_local_id(namespace, "event", octx_id)
    assert uuid.UUID(chunk_id).version == 5

    relation_id = relation_local_id(
        namespace,
        "event_entity",
        "018f5f7e-89ab-7def-8123-0123456789ac",
        "018f5f7e-89ab-7def-8123-0123456789ad",
    )
    assert relation_id == relation_local_id(
        namespace,
        "event_entity",
        "018f5f7e-89ab-7def-8123-0123456789ac",
        "018f5f7e-89ab-7def-8123-0123456789ad",
    )
    assert relation_id != relation_local_id(
        uuid.uuid4(),
        "event_entity",
        "018f5f7e-89ab-7def-8123-0123456789ac",
        "018f5f7e-89ab-7def-8123-0123456789ad",
    )


def test_installation_local_id_rejects_untyped_or_malformed_identity():
    from sag_api.sag.octx_ids import installation_local_id

    with pytest.raises(ValueError, match="record kind"):
        installation_local_id(uuid.uuid4(), "", str(uuid.uuid4()))
    with pytest.raises(ValueError, match="OCTX UUID"):
        installation_local_id(uuid.uuid4(), "chunk", "not-a-uuid")


def test_producer_id_map_reuses_octx_identity_across_exports(tmp_path):
    from sag_api.sag.octx_ids import ProducerIdMap

    path = tmp_path / "producer-ids.json"
    with ProducerIdMap(path) as ids:
        first = ids.exchange_id("chunk", "legacy-32-character-local-id")
        translated = ids.exchange_id("event", "a1591305-4d99-4e27-97ee-85bd23ba8ef5")
        preserved = ids.exchange_id(
            "entity",
            "local-entity",
            extra_data={"octx": {"record_id": "018f5f7e-89ab-7def-8123-0123456789ff"}},
        )

    with ProducerIdMap(path) as reopened:
        assert reopened.exchange_id("chunk", "legacy-32-character-local-id") == first
    assert uuid.UUID(first).version == 7
    assert translated == "a1591305-4d99-7e27-97ee-85bd23ba8ef5"
    assert preserved == "018f5f7e-89ab-7def-8123-0123456789ff"


def test_plan_store_persists_mappings_relations_and_primary_chunk(tmp_path):
    from sag_api.sag.octx_plan_store import OctxPlanStore

    namespace = uuid.UUID("9f38a52a-bceb-4d19-9889-7874cd049b43")
    document_id = "018f5f7e-89ab-7def-8123-0123456789a0"
    chunk_late = "018f5f7e-89ab-7def-8123-0123456789a1"
    chunk_early = "018f5f7e-89ab-7def-8123-0123456789a2"
    event_id = "018f5f7e-89ab-7def-8123-0123456789a3"
    entity_id = "018f5f7e-89ab-7def-8123-0123456789a4"

    path = tmp_path / "plan.sqlite3"
    with OctxPlanStore(path, namespace) as store:
        store.add_record(
            "chunk",
            {"id": chunk_late, "document_id": document_id, "ordinal": 9, "text": "late"},
        )
        store.add_record(
            "chunk",
            {"id": chunk_early, "document_id": document_id, "ordinal": 1, "text": "early"},
        )
        store.add_record("event", {"id": event_id, "title": "Event", "content": "Body"})
        store.add_record("entity", {"id": entity_id, "type": "topic", "name": "OCTX"})
        store.add_relation("chunk_event", {"chunk_id": chunk_late, "event_id": event_id})
        store.add_relation("chunk_event", {"chunk_id": chunk_early, "event_id": event_id})
        store.add_relation(
            "event_entity",
            {"event_id": event_id, "entity_id": entity_id, "weight": 1.25},
        )

        assert store.count("chunk") == 2
        assert store.local_id("event", event_id) != store.local_id("entity", entity_id)
        assert store.primary_chunk_id(event_id) == chunk_early
        assert store.chunk_ids_for_event(event_id) == [chunk_early, chunk_late]
        assert store.document_counts(document_id) == (2, 1)
        assert list(store.iter_records("chunk"))[0]["text"] == "late"

    with OctxPlanStore(path, namespace, create=False) as reopened:
        assert reopened.count("event_entity") == 1
        assert reopened.get_record("event", event_id)["title"] == "Event"


def test_plan_store_rejects_duplicates_and_dangling_relations(tmp_path):
    from sag_api.sag.octx_plan_store import OctxPlanError, OctxPlanStore

    namespace = uuid.uuid4()
    chunk_id = "018f5f7e-89ab-7def-8123-0123456789b1"
    event_id = "018f5f7e-89ab-7def-8123-0123456789b2"
    with OctxPlanStore(tmp_path / "plan.sqlite3", namespace) as store:
        store.add_record(
            "chunk",
            {
                "id": chunk_id,
                "document_id": "018f5f7e-89ab-7def-8123-0123456789b0",
                "ordinal": 0,
                "text": "body",
            },
        )
        with pytest.raises(OctxPlanError, match="duplicate"):
            store.add_record(
                "chunk",
                {
                    "id": chunk_id,
                    "document_id": "018f5f7e-89ab-7def-8123-0123456789b0",
                    "ordinal": 0,
                    "text": "changed",
                },
            )
        with pytest.raises(OctxPlanError, match="unknown event"):
            store.add_relation("chunk_event", {"chunk_id": chunk_id, "event_id": event_id})


def test_real_octx_structured_package_streams_into_plan_store(tmp_path):
    from octx import create_octx
    from octx.sag_align import write_structured_to_workspace

    from sag_api.sag.octx_importer import build_structured_plan

    document_id = "018f5f7e-89ab-7def-8123-0123456789c0"
    chunk_id = "018f5f7e-89ab-7def-8123-0123456789c1"
    event_id = "018f5f7e-89ab-7def-8123-0123456789c2"
    entity_id = "018f5f7e-89ab-7def-8123-0123456789c3"
    workspace = tmp_path / "workspace"
    knowledge = workspace / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "index.md").write_text(
        '---\nokf_version: "1.0"\n---\n# Index\n\n- [Sample](sample.md)\n',
        encoding="utf-8",
    )
    (knowledge / "sample.md").write_text(
        "---\noctx:\n  document_id: " + document_id + "\n---\n# Sample\n\nStructured body.\n",
        encoding="utf-8",
    )
    report = write_structured_to_workspace(
        workspace,
        chunks=[{"id": chunk_id, "document_id": document_id, "ordinal": 0, "text": "Body"}],
        events=[{"id": event_id, "title": "Released", "content": "Body"}],
        entities=[{"id": entity_id, "type": "topic", "name": "OCTX"}],
        chunk_events=[{"chunk_id": chunk_id, "event_id": event_id}],
        event_entities=[{"event_id": event_id, "entity_id": entity_id, "weight": 1.0}],
    )
    assert not report.constraint_issues
    package_path = tmp_path / "source.octx"
    result = create_octx(
        workspace,
        output=package_path,
        name="Structured",
        capabilities={"sag-structured": "0.1"},
    )
    assert result.report.valid and result.report.fully_validated

    stats = build_structured_plan(
        package_path,
        tmp_path / "plan.sqlite3",
        "9f38a52a-bceb-4d19-9889-7874cd049b43",
    )

    assert stats.counts == {
        "chunks": 1,
        "events": 1,
        "entities": 1,
        "chunk_events": 1,
        "event_entities": 1,
    }
    assert stats.document_ids == (document_id,)


@pytest.mark.asyncio
async def test_structured_plan_imports_one_atomic_shadow_partition(tmp_path):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from zleap.sag.db.base import Base
    from zleap.sag.db.models import (
        Article,
        ArticleSection,
        Entity,
        EventEntity,
        SourceChunk,
        SourceConfig,
        SourceEvent,
    )

    from sag_api.sag.octx_importer import import_structured_plan
    from sag_api.sag.octx_plan_store import OctxPlanStore

    namespace = "9f38a52a-bceb-4d19-9889-7874cd049b43"
    document_id = "018f5f7e-89ab-7def-8123-0123456789d0"
    chunk_id = "018f5f7e-89ab-7def-8123-0123456789d1"
    root_event_id = "018f5f7e-89ab-7def-8123-0123456789d2"
    child_event_id = "018f5f7e-89ab-7def-8123-0123456789d3"
    entity_id = "018f5f7e-89ab-7def-8123-0123456789d4"
    long_root_title = "Long OCTX event title " + ("x" * 300)
    plan_path = tmp_path / "plan.sqlite3"
    with OctxPlanStore(plan_path, namespace) as store:
        store.add_record(
            "document",
            {
                "id": document_id,
                "path": "knowledge/sample.md",
                "metadata": {"title": "Sample"},
                "body": "# Sample\n\nBody",
            },
        )
        store.add_record(
            "chunk",
            {"id": chunk_id, "document_id": document_id, "ordinal": 0, "text": "Body"},
        )
        store.add_record(
            "event",
            {"id": root_event_id, "title": long_root_title, "content": "Root body"},
        )
        store.add_record(
            "event",
            {
                "id": child_event_id,
                "title": "Child",
                "content": "Child body",
                "parent_id": root_event_id,
                "level": 1,
            },
        )
        store.add_record("entity", {"id": entity_id, "type": "topic", "name": "OCTX"})
        store.ensure_entity_type("topic")
        for event_id in (root_event_id, child_event_id):
            store.add_relation("chunk_event", {"chunk_id": chunk_id, "event_id": event_id})
            store.add_relation(
                "event_entity",
                {"event_id": event_id, "entity_id": entity_id, "weight": 1.25},
            )

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        stats = await import_structured_plan(
            plan_path,
            namespace,
            source_config_id="shadow-source-config",
            source_name="Imported",
            session_factory=sessions,
        )
        assert stats.counts == {
            "documents": 1,
            "chunks": 1,
            "events": 2,
            "entities": 1,
            "event_entities": 2,
        }
        async with sessions() as session:
            assert await session.get(SourceConfig, "shadow-source-config") is not None
            assert await session.scalar(select(func.count()).select_from(Article)) == 1
            assert await session.scalar(select(func.count()).select_from(ArticleSection)) == 1
            assert await session.scalar(select(func.count()).select_from(SourceChunk)) == 1
            assert await session.scalar(select(func.count()).select_from(SourceEvent)) == 2
            assert await session.scalar(select(func.count()).select_from(Entity)) == 1
            assert await session.scalar(select(func.count()).select_from(EventEntity)) == 2
            child = await session.scalar(select(SourceEvent).where(SourceEvent.title == "Child"))
            root = await session.scalar(
                select(SourceEvent).where(SourceEvent.title == long_root_title[:255])
            )
            assert child is not None and root is not None
            assert child.parent_id == root.id
            assert child.extra_data["octx"]["chunk_ids"] == [chunk_id]
            assert root.title == long_root_title[:255]
            assert root.extra_data["octx"]["original_title"] == long_root_title
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_structured_plan_resumes_an_exactly_materialized_shadow_partition(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from zleap.sag.db.base import Base
    from zleap.sag.db.models import Article, SourceConfig

    from sag_api.sag.octx_importer import import_structured_plan
    from sag_api.sag.octx_plan_store import OctxPlanError, OctxPlanStore

    namespace = "9f38a52a-bceb-4d19-9889-7874cd049b43"
    plan_path = tmp_path / "plan.sqlite3"
    with OctxPlanStore(plan_path, namespace) as store:
        store.add_record(
            "document",
            {
                "id": "018f5f7e-89ab-7def-8123-0123456789d0",
                "path": "knowledge/sample.md",
                "metadata": {"title": "Sample"},
                "body": "# Sample",
            },
        )

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with sessions() as session:
            session.add(
                SourceConfig(
                    id="shadow-source-config",
                    name="preprovisioned",
                    description="created by sag EngineManager",
                    target_config={},
                )
            )
            await session.commit()

        result = await import_structured_plan(
            plan_path,
            namespace,
            source_config_id="shadow-source-config",
            source_name="Imported",
            session_factory=sessions,
        )
        assert result.counts["documents"] == 1

        resumed = await import_structured_plan(
            plan_path,
            namespace,
            source_config_id="shadow-source-config",
            source_name="Imported",
            session_factory=sessions,
        )
        assert resumed.counts == result.counts

        async with sessions() as session:
            source = await session.get(SourceConfig, "shadow-source-config")
            source.name = "occupied"
            session.add(
                Article(
                    id="existing-article",
                    source_config_id=source.id,
                    title="Existing",
                    content="Existing",
                    status="COMPLETED",
                )
            )
            await session.commit()

        with pytest.raises(OctxPlanError, match="refusing to overwrite"):
            await import_structured_plan(
                plan_path,
                namespace,
                source_config_id="shadow-source-config",
                source_name="Imported",
                session_factory=sessions,
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_knowledge_only_package_reuses_document_processing_with_checkpoints(tmp_path):
    from octx import create_octx

    from sag_api.sag.dto import ProcessOutcome
    from sag_api.sag.octx_importer import import_knowledge_package

    source = tmp_path / "source"
    source.mkdir()
    (source / "index.md").write_text(
        '---\nokf_version: "1.0"\n---\n# Index\n\n- [Doc](doc.md)\n',
        encoding="utf-8",
    )
    (source / "doc.md").write_text("# Doc\n\nKnowledge only.\n", encoding="utf-8")
    package = tmp_path / "knowledge.octx"
    create_octx(tmp_path / "workspace", source=source, output=package, name="Knowledge")

    class Engine:
        paths: list[str] = []

        async def process_document(self, source_config_id, path, **kwargs):
            assert source_config_id == "shadow-knowledge"
            self.paths.append(path)
            await kwargs["on_checkpoint"](
                kwargs["checkpoint"].model_copy(update={"source_id": "article-k", "chunk_ids": ["chunk-k"]})
            )
            return ProcessOutcome(
                source_id="article-k",
                chunk_count=1,
                event_count=2,
                chunk_ids=["chunk-k"],
                processed_chunk_ids=["chunk-k"],
                token_usage=3,
            )

    saved: list[dict] = []

    async def on_checkpoint(value: dict) -> None:
        saved.append(value)

    engine = Engine()
    result = await import_knowledge_package(
        package,
        tmp_path / "controlled",
        source_config_id="shadow-knowledge",
        engine_manager=engine,
        checkpoint={},
        on_checkpoint=on_checkpoint,
    )

    assert result.counts == {"documents": 1, "chunks": 1, "events": 2}
    assert len(engine.paths) == 1
    assert (tmp_path / "controlled" / "00000000.md").is_file()
    assert saved[-1]["documents"]["knowledge/doc.md"]["status"] == "ready"


def test_sag_compatibility_validator_rejects_overflowing_title_before_write(tmp_path):
    from sag_api.sag.octx_importer import _SAG_LIMITS, _validate_sag_compatibility
    from sag_api.sag.octx_plan_store import OctxPlanError, OctxPlanStore

    namespace = uuid.UUID("9f38a52a-bceb-4d19-9889-7874cd049b43")
    document_id = "018f5f7e-89ab-7def-8123-0123456789b0"
    chunk_id = "018f5f7e-89ab-7def-8123-0123456789b1"
    event_id = "018f5f7e-89ab-7def-8123-0123456789b2"
    entity_id = "018f5f7e-89ab-7def-8123-0123456789b3"
    path = tmp_path / "plan.sqlite3"
    with OctxPlanStore(path, namespace) as store:
        store.add_record(
            "document",
            {
                "id": document_id,
                "path": "knowledge/doc.md",
                "metadata": {"title": "T" * (_SAG_LIMITS["article_title"] + 1)},
                "body": "Body",
            },
        )
        store.add_record(
            "chunk",
            {"id": chunk_id, "document_id": document_id, "ordinal": 0, "text": "Body"},
        )
        store.add_record("event", {"id": event_id, "title": "Event", "content": "Body"})
        store.add_record("entity", {"id": entity_id, "type": "topic", "name": "OCTX"})
        store.add_relation("chunk_event", {"chunk_id": chunk_id, "event_id": event_id})
        with pytest.raises(OctxPlanError, match="document title exceeds"):
            _validate_sag_compatibility(store)


def test_sag_compatibility_validator_rejects_event_without_chunk_relation(tmp_path):
    from sag_api.sag.octx_importer import _validate_sag_compatibility
    from sag_api.sag.octx_plan_store import OctxPlanError, OctxPlanStore

    namespace = uuid.UUID("9f38a52a-bceb-4d19-9889-7874cd049b43")
    document_id = "018f5f7e-89ab-7def-8123-0123456789c0"
    event_id = "018f5f7e-89ab-7def-8123-0123456789c1"
    path = tmp_path / "plan.sqlite3"
    with OctxPlanStore(path, namespace) as store:
        store.add_record(
            "document",
            {
                "id": document_id,
                "path": "knowledge/doc.md",
                "metadata": {"title": "OK"},
                "body": "Body",
            },
        )
        store.add_record("event", {"id": event_id, "title": "Orphan", "content": "Body"})
        with pytest.raises(OctxPlanError, match="event has no chunk relation"):
            _validate_sag_compatibility(store)


def test_sag_compatibility_validator_rejects_empty_entity_identity(tmp_path):
    from sag_api.sag.octx_importer import _validate_sag_compatibility
    from sag_api.sag.octx_plan_store import OctxPlanError, OctxPlanStore

    namespace = uuid.UUID("9f38a52a-bceb-4d19-9889-7874cd049b43")
    entity_id = "018f5f7e-89ab-7def-8123-0123456789d0"
    path = tmp_path / "plan.sqlite3"
    with OctxPlanStore(path, namespace) as store:
        store.add_record(
            "entity",
            {"id": entity_id, "type": "topic", "name": ""},
        )
        with pytest.raises(OctxPlanError, match="entity has empty name"):
            _validate_sag_compatibility(store)
