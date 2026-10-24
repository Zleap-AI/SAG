"""Offline contracts for the SAG2 evidence strategy (zleap-sag2 read-only search)."""

import asyncio
import threading
import time

import pytest

from sag_api.core.config import Settings, settings
from sag_api.core.errors import ConfigurationError, ServiceUnavailableError, ValidationError
from sag_api.sag import RetrievedSection, SearchOutcome, SourceGraphInfo
from sag_api.sag.engine_manager import EngineManager
from sag_api.sag.sag2_engine import Sag2EngineHolder, _fingerprint
from tests.test_jev_config import jev_config_client  # noqa: F401 - shared settings/API fixture


def sag2_settings(**changes):
    return Settings(_env_file=None).model_copy(update={
        "jev_endpoint": "https://jev.invalid/score",
        "jev_api_key": "synthetic-jev-key",
        "sag_vector_provider": "lancedb",
        "search_source_timeout": 12.0,
        **changes,
    })


def hit(chunk, score, *, config="cfg-1", document="document-1"):
    return RetrievedSection(
        chunk_id=chunk, heading=chunk, content=f"evidence {chunk}", score=score,
        source_config_id=config, source_id=document, metadata={"score_source": "jev", "hop": 0},
    )


class _NativeStore:
    """A store without the SAG reader: scope counting is skipped."""

    sag1_reader = None


@pytest.mark.parametrize(("changes", "expected"), [
    ({"jev_endpoint": None}, "jev_not_configured"),
    ({"jev_api_key": None}, "jev_api_key_missing"),
    ({"sag_relational_provider": "mysql"}, "sag2_relational_unsupported"),
    ({"sag_relational_provider": "oceanbase"}, "sag2_relational_unsupported"),
])
def test_sag2_requires_paid_jev_connection_and_supported_relational_store(changes, expected):
    manager = EngineManager(sag2_settings(**changes))
    with pytest.raises(ConfigurationError) as error:
        manager._effective_search_strategy("sag2")
    assert error.value.code == expected
    report = manager.strategies_capability_report(manager._settings)
    assert report["disabled"]["sag2"]["reason"] == expected
    assert "sag2" not in report["enabled"]


@pytest.mark.parametrize("relational_provider", [None, "sqlite", "postgres"])
def test_sag2_is_available_on_sqlite_and_postgres_without_lexical_vectors(relational_provider):
    manager = EngineManager(sag2_settings(sag_relational_provider=relational_provider, sag_vector_provider="pgvector"))
    assert manager._effective_search_strategy("sag2") == "sag2"
    report = manager.strategies_capability_report(manager._settings)
    assert "sag2" in report["enabled"]
    # The vector store still gates multi_jev; sag2 reads the relational store directly.
    assert report["disabled"]["multi_jev"]["reason"] == "vector_provider_lacks_lexical"


@pytest.mark.asyncio
async def test_sag2_fanout_is_one_read_only_request_over_published_sources(monkeypatch):
    manager = EngineManager(sag2_settings(search_source_candidate_limit=2))
    calls = []

    async def sag2_search(targets, query, **kwargs):
        calls.append((targets, query, kwargs))
        return SearchOutcome(query=query, sections=[hit("a", 0.9), hit("b", 0.8, config="cfg-3")], stats={
            "requested_strategy": "sag2", "effective_strategy": "sag2", "fallback_used": False,
        })

    async def raw(*_args, **_kwargs):
        pytest.fail("SAG2 must not run per-source zleap-sag searches")

    monkeypatch.setattr(manager, "_sag2_search", sag2_search)
    monkeypatch.setattr(manager, "_search_raw", raw)
    result = await manager.search_many(
        [("cfg-1", None), ("cfg-2", None), ("cfg-3", None)], "q", strategy="sag2", top_k=5,
        source_ids_by_config={"cfg-1": ("d1",), "cfg-2": (), "cfg-3": ("d3",)},
    )
    assert len(calls) == 1
    targets, _query, kwargs = calls[0]
    # The candidate limit keeps cfg-1 and cfg-2; cfg-2 has no published input left to search.
    assert [scid for scid, _ in targets] == ["cfg-1"]
    assert kwargs == {
        "top_k": 5, "requested_sources": 3, "source_limit_applied": True,
        "source_ids_by_config": {"cfg-1": ("d1",), "cfg-2": (), "cfg-3": ("d3",)},
    }
    assert [section.chunk_id for section in result.sections] == ["a", "b"]
    assert result.stats["effective_strategy"] == "sag2"


