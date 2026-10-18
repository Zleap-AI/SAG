"""JEV timeout classification must not change other retrieval strategies."""

from contextlib import asynccontextmanager

import pytest
from zleap.sag.exceptions import AIAuthError, AIInvalidRequestError, AITransientError
from zleap.sag.pipeline import SearchResult
from zleap.sag.pipeline.errors import SearchStageError

from sag_api.core.config import Settings
from sag_api.core.error_taxonomy import ErrorLayer, ErrorStage
from sag_api.core.errors import ConfigurationError, ServiceUnavailableError, UpstreamError, ValidationError
from sag_api.sag.engine_manager import EngineManager
from sag_api.sag.errors import map_sag_errors


def wrapped_timeout(*, nested=False, retryable=False):
    cause = TimeoutError("private-provider-body")
    if nested:
        wrapper = RuntimeError("private-wrapper")
        wrapper.__cause__ = cause
        cause = wrapper
    failure = SearchStageError("检索执行失败", code="search_failed", retryable=retryable)
    failure.__cause__ = cause
    return failure


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("retryable", [False, True])
def test_search_timeout_without_jev_context_keeps_legacy_error_contract(nested, retryable):
    with pytest.raises(UpstreamError) as raised:
        with map_sag_errors(stage=ErrorStage.RETRIEVE):
            raise wrapped_timeout(nested=nested, retryable=retryable)
    error = raised.value
    assert type(error) is UpstreamError
    assert (error.status_code, error.code, error.retryable) == (502, "upstream_error", retryable)
    assert error.message == "[search_failed] 检索执行失败"
    assert (error.layer, error.stage) == (ErrorLayer.ENGINE, ErrorStage.RETRIEVE)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["vector", "multi", "multi_es_fast", "multi_jev"])
@pytest.mark.parametrize("nested", [False, True])
async def test_reader_classifies_wrapped_timeout_by_actual_request_strategy(monkeypatch, strategy, nested):
    # The configured default deliberately differs from non-JEV request overrides.
    manager = EngineManager(Settings(_env_file=None).model_copy(update={
        "search_strategy": "multi_jev", "jev_endpoint": "https://jev.invalid/score",
    }))

    @asynccontextmanager
    async def use(*_args, **_kwargs):
        class Engine:
            async def search(self, _request):
                raise wrapped_timeout(nested=nested)
        yield Engine()

    monkeypatch.setattr(manager, "use", use)
    expected = ServiceUnavailableError if strategy == "multi_jev" else UpstreamError
    with pytest.raises(expected) as raised:
        await manager._search_raw("cfg", "q", source=None, strategy=strategy, top_k=2)
    error = raised.value
    assert (error.layer, error.stage) == (ErrorLayer.ENGINE, ErrorStage.RETRIEVE)
    if strategy == "multi_jev":
        assert (error.status_code, error.code, error.retryable) == (503, "service_unavailable", True)
        assert "JEV" in error.message
        assert "精排" not in error.message  # A wrapped timeout does not prove its retrieval phase.
        assert "private" not in error.message
    else:
        assert type(error) is UpstreamError
        assert (error.status_code, error.code, error.retryable) == (502, "upstream_error", False)
        assert error.message == "[search_failed] 检索执行失败"


@pytest.mark.parametrize(("provider_error", "expected"), [
    (AIAuthError, ConfigurationError),
    (AIInvalidRequestError, ValidationError),
    (AITransientError, ServiceUnavailableError),
])
@pytest.mark.parametrize("timeout_outside", [False, True])
def test_jev_provider_classification_takes_priority_over_nested_timeout(provider_error, expected, timeout_outside):
    provider = provider_error("private-provider-body", provider="jev")
    timeout = TimeoutError("private-timeout-body")
    if timeout_outside:
        timeout.__cause__ = provider
        cause = timeout
    else:
        provider.__cause__ = timeout
        cause = provider
    wrapped = SearchStageError("搜索失败", code="search_failed")
    wrapped.__cause__ = cause
    with pytest.raises(expected) as raised:
        with map_sag_errors(stage=ErrorStage.RETRIEVE):
            raise wrapped
    assert type(raised.value) is expected
    assert "JEV" in raised.value.message
    assert "private" not in raised.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["multi", "multi_es_fast"])
async def test_non_jev_timeout_still_uses_existing_vector_fallback(monkeypatch, strategy):
    manager = EngineManager(Settings(_env_file=None).model_copy(update={
        "search_strategy": "multi_jev", "jev_endpoint": "https://jev.invalid/score", "search_fallback_vector": True,
    }))

    @asynccontextmanager
    async def use(*_args, **_kwargs):
        class Engine:
            async def search(self, request):
                if request.options.strategy != "vector":
                    raise wrapped_timeout()
                return SearchResult(query=request.query, original_query=request.query, chunks=())
        yield Engine()

    monkeypatch.setattr(manager, "use", use)
    outcome = await manager.search("cfg", "q", strategy=strategy)
    assert outcome.stats["requested_strategy"] == strategy
    assert outcome.stats["effective_strategy"] == "vector"
    assert outcome.stats["fallback_used"] is True
