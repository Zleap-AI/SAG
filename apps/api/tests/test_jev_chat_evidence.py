"""Chat hydrates only the events selected by JEV, using the real graph reader."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable
from zleap.sag.db.models import Entity, EventEntity, SourceEvent

from sag_api.sag import RetrievedSection, SearchOutcome
from sag_api.sag.content_reader import ContentReader
from sag_api.tools import builtin
from sag_api.tools.base import ToolContext


@pytest.fixture
async def chat_runtime(monkeypatch):
    database = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with database.begin() as connection:
        for model in (SourceEvent, Entity, EventEntity):
            await connection.execute(CreateTable(model.__table__))
    sessions = async_sessionmaker(database, expire_on_commit=False)

    class Access:
        async def ensure_read_runtime(self, *_args, **_kwargs):
            pass

        async def relational_session_factory(self, *_args):
            return sessions

    reader = ContentReader(Access())
    manager = SimpleNamespace(
        _settings=SimpleNamespace(search_strategy="multi_jev"),
        graph_for_sections=reader.graph_for_sections,
    )
    source = SimpleNamespace(id="source", name="Knowledge", sag_source_config_id="cfg")
    monkeypatch.setattr(
        builtin, "published_document_inputs", AsyncMock(return_value={"cfg": ("document",)})
    )
    try:
        yield manager, sessions, source
    finally:
        await database.dispose()


def event(identifier, *, chunk="chunk", rank=0, document="document", status=None):
    return SourceEvent(
        id=identifier, data_source_id="cfg", source_type="article", source_id=document,
        chunk_id=chunk, title=f"Title {identifier}", summary=f"Summary {identifier}",
        content=f"Fact {identifier}", rank=rank, status=status,
    )


def section(*event_ids, chunk="chunk", score=0.93, score_source="jev"):
    return RetrievedSection(
        chunk_id=chunk, content=f"Original evidence {chunk}", source_id="document",
        source_config_id="cfg", score=score,
        metadata={"event_ids": list(event_ids), "score_source": score_source},
    )


async def invoke(monkeypatch, runtime, sections, *, event_scores=None, stats=None, offset=0):
    manager, _sessions, source = runtime
    recall = AsyncMock(return_value=event_scores or {})
    monkeypatch.setattr(builtin, "recall_event_scores", recall)
    monkeypatch.setattr(
        builtin, "retrieve_relevant_sections",
        AsyncMock(return_value=SearchOutcome(query="q", sections=sections, stats=stats or {})),
    )
    result = await builtin.SearchContextTool().invoke(
        {"query": "q"},
        ToolContext(engine_manager=manager, sources=[source], citation_offset=offset),
    )
    return result, recall


async def test_jev_chat_excludes_unselected_events_from_summary_graph_and_citations(monkeypatch, chat_runtime):
    _manager, sessions, _source = chat_runtime
    async with sessions() as session:
        session.add_all([
            event("selected"), event("unselected", rank=1),
            Entity(id="selected-entity", data_source_id="cfg", entity_type_id="person",
                   name="Selected entity", normalized_name="selected entity", type="person"),
            Entity(id="unselected-entity", data_source_id="cfg", entity_type_id="person",
                   name="Unselected entity", normalized_name="unselected entity", type="person"),
            EventEntity(id="selected-link", event_id="selected", entity_id="selected-entity", weight=1),
            EventEntity(id="unselected-link", event_id="unselected", entity_id="unselected-entity", weight=1),
        ])
        await session.commit()

    result, recall = await invoke(
        monkeypatch, chat_runtime, [section("selected")], event_scores={("cfg", "unselected"): 0.99},
        offset=4,
    )

    assert [item.id for item in result.data["_graph"].events] == ["selected"]
    assert [item.id for item in result.data["_graph"].entities] == ["selected-entity"]
    assert [item.event_id for item in result.data["_graph"].associations] == ["selected"]
    assert "[5] 事项：Title selected" in result.content
    assert "Summary selected" in result.content
    assert "unselected" not in result.content
    assert [item["id"] for item in result.citations[0]["event_refs"]] == ["selected"]
    assert result.citations[0]["n"] == 5
    recall.assert_not_awaited()


async def test_jev_chat_preserves_evidence_and_in_chunk_event_order(monkeypatch, chat_runtime):
    _manager, sessions, _source = chat_runtime
    async with sessions() as session:
        session.add_all([
            event("first-later", chunk="first", rank=9),
            event("first-earlier", chunk="first", rank=0),
            event("second", chunk="second"),
        ])
        await session.commit()

    result, recall = await invoke(monkeypatch, chat_runtime, [
        section("first-later", "first-earlier", chunk="first", score=0.2),
        section("second", chunk="second", score=0.99, score_source="event_score"),
    ])

    assert [item.id for item in result.data["_graph"].events] == ["first-later", "first-earlier", "second"]
    assert [item.chunk_id for item in result.data["sections"]] == ["first", "second"]
    assert [item["id"] for item in result.citations[0]["event_refs"]] == ["first-later", "first-earlier"]
    assert result.content.index("Title first-later") < result.content.index("Title second")
    recall.assert_not_awaited()


@pytest.mark.parametrize("score_source,event_ids", [("jev", ()), ("query_chunk_fallback", ("unscored",))])
async def test_jev_chunk_fallback_never_hydrates_unscored_events(
    monkeypatch, chat_runtime, score_source, event_ids,
):
    _manager, sessions, _source = chat_runtime
    async with sessions() as session:
        session.add(event("unscored"))
        await session.commit()

    result, recall = await invoke(
        monkeypatch, chat_runtime, [section(*event_ids, score_source=score_source)],
        event_scores={("cfg", "unscored"): 0.99},
    )

    assert result.data["event_count"] == 0
    assert not result.citations[0].get("event_refs")
    assert "Original evidence chunk" in result.content
    assert "Title unscored" not in result.content
    recall.assert_not_awaited()


async def test_jev_coarse_fallback_hydrates_only_its_evidence_events(monkeypatch, chat_runtime):
    _manager, sessions, _source = chat_runtime
    async with sessions() as session:
        session.add_all([event("coarse"), event("unselected", rank=1)])
        await session.commit()

    result, recall = await invoke(
        monkeypatch, chat_runtime, [section("coarse", score_source="event_score")],
        stats={"fallback_used": True, "ranking": {"method": "score", "fallback_reason": "timeout"}},
    )

    assert [item.id for item in result.data["_graph"].events] == ["coarse"]
    assert result.content.startswith("（JEV 精排未完成，本次返回粗排结果）")
    assert [item["id"] for item in result.citations[0]["event_refs"]] == ["coarse"]
    recall.assert_not_awaited()


async def test_jev_chat_respects_published_inputs_and_deleted_events(monkeypatch, chat_runtime):
    _manager, sessions, _source = chat_runtime
    async with sessions() as session:
        session.add_all([
            event("published"), event("unpublished", document="old-document"),
            event("deleted", status="DELETED"),
        ])
        await session.commit()

    result, _recall = await invoke(
        monkeypatch, chat_runtime, [section("unpublished", "deleted", "published")],
    )

    assert [item.id for item in result.data["_graph"].events] == ["published"]
    assert [item["id"] for item in result.citations[0]["event_refs"]] == ["published"]


async def test_jev_failure_never_starts_parallel_event_recall(monkeypatch, chat_runtime):
    manager, _sessions, source = chat_runtime
    retrieve = AsyncMock(side_effect=RuntimeError("JEV failed"))
    recall = AsyncMock(return_value={})
    monkeypatch.setattr(builtin, "retrieve_relevant_sections", retrieve)
    monkeypatch.setattr(builtin, "recall_event_scores", recall)

    with pytest.raises(RuntimeError, match="JEV failed"):
        await builtin.SearchContextTool().invoke(
            {"query": "q"}, ToolContext(engine_manager=manager, sources=[source]),
        )

    recall.assert_not_awaited()


async def test_non_jev_chat_keeps_vector_event_recall_and_prioritization(monkeypatch, chat_runtime):
    manager, sessions, _source = chat_runtime
    manager._settings.search_strategy = "vector"
    async with sessions() as session:
        session.add_all([event("chunk-event"), event("vector-event", rank=1)])
        await session.commit()

    result, recall = await invoke(
        monkeypatch, chat_runtime, [section("chunk-event")], event_scores={("cfg", "vector-event"): 0.99},
    )

    assert [item.id for item in result.data["_graph"].events] == ["vector-event", "chunk-event"]
    assert "[1] 事项：Title vector-event" in result.content
    assert result.citations[0]["event_refs"][0]["id"] == "vector-event"
    recall.assert_awaited_once()
