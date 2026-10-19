"""JEV's scored evidence survives every existing retrieval transport."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sag_api.core.config import settings
from sag_api.sag import GraphEventInfo, RetrievedSection, SearchOutcome, SourceGraphInfo
from sag_api.schemas.search import GlobalSearchRequest, SearchRequest
from sag_api.services.agent_service import _adapt_tool
from sag_api.tools.base import ToolContext, ToolResult
from sag_api.tools.builtin import SearchContextTool


def _source():
    return SimpleNamespace(id="source-1", name="资料", sag_source_config_id="config-1")


def _outcome(*, fallback=False):
    return SearchOutcome(
        query="问题",
        sections=[
            RetrievedSection(
                chunk_id="first", heading="第一", content="第一证据", score=0.42, source_config_id="config-1"
            ),
            RetrievedSection(
                chunk_id="second", heading="第二", content="第二证据", score=0.21, source_config_id="config-1"
            ),
        ],
        stats={
            "requested_strategy": "multi_jev",
            "effective_strategy": "multi_jev",
            "fallback_used": fallback,
            "ranking": {"method": "jev", "fallback_reason": "timeout" if fallback else None},
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "persona,global_strategy,expected",
    [
        ({}, "multi_jev", "multi_jev"),
        ({"search_strategy": "multi_jev"}, "vector", "multi_jev"),
        ({"search_strategy": "vector"}, "multi_jev", "vector"),
        ({}, "multi", "vector"),
    ],
)
async def test_agent_strategy_inheritance_keeps_jev_order(monkeypatch, persona, global_strategy, expected):
    from sag_api.tools import builtin

    monkeypatch.setattr(settings, "search_strategy", global_strategy)
    outcome = _outcome()
    retrieve = AsyncMock(return_value=outcome)
    recall = AsyncMock(return_value={})
    prioritize = AsyncMock(side_effect=AssertionError("JEV order must remain native"))
    monkeypatch.setattr(builtin, "retrieve_relevant_sections", retrieve)
    monkeypatch.setattr(builtin, "recall_event_scores", recall)
    monkeypatch.setattr(builtin, "_prioritize_event_evidence", prioritize)
    graph = (
        SourceGraphInfo(
            events=[
                GraphEventInfo(
                    id="event-second",
                    source_id="document-1",
                    source_config_id="config-1",
                    chunk_id="second",
                    title="第二事项",
                    score=0.99,
                )
            ]
        )
        if expected == "multi_jev"
        else SourceGraphInfo()
    )
    engine = SimpleNamespace(graph_for_sections=AsyncMock(return_value=graph))
    result = await SearchContextTool().invoke(
        {"query": "问题"},
        ToolContext(engine_manager=engine, sources=[_source()], persona=persona),
    )

    assert retrieve.call_args.kwargs["strategy"] == expected
    if expected == "multi_jev":
        recall.assert_not_awaited()
        prioritize.assert_not_awaited()
        assert [citation["chunk_id"] for citation in result.citations] == ["first", "second"]
        assert [citation["score"] for citation in result.citations] == [0.42, 0.21]
    assert result.data["retrieval_stats"] == outcome.stats


@pytest.mark.asyncio
async def test_agent_retrieval_details_filter_raw_diagnostics(monkeypatch):
    stats = {
        **_outcome(fallback=True).stats,
        "error": "secret-key and /private/nas/file",
        "ranking": {
            "method": "jev", "fallback_reason": "timeout", "error": "secret-key",
            "scored_chunks": 2, "query_chunk_fallback_chunks": 1,
        },
        "source_stats": [
            {
                "source_config_id": "config-1",
                "error": "secret-key",
                "ranking": {
                    "method": "jev", "fallback_reason": "timeout",
                    "scored_chunks": 2, "query_chunk_fallback_chunks": 1,
                },
            },
            {
                "source_config_id": "config-1",
                "ranking": {"method": "jev", "scored_chunks": True, "query_chunk_fallback_chunks": -1},
            }
        ],
    }
    tool = SearchContextTool()
    monkeypatch.setattr(
        tool,
        "invoke",
        AsyncMock(
            return_value=ToolResult(
                content="证据",
                data={"retrieval_stats": stats},
            )
        ),
    )
    adapted = _adapt_tool(tool, ToolContext(engine_manager=None, sources=[_source()]), [])
    result = await adapted.execute(
        {},
        SimpleNamespace(
            cancellation=SimpleNamespace(raise_if_cancelled=lambda: None),
        ),
    )

    retrieval = result.details["retrieval"]
    assert set(retrieval) == {"requested_strategy", "effective_strategy", "fallback_used", "ranking", "source_stats"}
    assert retrieval["ranking"] == {
        "method": "jev", "fallback_reason": "timeout", "scored_chunks": 2, "query_chunk_fallback_chunks": 1,
    }
    assert retrieval["source_stats"][0]["ranking"]["fallback_reason"] == "timeout"
    assert retrieval["source_stats"][0]["ranking"]["scored_chunks"] == 2
    assert "scored_chunks" not in retrieval["source_stats"][1]["ranking"]
    assert "query_chunk_fallback_chunks" not in retrieval["source_stats"][1]["ranking"]
    assert "secret-key" not in json.dumps(result.details)
    assert "config-1" not in json.dumps(result.details)


@pytest.mark.asyncio
@pytest.mark.parametrize("initial,updated", [("vector", "multi_jev"), ("multi_jev", "vector")])
async def test_agent_keeps_the_strategy_used_to_choose_its_tool_budget(monkeypatch, initial, updated):
    from sag_api.tools import builtin

    monkeypatch.setattr(settings, "search_strategy", initial)
    retrieve = AsyncMock(return_value=_outcome())
    monkeypatch.setattr(builtin, "retrieve_relevant_sections", retrieve)
    monkeypatch.setattr(builtin, "recall_event_scores", AsyncMock(return_value={}))
    context = ToolContext(engine_manager=None, sources=[_source()])
    adapted = _adapt_tool(SearchContextTool(), context, [])
    monkeypatch.setattr(settings, "search_strategy", updated)

    await adapted.execute(
        {"query": "问题"}, SimpleNamespace(cancellation=SimpleNamespace(raise_if_cancelled=lambda: None)),
    )

    assert retrieve.call_args.kwargs["strategy"] == initial
    assert context.persona == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["single", "global", "stream", "compare"])
@pytest.mark.parametrize("request_strategy", [None, "multi_jev"])
async def test_search_jev_does_not_supplement_native_evidence(monkeypatch, transport, request_strategy):
    from sag_api.api.v1 import search as api

    monkeypatch.setattr(settings, "search_strategy", "multi_jev")
    retrieve = AsyncMock(return_value=_outcome())
    recall = AsyncMock(side_effect=AssertionError("independent event recall must not run for JEV"))
    monkeypatch.setattr(api, "retrieve_relevant_sections", retrieve)
    monkeypatch.setattr(api, "recall_event_scores", recall)
    monkeypatch.setattr(api, "get_source", AsyncMock(return_value=_source()))
    monkeypatch.setattr(api, "search_source_candidates", AsyncMock(return_value=[_source()]))
    monkeypatch.setattr(api, "synthesize_search_answer", AsyncMock(return_value="答案 [1]"))

    async def stream(*_args, **_kwargs):
        yield SimpleNamespace(kind="completed", text="答案 [1]")

    monkeypatch.setattr(api, "stream_synthesize_search_answer", stream)
    engine = SimpleNamespace(graph_for_sections=AsyncMock(return_value=SourceGraphInfo()))
    session = SimpleNamespace(commit=AsyncMock())
    if transport == "single":
        response = await api.search(
            "source-1", SearchRequest(query="问题", strategy=request_strategy), None, session, engine, None
        )
    elif transport == "compare":
        response = await api._run_one_strategy(engine, [_source()], "问题", "multi_jev", 8, {"config-1": _source()})
    else:
        body = GlobalSearchRequest(query="问题", strategy=request_strategy)
        if transport == "global":
            response = await api.global_search(body, None, session, engine, None)
        else:
            response_stream = await api.global_search_stream(body, None, session, engine, None)
            frames = [frame async for frame in response_stream.body_iterator]
            result = next(json.loads(frame["data"]) for frame in frames if frame["event"] == "result")
            assert [section["chunk_id"] for section in result["sections"]] == ["first", "second"]
            assert result["stats"]["ranking"]["method"] == "jev"
            recall.assert_not_awaited()
            return
    recall.assert_not_awaited()
    assert [section.chunk_id for section in response.sections] == ["first", "second"]
    assert [section.score for section in response.sections] == [0.42, 0.21]
    assert response.stats["ranking"]["method"] == "jev"


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_mcp_inherits_global_jev_and_reports_real_fallback(monkeypatch, fallback):
    from sag_api.mcp import server

    monkeypatch.setattr(settings, "search_strategy", "multi_jev")
    retrieve = AsyncMock(return_value=_outcome(fallback=fallback))
    monkeypatch.setattr(server, "retrieve_relevant_sections", retrieve)
    mcp = server.build_source_mcp()
    with server.use_scope(SimpleNamespace(), [_source()]):
        content = await mcp.call_tool("search", {"query": "问题"})
    blocks = content[0] if isinstance(content, tuple) else content
    text = "\n".join(block.text for block in blocks)
    assert retrieve.call_args.kwargs["strategy"] == "multi_jev"
    assert text.index("第一证据") < text.index("第二证据")
    assert ("超时" in text and "降级" in text) is fallback
    assert "secret" not in text
