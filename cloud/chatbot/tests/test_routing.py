import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from litellm import ModelResponse
from sag_api.core.errors import ConfigurationError
from sag_api.sag.search_reader import SearchReader
from sag_chatbot import runtime as rt
from sag_chatbot.config import KEYLESS_API_KEY, Environment
from sag_chatbot.provider import QueryLLM


def configure(manager):
    manager.environment = Environment({
        "SAG_CHATBOT_LLM_ENABLED": "true", "SAG_CHATBOT_LLM_MODEL": "query-model",
        "SAG_CHATBOT_LLM_API_KEY": "query-key", "SAG_CHATBOT_LLM_BASE_URL": "https://query.invalid/v1",
        "SAG_CHATBOT_EMBEDDING_ENABLED": "true", "SAG_CHATBOT_EMBEDDING_API_KEY": "query-embedding-key",
        "SAG_CHATBOT_EMBEDDING_BASE_URL": "https://query-embedding.invalid/v1",
    })


@pytest.mark.parametrize("keyless", [False, True])
async def test_concurrent_durable_and_query_adapters(isolate, monkeypatch, keyless):
    configure(isolate)
    if keyless:
        isolate.environment = Environment({**isolate.environment.env,
            "SAG_CHATBOT_LLM_API_KEY": "", "SAG_CHATBOT_EMBEDDING_API_KEY": ""})
    import litellm
    from openai import AsyncOpenAI
    from zleap.sag.config import EmbeddingConfig, LLMConfig
    from zleap.sag.core.adapters import registry
    from zleap.sag.core.ai.models import LLMMessage
    calls = []

    def embed(request):
        import json
        body = json.loads(request.content)
        calls.append((str(request.url), request.headers.get("authorization"), body))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2, 3]}], "model": body["model"], "usage": {"prompt_tokens": 1, "total_tokens": 1}})

    monkeypatch.setattr("openai.AsyncOpenAI", lambda **kwargs: AsyncOpenAI(
        **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(embed)),
    ))

    async def complete(**kwargs):
        calls.append((kwargs.get("api_base"), kwargs["api_key"], kwargs["model"]))
        return ModelResponse(model=kwargs["model"], choices=[{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}])

    monkeypatch.setattr(litellm, "acompletion", complete)
    original_llm = registry.create_llm(config=LLMConfig(
        provider="litellm", model="openai/stock-model", api_key="stock-key", base_url="https://stock.invalid/v1",
    ))
    original_embed = registry.create_embedding(config=EmbeddingConfig(
        model=isolate.stock.embedding_model, api_key="stock-embedding-key", base_url="https://stock-embedding.invalid/v1",
        schema_dimensions=3, request_dimensions=3,
    ))
    message = [LLMMessage(role="user", content="query")]

    async def durable():
        # Extraction, indexing, import and rebuild all consume these registry resources without a query scope.
        for _ in ("extraction", "indexing", "import", "rebuild"):
            await original_llm.chat(message)
            await original_embed.generate("document")

    async def query():
        async with isolate.scope(query=True):
            await original_llm.chat(message)
            await original_embed.generate("query")
            await QueryLLM(isolate.stock).complete([{"role": "user", "content": "chat"}])

    monkeypatch.setattr("sag_api.generation.llm._litellm_completion", complete)
    try:
        await asyncio.gather(durable(), query())
        assert sum(url == "https://stock.invalid/v1" for url, _, _ in calls) == 4
        assert sum(url == "https://stock-embedding.invalid/v1/embeddings" for url, _, _ in calls) == 4
        assert sum(url == "https://query.invalid/v1" for url, _, _ in calls) == 2
        embedding_calls = [entry for entry in calls if isinstance(entry[2], dict)]
        assert all(body["model"] == isolate.stock.embedding_model and body["dimensions"] == 3 for _, _, body in embedding_calls)
        assert next(key for url, key, _ in calls if url == "https://query-embedding.invalid/v1/embeddings") == (None if keyless else "Bearer query-embedding-key")
        assert all(key == (KEYLESS_API_KEY if keyless else "query-key") for url, key, _ in calls if url == "https://query.invalid/v1")
    finally:
        await original_llm.close()
        await original_embed.close()


