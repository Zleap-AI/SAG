from types import SimpleNamespace

import pytest

from sag_api.sag.dto import RetrievedSection, SearchOutcome
from sag_api.services.engine_routing import retrieve_automatic


@pytest.mark.asyncio
async def test_auto_routes_bound_engines_and_interleaves_without_comparing_scores():
    sources = [SimpleNamespace(engine_kind="sag1", id="one"), SimpleNamespace(engine_kind="sag2", id="two")]
    calls = []

    async def retrieve(group, strategy):
        calls.append((group[0].id, strategy))
        return SearchOutcome(
            query="q",
            sections=[
                RetrievedSection(
                    chunk_id=strategy,
                    source_config_id=group[0].id,
                    content=strategy,
                    score=100 if strategy == "vector" else 0.9,
                )
            ],
        )

    result = await retrieve_automatic(sources, "q", sag1_strategy="vector", top_k=8, retrieve=retrieve)
    assert set(calls) == {("one", "vector"), ("two", "sag2")}
    assert {s.metadata["engine_kind"] for s in result.sections} == {"sag1", "sag2"}
    assert result.stats["effective_strategy"] == "auto"


@pytest.mark.asyncio
async def test_partial_failure_is_reported_instead_of_appearing_complete():
    sources = [SimpleNamespace(engine_kind="sag1"), SimpleNamespace(engine_kind="sag2")]

    async def retrieve(group, strategy):
        if strategy == "sag2":
            raise RuntimeError("JEV unavailable")
        return SearchOutcome(query="q", sections=[RetrievedSection(content="evidence", chunk_id="one")])

    result = await retrieve_automatic(sources, "q", sag1_strategy="vector", top_k=8, retrieve=retrieve)
    assert result.stats["partial_results"] is True
    assert result.stats["engine_failures"][0]["engine_kind"] == "sag2"
    assert len(result.sections) == 1


@pytest.mark.asyncio
async def test_sag1_fanout_failure_is_not_hidden_by_successful_native_group():
    from sag_api.core.config import Settings
    from sag_api.sag.search_reader import SearchReader

    async def broken(*args, **kwargs):
        raise RuntimeError("legacy storage offline")

    reader = SearchReader(SimpleNamespace(
        settings=Settings(_env_file=None), effective_search_strategy=lambda value: value, search=broken,
    ))
    async def retrieve(group, strategy):
        if strategy == "multi":
            return await reader.search_many([("legacy", None)], "q", strategy=strategy)
        return SearchOutcome(query="q", sections=[RetrievedSection(content="native evidence")])

    result = await retrieve_automatic(
        [SimpleNamespace(engine_kind="sag1"), SimpleNamespace(engine_kind="sag2")], "q",
        sag1_strategy="multi", top_k=8, retrieve=retrieve,
    )
    assert result.stats["partial_results"] is True
    assert result.stats["engine_failures"][0]["engine_kind"] == "sag1"
    assert "legacy storage offline" in result.stats["engine_failures"][0]["message"]


@pytest.mark.asyncio
async def test_mixed_results_preserve_jev_fallback_provenance():
    async def retrieve(group, strategy):
        return SearchOutcome(query="q", sections=[], stats={
            "fallback_used": strategy == "multi_jev",
            "ranking": {"method": "jev", "fallback_reason": "deadline" if strategy == "multi_jev" else None},
            "source_stats": {strategy: {"fallback_used": strategy == "multi_jev"}},
        })
    result = await retrieve_automatic(
        [SimpleNamespace(engine_kind="sag1"), SimpleNamespace(engine_kind="sag2")], "q",
        sag1_strategy="multi_jev", top_k=8, retrieve=retrieve,
    )
    assert result.stats["fallback_used"] is True
    assert result.stats["jev_fallback_used"] is True
    assert result.stats["engine_stats"]["sag1"]["ranking"]["fallback_reason"] == "deadline"
    assert result.stats["source_stats"]["multi_jev"]["fallback_used"] is True
