"""Publication body and count policy using real business and engine SQL stores."""

from uuid import uuid4

import pytest
from sqlalchemy import delete
from zleap.sag.db.models import Article, ArticleParseStatus, DataSource

from sag_api.api.v1.documents import get_parsed
from sag_api.core.config import Settings
from sag_api.core.db import SessionLocal, init_db
from sag_api.core.errors import ConflictError, NotFoundError
from sag_api.db.models import Document, Source
from sag_api.enums import DocumentStatus
from sag_api.sag import EngineManager
from sag_api.services.document_service import _refresh_source_counts, read_document_lines


@pytest.fixture
async def publication_body(tmp_path):
    await init_db()
    raw = tmp_path / "document.md"
    raw.write_text("# Unpublished candidate\n", encoding="utf-8")
    async with SessionLocal() as session:
        source = Source(name="publication-body", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="document.md",
            storage_path=str(raw),
            status=DocumentStatus.PAUSED,
            sag_source_id=uuid4().hex,
            chunk_count=3,
            event_count=5,
        )
        session.add(document)
        await session.commit()
    manager = EngineManager(
        Settings(
            _env_file=None,
            data_dir=str(tmp_path / "engine"),
            embedding_schema_dimensions=2,
            embedding_request_dimensions=2,
        )
    )
    try:
        await manager.provision(source.sag_source_config_id)
        factory = await manager.get_sag_session_factory(source.sag_source_config_id)
        async with factory() as session:
            await session.merge(DataSource(id=source.sag_source_config_id, name="publication-body"))
            session.add(
                Article(
                    id=document.sag_source_id,
                    data_source_id=source.sag_source_config_id,
                    document_id=document.id,
                    title="document",
                    content="# Published original\n",
                    status="COMPLETED",
                    parse_status=ArticleParseStatus.COMPLETED,
                )
            )
            await session.commit()
        yield manager, source, document
    finally:
        await manager.aclose_all()
        async with SessionLocal() as session:
            await session.execute(delete(Source).where(Source.id == source.id))
            await session.commit()


