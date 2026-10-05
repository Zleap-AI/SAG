"""Exercise the complete stock search_context tool during a real document job."""

import asyncio
import json
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from litellm import ModelResponse
from openai import AsyncOpenAI
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.api.v1.chatbot import save_config
from sag_api.core import db
from sag_api.core.chatbot_config import Environment, Update
from sag_api.core.config import settings
from sag_api.core.db import SessionLocal, init_db
from sag_api.generation.chatbot import QueryLLM
from sag_api.sag.engine_manager import EngineManager
from sag_api.services import chatbot_service as rt
from sag_api.tools.base import ToolContext
from sag_api.tools.builtin import SearchContextTool


@pytest.mark.asyncio
@pytest.mark.parametrize("query_embedding_enabled", [True, False])
@pytest.mark.parametrize("target_warm", [False, True])
@pytest.mark.parametrize("live_update", [False, True])
async def test_search_during_extraction(
    tmp_path, monkeypatch, query_embedding_enabled, target_warm, live_update,
):
    stock = settings.model_copy(deep=True)
    stock.data_dir = str(tmp_path / "engine")
    postgres_url = os.environ.get("SAG_CHATBOT_TEST_POSTGRES_URL")
    if postgres_url:
        url = make_url(postgres_url)
        stock.sag_vector_provider = "pgvector"
        stock.sag_relational_provider = "postgres"
        stock.sag_pg_host, stock.sag_pg_port = url.host, url.port or 5432
        stock.sag_pg_user, stock.sag_pg_password, stock.sag_pg_database = url.username, url.password, url.database
    else:
        stock.sag_vector_provider = "lancedb"
        stock.sag_relational_provider = None
    stock.llm_model = "stock-model"
    stock.llm_api_key = "stock-key"
    stock.llm_base_url = "https://stock.invalid/v1"
    stock.embedding_model = "stock-embedding"
    stock.embedding_api_key = "stock-embedding-key"
    stock.embedding_base_url = "https://stock-embedding.invalid/v1"
    stock.embedding_schema_dimensions = 16
    stock.embedding_request_dimensions = 16
    manager = rt.Manager(
        Environment({
            "SAG_CHATBOT_EMBEDDING_ENABLED": str(query_embedding_enabled).lower(),
            "SAG_CHATBOT_EMBEDDING_BASE_URL": "https://query-embedding.invalid/v1",
            **({
                "SAG_CHATBOT_LLM_ENABLED": "true",
                "SAG_CHATBOT_LLM_MODEL": "before-save",
                "SAG_CHATBOT_LLM_BASE_URL": "https://before-save.invalid/v1",
            } if live_update else {}),
        }),
        stock,
    )
    monkeypatch.setattr(rt, "manager", manager)
    assert manager.connections()["llm"].enabled == live_update
    calls, chat_calls = [], []
    blocked, release = asyncio.Event(), asyncio.Event()
    pause_extraction = False

    def embed(request):
        body = json.loads(request.content)
        calls.append((request.url.host, rt.query_scope.get()))
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        return httpx.Response(200, json={
            "data": [{"index": i, "embedding": [1.0] + [0.0] * 15} for i in range(len(inputs))],
            "model": body["model"],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        })

    monkeypatch.setattr("openai.AsyncOpenAI", lambda **kwargs: AsyncOpenAI(
        **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(embed)),
    ))

    async def complete(**kwargs):
        assert kwargs["api_key"] == stock.llm_api_key
        assert kwargs["api_base"] == stock.llm_base_url
        calls.append(("llm", rt.query_scope.get()))
        if pause_extraction:
            blocked.set()
            await release.wait()
        return ModelResponse(model=kwargs["model"], choices=[{
            "message": {"role": "assistant", "content": json.dumps({
                "type": "response", "data": {"items": [{
                    "reason": "The document specifies an annual leave entitlement.",
                    "title": "Annual leave policy",
                    "summary": "Employees receive twenty days of annual leave.",
                    "content": "Employees receive twenty days of annual leave.",
                    "references": [1],
                    "entities": [{"type": "concept", "name": "Annual leave", "description": "Employee leave"}],
                }]},
            })},
            "finish_reason": "stop",
        }])

    monkeypatch.setattr("litellm.acompletion", complete)

    async def chat_completion(**kwargs):
        chat_calls.append((kwargs["api_base"], kwargs["model"]))
        return ModelResponse(model=kwargs["model"], choices=[{
            "message": {"role": "assistant", "content": "chat-ok"}, "finish_reason": "stop",
        }])

    monkeypatch.setattr("sag_api.generation.llm._litellm_completion", chat_completion)
    await init_db()
    engine = EngineManager(stock)
    suffix = uuid.uuid4().hex[:12]
    target = SimpleNamespace(
        id="policy", name="Policies", sag_source_config_id=f"a-policy-{suffix}", config={},
    )
    policy = tmp_path / "policy.md"
    policy.write_text("# Annual leave policy\n\nEmployees receive twenty days of annual leave.")
    job = ongoing = None
    continue_ongoing = asyncio.Event()
    try:
        seeded = await engine.process_document(target.sag_source_config_id, str(policy))
        assert seeded.event_count > 0
        # Same shared store, outside the tool's authorized source scope.
        private = tmp_path / "private.md"
        private.write_text("# Private annual leave policy\n\nConfidential payroll exception: PRIVATE_ONLY.")
        await engine.process_document(f"b-private-{suffix}", str(private))
        await engine.release(f"b-private-{suffix}")
        # Model a restart / eviction: the target's durable data exists, but its engine is cold.
        if not target_warm:
            await engine.release(target.sag_source_config_id)
        pause_extraction = True
        other = tmp_path / "other.md"
        other.write_text("# Other document\n\nAn unrelated document being extracted.")
        job = asyncio.create_task(engine.process_document(f"z-extracting-{suffix}", str(other)))
        await asyncio.wait_for(blocked.wait(), 15)
        if live_update:
            started = asyncio.Event()

            async def current_chat():
                # One immutable operation covers model turns and nested tool retrieval.
                async with manager.scope():
                    assert await QueryLLM(stock).complete([{"role": "user", "content": "before"}]) == "chat-ok"
                    started.set()
                    await continue_ongoing.wait()
                    await QueryLLM(stock).complete([{"role": "user", "content": "next turn"}])
                    return await engine.search_many(
                        [(target.sag_source_config_id, target)], "annual leave policy", strategy="vector",
                    )

            ongoing = asyncio.create_task(current_chat())
            await asyncio.wait_for(started.wait(), 3)
            async with SessionLocal() as session:
                # Invoke the validated API save path while extraction still owns the lifecycle read gate.
                saved = await asyncio.wait_for(save_config(
                    Update(
                        llm={"enabled": True, "provider": "openai", "model": "after-save", "base_url": "https://after-save.invalid/v1"},
                        embedding={"enabled": True, "base_url": "https://after-save-embedding.invalid/v1"},
                    ),
                    _user=object(), session=session,
                ), 3)
            assert saved["config"]["llm"]["model"] == "after-save"
            assert not job.done() and not release.is_set()
            chat_calls.clear()
            new_chat = await asyncio.wait_for(
                QueryLLM(stock).complete([{"role": "user", "content": "new chat"}]), 3,
            )
            assert new_chat == "chat-ok"
            assert chat_calls == [("https://after-save.invalid/v1", "openai/after-save")]
        calls.clear()
        result = await asyncio.wait_for(
            SearchContextTool().invoke({"query": "annual leave policy"}, ToolContext(engine, sources=[target])),
            timeout=3,
        )
        assert not job.done() and not release.is_set()
        assert result.data["section_count"] > 0 and result.citations
        assert "twenty days" in result.content
        assert "PRIVATE_ONLY" not in result.content
        assert all(section.source_config_id == target.sag_source_config_id for section in result.data["sections"])
        assert result.data["_graph"].events
        assert all(event.source_config_id == target.sag_source_config_id for event in result.data["_graph"].events)
        embedding_host = "after-save-embedding.invalid" if live_update else (
            "query-embedding.invalid" if query_embedding_enabled else "stock-embedding.invalid"
        )
        assert calls and all(host == embedding_host and scoped for host, scoped in calls)
        assert (target.sag_source_config_id in engine._slots) == target_warm
        if live_update:
            calls.clear()
            chat_calls.clear()
            continue_ongoing.set()
            old_result = await asyncio.wait_for(ongoing, 3)
            assert old_result.sections
            assert chat_calls == [("https://before-save.invalid/v1", "openai/before-save")]
            old_host = "query-embedding.invalid" if query_embedding_enabled else "stock-embedding.invalid"
            assert calls and all(host == old_host and scoped for host, scoped in calls)
            assert not job.done() and not release.is_set()
    finally:
        continue_ongoing.set()
        release.set()
        if ongoing is not None:
            await ongoing
        if job is not None:
            await job
        await engine.aclose_all()


