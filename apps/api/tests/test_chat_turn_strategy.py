"""Per-turn retrieval mode in chat: explicit choice wins, is gated before streaming, and reports its origin."""

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from sag_api.core.config import Settings, settings
from sag_api.sag import RetrievedSection, SearchOutcome, SourceGraphInfo
from sag_api.sag.engine_manager import EngineManager
from sag_api.schemas.agent import AskRequest
from sag_api.tools.builtin import knowledge_search_strategy, knowledge_search_strategy_origin


def configured(**changes) -> Settings:
    return Settings(_env_file=None).model_copy(update={
        "jev_endpoint": "https://jev.invalid/score",
        "jev_api_key": "synthetic-jev-key",
        "sag_vector_provider": "lancedb",
        "search_strategy": "multi_es_fast",
        **changes,
    })


def source(default: str | None, config_id: str = "cfg-1"):
    return SimpleNamespace(
        id=config_id, name=config_id, sag_source_config_id=config_id, default_search_strategy=default,
    )


def test_ask_request_accepts_optional_strategy():
    assert AskRequest(query="q").strategy is None
    assert AskRequest(query="q", strategy="sag2").strategy == "sag2"
    with pytest.raises(ValueError):
        AskRequest(query="q", strategy="nope")


def test_turn_choice_precedes_persona_source_default_and_global():
    configuration = configured(search_strategy="vector")
    persona = {"search_strategy": "multi"}
    scoped = [source("sag2")]
    assert knowledge_search_strategy(persona, configuration, scoped, "multi_jev") == "multi_jev"
    assert knowledge_search_strategy_origin(persona, scoped, "multi_jev") == "request"
    assert knowledge_search_strategy(persona, configuration, scoped) == "multi"
    assert knowledge_search_strategy_origin(persona, scoped) == "persona"
    assert knowledge_search_strategy({}, configuration, scoped) == "sag2"
    assert knowledge_search_strategy_origin({}, scoped) == "source_default"
    assert knowledge_search_strategy({}, configuration, []) == "vector"
    assert knowledge_search_strategy_origin({}, []) == "global"


@pytest.mark.asyncio
async def test_search_tool_uses_turn_choice_and_reports_origin(monkeypatch, publication_source):
    from sag_api.services.agent_tooling import _adapt_tool
    from sag_api.tools import builtin
    from sag_api.tools.base import ToolContext

    scoped = await publication_source("turn-choice", "cfg-1", "document-1")
    scoped.default_search_strategy = "sag2"
    engine = EngineManager(configured(search_strategy="multi_es_fast"))
    calls = []

    async def retrieve(manager, sources, query, *, strategy, top_k):
        calls.append(strategy)
        return SearchOutcome(query=query, sections=[RetrievedSection(
            chunk_id="c1", heading="h", content="evidence", score=0.5, source_config_id="cfg-1",
            source_id="document-1", metadata={"score_source": "jev"},
        )], stats={"requested_strategy": strategy, "effective_strategy": strategy, "strategy_origin": "request"})

    async def events(*_args, **_kwargs):
        return {}

    async def graph(*_args, **_kwargs):
        return SourceGraphInfo(events=[])

    engine.graph_for_sections = graph
    monkeypatch.setattr(builtin, "retrieve_relevant_sections", retrieve)
    monkeypatch.setattr(builtin, "recall_event_scores", events)
    tool = builtin.SearchContextTool()

    # The explicit turn choice overrides the persona and the source default.
    result = await tool.invoke({"query": "q"}, ToolContext(
        engine_manager=engine, sources=[scoped], persona={"search_strategy": "multi"}, requested_strategy="vector",
    ))
    assert calls[-1] == "vector"
    assert result.data["retrieval_stats"]["strategy_origin"] == "request"

    # Without a turn choice the persona wins, then the source default.
    result = await tool.invoke({"query": "q"}, ToolContext(
        engine_manager=engine, sources=[scoped], persona={"search_strategy": "multi"},
    ))
    assert calls[-1] == "multi" and result.data["retrieval_stats"]["strategy_origin"] == "persona"
    result = await tool.invoke({"query": "q"}, ToolContext(engine_manager=engine, sources=[scoped], persona={}))
    assert calls[-1] == "sag2" and result.data["retrieval_stats"]["strategy_origin"] == "source_default"

    # The adapted tool's budget follows the turn choice as well.
    adapted = _adapt_tool(tool, ToolContext(
        engine_manager=engine, sources=[scoped], persona={}, requested_strategy="vector",
    ), [])
    assert adapted.spec.timeout_seconds is None

    captured = []
    original_invoke = tool.invoke

    async def capture(arguments, context):
        result = await original_invoke(arguments, context)
        captured.append(result.data["retrieval_stats"])
        return result

    monkeypatch.setattr(tool, "invoke", capture)
    for source_default, expected_origin in [("sag2", "source_default"), (None, "global")]:
        scoped.default_search_strategy = source_default
        persona = {}
        adapted = _adapt_tool(tool, ToolContext(engine_manager=engine, sources=[scoped], persona=persona), [])
        await adapted.execute({"query": "q"}, SimpleNamespace(
            cancellation=SimpleNamespace(raise_if_cancelled=lambda: None),
        ))
        assert captured[-1]["strategy_origin"] == expected_origin
        assert persona == {}


@pytest.fixture
async def chat_client(monkeypatch):
    from sag_api.main import app

    async def skip_unrelated_mcp(_factory, _app):
        return None

    monkeypatch.setattr("sag_api.runtime._RuntimeFactory.start_mcp", skip_unrelated_mcp)
    snapshot = settings.model_dump()
    try:
        async with app.router.lifespan_context(app):
            app.state.llm = SimpleNamespace(configured=True)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                registration = await client.post(
                    "/api/v1/auth/register",
                    json={"email": f"chat-mode-{uuid4().hex}@t.com", "password": "password123"},
                )
                assert registration.status_code == 201
                client.headers["Authorization"] = f"Bearer {registration.json()['access_token']}"
                yield client
    finally:
        for key, value in snapshot.items():
            setattr(settings, key, value)


async def test_ask_rejects_unavailable_turn_strategy_before_streaming(chat_client, monkeypatch):
    client = chat_client
    monkeypatch.setattr(settings, "sag_vector_provider", "lancedb")
    monkeypatch.setattr(settings, "jev_endpoint", "https://jev.example/score")
    monkeypatch.setattr(settings, "jev_api_key", None)
    agent = (await client.post("/api/v1/agents", json={"name": "模式助手"})).json()
    thread = (await client.post(f"/api/v1/agents/{agent['id']}/threads", json={})).json()
    endpoint = f"/api/v1/agents/{agent['id']}/threads/{thread['id']}/ask"

    rejected = await client.post(endpoint, json={"query": "问题", "strategy": "sag2"})
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["error"]["code"] == "jev_api_key_missing"
    # Nothing was persisted for the rejected turn.
    messages = await client.get(f"/api/v1/agents/{agent['id']}/threads/{thread['id']}/messages")
    assert messages.status_code == 200 and messages.json()["items"] == []

    unknown = await client.post(endpoint, json={"query": "问题", "strategy": "nope"})
    assert unknown.status_code == 422

    monkeypatch.setattr(settings, "sag_relational_provider", "mysql")
    monkeypatch.setattr(settings, "jev_api_key", "synthetic-jev-key")
    storage = await client.post(endpoint, json={"query": "问题", "strategy": "sag2"})
    assert storage.status_code == 400 and storage.json()["error"]["code"] == "sag2_relational_unsupported"