@pytest.fixture
async def publication_evidence(publication_body):
    from zleap.sag.db.models import Entity, EntityType, EventEntity, SourceChunk, SourceEvent

    manager, source, document = publication_body
    entity_id, hidden_entity_id = uuid4().hex, uuid4().hex
    published_chunk, hidden_chunk = uuid4().hex, uuid4().hex
    factory = await manager.get_sag_session_factory(source.sag_source_config_id)
    async with factory() as session:
        kind = EntityType(id=uuid4().hex, type="person", name="人物")
        session.add(kind)
        await session.flush()
        session.add_all(
            [
                Entity(
                    id=entity_id,
                    data_source_id=source.sag_source_config_id,
                    entity_type_id=kind.id,
                    type="person",
                    name="Published person",
                    normalized_name="published",
                    description="Published description",
                ),
                Entity(
                    id=hidden_entity_id,
                    data_source_id=source.sag_source_config_id,
                    entity_type_id=kind.id,
                    type="person",
                    name="Candidate person",
                    normalized_name="candidate",
                    description="Hidden description",
                ),
            ]
        )
        await session.flush()
        # Candidate associations precede the published one and dominate unqualified heat.
        for index in range(4):
            event = SourceEvent(
                id=uuid4().hex,
                data_source_id=source.sag_source_config_id,
                source_type="ARTICLE",
                source_id="candidate-input",
                title=f"Hidden event {index}",
                summary="hidden-candidate-context",
                content="hidden-candidate-context",
                status="COMPLETED",
                chunk_id=hidden_chunk,
            )
            session.add(event)
            await session.flush()
            session.add_all(
                [
                    EventEntity(id=uuid4().hex, event_id=event.id, entity_id=entity_id),
                    EventEntity(id=uuid4().hex, event_id=event.id, entity_id=hidden_entity_id),
                ]
            )
        event = SourceEvent(
            id=uuid4().hex,
            data_source_id=source.sag_source_config_id,
            source_type="ARTICLE",
            source_id=document.sag_source_id,
            title="Published event",
            summary="published-original-context",
            content="published-original-context",
            status="COMPLETED",
            chunk_id=published_chunk,
        )
        session.add(event)
        await session.flush()
        session.add(EventEntity(id=uuid4().hex, event_id=event.id, entity_id=entity_id))
        session.add_all(
            [
                SourceChunk(
                    id=hidden_chunk,
                    data_source_id=source.sag_source_config_id,
                    source_type="ARTICLE",
                    source_id="candidate-input",
                    content="hidden-candidate-chunk",
                    rank=0,
                ),
                SourceChunk(
                    id=published_chunk,
                    data_source_id=source.sag_source_config_id,
                    source_type="ARTICLE",
                    source_id=document.sag_source_id,
                    content="published-original-chunk",
                    rank=1,
                ),
            ]
        )
        await session.commit()
    return manager, source, document, entity_id, published_chunk, hidden_chunk


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["stable", "none", "graph", "chunk", "graph_existing"])
async def test_search_tool_graph_and_sparse_evidence_keep_one_publication(publication_evidence, stage, monkeypatch):
    from sqlalchemy import select
    from zleap.sag.db.models import SourceEvent

    from sag_api.sag import RetrievedSection, SearchOutcome
    from sag_api.tools import builtin
    from sag_api.tools.base import ToolContext

    manager, source, document, _entity, published_chunk, *_ = publication_evidence
    factory = await manager.get_sag_session_factory(source.sag_source_config_id)
    async with factory() as session:
        events = (await session.scalars(select(SourceEvent))).all()
    scores = {
        (source.sag_source_config_id, event.id): 0.99 if event.source_id == "candidate-input" else 0.8
        for event in events
    }

    async def recall(*args, **kwargs):
        return scores

    async def retrieve(*args, **kwargs):
        sections = (
            [
                RetrievedSection(
                    source_config_id=source.sag_source_config_id,
                    source_id=document.sag_source_id,
                    chunk_id=published_chunk,
                    content="published-original-chunk",
                    score=0.8,
                )
            ]
            if stage == "graph_existing"
            else []
        )
        return SearchOutcome(query="Acme", sections=sections)

    async def switch():
        async with SessionLocal() as session:
            saved = await session.get(Document, document.id)
            saved.sag_source_id = "candidate-input"
            await session.commit()

    monkeypatch.setattr(builtin, "recall_event_scores", recall)
    monkeypatch.setattr(builtin, "retrieve_relevant_sections", retrieve)
    if stage == "none":
        async with SessionLocal() as session:
            saved = await session.get(Document, document.id)
            saved.sag_source_id = None
            await session.commit()
    elif stage in {"graph", "graph_existing"}:
        original = manager.graph_for_sections

        async def graph(*args, **kwargs):
            result = await original(*args, **kwargs)
            await switch()
            return result

        monkeypatch.setattr(manager, "graph_for_sections", graph)
    elif stage == "chunk":
        original = manager.get_chunk

        async def chunk(*args, **kwargs):
            result = await original(*args, **kwargs)
            await switch()
            return result

        monkeypatch.setattr(manager, "get_chunk", chunk)
    result = await builtin.SearchContextTool().invoke(
        {"query": "Acme", "top_k": 1},
        ToolContext(
            engine_manager=manager,
            sources=[source],
        ),
    )
    assert "hidden-candidate" not in result.content
    if stage == "stable":
        assert "published-original-chunk" in result.content
        assert [item.source_id for item in result.data["sections"]] == [document.sag_source_id]
        assert [event.title for event in result.data["_graph"].events] == ["Published event"]
        assert len(result.citations) == 1
    else:
        assert "published-original" not in result.content
        assert result.citations == [] and result.data["event_count"] == 0