@pytest.mark.asyncio
async def test_sag2_single_source_search_never_falls_back_to_vector(monkeypatch):
    manager = EngineManager(sag2_settings(search_fallback_vector=True))
    calls = []

    async def sag2_search(targets, query, **kwargs):
        calls.append((targets, kwargs))
        raise RuntimeError("scorer unavailable")

    async def raw(*_args, **_kwargs):
        pytest.fail("SAG2 has no vector fallback")

    monkeypatch.setattr(manager, "_sag2_search", sag2_search)
    monkeypatch.setattr(manager, "_search_raw", raw)
    with pytest.raises(RuntimeError, match="scorer unavailable"):
        await manager.search("cfg-1", "q", strategy="sag2", source_ids=("d1",))
    assert calls == [([("cfg-1", None)], {
        "top_k": settings.search_top_k, "requested_sources": 1, "source_limit_applied": False,
        "source_ids_by_config": {"cfg-1": ("d1",)},
    })]


@pytest.mark.asyncio
async def test_holder_builds_scope_and_options_and_preserves_engine_order(monkeypatch):
    from zleap.sag2 import SearchHit, SearchResult

    holder = Sag2EngineHolder(sag2_settings(
        sag2_candidate_k=40, sag2_seed_k=5, sag2_max_hops=1, sag2_expansion_term_limit=7,
    ))
    captured = []

    class Engine:
        store = _NativeStore()

        async def search(self, request):
            captured.append(request)
            return SearchResult(
                query=request.query, original_query=request.query, snapshot_revision=7,
                chunks=(
                    SearchHit(id="c-low", chunk_id="c-low", data_source_id="cfg-2", source_id="d3",
                              title="Title", heading="Heading", content="low", score=0.2, hop=1),
                    SearchHit(id="c-high", chunk_id="c-high", data_source_id="cfg-1", source_id="d1",
                              title="Title", heading="", content="high", score=0.9, hop=0),
                ),
                trace=({"stage": "bm25", "hop": 0, "count": 2}, {"stage": "jev", "hop": 0, "count": 2}),
                usage={"requests": 2, "judgments": 4, "input_tokens": 1234, "output_tokens": None, "batches": []},
                elapsed_ms=12.5, local_ms=2.5, jev_ms=10.0,
            )

    async def engine():
        return Engine()

    async def count(_engine, data_source_ids):
        return 3 * len(data_source_ids)

    monkeypatch.setattr(holder, "engine", engine)
    monkeypatch.setattr(holder, "_count_scope", count)
    outcome = await holder.search(
        [("cfg-1", None), ("cfg-2", None)], "q", top_k=5, requested_sources=2, source_limit_applied=False,
        source_ids_by_config={"cfg-1": ("d2", "d1"), "cfg-2": ("d3",)},
    )
    request = captured[0]
    assert request.scope.data_source_ids == ("cfg-1", "cfg-2")
    assert request.scope.source_ids == ("d1", "d2", "d3")
    option_fields = {"candidate_k", "seed_k", "max_hops", "expansion_term_limit", "top_k"}
    assert request.options.model_dump(include=option_fields) == {
        "candidate_k": 40, "seed_k": 5, "max_hops": 1, "expansion_term_limit": 7, "top_k": 5,
    }
    # The engine order wins over raw score order; scores stay the raw JEV values.
    assert [(s.chunk_id, s.score, s.rank, s.source_config_id, s.source_id) for s in outcome.sections] == [
        ("c-low", 0.2, 0, "cfg-2", "d3"), ("c-high", 0.9, 1, "cfg-1", "d1"),
    ]
    assert [s.metadata for s in outcome.sections] == [
        {"score_source": "jev", "hop": 1}, {"score_source": "jev", "hop": 0},
    ]
    assert outcome.sections[1].heading == "Title"
    assert outcome.stats["requested_strategy"] == outcome.stats["effective_strategy"] == "sag2"
    assert outcome.stats["fallback_used"] is False
    assert outcome.stats["ranking"] == {"method": "jev", "fallback_reason": None}
    assert outcome.stats["jev_usage"] == {"requests": 2, "judgments": 4, "input_tokens": 1234, "output_tokens": None}
    assert outcome.stats["sag2"]["snapshot_revision"] == 7
    assert outcome.stats["sag2"]["scope_chunks"] == 6
    assert outcome.stats["sag2"]["hops"] == [
        {"stage": "bm25", "hop": 0, "count": 2}, {"stage": "jev", "hop": 0, "count": 2},
    ]
    assert outcome.stats["sources"] == 2 and outcome.stats["candidates"] == 2

    unscoped = await holder.search(
        [("cfg-1", None)], "q", top_k=3, requested_sources=1, source_limit_applied=False, source_ids_by_config=None,
    )
    assert captured[1].scope.source_ids is None
    assert len(unscoped.sections) == 2

    empty = await holder.search(
        [("cfg-1", None)], "q", top_k=3, requested_sources=1, source_limit_applied=False,
        source_ids_by_config={"cfg-1": ()},
    )
    assert empty.sections == [] and empty.stats["candidates"] == 0 and len(captured) == 2


