"""JEV must be explicitly available and never silently become vector retrieval."""

import pytest

from sag_api.core.config import Settings
from sag_api.core.errors import ConfigurationError
from sag_api.sag.engine_manager import EngineManager
from sag_api.schemas.search import GlobalSearchRequest


def configured(**updates):
    return Settings(_env_file=None, jev_endpoint="https://scorer.invalid/v1/score", jev_api_key="jev-only", **updates)


def test_explicit_jev_request_is_valid_and_maps_to_native_profile():
    request = GlobalSearchRequest(query="a question", strategy="multi_jev")
    manager = EngineManager(configured())
    assert manager._effective_search_strategy(request.strategy) == "multi_jev"
    assert manager._zleap_engine_strategy(request.strategy) == "pruned_expand_jev"


@pytest.mark.parametrize("provider", ["pgvector", "oceanbase"])
def test_explicit_jev_rejects_incompatible_storage_instead_of_falling_back(provider):
    manager = EngineManager(configured(sag_vector_provider=provider))
    report = manager.strategies_capability_report(manager._settings)
    assert "multi_jev" not in report["enabled"]
    assert report["disabled"]["multi_jev"]["reason"] == "vector_provider_lacks_lexical"
    with pytest.raises(ConfigurationError):
        manager._effective_search_strategy("multi_jev")


def test_saved_connection_does_not_enable_jev_and_missing_key_cannot_enable_it():
    manager = EngineManager(configured())
    assert manager._effective_search_strategy(None) == "vector"
    assert "multi_jev" in manager.strategies_capability_report(manager._settings)["enabled"]
    incomplete = EngineManager(Settings(_env_file=None, jev_endpoint="https://scorer.invalid/v1/score"))
    with pytest.raises(ConfigurationError):
        incomplete._effective_search_strategy("multi_jev")