@pytest.mark.asyncio
async def test_rest_event_graph_omits_unpublished_direct_event_hits(publication_evidence):
    from sqlalchemy import select
    from zleap.sag.db.models import SourceEvent

    from sag_api.api.v1.search import _event_graph_fields

    manager, source, *_ = publication_evidence
    factory = await manager.get_sag_session_factory(source.sag_source_config_id)
    async with factory() as session:
        events = (await session.scalars(select(SourceEvent))).all()
    fields = await _event_graph_fields(
        manager,
        [],
        {source.sag_source_config_id: source},
        event_scores={
            (source.sag_source_config_id, event.id): 0.99 if event.source_id == "candidate-input" else 0.8
            for event in events
        },
    )
    assert [event.title for event in fields["events"]] == ["Published event"]
    assert [entity.name for entity in fields["entities"]] == ["Published person"]
    assert len(fields["relations"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["source", "global"])
async def test_rest_search_discards_old_body_when_publication_switches_during_graph(
    publication_evidence,
    monkeypatch,
    entry,
):
    from sag_api.api.v1 import search as routes
    from sag_api.sag import RetrievedSection, SearchOutcome
    from sag_api.schemas.search import GlobalSearchRequest, SearchRequest

    manager, source, document, _entity, chunk_id, *_ = publication_evidence

    async def retrieve(*args, **kwargs):
        return SearchOutcome(
            query="Acme",
            sections=[
                RetrievedSection(
                    source_config_id=source.sag_source_config_id,
                    source_id=document.sag_source_id,
                    chunk_id=chunk_id,
                    content="published-original-chunk",
                    score=0.8,
                )
            ],
        )

    async def recall(*args, **kwargs):
        return {}

    original = manager.graph_for_sections

    async def graph(*args, **kwargs):
        result = await original(*args, **kwargs)
        async with SessionLocal() as session:
            saved = await session.get(Document, document.id)
            saved.sag_source_id = "candidate-input"
            await session.commit()
        return result

    monkeypatch.setattr(routes, "retrieve_relevant_sections", retrieve)
    monkeypatch.setattr(routes, "recall_event_scores", recall)
    monkeypatch.setattr(manager, "graph_for_sections", graph)
    async with SessionLocal() as session:
        if entry == "source":
            response = await routes.search(
                source.id,
                SearchRequest(query="Acme", top_k=1),
                _user=None,
                session=session,
                engine_manager=manager,
                llm=None,
            )
        else:
            prepared = await routes._prepare_global_search(
                session,
                manager,
                GlobalSearchRequest(query="Acme", source_ids=[source.id], top_k=1),
            )
            assert prepared.outcome.sections == []
            response = prepared.response
    assert response.sections == [] and response.events == [] and response.entities == []
    assert response.relations == [] and response.source_hits == []


@pytest.mark.asyncio
@pytest.mark.parametrize("publication", ["published", "none"])
async def test_universe_timeline_scopes_results_counts_and_entity_heat(publication_evidence, publication):
    from sag_api.services.universe_query import universe_timeline

    manager, source, document, *_ = publication_evidence
    async with SessionLocal() as session:
        if publication == "none":
            saved = await session.get(Document, document.id)
            saved.sag_source_id = None
            await session.commit()
        page = await universe_timeline(
            session,
            manager,
            user_id="publication-test",
            source_id=source.id,
            limit=1,
            direction="older",
            cursor=None,
            snapshot_id=None,
        )
    assert page["total_events"] == (1 if publication == "published" else 0)
    assert len(page["bundles"]) == (1 if publication == "published" else 0)
    if publication == "published":
        bundle = page["bundles"][0]
        assert bundle["event"]["label"] == "Published event"
        assert bundle["ordinal"] == 0
        assert [(node["label"], node["related_count"]) for node in bundle["nodes"]] == [("Published person", 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["expand", "detail"])
@pytest.mark.parametrize("kind,candidate", [("event", False), ("event", True), ("entity", False), ("entity", True)])
async def test_universe_nodes_require_published_associations(publication_evidence, entry, kind, candidate):
    from sqlalchemy import select
    from zleap.sag.db.models import Entity, SourceEvent

    from sag_api.services.universe_query import universe_expand, universe_node_detail

    manager, source, _document, entity_id, *_ = publication_evidence
    factory = await manager.get_sag_session_factory(source.sag_source_config_id)
    async with factory() as session:
        if kind == "event":
            node_id = await session.scalar(
                select(SourceEvent.id).where(
                    SourceEvent.title == ("Hidden event 0" if candidate else "Published event"),
                )
            )
        else:
            node_id = (
                await session.scalar(select(Entity.id).where(Entity.normalized_name == "candidate"))
                if candidate
                else entity_id
            )

    async def read(session):
        if entry == "expand":
            return await universe_expand(
                session,
                manager,
                user_id="publication-test",
                source_id=source.id,
                node_kind=kind,
                node_id=node_id,
                limit=1,
                cursor=None,
                snapshot_id=None,
                after=None,
                before=None,
            )
        return await universe_node_detail(session, manager, kind, node_id, source_id=source.id)

    async with SessionLocal() as session:
        if candidate:
            with pytest.raises(NotFoundError):
                await read(session)
            return
        response = await read(session)
    if entry == "expand":
        assert response["anchor"]["related_count"] == 1
        assert len(response["nodes"]) == 1
        assert response["nodes"][0]["label"] == ("Published person" if kind == "event" else "Published event")
    elif kind == "event":
        assert response["evidence"]["content"] == "published-original-chunk"


@pytest.mark.asyncio
async def test_universe_overview_counts_only_published_relations(publication_evidence):
    from sqlalchemy import select

    from sag_api.db.models import UniversePartition, User
    from sag_api.services.universe_service import rebuild_universe_overview

    manager, source, *_ = publication_evidence
    async with SessionLocal() as session:
        user = User(email=f"publication-{uuid4().hex}@test", password_hash="unused")
        session.add(user)
        await session.commit()
        try:
            overview = await rebuild_universe_overview(session, manager, user_id=user.id)
            partition = await session.scalar(
                select(UniversePartition).where(
                    UniversePartition.overview_id == overview.id,
                    UniversePartition.source_id == source.id,
                    UniversePartition.kind == "source",
                )
            )
            assert (partition.event_count, partition.entity_count, partition.relation_count) == (1, 1, 1)
            assert sum(bucket["count"] for bucket in partition.time_buckets) == 1
        finally:
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()


@pytest.mark.asyncio
async def test_unqualified_legacy_universe_aggregate_is_not_reused(publication_evidence):
    from sag_api.db.models import UniverseOverview, UniversePartition, User
    from sag_api.services.universe_query import universe_manifest
    from sag_api.services.universe_service import _previous_source_stats

    _manager, source, *_ = publication_evidence
    async with SessionLocal() as session:
        user = User(email=f"publication-{uuid4().hex}@test", password_hash="unused")
        session.add(user)
        await session.flush()
        overview = UniverseOverview(
            user_id=user.id,
            schema_version=3,
            is_active=True,
            status="ready",
            source_count=1,
            event_count=5,
            entity_count=2,
            relation_count=9,
        )
        session.add(overview)
        await session.flush()
        session.add(
            UniversePartition(
                overview_id=overview.id,
                user_id=user.id,
                source_id=source.id,
                kind="source",
                key=source.id,
                label=source.name,
                x=0,
                y=0,
                event_count=5,
                entity_count=2,
                relation_count=9,
            )
        )
        await session.commit()
        try:
            assert await _previous_source_stats(session, user.id) == {}
            manifest = await universe_manifest(session, user_id=user.id)
            partition = next(item for item in manifest["partitions"] if item["source_id"] == source.id)
            assert manifest["version"] is None
            assert partition["event_count"] == 0
            assert manifest["counts"]["entities"] == 0
            assert manifest["counts"]["relations"] == 0
        finally:
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["rest", "mcp"])
async def test_outline_rejects_unqualified_legacy_input(publication_evidence, entry):
    manager, source, document, *_ = publication_evidence
    async with SessionLocal() as session:
        saved = await session.get(Document, document.id)
        saved.control_revision = 0
        await session.commit()
        if entry == "rest":
            from sag_api.api.v1.knowledge import outline

            with pytest.raises(NotFoundError):
                await outline(source.id, document_id=document.id, _user=None, session=session, engine_manager=manager)
            return
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from sag_api.mcp.server import build_source_mcp, use_scope

    with use_scope(manager, (source,)):
        async with connect(build_source_mcp()) as client:
            await client.initialize()
            response = await client.call_tool("outline", {"document_id": document.id})
            text = "\n".join(block.text for block in response.content if block.type == "text")
    assert "尚无大纲" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("publication", ["published", "none"])
async def test_entity_heat_scope_precedes_limit(publication_evidence, publication):
    from sag_api.services.insight_service import list_entities

    manager, source, document, entity_id, *_ = publication_evidence
    if publication == "none":
        async with SessionLocal() as session:
            saved = await session.get(Document, document.id)
            saved.sag_source_id = None
            await session.commit()
    rows = await list_entities(manager, source, limit=1)
    assert [(row.id, row.heat) for row in rows] == ([(entity_id, 1)] if publication == "published" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["rest", "mcp"])
@pytest.mark.parametrize("name", ["Published person", "Candidate person"])
async def test_entity_context_entries_hide_candidate_contributions(publication_evidence, entry, name):
    manager, source, *_ = publication_evidence
    if entry == "rest":
        from sag_api.api.v1.knowledge import entity_context

        async with SessionLocal() as session:
            if name == "Candidate person":
                with pytest.raises(NotFoundError):
                    await entity_context(source.id, name, _user=None, session=session, engine_manager=manager)
                return
            response = await entity_context(source.id, name, _user=None, session=session, engine_manager=manager)
            text = response.context
    else:
        from mcp.shared.memory import create_connected_server_and_client_session as connect

        from sag_api.mcp.server import build_source_mcp, use_scope

        with use_scope(manager, (source,)):
            async with connect(build_source_mcp()) as client:
                await client.initialize()
                response = await client.call_tool("get_entity", {"name": name})
                assert not response.isError
                text = "\n".join(block.text for block in response.content if block.type == "text")
        if name == "Candidate person":
            assert "未找到" in text
            return
    assert "hidden-candidate" not in text
    assert "published-original-context" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["rest", "mcp"])