async def test_operation_snapshot_and_client_close_after_update(isolate, database):
    started, release = asyncio.Event(), asyncio.Event()
    closed = []
    async with database() as session:
        await isolate.save(session, {"llm": {"enabled": True, "model": "first", "api_key": "first-key"}})

    async def active_request():
        async with isolate.scope(query=True) as snapshot:
            class Adapter:
                async def close(self): closed.append("first")
            snapshot.adapters["llm"] = Adapter()
            started.set()
            await release.wait()
            assert snapshot.settings.llm_model == "first"
            assert snapshot.settings.llm_api_key == "first-key"
            assert not closed
        assert snapshot.closed

    task = asyncio.create_task(active_request())
    await started.wait()
    async with database() as session:
        await isolate.save(session, {"llm": {"model": "second", "api_key": "second-key"}})
    assert isolate.snapshot().settings.llm_model == "second"
    release.set()
    await task
    assert closed == ["first"]


async def test_cancelled_operation_releases_client(isolate):
    closed = []
    started = asyncio.Event()
    async def run():
        async with isolate.scope() as snapshot:
            class Adapter:
                async def close(self): closed.append(True)
            snapshot.adapters["embedding"] = Adapter()
            started.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True] and rt.operation.get() is None and not rt.query_scope.get()


@pytest.mark.parametrize("vector", [[1, 2], [float("nan"), 2, 3], [float("inf"), 2, 3], [True, 2, 3], []])
async def test_bad_query_vectors_rejected(isolate, vector):
    configure(isolate)
    class Adapter:
        async def generate(self, _): return vector
        async def close(self): pass
    async with isolate.scope(query=True) as snapshot:
        snapshot.adapters["embedding"] = Adapter()
        with pytest.raises(ConfigurationError):
            await rt.ScopedAdapter(Adapter(), "embedding").generate("q")


async def test_search_reader_scope_covers_raw_bulk_and_event_entries(isolate):
    seen = []
    class Embedding:
        async def generate(self, _):
            seen.append(rt.query_scope.get())
            return [1, 2, 3]
    class Vector:
        async def query(self, *args): return []
    async def engine_search(request):
        seen.append(rt.query_scope.get())
        # SearchOutcome.from_result accepts a zleap-like chunk response.
        return SimpleNamespace(query=request.query, chunks=[], stats={})
    engine = SimpleNamespace(resources=SimpleNamespace(embedding=Embedding(), vector=Vector()), search=engine_search)
    class Access:
        settings = isolate.stock
        @asynccontextmanager
        async def use(self, *args): yield engine
        def zleap_engine_strategy(self, strategy): return strategy
        async def ensure_read_runtime(self, *args): pass
        async def slot(self, *args): return SimpleNamespace(engine=engine)
    reader = SearchReader(Access())
    await reader._search_raw("source", "q", source=None, strategy="vector", top_k=3)
    await reader._search_chunk_vectors([("source", None)], "q", top_k=3, requested_sources=1)
    await reader.search_event_scores("q", {"source": None})
    assert seen == [True, True, True]
    assert not rt.query_scope.get()


async def test_history_budget_and_capabilities_follow_snapshot(isolate, monkeypatch):
    from sag_api.api.v1 import system
    from sag_api.services import agent_domain
    configure(isolate)
    isolate.environment.tuning["llm_context_window"] = 123456
    async with isolate.scope():
        assert agent_domain.settings.llm_context_window == 123456
        capabilities = system._capabilities()
        assert capabilities["context_window"] == 123456
        assert capabilities["llm_model"] == "query-model"


