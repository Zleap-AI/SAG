"""Persist complete scorer bindings and keep malformed stored drafts recoverable."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.config import Settings
from sag_api.core.errors import ConfigurationError
from sag_api.db.models import Setting
from sag_api.sag import EngineManager
from sag_api.sag.config_builder import build_engine_config
from sag_api.sag.jev_probe import probe_jev_connection
from sag_api.services import settings_service


@pytest.fixture
async def binding_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/binding.db")
    async with engine.begin() as connection:
        await connection.run_sync(Setting.__table__.create)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.mark.parametrize("patch, expected_key", [
    ({"jev_api_key": "tenant-key"}, "tenant-key"),
    ({"jev_endpoint": "https://first.test/score"}, "first-key"),
    ({"jev_model": "tenant-model"}, "first-key"),
    ({"jev_ranking_timeout_seconds": 42}, "first-key"),
    ({"jev_failure_policy": "fallback_to_score"}, "first-key"),
    ({"jev_api_key": ""}, "first-key"),
])
async def test_partial_jev_save_keeps_complete_binding_after_environment_changes(
    binding_session, monkeypatch, patch, expected_key,
):
    active = Settings(_env_file=None, jev_endpoint="https://first.test/score", jev_api_key="first-key")
    monkeypatch.setattr(settings_service, "_settings", active)
    await settings_service.save_model_config(binding_session, patch)

    # Simulate a new worker inheriting a different host connection.
    restarted = Settings(_env_file=None, jev_endpoint="https://second.test/score", jev_api_key="second-key")
    settings_service.apply_overrides(restarted, await settings_service.load_overrides(binding_session))
    assert restarted.jev_endpoint == "https://first.test/score"
    assert restarted.jev_api_key == expected_key
    assert restarted.jev_configured is True


async def test_unrelated_model_save_does_not_capture_environment_jev_credentials(binding_session, monkeypatch):
    active = Settings(_env_file=None, jev_endpoint="https://first.test/score", jev_api_key="first-key")
    monkeypatch.setattr(settings_service, "_settings", active)
    await settings_service.save_model_config(binding_session, {"llm_model": "ordinary-model"})
    row = await binding_session.scalar(select(Setting))
    assert row.value == {"llm_model": "ordinary-model"}
    restarted = Settings(_env_file=None, jev_endpoint="https://second.test/score", jev_api_key="second-key")
    settings_service.apply_overrides(restarted, await settings_service.load_overrides(binding_session))
    assert restarted.jev_endpoint == "https://second.test/score"
    assert restarted.jev_api_key == "second-key"


@pytest.mark.parametrize("stored, expected_endpoint", [
    ({"jev_api_key": "unbound-tenant-key"}, None),
    ({"jev_api_key": None}, None),
    ({"jev_endpoint": "https://first.test/score"}, "https://first.test/score"),
])
async def test_legacy_partial_binding_never_combines_with_environment_credentials(
    binding_session, stored, expected_endpoint,
):
    binding_session.add(Setting(scope="global", key="model_config", value=stored))
    await binding_session.commit()
    restarted = Settings(_env_file=None, jev_endpoint="https://second.test/score", jev_api_key="second-key")
    settings_service.apply_overrides(restarted, await settings_service.load_overrides(binding_session))
    assert restarted.jev_endpoint == expected_endpoint
    assert restarted.jev_api_key is None
    assert restarted.jev_configured is False


BAD_JEV_FIELDS = [
    {"jev_endpoint": 17},
    {"jev_endpoint": "https://score.test/" + "x" * 1000},
    {"jev_model": None},
    {"jev_model": 17},
    {"jev_model": " "},
    {"jev_model": "x" * 201},
    {"jev_api_key": 17},
    {"jev_api_key": " "},
    {"jev_api_key": "x" * 8193},
    {"jev_ranking_timeout_seconds": None},
    {"jev_ranking_timeout_seconds": "30"},
    {"jev_ranking_timeout_seconds": True},
    {"jev_ranking_timeout_seconds": 0},
    {"jev_ranking_timeout_seconds": 121},
    {"jev_ranking_timeout_seconds": float("nan")},
    {"jev_ranking_timeout_seconds": float("inf")},
    {"jev_failure_policy": None},
    {"jev_failure_policy": []},
    {"jev_failure_policy": "vector"},
]


@pytest.mark.parametrize("bad_fields", BAD_JEV_FIELDS)
def test_malformed_stored_jev_group_does_not_block_vector_or_offer_jev(bad_fields):
    active = Settings(_env_file=None, jev_endpoint="https://score.test/q", jev_api_key="tenant-key")
    settings_service.apply_overrides(active, {
        "jev_endpoint": "https://score.test/q", "jev_api_key": "tenant-key", **bad_fields,
    })
    assert active.jev_configured is False
    assert build_engine_config(active).jev is None
    report = EngineManager.strategies_capability_report(active)
    assert "vector" in report["enabled"]
    assert "multi_jev" in report["disabled"]
    with pytest.raises(ConfigurationError) as error:
        EngineManager(active)._effective_search_strategy("multi_jev")
    assert "JEV" in str(error.value)


@pytest.mark.parametrize("bad_fields", [
    {"jev_model": None}, {"jev_model": 17},
    {"jev_ranking_timeout_seconds": 0}, {"jev_failure_policy": "vector"},
])
async def test_invalid_stored_jev_draft_is_rejected_by_probe_without_calling_scorer(monkeypatch, bad_fields):
    from sag_api.sag import jev_probe

    active = Settings(_env_file=None, jev_endpoint="https://score.test/q", jev_api_key="tenant-key")
    settings_service.apply_overrides(active, bad_fields)

    def unexpected_scorer(**_kwargs):
        pytest.fail("invalid stored JEV draft reached the scorer")

    monkeypatch.setattr(jev_probe, "JevClient", unexpected_scorer)
    result = await probe_jev_connection(active)
    assert result.ok is False
    assert "JEV" in result.message


async def test_bad_persisted_model_keeps_capabilities_and_vector_recovery_available(binding_session, monkeypatch):
    from sag_api.api.v1 import system
    from sag_api.core.db import get_session
    from sag_api.core.deps import get_current_user
    from sag_api.main import create_app

    binding_session.add(Setting(scope="global", key="model_config", value={
        "search_strategy": "multi_jev", "jev_endpoint": "https://score.test/q",
        "jev_api_key": "tenant-key", "jev_model": None,
    }))
    await binding_session.commit()
    active = Settings(_env_file=None)
    settings_service.apply_overrides(active, await settings_service.load_overrides(binding_session))
    monkeypatch.setattr(system, "settings", active)
    monkeypatch.setattr(settings_service, "_settings", active)
    app = create_app()
    app.state.engine_manager = EngineManager(active)
    app.state.engine_manager.aclose_all = AsyncMock()

    async def current_user():
        return SimpleNamespace(id="tenant-user")

    async def current_session():
        yield binding_session

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_session] = current_session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        capabilities = await client.get("/api/v1/system/capabilities")
        assert capabilities.status_code == 200
        assert "multi_jev" in capabilities.json()["search_strategies_disabled"]
        enabled = await client.put("/api/v1/system/model-config", json={"search_strategy": "multi_jev"})
        assert enabled.status_code == 400
        fixed = await client.put("/api/v1/system/model-config", json={"search_strategy": "vector"})
        assert fixed.status_code == 200
    assert active.search_strategy == "vector"
    assert build_engine_config(active).jev is None