@pytest.mark.parametrize("candidate", [False, True])
async def test_citation_entries_require_current_publication(publication_evidence, entry, candidate):
    manager, source, _document, _entity, published_chunk, hidden_chunk = publication_evidence
    chunk_id = hidden_chunk if candidate else published_chunk
    if entry == "rest":
        from sag_api.api.v1.sources import get_chunk

        async with SessionLocal() as session:
            if candidate:
                with pytest.raises(NotFoundError):
                    await get_chunk(source.id, chunk_id, _user=None, session=session, engine_manager=manager)
                return
            response = await get_chunk(source.id, chunk_id, _user=None, session=session, engine_manager=manager)
            text = response["content"]
    else:
        from mcp.shared.memory import create_connected_server_and_client_session as connect

        from sag_api.mcp.server import build_source_mcp, use_scope

        with use_scope(manager, (source,)):
            async with connect(build_source_mcp()) as client:
                await client.initialize()
                response = await client.call_tool("get_chunk", {"chunk_id": chunk_id})
                assert not response.isError
                text = "\n".join(block.text for block in response.content if block.type == "text")
        if candidate:
            assert "未找到" in text
            return
    assert "hidden-candidate" not in text
    assert "published-original-chunk" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["rest", "mcp"])
@pytest.mark.parametrize("publication", ["published", "none", "switch_during_read"])
async def test_grep_entries_filter_publications_before_limit(publication_body, entry, publication, monkeypatch):
    """An unfiltered entry exposes a candidate or lets it occupy the only result slot."""
    from zleap.sag.db.models import SourceChunk

    manager, source, document = publication_body
    factory = await manager.get_sag_session_factory(source.sag_source_config_id)
    async with factory() as session:
        session.add_all(
            [
                SourceChunk(
                    id=uuid4().hex,
                    data_source_id=source.sag_source_config_id,
                    source_type="ARTICLE",
                    source_id="unpublished-candidate",
                    heading="Candidate",
                    content="KEYWORD hidden-candidate",
                    rank=0,
                ),
                SourceChunk(
                    id=uuid4().hex,
                    data_source_id=source.sag_source_config_id,
                    source_type="ARTICLE",
                    source_id=document.sag_source_id,
                    heading="Published",
                    content="KEYWORD published-original",
                    rank=1,
                ),
            ]
        )
        await session.commit()
    if publication == "none":
        async with SessionLocal() as session:
            stored = await session.get(Document, document.id)
            stored.sag_source_id = None
            await session.commit()
    elif publication == "switch_during_read":
        original_grep = manager.grep_chunks

        async def switched(*args, **kwargs):
            rows = await original_grep(*args, **kwargs)
            async with SessionLocal() as session:
                stored = await session.get(Document, document.id)
                stored.sag_source_id = "unpublished-candidate"
                await session.commit()
            return rows

        monkeypatch.setattr(manager, "grep_chunks", switched)
    if entry == "rest":
        from sag_api.api.v1.knowledge import grep

        async with SessionLocal() as session:
            response = await grep(
                source.id,
                pattern="KEYWORD",
                limit=1,
                _user=None,
                session=session,
                engine_manager=manager,
            )
        assert response.count == (1 if publication == "published" else 0)
        content = "\n".join(match.snippet for match in response.matches)
    else:
        from mcp.shared.memory import create_connected_server_and_client_session as connect

        from sag_api.mcp.server import build_source_mcp, use_scope

        with use_scope(manager, (source,)):
            async with connect(build_source_mcp()) as client:
                await client.initialize()
                response = await client.call_tool("grep", {"pattern": "KEYWORD", "limit": 1})
                assert not response.isError
                content = "\n".join(block.text for block in response.content if block.type == "text")
    assert "hidden-candidate" not in content
    assert ("published-original" in content) is (publication == "published")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,revision,input_present,available",
    [
        (DocumentStatus.PAUSED, 1, True, True),
        (DocumentStatus.FAILED, 1, True, True),
        (DocumentStatus.EXTRACTING, 1, True, True),
        (DocumentStatus.PENDING, 1, False, False),
        (DocumentStatus.READY, 0, True, False),
        (DocumentStatus.DELETING, 1, True, False),
        (DocumentStatus.DELETE_FAILED, 1, True, False),
    ],
)
async def test_line_read_uses_only_qualified_published_content(
    publication_body,
    status,
    revision,
    input_present,
    available,
):
    manager, source, document = publication_body
    document.status = status
    document.control_revision = revision
    if not input_present:
        document.sag_source_id = None

    lines = await read_document_lines(document, source, manager)

    assert lines == (["# Published original\n"] if available else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [DocumentStatus.PAUSED, DocumentStatus.FAILED, DocumentStatus.EXTRACTING])
