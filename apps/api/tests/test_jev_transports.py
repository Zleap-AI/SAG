"""JEV execution budgets and ranking status survive the public adapters."""

from types import SimpleNamespace

import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect
from zleap.sag.exceptions import AIAuthError, AIInvalidRequestError, AITransientError, LLMContextLengthError
from zleap.sag.pipeline.errors import SearchStageError

from sag_api.core.error_taxonomy import ErrorLayer, ErrorStage
from sag_api.core.errors import ConfigurationError, ServiceUnavailableError, ValidationError
from sag_api.sag import SearchOutcome
from sag_api.sag.errors import map_sag_errors
from sag_api.services.agent_tooling import _adapt_tool
from sag_api.tools.base import ToolContext, ToolMeta, ToolResult


class SearchTool:
    meta = ToolMeta(name="search_context", description="search", parameters={"type": "object"})

    async def invoke(self, _arguments, _context):
        return ToolResult(
            content="evidence",
            data={
                "retrieval_stats": {
                    "requested_strategy": "multi_jev",
                    "effective_strategy": "multi_jev",
                    "fallback_used": True,
                    "ranking": {"method": "jev", "fallback_reason": "ranking_timeout"},
                },
            },
        )


@pytest.mark.parametrize("persona", [{}, {"search_strategy": "vector"}])
def test_search_tool_budget_covers_jev_fanout_and_preserves_explicit_override(monkeypatch, persona):
    import sag_api.core.config as config

    monkeypatch.setattr(config, "settings", SimpleNamespace(
        search_strategy="multi_jev", search_source_timeout=2.0, jev_ranking_timeout_seconds=4.0,
        search_source_candidate_limit=16, search_source_concurrency=4, agent_tool_timeout_seconds=5.0,
    ))
    # The caller may raise the JEV budget while the model is preparing a tool call.
    context = ToolContext(
        engine_manager=object(), sources=[SimpleNamespace(id=str(i)) for i in range(5)], persona=persona,
    )
    adapted = _adapt_tool(SearchTool(), context, [])
    config.settings.jev_ranking_timeout_seconds = 120.0
    if persona:
        assert adapted.spec.timeout_seconds is None
    else:
        # Configuration admission, two source waves, then graph/citation work.
        assert adapted.spec.timeout_seconds >= 368.0


@pytest.mark.asyncio
async def test_agent_tool_keeps_the_strategy_selected_when_the_run_was_prepared(monkeypatch):
    import sag_api.core.config as config

    active = SimpleNamespace(search_strategy="vector")
    monkeypatch.setattr(config, "settings", active)
    used = []

    class RecordingSearchTool(SearchTool):
        async def invoke(self, _arguments, context):
            used.append(context.persona.get("search_strategy"))
            return ToolResult(content="evidence")

    adapted = _adapt_tool(RecordingSearchTool(), ToolContext(engine_manager=object()), [])
    active.search_strategy = "multi_jev"
    await adapted.execute({}, SimpleNamespace(cancellation=SimpleNamespace(raise_if_cancelled=lambda: None)))
    assert used == ["vector"]


@pytest.mark.asyncio
async def test_agent_adapter_keeps_jev_fallback_status_in_details_and_artifacts():
    context = ToolContext(engine_manager=object())
    adapted = _adapt_tool(SearchTool(), context, [])
    result = await adapted.execute({}, SimpleNamespace(cancellation=SimpleNamespace(raise_if_cancelled=lambda: None)))
    assert result.details["retrieval_stats"]["fallback_used"] is True
    assert result.artifacts["retrieval_stats"]["ranking"]["fallback_reason"] == "ranking_timeout"


