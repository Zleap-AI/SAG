"""JEV configuration remains independent from existing model defaults."""

import pytest
from pydantic import ValidationError

from sag_api.core.config import Settings
from sag_api.sag.config_builder import build_engine_config
from sag_api.schemas.system import ModelConfigUpdate


def test_jev_defaults_preserve_existing_search_and_embedding_defaults():
    configuration = Settings(_env_file=None)
    assert configuration.search_strategy == "vector"
    assert configuration.jev_endpoint is None
    assert configuration.jev_api_key is None
    assert configuration.jev_model == "jev-latest"
    assert configuration.jev_ranking_timeout_seconds == 30
    assert configuration.jev_failure_policy == "error"
    assert configuration.effective_embedding_schema_dimensions == 1024
    assert build_engine_config(configuration).jev is None


def test_application_env_builds_explicit_jev_connection(monkeypatch):
    monkeypatch.setenv("SAG_JEV_ENDPOINT", "https://score.test/custom/questions")
    monkeypatch.setenv("SAG_JEV_API_KEY", " independent-key ")
    monkeypatch.setenv("SAG_JEV_MODEL", " custom-model ")
    monkeypatch.setenv("JEV_ENDPOINT", "https://wrong.test/score")
    monkeypatch.setenv("JEV_API_KEY", "wrong-sdk-key")
    configuration = Settings(_env_file=None, llm_api_key="llm-secret")
    connection = build_engine_config(configuration).jev
    assert connection is not None
    assert connection.endpoint == "https://score.test/custom/questions"
    assert connection.api_key == "independent-key"
    assert connection.model == "custom-model"
    assert "independent-key" not in repr(configuration)


def test_bare_sdk_environment_cannot_enable_jev_or_borrow_llm_key(monkeypatch):
    monkeypatch.setenv("JEV_ENDPOINT", "https://wrong.test/score")
    monkeypatch.setenv("JEV_API_KEY", "sdk-secret")
    for name in ("SAG_JEV_ENDPOINT", "SAG_JEV_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    configuration = Settings(_env_file=None, llm_api_key="llm-secret")
    assert configuration.jev_api_key is None
    assert build_engine_config(configuration).jev is None


@pytest.mark.parametrize("endpoint", [
    "ftp://score.test/q", "https://user:secret@score.test/q", "https://score.test/q#fragment",
    "https://score.test:0/q", "https://score.test:70000/q", "https://score.test:invalid/q",
    "https:///q", "https://score.test/a b",
])
def test_invalid_jev_endpoint_rejected_without_including_value(endpoint):
    with pytest.raises(ValidationError):
        ModelConfigUpdate(jev_endpoint=endpoint)
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, jev_endpoint=endpoint)
    assert endpoint not in str(error.value)


@pytest.mark.parametrize("patch", [
    {"jev_model": "  "}, {"jev_model": None}, {"jev_ranking_timeout_seconds": 0},
    {"jev_ranking_timeout_seconds": 121}, {"jev_ranking_timeout_seconds": None},
    {"jev_failure_policy": "vector"}, {"jev_failure_policy": None},
])
def test_invalid_jev_options_rejected(patch):
    with pytest.raises(ValidationError):
        ModelConfigUpdate(**patch)


def test_partial_jev_update_normalizes_without_enabling_strategy():
    update = ModelConfigUpdate(jev_endpoint=" https://score.test/custom?q=1 ", jev_model=" custom ", jev_api_key="  ")
    assert update.model_dump(exclude_unset=True) == {
        "jev_endpoint": "https://score.test/custom?q=1", "jev_model": "custom", "jev_api_key": None,
    }
    assert "search_strategy" not in update.model_fields_set


def test_engine_scorer_transport_timeout_is_bounded_and_retries_disabled():
    for ranking_timeout, expected_transport_timeout in [(0.1, 1.0), (30.0, 30.0), (120.0, 120.0)]:
        configuration = Settings(_env_file=None, jev_endpoint="https://score.test/q", jev_api_key="key",
                                 jev_ranking_timeout_seconds=ranking_timeout)
        connection = build_engine_config(configuration).jev
        assert connection is not None
        assert connection.timeout == expected_transport_timeout
        assert connection.max_retries == 0