async def test_parsed_endpoint_keeps_prior_publication_readable(publication_body, status):
    manager, source, document = publication_body
    async with SessionLocal() as session:
        saved = await session.get(Document, document.id)
        saved.status = status
        await session.commit()
        response = await get_parsed(
            source_id=source.id,
            document_id=document.id,
            _user=None,
            session=session,
            engine_manager=manager,
        )

    assert response.body == b"# Published original\n"


@pytest.mark.asyncio
async def test_parsed_endpoint_rejects_unqualified_legacy_pointer(publication_body):
    manager, source, document = publication_body
    async with SessionLocal() as session:
        saved = await session.get(Document, document.id)
        saved.status = DocumentStatus.READY
        saved.control_revision = 0
        await session.commit()
        with pytest.raises((ConflictError, NotFoundError)):
            await get_parsed(
                source_id=source.id,
                document_id=document.id,
                _user=None,
                session=session,
                engine_manager=manager,
            )


@pytest.mark.asyncio
async def test_paused_candidate_retains_published_source_counts(publication_body):
    _manager, source, _document = publication_body
    async with SessionLocal() as session:
        saved = await session.get(Source, source.id)
        await _refresh_source_counts(session, saved)
        await session.commit()

        assert saved.document_count == 1
        assert saved.chunk_count == 3
        assert saved.event_count == 5