@pytest.mark.parametrize(("cause", "expected"), [
    (AIAuthError("private-fragment", provider="jev"), ConfigurationError),
    (AITransientError("private-fragment", provider="jev"), ServiceUnavailableError),
    (AIInvalidRequestError("private-fragment", provider="jev"), ValidationError),
    (LLMContextLengthError("private-fragment", provider="jev"), ValidationError),
    (TimeoutError("private-fragment"), ServiceUnavailableError),
])
def test_wrapped_jev_errors_keep_actionable_classification_without_provider_body(cause, expected):
    with pytest.raises(expected) as raised:
        with map_sag_errors(stage=ErrorStage.RETRIEVE):
            try:
                raise cause
            except Exception as error:
                raise SearchStageError("搜索失败", code="search_failed") from error
    assert raised.value.layer == ErrorLayer.ENGINE
    assert raised.value.stage == ErrorStage.RETRIEVE
    assert "private-fragment" not in raised.value.message


@pytest.mark.asyncio
async def test_mcp_search_marks_coarse_results_when_jev_falls_back(monkeypatch):
    import sag_api.mcp.server as server

    async def retrieve(*_args, **_kwargs):
        return SearchOutcome(query="q", sections=[], stats={
            "requested_strategy": "multi_jev", "effective_strategy": "multi_jev", "fallback_used": True,
            "ranking": {"method": "jev", "fallback_reason": "ranking_timeout"},
        })

    monkeypatch.setattr(server, "retrieve_relevant_sections", retrieve)
    mcp = server.build_source_mcp()
    source = SimpleNamespace(id="source", sag_source_config_id="config", name="source")
    with server.use_scope(object(), (source,)):
        async with connect(mcp) as client:
            await client.initialize()
            response = await client.call_tool("search", {"query": "q"})
    assert not response.isError
    text = "\n".join(item.text for item in response.content if hasattr(item, "text"))
    assert "JEV" in text and "粗排" in text


@pytest.mark.asyncio
async def test_search_sse_keeps_retryable_retrieval_error(monkeypatch, published_inputs):
    from sag_api.api.v1 import search
    from tests.test_search_stream import StreamingLLM, _search

    async def fail(*_args, **_kwargs):
        raise ServiceUnavailableError("JEV 精排服务暂不可用", layer=ErrorLayer.ENGINE, stage=ErrorStage.RETRIEVE)

    monkeypatch.setattr(search, "_prepare_global_search", fail)
    events = await _search(StreamingLLM([]), published_inputs)
    assert len(events) == 1 and events[0][0] == "error"
    error = events[0][1]
    assert error["code"] == "service_unavailable"
    assert error["stage"] == "retrieve" and error["layer"] == "engine"
    assert error["retryable"] is True


def test_search_response_keeps_jev_score_source():
    from sag_api.schemas.search import SectionOut

    section = SectionOut(
        chunk_id="chunk", heading="title", content="evidence", score=0.8, rank=0,
        source_id="source", metadata={"score_source": "jev"},
    )
    assert section.model_dump()["metadata"] == {"score_source": "jev"}


@pytest.mark.asyncio
async def test_dify_jev_threshold_uses_scored_evidence_and_reports_fallback(monkeypatch):
    from sag_api.api.v1 import dify
    from sag_api.core.config import settings
    from sag_api.sag import RetrievedSection
    from sag_api.schemas.dify import DifyRetrievalRequest

    monkeypatch.setattr(settings, "dify_search_strategy", "multi_jev")

    async def get_source(*_args):
        return SimpleNamespace(id="source", name="source")

    async def retrieve(*_args, **_kwargs):
        return SearchOutcome(query="q", sections=[
            RetrievedSection(content="scored", score=0.8, metadata={"score_source": "jev"}),
            RetrievedSection(content="vector top-up", score=0.95, metadata={"score_source": "query_chunk_fallback"}),
            RetrievedSection(content="below threshold", score=0.1, metadata={"score_source": "jev"}),
        ], stats={"effective_strategy": "multi_jev", "fallback_used": False})

    monkeypatch.setattr(dify, "get_source", get_source)
    monkeypatch.setattr(dify, "retrieve_relevant_sections", retrieve)
    result = await dify.retrieval(
        DifyRetrievalRequest(knowledge_id="source", query="q", retrieval_setting={"score_threshold": 0.6}),
        _authorized=None, session=None, engine_manager=None,
    )
    assert [record.content for record in result.records] == ["scored"]
    assert result.records[0].metadata["score_source"] == "jev"
