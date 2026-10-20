"""Only enabled JEV knowledge tools receive the bounded retrieval budget."""

import asyncio
import os
import socket
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sag_api.core.config import Settings, settings
from sag_api.core.errors import ServiceUnavailableError
from sag_api.core.retrieval_policy import search_request_timeout_seconds, search_source_timeout_seconds
from sag_api.sag.engine_manager import EngineManager
from sag_api.services.agent_service import _adapt_tool
from sag_api.tools.base import ToolContext
from sag_api.tools.builtin import GetEntityTool, GetTimeTool, SearchContextTool


@pytest.mark.parametrize("count,expected", [(0, 0), (1, 42), (4, 42), (5, 84), (100, 126)])
def test_jev_request_budget_accounts_for_bounded_source_waves(count, expected):
    config = Settings(
        search_source_timeout=12,
        search_source_concurrency=4,
        search_source_candidate_limit=12,
        jev_ranking_timeout_seconds=30,
    )
    assert search_request_timeout_seconds(config, "multi_jev", count) == expected
    assert search_source_timeout_seconds(config, "vector") == 12


@pytest.mark.parametrize(
    "global_strategy,persona,expected",
    [
        ("vector", {}, None),
        ("multi", {}, None),
        ("vector", {"search_strategy": "multi_jev"}, 89),
        ("multi_jev", {}, 89),
        ("multi_jev", {"search_strategy": "vector"}, None),
    ],
)
def test_only_jev_search_context_overrides_default_tool_deadline(monkeypatch, global_strategy, persona, expected):
    monkeypatch.setattr(settings, "search_strategy", global_strategy)
    monkeypatch.setattr(settings, "search_source_timeout", 12)
    monkeypatch.setattr(settings, "jev_ranking_timeout_seconds", 30)
    monkeypatch.setattr(settings, "search_source_concurrency", 4)
    monkeypatch.setattr(settings, "search_source_candidate_limit", 12)
    context = ToolContext(engine_manager=None, sources=[SimpleNamespace() for _ in range(5)], persona=persona)
    assert _adapt_tool(SearchContextTool(), context, []).spec.timeout_seconds == expected
    assert _adapt_tool(GetEntityTool(), context, []).spec.timeout_seconds is None
    assert _adapt_tool(GetTimeTool(), context, []).spec.timeout_seconds is None