@pytest.mark.asyncio
async def test_graph_reads_only_qualified_publications(publication_body):
    from zleap.sag.db.models import SourceEvent

    from sag_api.services.insight_service import get_source_graph

    manager, source, document = publication_body
    unpublished_input = uuid4().hex
    async with SessionLocal() as session:
        source.document_count = 3
        session.add(source)
        session.add_all(
            [
                Document(source_id=source.id, filename="first-candidate.md", storage_path="/test/candidate.md"),
                Document(
                    source_id=source.id,
                    filename="legacy-incomplete.md",
                    storage_path="/test/legacy.md",
                    status=DocumentStatus.EXTRACTING,
                    sag_source_id=unpublished_input,
                    control_revision=0,
                ),
            ]
        )
        await session.commit()
    factory = await manager.get_sag_session_factory(source.sag_source_config_id)
    async with factory() as session:
        session.add_all(
            [
                SourceEvent(
                    id=uuid4().hex,
                    data_source_id=source.sag_source_config_id,
                    source_type="ARTICLE",
                    source_id=input_id,
                    title=title,
                    summary=title,
                    content=title,
                )
                for input_id, title in [
                    (document.sag_source_id, "Published event"),
                    (unpublished_input, "Unpublished event"),
                ]
            ]
        )
        await session.commit()
    async with SessionLocal() as session:
        graph = await get_source_graph(session, manager, source)

    assert [node.id for node in graph.documents] == [document.id]
    assert [event.title for event in graph.events] == ["Published event"]
    assert graph.counts.documents == 1


