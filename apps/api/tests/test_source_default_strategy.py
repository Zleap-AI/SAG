"""Per-source default retrieval mode: validation, persistence and resolution order."""

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from sag_api.core.config import Settings, settings
from sag_api.services.retrieval_service import (
    resolve_search_strategy,
)
from sag_api.tools.builtin import knowledge_search_strategy


def configured(**changes) -> Settings:
    return Settings(_env_file=None).model_copy(update={
        "jev_endpoint": "https://jev.invalid/score",
        "jev_api_key": "synthetic-jev-key",
        "sag_vector_provider": "lancedb",
        "search_strategy": "multi_es_fast",
        **changes,
    })


def source(default: str | None, config_id: str = "cfg-1"):
    return SimpleNamespace(
        id=config_id, name=config_id, sag_source_config_id=config_id, default_search_strategy=default,
    )


@pytest.fixture
async def source_client(monkeypatch):
    from sag_api.main import app

    async def skip_unrelated_mcp(_factory, _app):
        return None

    async def skip_unrelated_universe_refresh(*_args, **_kwargs):
        return None

    monkeypatch.setattr("sag_api.runtime._RuntimeFactory.start_mcp", skip_unrelated_mcp)
    # Source updates schedule a universe projection job. It keeps writing SQLite
    # after the app lifespan ends and races the shared teardown cleanup.
    monkeypatch.setattr(
        "sag_api.services.universe_service.schedule_universe_refresh", skip_unrelated_universe_refresh,
    )
    snapshot = settings.model_dump()
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                registration = await client.post(
                    "/api/v1/auth/register",
                    json={"email": f"source-mode-{uuid4().hex}@t.com", "password": "password123"},
                )
                assert registration.status_code == 201
                client.headers["Authorization"] = f"Bearer {registration.json()['access_token']}"
                yield client
    finally:
        for key, value in snapshot.items():
            setattr(settings, key, value)


def test_default_resolution_ignores_retired_per_source_preferences():
    configuration = configured(search_strategy="vector")
    scoped = [source("sag2"), source("sag2", "cfg-2")]
    assert resolve_search_strategy(configuration=configuration, sources=scoped).strategy == "auto"
    assert knowledge_search_strategy({}, configuration, scoped) == "auto"
    assert resolve_search_strategy(requested="multi", configuration=configuration, sources=scoped).strategy == "multi"


@pytest.mark.asyncio
@pytest.mark.parametrize("engine_kind", ["sag1", "sag2"])
async def test_source_algorithm_is_fixed_but_metadata_remains_editable(source_client, engine_kind):
    created = await source_client.post("/api/v1/sources", json={"name": "Engine binding", "engine_kind": engine_kind})
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]
    assert created.json()["engine_kind"] == engine_kind
    changed = await source_client.patch(f"/api/v1/sources/{source_id}", json={"name": "Renamed"})
    assert changed.status_code == 200
    assert changed.json()["engine_kind"] == engine_kind
    for replacement in ["sag1", "sag2"]:
        rejected = await source_client.patch(f"/api/v1/sources/{source_id}", json={"engine_kind": replacement})
        assert rejected.status_code == 422
    assert (await source_client.get(f"/api/v1/sources/{source_id}")).json()["engine_kind"] == engine_kind


@pytest.mark.asyncio
async def test_retired_source_default_mode_is_not_an_editable_setting(source_client):
    created = await source_client.post("/api/v1/sources", json={"name": "Default SAG1"})
    assert created.status_code == 201
    assert created.json()["engine_kind"] == "sag1"
    rejected = await source_client.patch(
        f"/api/v1/sources/{created.json()['id']}", json={"default_search_strategy": "sag2"}
    )
    assert rejected.status_code == 422
