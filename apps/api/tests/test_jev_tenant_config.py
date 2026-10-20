"""Tenant storage, endpoint-bound Key retention, and draft API isolation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.config import Settings, settings
from sag_api.db.models import Setting
from sag_api.services import settings_service

JEV_FIELDS = (
    "jev_endpoint", "jev_model", "jev_api_key", "jev_ranking_timeout_seconds", "jev_failure_policy", "search_strategy",
)


@pytest.fixture
async def tenant_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/tenant.db")
    async with engine.begin() as connection:
        await connection.run_sync(Setting.__table__.create)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture
def restore_jev_settings(monkeypatch):
    monkeypatch.setattr(settings, "search_top_k", settings.search_top_k)
    for field in JEV_FIELDS:
        monkeypatch.setattr(settings, field, getattr(Settings(_env_file=None), field))


@pytest.mark.asyncio
async def test_endpoint_changes_clear_key_but_same_endpoint_empty_input_retains(tenant_session, monkeypatch):
    active = Settings(_env_file=None, llm_api_key="llm-key")
    monkeypatch.setattr(settings_service, "_settings", active)
    original = {"jev_endpoint": "https://first.test/custom", "jev_api_key": "tenant-key"}
    first = await settings_service.save_model_config(tenant_session, original)
    assert first["jev_api_key_set"] is True
    assert "jev_api_key" not in first
    assert first["search_strategy"] == "vector"
    retained = await settings_service.save_model_config(tenant_session, {"jev_api_key": ""})
    assert retained["jev_api_key_set"] is True
    changed = await settings_service.save_model_config(tenant_session, {"jev_endpoint": "https://second.test/custom"})
    assert changed["jev_api_key_set"] is False
    assert active.jev_api_key is None
    row = await tenant_session.scalar(select(Setting))
    assert row is not None and row.value["jev_api_key"] is None
    cleared = await settings_service.save_model_config(tenant_session, {"jev_endpoint": None})
    assert cleared["jev_endpoint"] is None
    assert cleared["jev_api_key_set"] is False


def test_preview_never_leaks_saved_key_to_different_draft_endpoint():
    active = Settings(_env_file=None, jev_endpoint="https://first.test/q", jev_api_key="first-key")
    same = settings_service.model_config_preview(active, {"jev_api_key": ""})
    different = settings_service.model_config_preview(
        active, {"jev_endpoint": "https://second.test/q", "jev_api_key": ""},
    )
    assert same.jev_api_key == "first-key"
    assert different.jev_api_key is None
    assert active.jev_api_key == "first-key"
    assert settings_service.model_config_preview(active, {
        "jev_endpoint": "https://second.test/q", "jev_api_key": "second-key",
    }).jev_api_key == "second-key"


@pytest.mark.asyncio
async def test_endpoint_switch_clears_environment_key_in_persisted_override(tenant_session, monkeypatch):
    active = Settings(_env_file=None, jev_endpoint="https://environment.test/q", jev_api_key="env-key")
    monkeypatch.setattr(settings_service, "_settings", active)
    await settings_service.save_model_config(tenant_session, {"jev_endpoint": "https://tenant.test/q"})
    restarted = Settings(_env_file=None, jev_endpoint="https://environment.test/q", jev_api_key="env-key")
    settings_service.apply_overrides(restarted, await settings_service.load_overrides(tenant_session))
    assert restarted.jev_endpoint == "https://tenant.test/q"
    assert restarted.jev_api_key is None


@pytest.mark.asyncio
async def test_two_tenant_databases_reopen_with_their_own_keys(tmp_path, monkeypatch):
    engines = []
    try:
        for tenant in ("first", "second"):
            engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/{tenant}.db")
            engines.append(engine)
            async with engine.begin() as connection:
                await connection.run_sync(Setting.__table__.create)
            active = Settings(_env_file=None)
            monkeypatch.setattr(settings_service, "_settings", active)
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                await settings_service.save_model_config(session, {
                    "jev_endpoint": f"https://{tenant}.test/q", "jev_api_key": f"{tenant}-key",
                })
        for tenant, engine in zip(("first", "second"), engines, strict=True):
            restarted = Settings(_env_file=None)
            async with async_sessionmaker(engine)() as session:
                settings_service.apply_overrides(restarted, await settings_service.load_overrides(session))
            assert restarted.jev_endpoint == f"https://{tenant}.test/q"
            assert restarted.jev_api_key == f"{tenant}-key"
            assert restarted.llm_api_key != restarted.jev_api_key
    finally:
        for engine in engines:
            await engine.dispose()


@pytest.fixture
async def config_client(tenant_session, restore_jev_settings):
    from sag_api.core.db import get_session
    from sag_api.core.deps import get_current_user
    from sag_api.main import create_app

    app = create_app()
    from sag_api.sag import EngineManager

    app.state.engine_manager = EngineManager(settings)
    app.state.engine_manager.aclose_all = AsyncMock()

    async def current_user():
        return SimpleNamespace(id="tenant-user")

    async def current_session():
        yield tenant_session

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_session] = current_session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, app


@pytest.mark.asyncio
async def test_draft_api_uses_own_key_and_never_persists(config_client, tenant_session, monkeypatch):
    from sag_api.api.v1 import system
    from sag_api.sag.jev_probe import JevConnectionTestResult

    client, app = config_client
    observed = []

    async def probe(configuration):
        observed.append(configuration)
        return JevConnectionTestResult(ok=True, message="JEV 连接成功")

    monkeypatch.setattr(system, "probe_jev_connection", probe)
    saved = await client.put("/api/v1/system/model-config", json={
        "jev_endpoint": "https://first.test/q", "jev_api_key": "stored-key",
    })
    assert saved.status_code == 200, saved.text
    row_before = dict((await tenant_session.scalar(select(Setting))).value)
    response = await client.post("/api/v1/system/model-config/jev/test", json={
        "jev_endpoint": "https://second.test/q", "jev_api_key": "draft-key", "jev_model": "draft-model",
    })
    assert response.status_code == 200 and response.json()["ok"] is True
    assert observed[0].jev_api_key == "draft-key"
    assert observed[0].jev_endpoint == "https://second.test/q"
    assert (await tenant_session.scalar(select(Setting))).value == row_before
    assert settings.jev_endpoint == "https://first.test/q"
    assert settings.jev_api_key == "stored-key"
    assert "stored-key" not in saved.text and "draft-key" not in response.text
    assert app.state.engine_manager.aclose_all.await_count == 1


@pytest.mark.asyncio
async def test_enable_without_key_rejected_before_persistence_and_bad_config_can_be_fixed(
    config_client, tenant_session,
):
    client, _app = config_client
    rejected = await client.put("/api/v1/system/model-config", json={
        "search_strategy": "multi_jev", "jev_endpoint": "https://first.test/q",
    })
    assert rejected.status_code == 400, rejected.text
    assert "Key" in rejected.text
    assert await tenant_session.scalar(select(Setting)) is None
    settings.search_strategy = "multi_jev"
    settings.jev_endpoint = "invalid legacy draft"
    fixed = await client.put("/api/v1/system/model-config", json={"search_strategy": "vector"})
    assert fixed.status_code == 200, fixed.text
    assert settings.search_strategy == "vector"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/v1/system/model-config", "/api/v1/system/model-config/jev/test"])
async def test_invalid_credential_inputs_are_not_reflected_by_422(config_client, path):
    client, _app = config_client
    method = "put" if path.endswith("model-config") else "post"
    for payload, secret in [
        ({"jev_endpoint": "https://user:embedded-secret@score.test/q"}, "embedded-secret"),
        ({"jev_api_key": "oversized-secret" * 700}, "oversized-secret"),
    ]:
        response = await getattr(client, method)(path, json=payload)
        assert response.status_code == 422, response.text
        assert secret not in response.text
        assert all("input" not in item and "ctx" not in item for item in response.json()["detail"])


@pytest.mark.asyncio
async def test_active_jev_endpoint_change_requires_new_key_but_unrelated_save_still_works(config_client):
    client, _app = config_client
    saved = await client.put("/api/v1/system/model-config", json={
        "search_strategy": "multi_jev", "jev_endpoint": "https://first.test/q", "jev_api_key": "first-key",
    })
    assert saved.status_code == 200, saved.text
    refused = await client.put("/api/v1/system/model-config", json={"jev_endpoint": "https://second.test/q"})
    assert refused.status_code == 400, refused.text
    assert settings.jev_endpoint == "https://first.test/q"
    assert settings.jev_api_key == "first-key"
    settings.jev_api_key = None  # An old invalid draft must still allow unrelated cards to save.
    unrelated = await client.put("/api/v1/system/model-config", json={"search_top_k": 9})
    assert unrelated.status_code == 200, unrelated.text


@pytest.mark.asyncio
async def test_hot_update_waits_for_complete_fanout_before_mutating_key(config_client, monkeypatch):
    import asyncio

    from sag_api.api.v1 import system
    from sag_api.sag import EngineManager
    from sag_api.sag.dto import SearchOutcome

    client, app = config_client
    route_entered = asyncio.Event()
    async def immediate_save(_session, patch):
        settings.jev_endpoint = patch["jev_endpoint"]
        settings.jev_api_key = patch["jev_api_key"]
        return settings_service.effective_model_config()

    monkeypatch.setattr(system.settings_service, "save_model_config", immediate_save)
    settings.jev_endpoint = "https://old.test/score"
    settings.jev_api_key = "old-tenant-key"
    monkeypatch.setattr(settings, "search_source_concurrency", 1)
    from contextlib import asynccontextmanager

    owner = EngineManager(settings)
    original_update = owner.configuration_update

    @asynccontextmanager
    async def configuration_update():
        route_entered.set()
        async with original_update():
            yield

    owner.configuration_update = configuration_update
    owner.aclose_all = AsyncMock()
    app.state.engine_manager = owner
    started = asyncio.Event()
    release = asyncio.Event()
    observed = []

    async def query_source(config_id, *_args, **_kwargs):
        observed.append((config_id, settings.jev_endpoint, settings.jev_api_key))
        if config_id == "first":
            started.set()
            await release.wait()
        return SearchOutcome(query="q", sections=[], stats={"ranking": {"method": "jev"}})

    owner.search = query_source
    query = asyncio.create_task(owner.search_many([("first", None), ("second", None)], "q", strategy="multi_jev"))
    await started.wait()
    update = asyncio.create_task(client.put("/api/v1/system/model-config", json={
        "jev_endpoint": "https://new.test/score", "jev_api_key": "new-tenant-key",
    }))
    try:
        # Let the request reach its mutation boundary while the old query owns its lease.
        await asyncio.wait_for(route_entered.wait(), 1)
        await asyncio.sleep(0)
        assert settings.jev_api_key == "old-tenant-key"
        assert not update.done()
        assert not owner.aclose_all.called
    finally:
        release.set()
        await query
        response = await update
    assert response.status_code == 200, response.text
    assert observed == [("first", "https://old.test/score", "old-tenant-key"),
                        ("second", "https://old.test/score", "old-tenant-key")]
    owner.aclose_all.assert_awaited_once()
    assert settings.jev_api_key == "new-tenant-key"
