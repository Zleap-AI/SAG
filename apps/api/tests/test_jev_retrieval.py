"""Offline contracts for the JEV strategy and the application's evidence boundary."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from sag_api.core.config import Settings
from sag_api.core.errors import ConfigurationError
from sag_api.sag import GraphEventInfo, RetrievedSection, SearchOutcome, SourceGraphInfo
from sag_api.sag.engine_manager import EngineManager


def jev_settings(**changes):
    return Settings(_env_file=None).model_copy(update={
        "jev_endpoint": "https://jev.invalid/score",
        "jev_api_key": "synthetic-jev-key",
        "jev_ranking_timeout_seconds": 30.0,
        "jev_failure_policy": "error",
        "sag_vector_provider": "lancedb",
        "search_source_timeout": 12.0,
        **changes,
    })


def hit(chunk, score, *, config="cfg-1", document="document-1", score_source="jev"):
    return RetrievedSection(
        chunk_id=chunk, heading=chunk, content=f"evidence {chunk}", score=score,
        source_config_id=config, source_id=document,
        metadata={"event_ids": [f"event-{chunk}"], "score_source": score_source},
    )


def test_jev_mode_requires_both_connection_and_lexical_capability():
    manager = EngineManager(jev_settings(jev_endpoint=None))
    with pytest.raises(ConfigurationError) as error:
        manager._effective_search_strategy("multi_jev")
    assert error.value.code == "jev_not_configured"
    report = manager.strategies_capability_report(manager._settings)
    assert report["disabled"]["multi_jev"]["reason"] == "jev_not_configured"

    # JEV is a paid service: a saved endpoint without a key must not enable any JEV-scored mode.
    manager = EngineManager(jev_settings(jev_api_key=None))
    with pytest.raises(ConfigurationError) as error:
        manager._effective_search_strategy("multi_jev")
    assert error.value.code == "jev_api_key_missing"
    report = manager.strategies_capability_report(manager._settings)
    assert report["disabled"]["multi_jev"]["reason"] == "jev_api_key_missing"
    assert report["disabled"]["sag2"]["reason"] == "jev_api_key_missing"

    manager = EngineManager(jev_settings(sag_vector_provider="pgvector"))
    with pytest.raises(ConfigurationError) as error:
        manager._effective_search_strategy("multi_jev")
    assert error.value.code == "vector_provider_lacks_lexical"
    report = manager.strategies_capability_report(manager._settings)
    assert report["disabled"]["multi_jev"]["reason"] == "vector_provider_lacks_lexical"

    manager = EngineManager(jev_settings())
    assert manager._effective_search_strategy("multi_jev") == "multi_jev"
    assert "multi_jev" in manager.strategies_capability_report(manager._settings)["enabled"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_reason", [None, "ranking_timeout"])
async def test_jev_native_request_keeps_scope_and_overrides_only_ranking_budget(monkeypatch, fallback_reason):
    from zleap.sag.pipeline import SearchHit, SearchResult

    from sag_api.sag.search_reader import search_source_timeout_seconds
    from sag_api.schemas.search import GlobalSearchRequest

    assert GlobalSearchRequest(query="q", strategy="multi_jev").strategy == "multi_jev"
    config = jev_settings(jev_ranking_timeout_seconds=7.5, jev_failure_policy="fallback_to_score")
    assert search_source_timeout_seconds(config, "multi_jev") == 19.5
    assert search_source_timeout_seconds(config, "vector") == 12.0
    manager = EngineManager(config)
    captured = []

    @asynccontextmanager
    async def use(*_args, **_kwargs):
        class Engine:
            async def search(self, request):
                captured.append(request)
                return SearchResult(query=request.query, original_query=request.query, chunks=(
                    SearchHit(id="ranked", chunk_id="ranked", score=0.4, source_id="published",
                              data_source_id="cfg-1", metadata={"event_ids": ["event-1"]}),
                    SearchHit(id="extra", chunk_id="extra", score=0.98, source_id="published",
                              data_source_id="cfg-1", metadata={"recall_channel": "query_chunk_fallback"}),
                ), stats={"ranking": {"method": "jev", "fallback_reason": fallback_reason}})
        yield Engine()

    monkeypatch.setattr(manager, "use", use)
    result = await manager.search("cfg-1", "q", strategy="multi_jev", top_k=2,
                                  source_ids=("published",), include_ranked_candidates=True)
    request = captured[0]
    assert request.options.strategy == "pruned_expand_jev"
    assert request.scope.source_ids == ("published",)
    assert request.options.ranking.model_dump(exclude_unset=True) == {
        "timeout_seconds": 7.5, "on_timeout": "fallback_to_score",
    }
    assert request.options.output.include_ranked_candidates is True
    expected_source = "event_score" if fallback_reason else "jev"
    assert [section.metadata["score_source"] for section in result.sections] == [
        expected_source, "query_chunk_fallback",
    ]
    assert result.stats["fallback_used"] is bool(fallback_reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), RuntimeError("scorer unavailable"), None])
async def test_jev_never_retries_as_vector_on_error_or_empty(monkeypatch, failure):
    manager = EngineManager(jev_settings(search_fallback_vector=True))
    calls = []

    async def raw(scid, query, *, strategy, **kwargs):
        calls.append(strategy)
        if failure is not None:
            raise failure
        return SearchOutcome(query=query, sections=[], stats={"ranking": {"method": "jev", "fallback_reason": None}})

    monkeypatch.setattr(manager, "_search_raw", raw)
    if failure is not None:
        with pytest.raises(type(failure)):
            await manager.search("cfg-1", "q", strategy="multi_jev")
    else:
        result = await manager.search("cfg-1", "q", strategy="multi_jev")
        assert result.sections == []
        assert result.stats["fallback_used"] is False
    assert calls == ["multi_jev"]


@pytest.mark.asyncio
async def test_jev_fanout_preserves_scoring_sources_and_fallback_diagnostics(monkeypatch):
    manager = EngineManager(jev_settings())

    async def raw(scid, query, **kwargs):
        if scid == "cfg-1":
            return SearchOutcome(query=query, sections=[
                hit("ranked-a", 0.4), hit("vector-extra", 0.99, score_source="query_chunk_fallback"),
            ], stats={"ranking": {"method": "jev", "fallback_reason": None}, "timings": {"ranking": 0.1}})
        return SearchOutcome(query=query, sections=[
            hit("coarse", 0.95, config=scid, score_source="event_score"),
        ], stats={"ranking": {"method": "jev", "fallback_reason": "ranking_timeout"}})

    monkeypatch.setattr(manager, "_search_raw", raw)
    result = await manager.search_many([("cfg-1", None), ("cfg-2", None)], "q", strategy="multi_jev", top_k=3)
    assert [section.chunk_id for section in result.sections] == ["ranked-a", "coarse", "vector-extra"]
    assert result.stats["effective_strategy"] == "multi_jev"
    assert result.stats["fallback_used"] is True
    assert result.stats["ranking"] == {"method": "jev", "fallback_reason": "ranking_timeout"}
    assert result.stats["source_stats"]["cfg-1"]["timings"] == {"ranking": 0.1}
    assert result.stats["source_stats"]["cfg-2"]["fallback_used"] is True


@pytest.mark.asyncio
async def test_jev_fanout_fails_when_any_source_fails(monkeypatch):
    manager = EngineManager(jev_settings())

    async def raw(scid, query, **kwargs):
        if scid == "cfg-2":
            raise RuntimeError("scorer unavailable")
        return SearchOutcome(query=query, sections=[hit("ranked", 0.5)])

    monkeypatch.setattr(manager, "_search_raw", raw)
    with pytest.raises(RuntimeError, match="scorer unavailable"):
        await manager.search_many([("cfg-1", None), ("cfg-2", None)], "q", strategy="multi_jev")


@pytest.mark.asyncio
async def test_jev_business_path_preserves_engine_order_limit_and_publication(monkeypatch, publication_source):
    from sag_api.services import retrieval_service

    source = await publication_source("jev-business", "cfg-1", "document-1")

    class Engine:
        _settings = jev_settings(search_strategy="multi_jev")

        async def search_many(self, targets, query, **kwargs):
            assert kwargs["source_ids_by_config"] == {"cfg-1": ("document-1",)}
            return SearchOutcome(query=query, sections=[
                hit("unpublished", 1.0, document="hidden"), hit("jev-first", 0.21),
                hit("jev-second", 0.2), hit("extra", 0.99, score_source="query_chunk_fallback"),
            ], stats={"ranking": {"method": "jev", "fallback_reason": None}})

        async def grep_chunks(self, *args, **kwargs):
            pytest.fail("JEV must not add unranked application lexical candidates")

    result = await retrieval_service.retrieve_relevant_sections(Engine(), [source], "q", top_k=2)
    assert [section.chunk_id for section in result.sections] == ["jev-first", "jev-second"]
    assert [section.score for section in result.sections] == [0.21, 0.2]
    assert result.stats["publication_filtered"] == 1
    assert result.stats["requested_top_k"] == 2
    assert result.stats["lexical_candidates"] == 0


@pytest.mark.asyncio
async def test_jev_publication_switch_during_recall_hides_previous_input(publication_source):
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Document
    from sag_api.services.retrieval_service import retrieve_relevant_sections

    source = await publication_source("jev-publication", "cfg-1", "document-1")

    class Engine:
        async def search_many(self, targets, query, **kwargs):
            async with SessionLocal() as session:
                document = await session.scalar(select(Document).where(Document.source_id == source.id))
                document.sag_source_id = "new-publication"
                await session.commit()
            return SearchOutcome(query=query, sections=[hit("old-publication", 0.9)])

    result = await retrieve_relevant_sections(Engine(), [source], "q", strategy="multi_jev")
    assert result.sections == []
    assert result.stats["publication_filtered"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", [{}, {"search_strategy": "retired_custom_mode"}])
async def test_agent_uses_global_jev_and_preserves_order_and_stats(monkeypatch, publication_source, persona):
    from sag_api.tools import builtin
    from sag_api.tools.base import ToolContext

    source = await publication_source("jev-agent", "cfg-1", "document-1")
    engine = EngineManager(jev_settings(search_strategy="multi_jev"))
    calls = []

    async def retrieve(manager, sources, query, *, strategy, top_k):
        calls.append(strategy)
        assert manager._effective_search_strategy(strategy) == "multi_jev"
        return SearchOutcome(query=query, sections=[hit("first", 0.2), hit("second", 0.1)],
                             stats={"ranking": {"method": "jev", "fallback_reason": None}})

    async def graph(sections, _sources, **kwargs):
        assert sections == []
        assert list(kwargs["event_scores"]) == [("cfg-1", "event-first"), ("cfg-1", "event-second")]
        return SourceGraphInfo(events=[GraphEventInfo(
            id="second-event", source_id="document-1", source_config_id="cfg-1",
            chunk_id="second", title="Second evidence", score=0.99,
        )])

    async def events(*args, **kwargs):
        pytest.fail("Independent event recall must not change JEV-selected evidence")

    async def reorder(*args, **kwargs):
        pytest.fail("JEV evidence must retain its engine order")

    engine.graph_for_sections = graph
    monkeypatch.setattr(builtin, "retrieve_relevant_sections", retrieve)
    monkeypatch.setattr(builtin, "recall_event_scores", events)
    monkeypatch.setattr(builtin, "_prioritize_event_evidence", reorder)
    result = await builtin.SearchContextTool().invoke(
        {"query": "q"}, ToolContext(engine_manager=engine, sources=[source], persona=persona),
    )
    assert calls == ["multi_jev"]
    assert [section.chunk_id for section in result.data["sections"]] == ["first", "second"]
    assert result.data["retrieval_stats"]["ranking"]["method"] == "jev"


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["error", "fallback_to_score"])
async def test_jev_outer_deadline_is_retryable_error_and_cancels_work(monkeypatch, policy):
    import asyncio

    from sag_api.core.errors import ServiceUnavailableError

    manager = EngineManager(jev_settings(
        search_source_timeout=0.01, jev_ranking_timeout_seconds=0.1, jev_failure_policy=policy,
    ))
    stopped = asyncio.Event()

    @asynccontextmanager
    async def use(*args, **kwargs):
        try:
            await asyncio.Event().wait()
            yield None
        finally:
            stopped.set()

    monkeypatch.setattr(manager, "use", use)
    with pytest.raises(ServiceUnavailableError) as error:
        await manager.search("cfg-1", "q", strategy="multi_jev")
    assert error.value.layer.value == "engine"
    assert error.value.stage.value == "retrieve"
    assert error.value.retryable is True
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_jev_failed_source_cancels_other_inflight_scorers(monkeypatch):
    import asyncio

    manager = EngineManager(jev_settings())
    started, stopped = asyncio.Event(), asyncio.Event()

    async def raw(scid, query, **kwargs):
        if scid == "cfg-1":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        await started.wait()
        raise RuntimeError("scorer unavailable")

    monkeypatch.setattr(manager, "_search_raw", raw)
    with pytest.raises(RuntimeError, match="scorer unavailable"):
        await manager.search_many([("cfg-1", None), ("cfg-2", None)], "q", strategy="multi_jev")
    assert stopped.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(("initial", "changed"), [("vector", "multi_jev"), ("multi_jev", "vector")])
async def test_retrieval_pins_mode_before_publication_lookup_await(monkeypatch, initial, changed):
    from sag_api.services import retrieval_service

    recorded = []

    class Engine:
        _settings = jev_settings(search_strategy=initial)

        async def search_many(self, _targets, query, *, strategy=None, **kwargs):
            recorded.append(strategy or self._settings.search_strategy)
            return SearchOutcome(query=query, sections=[hit("evidence", 0.9)])

    engine = Engine()

    async def published(_sources):
        engine._settings.search_strategy = changed
        return {"cfg-1": ("document-1",)}

    monkeypatch.setattr(retrieval_service, "published_document_inputs", published)
    source = SimpleNamespace(id="source", name="source", sag_source_config_id="cfg-1")
    await retrieval_service.retrieve_relevant_sections(engine, [source], "evidence", top_k=1)
    assert recorded == [initial]


@pytest.mark.asyncio
async def test_settings_wait_for_whole_jev_fanout_before_new_endpoint_and_timeout(monkeypatch):
    import asyncio

    manager = EngineManager(jev_settings(search_source_concurrency=1, jev_ranking_timeout_seconds=4.0))
    updated = jev_settings(
        search_source_concurrency=1, jev_endpoint="https://new-jev.invalid/score", jev_ranking_timeout_seconds=7.0,
    )
    first_started, release_first, update_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed = []

    async def raw(scid, query, **kwargs):
        observed.append((scid, manager._settings.jev_endpoint, manager._settings.jev_ranking_timeout_seconds))
        if scid == "cfg-1":
            first_started.set()
            await release_first.wait()
        return SearchOutcome(query=query, sections=[hit(scid, 0.9, config=scid)],
                             stats={"ranking": {"method": "jev", "fallback_reason": None}})

    async def save():
        update_started.set()
        await manager.apply_settings(updated, reset_engines=True)

    monkeypatch.setattr(manager, "_search_raw", raw)
    query_task = asyncio.create_task(manager.search_many(
        [("cfg-1", None), ("cfg-2", None)], "q", strategy="multi_jev",
    ))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    save_task = asyncio.create_task(save())
    try:
        await asyncio.wait_for(update_started.wait(), timeout=1)
        assert not save_task.done(), "settings must wait for the whole search, including queued sources"
        release_first.set()
        await asyncio.wait_for(query_task, timeout=1)
        await asyncio.wait_for(save_task, timeout=1)
        await manager.search_many([("cfg-3", None)], "q", strategy="multi_jev")
    finally:
        release_first.set()
        for task in (query_task, save_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(query_task, save_task, return_exceptions=True)

    assert observed == [
        ("cfg-1", "https://jev.invalid/score", 4.0),
        ("cfg-2", "https://jev.invalid/score", 4.0),
        ("cfg-3", "https://new-jev.invalid/score", 7.0),
    ]


@pytest.mark.asyncio
async def test_configuration_read_gate_keeps_searches_concurrent(monkeypatch):
    import asyncio

    manager = EngineManager(jev_settings())
    first_started, release_first = asyncio.Event(), asyncio.Event()

    async def raw(scid, query, **kwargs):
        if scid == "cfg-1":
            first_started.set()
            await release_first.wait()
        return SearchOutcome(query=query, sections=[hit(scid, 0.9, config=scid)])

    monkeypatch.setattr(manager, "_search_raw", raw)
    first = asyncio.create_task(manager.search_many([("cfg-1", None)], "q", strategy="multi_jev"))
    try:
        await asyncio.wait_for(first_started.wait(), timeout=1)
        second = await asyncio.wait_for(
            manager.search_many([("cfg-2", None)], "q", strategy="multi_jev"), timeout=1,
        )
        assert second.sections[0].chunk_id == "cfg-2"
        assert not first.done()
    finally:
        release_first.set()
        await first


@pytest.mark.asyncio
async def test_agent_jev_coarse_fallback_is_visible_to_answer_model(monkeypatch, publication_source):
    from sag_api.tools import builtin
    from sag_api.tools.base import ToolContext

    source = await publication_source("jev-coarse-agent", "cfg-1", "document-1")
    engine = SimpleNamespace(_settings=jev_settings(search_strategy="multi_jev"))

    async def retrieve(*args, **kwargs):
        return SearchOutcome(query="q", sections=[hit("coarse", 0.7, score_source="event_score")], stats={
            "requested_strategy": "multi_jev", "effective_strategy": "multi_jev", "fallback_used": True,
            "ranking": {"method": "jev", "fallback_reason": "ranking_timeout"},
        })

    async def events(*args, **kwargs):
        return {}

    monkeypatch.setattr(builtin, "retrieve_relevant_sections", retrieve)
    monkeypatch.setattr(builtin, "recall_event_scores", events)
    result = await builtin.SearchContextTool().invoke(
        {"query": "q"}, ToolContext(engine_manager=engine, sources=[source]),
    )
    assert result.content.startswith("（JEV 精排未完成，本次返回粗排结果）\n")
    assert "evidence coarse" in result.content
    assert result.citations[0]["chunk_id"] == "coarse"


@pytest.mark.asyncio
async def test_pinned_execution_mode_keeps_original_legacy_persona_request(monkeypatch):
    from sag_api.services import retrieval_service

    async def published(_sources):
        return {"cfg-1": ("document-1",)}

    class Engine:
        async def search_many(self, _targets, query, *, strategy, **kwargs):
            assert strategy == "multi"
            return SearchOutcome(query=query, sections=[hit("evidence", 0.9)], stats={
                "requested_strategy": strategy, "effective_strategy": strategy,
            })

    monkeypatch.setattr(retrieval_service, "published_document_inputs", published)
    source = SimpleNamespace(id="source", name="source", sag_source_config_id="cfg-1")
    result = await retrieval_service.retrieve_relevant_sections(Engine(), [source], "evidence", strategy="atomic")
    assert result.stats["requested_strategy"] == "atomic"
    assert result.stats["effective_strategy"] == "multi"


@pytest.mark.asyncio
async def test_jev_configuration_admission_times_out_without_starting_or_leaking_reader(monkeypatch):
    import asyncio

    from sag_api.core.errors import ServiceUnavailableError

    manager = EngineManager(jev_settings(search_source_timeout=0.01, jev_ranking_timeout_seconds=0.1))
    started = []

    async def raw(scid, query, **kwargs):
        started.append(scid)
        return SearchOutcome(query=query, sections=[hit(scid, 0.9, config=scid)])

    monkeypatch.setattr(manager, "_search_raw", raw)
    async with manager._search_configuration_gate.write():
        with pytest.raises(ServiceUnavailableError) as error:
            await asyncio.wait_for(
                manager.search_many([("blocked", None)], "q", strategy="multi_jev"), timeout=1.6,
            )
        assert error.value.layer.value == "engine"
        assert error.value.stage.value == "retrieve"
        assert error.value.retryable is True
        assert started == []

    result = await asyncio.wait_for(
        manager.search_many([("after-release", None)], "q", strategy="multi_jev"), timeout=1,
    )
    assert result.sections[0].chunk_id == "after-release"
    assert started == ["after-release"]
    # A leaked admission reader would prevent a later settings write forever.
    await asyncio.wait_for(manager.apply_settings(jev_settings(), reset_engines=False), timeout=1)


@pytest.mark.asyncio
async def test_gate_admission_timeout_does_not_limit_work_after_entry():
    import asyncio

    from sag_api.sag.engine_manager import _EngineLifecycleGate

    gate = _EngineLifecycleGate()
    async with gate.read(timeout=0.01):
        await asyncio.sleep(0.03)
    async with gate.write():
        pass
