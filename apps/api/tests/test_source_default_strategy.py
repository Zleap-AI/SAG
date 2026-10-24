"""Per-source default retrieval mode: validation, persistence and resolution order."""

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from sag_api.core.config import Settings, settings
from sag_api.sag import RetrievedSection, SearchOutcome
from sag_api.sag.engine_manager import EngineManager
from sag_api.services.retrieval_service import (
    common_source_default_strategy,
    resolve_search_strategy,
    retrieve_relevant_sections,
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


def test_common_default_requires_every_source_to_agree():
    assert common_source_default_strategy([]) is None
    assert common_source_default_strategy([source("sag2"), source("sag2", "cfg-2")]) == "sag2"
    assert common_source_default_strategy([source("sag2"), source(None, "cfg-2")]) is None
    assert common_source_default_strategy([source("sag2"), source("vector", "cfg-2")]) is None
    # Legacy aliases normalize before comparison.
    assert common_source_default_strategy([source("atomic"), source("multi", "cfg-2")]) == "multi"


def test_resolution_order_request_persona_source_global():
    configuration = configured(search_strategy="vector")
    scoped = [source("sag2"), source("sag2", "cfg-2")]

    def resolved(**kwargs):
        result = resolve_search_strategy(configuration=configuration, **kwargs)
        return result.strategy, result.origin

    assert resolved(requested="multi", persona_strategy="multi_jev", sources=scoped) == ("multi", "request")
    assert resolved(persona_strategy="multi_jev", sources=scoped) == ("multi_jev", "persona")
    assert resolved(sources=scoped) == ("sag2", "source_default")
    assert resolved(sources=[source("sag2"), source(None, "cfg-2")]) == ("vector", "global")
    assert resolved() == ("vector", "global")


def test_agent_tool_follows_source_default_before_legacy_vector():
    configuration = configured(search_strategy="multi_es_fast")
    assert knowledge_search_strategy({}, configuration, [source("sag2")]) == "sag2"
    assert knowledge_search_strategy({"search_strategy": "vector"}, configuration, [source("sag2")]) == "vector"
    assert knowledge_search_strategy({}, configuration, [source("sag2"), source(None, "cfg-2")]) == "vector"
    # A global JEV default still wins over an unknown legacy persona value, as before.
    assert knowledge_search_strategy({"search_strategy": "retired"}, configured(search_strategy="sag2"), []) == "sag2"


@pytest.mark.asyncio
async def test_retrieval_reports_source_default_origin(monkeypatch):
    from sag_api.services import retrieval_service

    captured = []

    class Engine:
        _settings = configured(search_strategy="vector")

        async def search_many(self, targets, query, **kwargs):
            captured.append(kwargs["strategy"])
            return SearchOutcome(query=query, sections=[RetrievedSection(
                chunk_id="c1", heading="h", content="evidence", score=0.9, source_config_id="cfg-1", source_id="d1",
                metadata={"score_source": "jev"},
            )], stats={"effective_strategy": "sag2"})

    async def published(sources, **_kwargs):
        return {s.sag_source_config_id: ("d1",) for s in sources}

    monkeypatch.setattr(retrieval_service, "published_document_inputs", published)
    outcome = await retrieve_relevant_sections(Engine(), [source("sag2")], "q", top_k=3)
    assert captured == ["sag2"]
    assert outcome.stats["requested_strategy"] == "sag2"
    assert outcome.stats["strategy_origin"] == "source_default"

    outcome = await retrieve_relevant_sections(Engine(), [source("sag2")], "q", strategy="multi_jev", top_k=3)
    assert captured[-1] == "multi_jev"
    assert outcome.stats["strategy_origin"] == "request"


async def test_source_default_mode_is_validated_persisted_and_clearable(source_client, monkeypatch):
    client = source_client
    monkeypatch.setattr(settings, "sag_vector_provider", "lancedb")
    monkeypatch.setattr(settings, "jev_endpoint", "https://jev.example/score")
    monkeypatch.setattr(settings, "jev_api_key", None)
    before = {item["id"] for item in (await client.get("/api/v1/sources")).json()}

    # Paid mode without a key is rejected before any row is written.
    rejected = await client.post("/api/v1/sources", json={"name": "SAG2 库", "default_search_strategy": "sag2"})
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["error"]["code"] == "jev_api_key_missing"
    assert {item["id"] for item in (await client.get("/api/v1/sources")).json()} == before

    unknown = await client.post("/api/v1/sources", json={"name": "x", "default_search_strategy": "nope"})
    assert unknown.status_code == 422

    monkeypatch.setattr(settings, "jev_api_key", "synthetic-jev-key")
    created = await client.post("/api/v1/sources", json={"name": "SAG2 库", "default_search_strategy": "sag2"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["default_search_strategy"] == "sag2"
    source_id = body["id"]
    assert (await client.get(f"/api/v1/sources/{source_id}")).json()["default_search_strategy"] == "sag2"

    # Follow-global is the default when the field is omitted.
    plain = await client.post("/api/v1/sources", json={"name": "普通库"})
    assert plain.status_code == 201 and plain.json()["default_search_strategy"] is None

    # Updates: a mode the storage cannot serve is rejected; null clears back to global.
    monkeypatch.setattr(settings, "sag_vector_provider", "pgvector")
    blocked = await client.patch(f"/api/v1/sources/{source_id}", json={"default_search_strategy": "multi_jev"})
    assert blocked.status_code == 400 and blocked.json()["error"]["code"] == "vector_provider_lacks_lexical"
    assert (await client.get(f"/api/v1/sources/{source_id}")).json()["default_search_strategy"] == "sag2"
    renamed = await client.patch(f"/api/v1/sources/{source_id}", json={"name": "改名"})
    assert renamed.status_code == 200 and renamed.json()["default_search_strategy"] == "sag2"
    cleared = await client.patch(f"/api/v1/sources/{source_id}", json={"default_search_strategy": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["default_search_strategy"] is None
    assert cleared.json()["name"] == "改名"
    # Sources are left in place: deleting would queue background work that
    # races the shared teardown cleanup for the SQLite lock.


async def test_global_search_uses_shared_source_default(source_client, monkeypatch):
    from sag_api.core.deps import get_engine_manager
    from sag_api.main import app

    client = source_client
    monkeypatch.setattr(settings, "sag_vector_provider", "lancedb")
    monkeypatch.setattr(settings, "search_strategy", "vector")
    monkeypatch.setattr(settings, "jev_endpoint", "https://jev.example/score")
    monkeypatch.setattr(settings, "jev_api_key", "synthetic-jev-key")
    created = await client.post("/api/v1/sources", json={"name": "SAG2 库", "default_search_strategy": "sag2"})
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]
    seen = []

    class StubEngine:
        _settings = settings

        async def search_many(self, targets, query, **kwargs):
            seen.append(kwargs.get("strategy"))
            return SearchOutcome(query=query, sections=[], stats={"effective_strategy": kwargs.get("strategy")})

        async def grep_chunks(self, *args, **kwargs):
            return []

    app.dependency_overrides[get_engine_manager] = lambda: StubEngine()
    try:
        scoped = await client.post("/api/v1/search", json={"query": "q", "source_ids": [source_id]})
        assert scoped.status_code == 200, scoped.text
        assert seen[-1] == "sag2"
        assert scoped.json()["stats"]["strategy_origin"] == "source_default"

        explicit = await client.post(
            "/api/v1/search", json={"query": "q", "source_ids": [source_id], "strategy": "vector"},
        )
        assert explicit.status_code == 200, explicit.text
        assert seen[-1] == "vector"
        assert explicit.json()["stats"]["strategy_origin"] == "request"
    finally:
        app.dependency_overrides.pop(get_engine_manager, None)


def test_require_strategy_available_matches_capability_report():
    assert EngineManager.require_strategy_available(configured(), "sag2") is None
    with pytest.raises(Exception) as error:
        EngineManager.require_strategy_available(configured(sag_relational_provider="mysql"), "sag2")
    assert getattr(error.value, "code", None) == "sag2_relational_unsupported"