@pytest.mark.asyncio
async def test_saved_query_embedding_can_still_wait_for_shared_engine_capacity(monkeypatch):
    from zleap.sag.config import RuntimeLimits
    from zleap.sag.core.adapters.limited import LimitedEmbeddingAdapter
    from zleap.sag.runtime import RuntimeGovernor, RuntimeResource

    stock = settings.model_copy(deep=True)
    stock.embedding_schema_dimensions = 16
    manager = rt.Manager(Environment({}), stock)
    monkeypatch.setattr(rt, "manager", manager)
    governor = RuntimeGovernor(RuntimeLimits(embedding_concurrency=1))
    vector = [1.0] + [0.0] * 15
    occupied, release, query_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def original_embedding(text):
        occupied.set()
        await release.wait()
        return vector

    original = SimpleNamespace(generate=original_embedding)
    query_provider = SimpleNamespace(generate=AsyncMock(return_value=vector), close=AsyncMock())
    embedding = LimitedEmbeddingAdapter(rt.ScopedAdapter(original, "embedding"), governor)
    durable = asyncio.create_task(embedding.generate("document"))
    query = None
    try:
        await asyncio.wait_for(occupied.wait(), 3)
        await init_db()
        async with SessionLocal() as session:
            await asyncio.wait_for(save_config(
                Update(embedding={"enabled": True, "base_url": "https://saved-embedding.invalid/v1"}),
                _user=object(), session=session,
            ), 3)

        async def new_query():
            async with manager.scope(query=True) as snapshot:
                assert snapshot.connections["embedding"].base_url == "https://saved-embedding.invalid/v1"
                snapshot.adapters["embedding"] = query_provider
                query_started.set()
                return await embedding.generate("question")

        query = asyncio.create_task(new_query())
        await asyncio.wait_for(query_started.wait(), 3)
        assert governor.snapshot()[RuntimeResource.EMBEDDING].waiting == 1
        query_provider.generate.assert_not_awaited()
        assert not query.done()
        release.set()
        assert await asyncio.wait_for(query, 3) == vector
        query_provider.generate.assert_awaited_once_with("question")
    finally:
        release.set()
        await durable
        if query is not None:
            await query


@pytest.fixture(autouse=True)
async def application_database(tmp_path, monkeypatch):
    """Keep concurrency tests' settings tables and pools out of unrelated tests."""
    url = os.environ.get("SAG_CHATBOT_TEST_POSTGRES_URL") or f"sqlite+aiosqlite:///{tmp_path}/application.db"
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    original_engine, original_factory = db.engine, db.SessionLocal
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(sys.modules[__name__], "SessionLocal", factory)
    try:
        yield
    finally:
        # Restore before outer database-cleanup fixtures run; stdio tests replace
        # SessionLocal with a minimal schema and must not see these job tables.
        monkeypatch.setattr(db, "engine", original_engine)
        monkeypatch.setattr(db, "SessionLocal", original_factory)
        await engine.dispose()