@pytest.mark.asyncio
async def test_holder_rejects_scopes_above_chunk_limit_before_scoring(monkeypatch):
    holder = Sag2EngineHolder(sag2_settings(sag2_max_scope_chunks=100))

    class Engine:
        store = _NativeStore()

        async def search(self, request):
            pytest.fail("An oversized scope must not reach the paid scorer")

    async def engine():
        return Engine()

    async def count(_engine, _data_source_ids):
        return 101

    monkeypatch.setattr(holder, "engine", engine)
    monkeypatch.setattr(holder, "_count_scope", count)
    with pytest.raises(ValidationError, match="SAG2"):
        await holder.search(
            [("cfg-1", None)], "q", top_k=3, requested_sources=1, source_limit_applied=False, source_ids_by_config=None,
        )


@pytest.mark.asyncio
async def test_holder_timeout_is_a_retryable_service_error(monkeypatch):
    configuration = sag2_settings()
    configuration.sag2_search_timeout_seconds = 0.05
    holder = Sag2EngineHolder(configuration)

    async def run(*_args, **_kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(holder, "_run", run)
    with pytest.raises(ServiceUnavailableError) as error:
        await holder.search(
            [("cfg-1", None)], "q", top_k=3, requested_sources=1, source_limit_applied=False, source_ids_by_config=None,
        )
    assert error.value.retryable is True


@pytest.mark.asyncio
async def test_holder_limits_concurrent_paid_searches(monkeypatch):
    from zleap.sag2 import SearchResult

    holder = Sag2EngineHolder(sag2_settings(sag2_max_concurrent_searches=1))
    active = peak = 0

    class Engine:
        store = _NativeStore()

        async def search(self, request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return SearchResult(query=request.query, original_query=request.query)

    async def engine():
        return Engine()

    async def count(_engine, _data_source_ids):
        return 1

    monkeypatch.setattr(holder, "engine", engine)
    monkeypatch.setattr(holder, "_count_scope", count)
    await asyncio.gather(*(
        holder.search([("cfg-1", None)], "q", top_k=3, requested_sources=1, source_limit_applied=False,
                      source_ids_by_config=None)
        for _ in range(3)
    ))
    assert peak == 1


@pytest.mark.asyncio
async def test_refresh_rebuilds_lazily_only_when_connection_or_storage_changes(monkeypatch):
    configuration = sag2_settings()
    holder = Sag2EngineHolder(configuration)
    closed, started = [], []

    class Engine:
        def __init__(self, name):
            self.name = name

        async def aclose(self):
            closed.append(self.name)

    async def start(settings):
        started.append(settings.jev_api_key)
        return Engine(settings.jev_api_key)

    monkeypatch.setattr(holder, "_start", start)
    holder._engine, holder._fingerprint = Engine("synthetic-jev-key"), _fingerprint(configuration)
    await holder.refresh(configuration.model_copy(update={"search_top_k": 20, "sag2_candidate_k": 30}))
    assert holder.started and not holder.stale and not closed
    assert (await holder.engine()).name == "synthetic-jev-key" and started == []

    # A connection change keeps the running engine for in-flight searches and rebuilds on the next call.
    await holder.refresh(configuration.model_copy(update={"jev_api_key": "rotated-key"}))
    assert holder.started and holder.stale and closed == []
    rebuilt = await holder.engine()
    assert rebuilt.name == "rotated-key" and started == ["rotated-key"] and closed == ["synthetic-jev-key"]
    assert not holder.stale

    await holder.aclose()
    assert closed == ["synthetic-jev-key", "rotated-key"] and not holder.started


@pytest.mark.asyncio
async def test_refresh_applies_new_concurrency_limit_without_dropping_inflight_searches(monkeypatch):
    from zleap.sag2 import SearchResult

    configuration = sag2_settings(sag2_max_concurrent_searches=1)
    holder = Sag2EngineHolder(configuration)
    # The engine runs on the holder's dedicated loop thread, so signal across threads.
    release, first_running = threading.Event(), threading.Event()
    active = peak = 0

    class Engine:
        async def search(self, request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            first_running.set()
            await asyncio.to_thread(release.wait, 5)
            active -= 1
            return SearchResult(query=request.query, original_query=request.query)

    async def engine():
        return Engine()

    async def count(_engine, _data_source_ids):
        return 1

    monkeypatch.setattr(holder, "engine", engine)
    monkeypatch.setattr(holder, "_count_scope", count)

    def run():
        return asyncio.create_task(holder.search(
            [("cfg-1", None)], "q", top_k=3, requested_sources=1, source_limit_applied=False, source_ids_by_config=None,
        ))

    first, second = run(), run()
    assert await asyncio.to_thread(first_running.wait, 1)
    await asyncio.sleep(0.05)
    assert active == 1  # the second search queues behind the limit of 1
    await holder.refresh(configuration.model_copy(update={"sag2_max_concurrent_searches": 2}))
    await asyncio.sleep(0.05)
    assert active == 2  # the raised limit admits the queued search; the first keeps running
    release.set()
    await asyncio.gather(first, second)
    assert peak == 2
    await holder.aclose()


@pytest.mark.asyncio
async def test_engine_work_runs_off_the_api_loop_and_timeouts_cancel_it(monkeypatch):
    from zleap.sag2 import SearchResult

    holder = Sag2EngineHolder(sag2_settings())
    cancelled = threading.Event()

    class Engine:
        async def search(self, request):
            if request.query == "block":
                time.sleep(0.3)  # synchronous corpus build: must not stall the API loop
                return SearchResult(query=request.query, original_query=request.query)
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return SearchResult(query=request.query, original_query=request.query)

    async def engine():
        return Engine()

    async def count(_engine, _data_source_ids):
        return 1

    monkeypatch.setattr(holder, "engine", engine)
    monkeypatch.setattr(holder, "_count_scope", count)

    def run(query):
        return holder.search(
            [("cfg-1", None)], query, top_k=3, requested_sources=1, source_limit_applied=False,
            source_ids_by_config=None,
        )

    # While the engine blocks its own thread, the API loop keeps ticking.
    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(10):
            await asyncio.sleep(0.02)
            ticks += 1

    await asyncio.gather(run("block"), ticker())
    assert ticks == 10
    assert threading.current_thread() is threading.main_thread()

    # The whole-search timeout cancels the coroutine inside the dedicated loop.
    holder._settings.sag2_search_timeout_seconds = 1.0
    with pytest.raises(ServiceUnavailableError):
        await run("hang")
    assert cancelled.wait(1)
    assert holder._gate.active == 0
    await holder.aclose()
    assert holder._loop is None


@pytest.mark.parametrize(("status", "expected_type", "expected_message"), [
    (401, ConfigurationError, "JEV 鉴权失败，请检查服务地址和 API Key"),
    (403, ConfigurationError, "JEV 鉴权失败，请检查服务地址和 API Key"),
    (422, ValidationError, "JEV 精排请求被拒绝，请检查模型和候选内容限制"),
    (429, ServiceUnavailableError, "JEV 精排服务暂不可用，请稍后重试"),
    (None, ServiceUnavailableError, "JEV 精排服务暂不可用，请稍后重试"),
])
def test_sag2_jev_errors_translate_like_multi_jev(status, expected_type, expected_message):
    from zleap.sag2 import JevError

    from sag_api.core.errors import ApiError

    error = JevError("JEV rejected the request", details={} if status is None else {"status": status})
    translated = Sag2EngineHolder._translate(error)
    assert isinstance(translated, expected_type)
    assert translated.message == expected_message
    assert translated.code == expected_type.code  # no sag2-specific codes for shared JEV failures
    assert translated.layer.value == "engine" and translated.stage.value == "retrieve"
    assert isinstance(translated, ApiError)


@pytest.mark.asyncio
async def test_sag2_configuration_admission_times_out_without_starting_or_leaking_reader(monkeypatch):
    manager = EngineManager(sag2_settings().model_copy(update={"sag2_search_timeout_seconds": 0.05}))
    started = []

    async def sag2_search(targets, query, **kwargs):
        started.append([scid for scid, _ in targets])
        return SearchOutcome(query=query, sections=[hit("ranked", 0.9)], stats={"effective_strategy": "sag2"})

    monkeypatch.setattr(manager, "_sag2_search", sag2_search)
    async with manager._search_configuration_gate.write():
        with pytest.raises(ServiceUnavailableError) as error:
            await asyncio.wait_for(manager.search_many([("blocked", None)], "q", strategy="sag2"), timeout=1.5)
        assert error.value.retryable is True
        assert error.value.layer.value == "engine" and error.value.stage.value == "retrieve"
        assert started == []

    result = await asyncio.wait_for(manager.search_many([("after-release", None)], "q", strategy="sag2"), timeout=1)
    assert result.sections[0].chunk_id == "ranked"
    assert started == [["after-release"]]
    # A leaked admission reader would prevent a later settings write forever.
    await asyncio.wait_for(manager.apply_settings(sag2_settings(), reset_engines=False), timeout=1)


@pytest.mark.asyncio
async def test_settings_wait_for_whole_sag2_search_before_publishing(monkeypatch):
    manager = EngineManager(sag2_settings(sag2_candidate_k=50))
    updated = sag2_settings(jev_endpoint="https://new-jev.invalid/score", sag2_candidate_k=70)
    search_started, release_search, update_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed = []

    async def sag2_search(targets, query, **kwargs):
        observed.append((manager._settings.jev_endpoint, manager._settings.sag2_candidate_k))
        search_started.set()
        await release_search.wait()
        return SearchOutcome(query=query, sections=[hit("ranked", 0.9)], stats={"effective_strategy": "sag2"})

    async def save():
        update_started.set()
        await manager.apply_settings(updated, reset_engines=True)

    monkeypatch.setattr(manager, "_sag2_search", sag2_search)
    query_task = asyncio.create_task(manager.search_many([("cfg-1", None), ("cfg-2", None)], "q", strategy="sag2"))
    await asyncio.wait_for(search_started.wait(), timeout=1)
    save_task = asyncio.create_task(save())
    try:
        await asyncio.wait_for(update_started.wait(), timeout=1)
        await asyncio.sleep(0.05)
        assert not save_task.done(), "settings must wait for the whole SAG2 search"
        release_search.set()
        await asyncio.wait_for(query_task, timeout=1)
        await asyncio.wait_for(save_task, timeout=1)
        await manager.search_many([("cfg-3", None)], "q", strategy="sag2")
    finally:
        release_search.set()
        for task in (query_task, save_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(query_task, save_task, return_exceptions=True)
    assert observed == [("https://jev.invalid/score", 50), ("https://new-jev.invalid/score", 70)]


@pytest.mark.asyncio
async def test_sag2_business_path_preserves_engine_order_limit_and_publication(publication_source):
    from sag_api.services import retrieval_service

    source = await publication_source("sag2-business", "cfg-1", "document-1")

    class Engine:
        _settings = sag2_settings(search_strategy="sag2")

        async def search_many(self, targets, query, **kwargs):
            assert kwargs["strategy"] == "sag2"
            assert kwargs["source_ids_by_config"] == {"cfg-1": ("document-1",)}
            return SearchOutcome(query=query, sections=[
                hit("unpublished", 1.0, document="hidden"), hit("first", 0.21), hit("second", 0.2), hit("third", 0.19),
            ], stats={"effective_strategy": "sag2", "ranking": {"method": "jev", "fallback_reason": None}})

        async def grep_chunks(self, *args, **kwargs):
            pytest.fail("SAG2 must not add unranked application lexical candidates")

    result = await retrieval_service.retrieve_relevant_sections(Engine(), [source], "q", top_k=2)
    assert [section.chunk_id for section in result.sections] == ["first", "second"]
    assert [section.score for section in result.sections] == [0.21, 0.2]
    assert result.stats["requested_strategy"] == "sag2"
    assert result.stats["publication_filtered"] == 1
    assert result.stats["lexical_candidates"] == 0


@pytest.mark.asyncio
async def test_agent_follows_global_sag2_and_gets_whole_search_budget(monkeypatch, publication_source):
    from sag_api.services.agent_tooling import _adapt_tool
    from sag_api.tools import builtin
    from sag_api.tools.base import ToolContext

    source = await publication_source("sag2-agent", "cfg-1", "document-1")
    engine = EngineManager(sag2_settings(
        search_strategy="sag2", agent_tool_timeout_seconds=5, sag2_search_timeout_seconds=90,
    ))
    calls = []

    async def retrieve(manager, sources, query, *, strategy, top_k):
        calls.append(strategy)
        assert manager._effective_search_strategy(strategy) == "sag2"
        return SearchOutcome(query=query, sections=[hit("first", 0.2), hit("second", 0.1)], stats={
            "effective_strategy": "sag2", "ranking": {"method": "jev", "fallback_reason": None},
        })

    async def graph(sections, _sources, **kwargs):
        assert sections == []
        return SourceGraphInfo(events=[])

    async def events(*args, **kwargs):
        pytest.fail("Independent event recall must not change SAG2 evidence")

    async def reorder(*args, **kwargs):
        pytest.fail("SAG2 evidence must retain its engine order")

    engine.graph_for_sections = graph
    monkeypatch.setattr(builtin, "retrieve_relevant_sections", retrieve)
    monkeypatch.setattr(builtin, "recall_event_scores", events)
    monkeypatch.setattr(builtin, "_prioritize_event_evidence", reorder)
    result = await builtin.SearchContextTool().invoke(
        {"query": "q"}, ToolContext(engine_manager=engine, sources=[source], persona={}),
    )
    assert calls == ["sag2"]
    assert [section.chunk_id for section in result.data["sections"]] == ["first", "second"]
    assert result.data["retrieval_stats"]["effective_strategy"] == "sag2"

    adapted = _adapt_tool(builtin.SearchContextTool(), ToolContext(engine_manager=engine, persona={}), [])
    assert adapted.spec.timeout_seconds is not None
    assert adapted.spec.timeout_seconds >= 90 + 12


async def test_saving_sag2_default_requires_key_and_supported_relational_store(jev_config_client):  # noqa: F811
    client, headers, _resets = jev_config_client
    settings.sag_relational_provider = None
    settings.sag_vector_provider = "pgvector"
    settings.search_strategy = "vector"
    settings.jev_endpoint = None
    settings.jev_api_key = None

    rejected = await client.put(
        "/api/v1/system/model-config", headers=headers,
        json={"search_strategy": "sag2", "jev_endpoint": "https://jev.example/score"},
    )
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["error"]["code"] == "jev_api_key_missing"
    assert settings.search_strategy == "vector" and settings.jev_endpoint is None

    accepted = await client.put(
        "/api/v1/system/model-config", headers=headers,
        json={
            "search_strategy": "sag2", "jev_endpoint": "https://jev.example/score",
            "jev_api_key": "synthetic-jev-key",
        },
    )
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["config"]["search_strategy"] == "sag2"
    assert body["config"]["jev_api_key_set"] is True
    assert "sag2" in body["capabilities"]["search_strategies"]
    assert body["capabilities"]["search_strategies_disabled"]["multi_jev"]["reason"] == "vector_provider_lacks_lexical"

    settings.sag_relational_provider = "mysql"
    unsupported = await client.put(
        "/api/v1/system/model-config", headers=headers, json={"search_strategy": "sag2"},
    )
    assert unsupported.status_code == 400, unsupported.text
    assert unsupported.json()["error"]["code"] == "sag2_relational_unsupported"
    settings.sag_relational_provider = None

    # Changing the endpoint clears its key; with sag2 still the default that must be rejected.
    cleared = await client.put(
        "/api/v1/system/model-config", headers=headers, json={"jev_endpoint": "https://other.example/score"},
    )
    assert cleared.status_code == 400, cleared.text
    assert cleared.json()["error"]["code"] == "jev_api_key_missing"
    assert settings.jev_endpoint == "https://jev.example/score" and settings.jev_api_key == "synthetic-jev-key"
    assert settings.search_strategy == "sag2"

    # Supplying the new endpoint's key in the same save keeps the paid default usable.
    rotated = await client.put(
        "/api/v1/system/model-config", headers=headers,
        json={"jev_endpoint": "https://other.example/score", "jev_api_key": "other-key"},
    )
    assert rotated.status_code == 200, rotated.text
    assert settings.jev_endpoint == "https://other.example/score" and settings.jev_api_key == "other-key"

    # With a non-JEV default the endpoint may change freely and its key is cleared.
    assert (await client.put(
        "/api/v1/system/model-config", headers=headers, json={"search_strategy": "vector"},
    )).status_code == 200
    freed = await client.put(
        "/api/v1/system/model-config", headers=headers, json={"jev_endpoint": "https://third.example/score"},
    )
    assert freed.status_code == 200, freed.text
    assert freed.json()["config"]["jev_api_key_set"] is False
    assert freed.json()["capabilities"]["search_strategies_disabled"]["sag2"]["reason"] == "jev_api_key_missing"
