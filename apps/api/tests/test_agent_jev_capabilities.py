"""Agent strategy writes honor deployment capabilities without changing legacy personas."""

from uuid import uuid4

import httpx
import pytest

from sag_api.core.config import settings


@pytest.fixture
async def agent_config_client(monkeypatch):
    from sag_api.main import app

    async def skip_unrelated_mcp(_factory, _app):
        return None

    monkeypatch.setattr("sag_api.runtime._RuntimeFactory.start_mcp", skip_unrelated_mcp)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            registration = await client.post(
                "/api/v1/auth/register",
                json={"email": f"agent-jev-{uuid4().hex}@t.com", "password": "password123"},
            )
            assert registration.status_code == 201
            client.headers["Authorization"] = f"Bearer {registration.json()['access_token']}"
            yield client


@pytest.mark.parametrize(
    ("vector_provider", "endpoint", "expected_code"),
    [
        ("pgvector", "https://jev.example/score", "vector_provider_lacks_lexical"),
        ("pgvector", None, "vector_provider_lacks_lexical"),
        ("lancedb", None, "jev_not_configured"),
        ("es", None, "jev_not_configured"),
    ],
)
async def test_unavailable_jev_agent_creation_leaves_no_row(
    agent_config_client, monkeypatch, vector_provider, endpoint, expected_code,
):
    client = agent_config_client
    monkeypatch.setattr(settings, "sag_relational_provider", "postgres")
    monkeypatch.setattr(settings, "sag_vector_provider", vector_provider)
    monkeypatch.setattr(settings, "jev_endpoint", endpoint)
    before = (await client.get("/api/v1/agents")).json()

    response = await client.post(
        "/api/v1/agents",
        json={"name": "Unavailable JEV", "avatar": "J", "persona": {"search_strategy": "multi_jev"}},
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == expected_code
    assert (await client.get("/api/v1/agents")).json() == before


async def test_pgvector_rejects_jev_agent_update_without_changing_other_fields(agent_config_client, monkeypatch):
    client = agent_config_client
    created = await client.post(
        "/api/v1/agents",
        json={"name": "Existing assistant", "avatar": "E", "persona": {"search_strategy": "vector"}},
    )
    assert created.status_code == 201, created.text
    before = created.json()
    path = f"/api/v1/agents/{before['id']}"
    monkeypatch.setattr(settings, "sag_relational_provider", "postgres")
    monkeypatch.setattr(settings, "sag_vector_provider", "pgvector")
    monkeypatch.setattr(settings, "jev_endpoint", "https://jev.example/score")

    response = await client.patch(
        path, json={"name": "Must not persist", "avatar": "X", "persona": {"search_strategy": "multi_jev"}},
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "vector_provider_lacks_lexical"
    assert (await client.get(path)).json() == before


@pytest.mark.parametrize(
    ("relational_provider", "vector_provider"),
    [("sqlite", "lancedb"), ("postgres", "lancedb"), ("postgres", "es")],
)
async def test_lexical_backends_allow_creating_and_updating_jev_agents(
    agent_config_client, monkeypatch, relational_provider, vector_provider,
):
    client = agent_config_client
    monkeypatch.setattr(settings, "sag_relational_provider", relational_provider)
    monkeypatch.setattr(settings, "sag_vector_provider", vector_provider)
    monkeypatch.setattr(settings, "jev_endpoint", "https://jev.example/score")

    created = await client.post(
        "/api/v1/agents", json={"name": "JEV assistant", "persona": {"search_strategy": "multi_jev"}},
    )
    assert created.status_code == 201, created.text
    path = f"/api/v1/agents/{created.json()['id']}"
    updated = await client.patch(
        path, json={"name": "Updated JEV assistant", "persona": {"search_strategy": "multi_jev", "greeting": "Hello"}},
    )

    assert updated.status_code == 200, updated.text
    saved = (await client.get(path)).json()
    assert saved["name"] == "Updated JEV assistant"
    assert saved["persona"] == {"search_strategy": "multi_jev", "greeting": "Hello"}


@pytest.mark.parametrize(
    "persona",
    [{}, {"search_strategy": ""}, {"search_strategy": False}, {"search_strategy": "multi_es_fast"},
     {"search_strategy": "historical-custom-value"}],
)
async def test_pgvector_preserves_legacy_persona_writes(agent_config_client, monkeypatch, persona):
    client = agent_config_client
    monkeypatch.setattr(settings, "sag_relational_provider", "postgres")
    monkeypatch.setattr(settings, "sag_vector_provider", "pgvector")
    monkeypatch.setattr(settings, "jev_endpoint", None)

    created = await client.post("/api/v1/agents", json={"name": "Legacy assistant", "persona": persona})
    assert created.status_code == 201, created.text
    path = f"/api/v1/agents/{created.json()['id']}"
    response = await client.patch(path, json={"name": "Renamed assistant", "persona": persona})

    assert response.status_code == 200, response.text
    saved = (await client.get(path)).json()
    assert saved["name"] == "Renamed assistant"
    assert saved["persona"] == persona


async def test_pgvector_can_edit_other_fields_and_repair_a_previously_saved_jev_agent(
    agent_config_client, monkeypatch,
):
    client = agent_config_client
    monkeypatch.setattr(settings, "sag_vector_provider", "lancedb")
    monkeypatch.setattr(settings, "jev_endpoint", "https://jev.example/score")
    created = await client.post(
        "/api/v1/agents", json={"name": "Existing JEV assistant", "persona": {"search_strategy": "multi_jev"}},
    )
    assert created.status_code == 201, created.text
    path = f"/api/v1/agents/{created.json()['id']}"
    monkeypatch.setattr(settings, "sag_relational_provider", "postgres")
    monkeypatch.setattr(settings, "sag_vector_provider", "pgvector")

    renamed = await client.patch(path, json={"name": "Renamed assistant"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["persona"] == {"search_strategy": "multi_jev"}
    repaired = await client.patch(path, json={"persona": {"search_strategy": "vector"}})

    assert repaired.status_code == 200, repaired.text
    saved = (await client.get(path)).json()
    assert saved["name"] == "Renamed assistant"
    assert saved["persona"] == {"search_strategy": "vector"}