@pytest.mark.asyncio
async def test_yielded_candidate_is_queued_without_losing_its_publication(publication_body):
    from sag_api.db.models import Job
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs.inproc import _mark_document_queued

    _manager, source, document = publication_body
    async with SessionLocal() as session:
        saved = await session.get(Document, document.id)
        saved.status = DocumentStatus.EXTRACTING
        saved.processing_stage = "extracting"
        saved.processing_run_id = uuid4().hex
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=document.id,
            payload={"control_revision": 1},
        )
        session.add(job)
        await session.commit()
        await _mark_document_queued(session, job)
        await session.commit()
        await session.refresh(saved)

        assert saved.status == DocumentStatus.PENDING
        assert saved.processing_stage == "queued"
        assert saved.processing_run_id is None
        assert saved.sag_source_id == document.sag_source_id
        assert saved.chunk_count == 3 and saved.event_count == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["source", "document"])
async def test_export_selects_prior_publication_while_candidate_is_paused(publication_body, scope):
    from sag_api.services.octx_export import create_document_export_transfer, create_export_transfer

    _manager, source, document = publication_body

    class Queue:
        async def enqueue(self, _job_id):
            pass

    async with SessionLocal() as session:
        if scope == "source":
            transfer = await create_export_transfer(session, source.id, version=None, job_queue=Queue())
        else:
            transfer = await create_document_export_transfer(
                session,
                source.id,
                document.id,
                version=None,
                job_queue=Queue(),
            )
        assert transfer.checkpoint["selected_document_ids"] == [document.id]
        assert transfer.checkpoint["selected_article_ids"] == [document.sag_source_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [DocumentStatus.READY, DocumentStatus.PAUSED, DocumentStatus.FAILED])
async def test_vector_identity_follows_publication_while_replacement_is_unfinished(publication_body, status):
    from types import SimpleNamespace

    from sag_api.sag.document_vector_identity import refresh_source_vector_identity
    from sag_api.sag.octx_vector_protocol import embedding_identity

    _manager, source, document = publication_body
    old_identity = embedding_identity(
        SimpleNamespace(model="published-model", base_url="https://local.invalid", dimensions=2)
    )
    other_identity = {**old_identity, "model": "candidate-model"}
    async with SessionLocal() as session:
        document = await session.get(Document, document.id)
        document.status = status
        document.vector_identity = old_identity
        session.add_all(
            [
                Document(
                    source_id=source.id,
                    filename="first-candidate.md",
                    storage_path="candidate.md",
                    status=DocumentStatus.PAUSED,
                    vector_identity=other_identity,
                ),
                Document(
                    source_id=source.id,
                    filename="unqualified-legacy.md",
                    storage_path="legacy.md",
                    status=DocumentStatus.PAUSED,
                    sag_source_id="incomplete-legacy",
                    control_revision=0,
                    vector_identity=other_identity,
                ),
            ]
        )
        await session.commit()
        source = await session.get(Source, source.id)
        await refresh_source_vector_identity(session, source)
        assert source.config["octx_vector_identity_state"] == "known"
        assert source.config["octx_vector_identity"] == old_identity