@pytest.mark.asyncio
@pytest.mark.parametrize("initial,updated", [(30, 120), (120, 30)])
async def test_agent_pins_only_ranking_budget_and_uses_current_jev_connection(monkeypatch, initial, updated):
    from sag_api.services import retrieval_service

    monkeypatch.setattr(settings, "search_strategy", "multi_jev")
    monkeypatch.setattr(settings, "search_source_timeout", 12)
    monkeypatch.setattr(settings, "jev_ranking_timeout_seconds", initial)
    monkeypatch.setattr(settings, "jev_endpoint", "https://old.invalid/score")
    monkeypatch.setattr(settings, "jev_api_key", "old-key")
    owner = EngineManager(settings)
    source = SimpleNamespace(id="source", name="资料", sag_source_config_id="config", config={})
    monkeypatch.setattr(
        retrieval_service,
        "_hidden_document_derivatives",
        AsyncMock(return_value=retrieval_service._HiddenDerivatives()),
    )
    monkeypatch.setattr(retrieval_service, "_visible_document_inputs", AsyncMock(return_value={"config": ("article",)}))
    requests, configurations, deadlines = [], [], []

    class Engine:
        async def search(self, request):
            requests.append(request)
            return SimpleNamespace(query="q", sections=[], stats={"ranking": {"method": "jev"}})

    @asynccontextmanager
    async def use(_config_id, current_source):
        configurations.append(owner._config_for(current_source))
        yield Engine()

    real_wait_for = asyncio.wait_for

    async def wait_for(awaitable, timeout):
        deadlines.append(timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(owner, "use", use)
    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    context = ToolContext(engine_manager=owner, sources=[source])
    tool = _adapt_tool(SearchContextTool(), context, [])
    assert tool.spec.timeout_seconds == 12 + initial + 5
    monkeypatch.setattr(settings, "jev_ranking_timeout_seconds", updated)
    monkeypatch.setattr(settings, "jev_endpoint", "https://current.invalid/score")
    monkeypatch.setattr(settings, "jev_api_key", "current-key")

    await tool.execute(
        {"query": "q"},
        SimpleNamespace(
            cancellation=SimpleNamespace(raise_if_cancelled=lambda: None),
        ),
    )

    assert len(requests) == 1
    assert requests[0].options.ranking.timeout_seconds == min(initial, updated)
    assert deadlines == [12 + min(initial, updated)]
    assert configurations[0].jev.endpoint == "https://current.invalid/score"
    assert configurations[0].jev.api_key == "current-key"
    assert "old-key" not in repr(context)


def test_jev_source_deadline_honors_pin_without_extending_current_budget():
    configuration = Settings(_env_file=None, search_source_timeout=12, jev_ranking_timeout_seconds=120)
    assert search_source_timeout_seconds(configuration, "multi_jev", jev_ranking_timeout_seconds=30) == 42
    configuration.jev_ranking_timeout_seconds = 10
    assert search_source_timeout_seconds(configuration, "multi_jev", jev_ranking_timeout_seconds=30) == 22
    assert search_source_timeout_seconds(configuration, "vector", jev_ranking_timeout_seconds=30) == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", [None, "multi_jev"])
@pytest.mark.parametrize("pinned", [False, True])
async def test_jev_request_deadline_includes_waiting_for_configuration(strategy, pinned):
    owner = EngineManager(Settings(
        _env_file=None, search_strategy="multi_jev", search_source_timeout=1,
        jev_endpoint="https://scorer.invalid/score", jev_api_key="independent",
        jev_ranking_timeout_seconds=120 if pinned else 0.1,
    ))
    options = {"jev_ranking_timeout_seconds": 0.1} if pinned else {}
    async with owner.configuration_update():
        request = asyncio.create_task(owner.search_many([("config", None)], "q", strategy=strategy, **options))
        try:
            done, _pending = await asyncio.wait({request}, timeout=1.5)
            assert done, "the 1.1-second retrieval deadline omitted its configuration wait"
            with pytest.raises(ServiceUnavailableError) as raised:
                await request
            assert raised.value.status_code == 503
            assert str(raised.value.stage) == "retrieve"
            assert raised.value.retryable is True
            assert "JEV" in raised.value.message and "超时" in raised.value.message
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
async def test_empty_jev_request_does_not_wait_for_configuration():
    owner = EngineManager(Settings(
        _env_file=None, search_strategy="multi_jev",
        jev_endpoint="https://scorer.invalid/score", jev_api_key="independent",
    ))
    async with owner.configuration_update():
        result = await asyncio.wait_for(owner.search_many([], "q"), 0.1)
    assert result.sections == []
    assert result.stats["sources"] == 0


@pytest.mark.asyncio
async def test_vector_request_keeps_its_existing_budget_after_configuration_wait():
    from sag_api.sag.dto import SearchOutcome

    owner = EngineManager(Settings(_env_file=None, search_source_timeout=1))

    async def vectors(*_args, **_kwargs):
        return SearchOutcome(query="q", sections=[], stats={"effective_strategy": "vector"})

    owner._search_chunk_vectors = vectors
    async with owner.configuration_update():
        request = asyncio.create_task(owner.search_many([("config", None)], "q", strategy="vector"))
        done, _pending = await asyncio.wait({request}, timeout=1.2)
        assert not done, "the new overall JEV deadline changed the existing vector path"
    result = await asyncio.wait_for(request, 1)
    assert result.stats["effective_strategy"] == "vector"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_during_cleanup", [False, True])
async def test_total_jev_deadline_drains_real_sockets_before_reporting_timeout(cancel_during_cleanup):
    owner = EngineManager(Settings(
        _env_file=None, search_source_timeout=1, jev_ranking_timeout_seconds=0.1,
        jev_endpoint="https://scorer.invalid/score", jev_api_key="independent",
    ))
    started, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    left, right = socket.socketpair()
    descriptors = (left.fileno(), right.fileno())

    class Engine:
        async def search(self, _request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                draining.set()
                await release.wait()
                left.close()
                right.close()

    @asynccontextmanager
    async def use(*_args):
        yield Engine()

    owner.use = use
    request = None
    try:
        # Leave less of the total deadline than the source's own 1.1 seconds.
        async with owner.configuration_update():
            request = asyncio.create_task(owner.search_many([("config", None)], "q", strategy="multi_jev"))
            await asyncio.sleep(0.3)
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(draining.wait(), 1)
        assert not request.done(), "the timeout escaped before socket cleanup completed"
        for descriptor in descriptors:
            os.fstat(descriptor)
        if cancel_during_cleanup:
            request.cancel("caller disconnected while the deadline was draining")
            done, _pending = await asyncio.wait({request}, timeout=0.05)
            assert not done
        release.set()
        expected = asyncio.CancelledError if cancel_during_cleanup else ServiceUnavailableError
        with pytest.raises(expected):
            await asyncio.wait_for(asyncio.shield(request), 1)
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        release.set()
        if request is not None:
            request.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(request, 1)
        left.close()
        right.close()