async def test_real_document_import_rebuild_and_concurrent_search(isolate, monkeypatch, tmp_path):
    """Real stock pipelines and local vector storage; only provider transports are controlled."""
    import json

    import litellm
    from octx import create_octx
    from openai import AsyncOpenAI
    from sag_api.sag.engine_manager import EngineManager
    from sag_api.sag.octx_importer import import_knowledge_package
    from sag_api.sag.octx_vector_rebuilder import rebuild_vectors

    configure(isolate)
    isolate.stock.data_dir = str(tmp_path / "engine")
    isolate.stock.sag_vector_provider = "lancedb"
    isolate.stock.sag_relational_provider = None
    calls = []
    blocked, release = asyncio.Event(), asyncio.Event()
    pause_extraction = False

    def embed(request):
        body = json.loads(request.content)
        calls.append((request.url.host, request.headers["authorization"], rt.query_scope.get()))
        texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [1., 0., 0.]} for i in range(len(texts))],
                                         "model": body["model"], "usage": {"prompt_tokens": 1, "total_tokens": 1}})
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **kwargs: AsyncOpenAI(
        **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(embed)),
    ))
    async def complete(**kwargs):
        calls.append(("llm", kwargs["api_key"], rt.query_scope.get()))
        if pause_extraction:
            blocked.set()
            await release.wait()
        return ModelResponse(model=kwargs["model"], choices=[{"message": {"role": "assistant", "content": json.dumps({
            "type": "response", "data": {"items": [{
                "reason": "The source contains a reusable observation.", "title": "Source observation",
                "summary": "A retrieval document", "content": "A source document for retrieval and extraction.",
                "references": [1], "entities": [{"type": "concept", "name": "Retrieval", "description": "The document topic"}],
            }]},
        })}, "finish_reason": "stop"}])
    monkeypatch.setattr(litellm, "acompletion", complete)
    engine = EngineManager(isolate.stock)
    document = tmp_path / "document.md"
    document.write_text("# Original document\n\nA source document for retrieval and extraction.")
    try:
        seed = await engine.process_document("source", str(document))
        assert seed.chunk_count > 0 and seed.event_count > 0 and not seed.paused
        assert any(host == "stock-embedding.invalid" for host, _, _ in calls)
        calls.clear()
        pause_extraction = True
        next_document = tmp_path / "next.md"
        next_document.write_text("# Concurrent document\n\nAnother document written while queries use a separate endpoint.")
        async def query():
            await asyncio.wait_for(blocked.wait(), 15)
            try:
                return await engine.search_many([("source", None)], "source document", strategy="vector")
            finally:
                release.set()
        processed, found = await asyncio.gather(engine.process_document("source", str(next_document)), query())
        assert processed.chunk_count > 0 and found.stats["chunk_recall"] == "batch-vector"
        assert any(host == "query-embedding.invalid" and scoped for host, _, scoped in calls)
        assert all(key == "stock-key" and not scoped for host, key, scoped in calls if host == "llm")
        assert all(key == "Bearer stock-embedding-key" and not scoped for host, key, scoped in calls if host == "stock-embedding.invalid")

        pause_extraction = False
        calls.clear()
        package_source = tmp_path / "package-source"
        package_source.mkdir()
        (package_source / "import.md").write_text("# Imported document\n\nKnowledge imported through the durable document pipeline.")
        package = create_octx(tmp_path / "package-workspace", source=package_source, name="Query isolation", output=tmp_path / "test.octx")
        imported = await import_knowledge_package(package.output, tmp_path / "staging", source_config_id="source", engine_manager=engine, checkpoint={})
        assert imported.counts["chunks"] > 0
        assert any(host == "llm" for host, _, _ in calls)
        assert all(host in {"llm", "stock-embedding.invalid"} and not scoped for host, _, scoped in calls)

        calls.clear()
        async with engine.use("source") as source:
            await rebuild_vectors("source", {}, session_factory=source.resources.relational.session_factory(),
                                  embedding_client=source.resources.embedding, vector_store=source.resources.vector)
        assert calls and all(host == "stock-embedding.invalid" and not scoped for host, _, scoped in calls)
    finally:
        release.set()
        await engine.aclose_all()
