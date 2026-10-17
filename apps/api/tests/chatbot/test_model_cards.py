"""Independent model card updates at the ASGI/database boundary, without provider traffic."""

import asyncio
import copy

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import select

from sag_api.api.v1 import chatbot, system
from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user
from sag_api.core.errors import ApiError
from sag_api.db.models import Document, Setting
from sag_api.services import settings_service


@pytest.fixture
async def card_client(isolate, database, monkeypatch):
    monkeypatch.setattr(settings_service, "_settings", isolate.stock)
    monkeypatch.setattr(settings_service, "_MODEL_CONFIG_SAVE_LOCK", asyncio.Lock())
    monkeypatch.setattr(settings_service, "_MODEL_CONFIG_SOURCES", dict(settings_service._MODEL_CONFIG_SOURCES))
    monkeypatch.setattr(system, "settings", isolate.stock)
    async with database() as session:
        connection = await session.connection()
        await connection.run_sync(lambda sync: Document.__table__.create(sync))
        await session.commit()

    class Runtime:
        def __init__(self):
            self.applied = []

        async def apply_settings(self, updated, *, reset_engines):
            self.applied.append((updated.model_dump(), reset_engines))

    app = FastAPI()
    app.include_router(system.router, prefix="/api/v1")
    app.include_router(chatbot.router, prefix="/api/v1/system")
    app.state.knowledge_runtime = Runtime()
    app.dependency_overrides[get_current_user] = lambda: object()

    @app.exception_handler(ApiError)
    async def errors(request, error):
        return JSONResponse(error.to_envelope(), status_code=error.status_code)

    async def session():
        async with database() as value:
            yield value

    app.dependency_overrides[get_session] = session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        yield client, app.state.knowledge_runtime


@pytest.mark.parametrize(
    "patch,stored",
    [
        ({"llm_model": "generation-card"}, {"llm_model": "generation-card"}),
        (
            {"embedding_base_url": "https://embedding-card.invalid/v1"},
            {"embedding_base_url": "https://embedding-card.invalid/v1"},
        ),
        (
            {"jev_endpoint": "https://jev-card.invalid/score"},
            {"jev_endpoint": "https://jev-card.invalid/score", "jev_api_key": None},
        ),
    ],
)
async def test_original_card_partial_save_preserves_other_cards(card_client, isolate, database, patch, stored):
    client, runtime = card_client
    before = isolate.stock.model_dump()
    result = await client.put("/api/v1/system/model-config", json=patch)
    assert result.status_code == 200, result.text
    for field, previous in before.items():
        assert getattr(isolate.stock, field) == patch.get(field, previous)
    assert len(runtime.applied) == 1
    async with database() as session:
        row = await session.scalar(select(Setting).where(Setting.key == "model_config"))
        assert row.value == stored


@pytest.mark.parametrize(
    "target,valid,invalid",
    [
        ("llm", {"enabled": True, "model": "saved-card"}, {"model": ""}),
        ("embedding", {"enabled": True, "base_url": "https://card.invalid/v1"}, {"base_url": ""}),
    ],
)
async def test_optional_card_save_is_independent_and_invalid_save_is_atomic(
    card_client, isolate, database, target, valid, invalid,
):
    client, runtime = card_client
    result = await client.put("/api/v1/system/chatbot-config", json={target: valid})
    assert result.status_code == 200, result.text
    other = "embedding" if target == "llm" else "llm"
    assert result.json()["config"][other]["enabled"] is False
    saved = copy.deepcopy(isolate.persisted)
    before = isolate.stock.model_dump()
    result = await client.put("/api/v1/system/chatbot-config", json={target: invalid})
    assert result.status_code == 400
    assert isolate.persisted == saved and isolate.stock.model_dump() == before
    assert runtime.applied == []
    async with database() as session:
        row = await session.scalar(select(Setting).where(Setting.key == "chatbot_config"))
        assert row.value == saved


@pytest.mark.parametrize(
    "patch,field",
    [
        ({"embedding_dimensions": True}, "embedding_dimensions"),
        ({"embedding_dimensions": "16"}, "embedding_dimensions"),
        ({"embedding_dimensions": {}}, "embedding_dimensions"),
        ({"embedding_dimensions": 0}, "embedding_dimensions"),
        ({"llm_model": None}, "llm_model"),
        ({"llm_model": "   "}, "llm_model"),
        ({"embedding_model": None}, "embedding_model"),
        ({"embedding_model": "   "}, "embedding_model"),
    ],
)
async def test_invalid_card_fields_do_not_publish_runtime_or_persistence(card_client, isolate, database, patch, field):
    client, runtime = card_client
    result = await client.put("/api/v1/system/model-config", json={"llm_model": "saved-generation"})
    assert result.status_code == 200
    runtime.applied.clear()
    before = isolate.stock.model_dump()
    async with database() as session:
        row = await session.scalar(select(Setting).where(Setting.key == "model_config"))
        saved = copy.deepcopy(row.value)
    result = await client.put(
        "/api/v1/system/model-config", json={"llm_api_key": "validation-secret", **patch}
    )
    assert result.status_code == 422, result.text
    assert field in result.json()["error"]["message"]
    assert "validation-secret" not in result.text
    assert isolate.stock.model_dump() == before and runtime.applied == []
    async with database() as session:
        row = await session.scalar(select(Setting).where(Setting.key == "model_config"))
        assert row.value == saved


@pytest.mark.parametrize("field", ["embedding_schema_dimensions", "embedding_request_dimensions"])
async def test_canonical_embedding_fields_cannot_be_submitted_by_api_clients(
    card_client, isolate, database, field,
):
    client, runtime = card_client
    before = isolate.stock.model_dump()

    response = await client.put("/api/v1/system/model-config", json={field: 1024})

    assert response.status_code == 200, response.text
    assert isolate.stock.model_dump() == before
    assert runtime.applied[0][0]["embedding_schema_dimensions"] == before["embedding_schema_dimensions"]
    assert runtime.applied[0][0]["embedding_request_dimensions"] == before["embedding_request_dimensions"]
    async with database() as session:
        row = await session.scalar(select(Setting).where(Setting.key == "model_config"))
        assert row.value == {}


@pytest.mark.parametrize("dimensions", [None, 16])
async def test_embedding_dimension_save_accepts_null_or_integer(card_client, isolate, dimensions):
    client, _ = card_client
    result = await client.put("/api/v1/system/model-config", json={"embedding_dimensions": dimensions})
    assert result.status_code == 200, result.text
    assert isolate.stock.embedding_dimensions == dimensions


@pytest.mark.parametrize("dimensions,expected", [(1024, 1024), (None, 1024)])
async def test_embedding_dimension_save_publishes_and_reloads_over_split_environment(
    card_client, isolate, database, dimensions, expected,
):
    client, runtime = card_client
    isolate.stock.embedding_dimensions = 16
    isolate.stock.embedding_schema_dimensions = 16
    isolate.stock.embedding_request_dimensions = 16
    deployment = isolate.stock.model_copy(deep=True)

    response = await client.put("/api/v1/system/model-config", json={"embedding_dimensions": dimensions})

    assert response.status_code == 200, response.text
    assert isolate.stock.effective_embedding_schema_dimensions == expected
    assert isolate.stock.effective_embedding_request_dimensions == expected
    assert runtime.applied[0][0]["embedding_schema_dimensions"] == dimensions
    assert runtime.applied[0][0]["embedding_request_dimensions"] == dimensions
    async with database() as session:
        stored = await settings_service.load_overrides(session)
    assert stored == {
        "embedding_dimensions": dimensions,
        "embedding_schema_dimensions": dimensions,
        "embedding_request_dimensions": dimensions,
    }
    settings_service.apply_overrides(deployment, stored)
    assert deployment.effective_embedding_schema_dimensions == expected
    assert deployment.effective_embedding_request_dimensions == expected


@pytest.mark.parametrize("dimensions", [None, 1024])
async def test_indexed_embedding_guard_uses_proposed_effective_dimensions(
    card_client, isolate, database, dimensions,
):
    client, runtime = card_client
    isolate.stock.embedding_dimensions = 1024
    isolate.stock.embedding_schema_dimensions = 16
    isolate.stock.embedding_request_dimensions = 16
    before = isolate.stock.model_dump()
    async with database() as session:
        session.add(Document(
            source_id="indexed-source", filename="indexed.md", storage_path="/tmp/indexed.md",
            sag_source_id="indexed-document",
        ))
        await session.commit()

    response = await client.put("/api/v1/system/model-config", json={"embedding_dimensions": dimensions})

    assert response.status_code == 409, response.text
    assert isolate.stock.model_dump() == before and runtime.applied == []
    async with database() as session:
        assert await session.scalar(select(Setting)) is None


async def test_loading_legacy_dimensions_keeps_existing_indexed_space(card_client, isolate, database):
    client, _ = card_client
    isolate.stock.embedding_dimensions = 16
    isolate.stock.embedding_schema_dimensions = 16
    isolate.stock.embedding_request_dimensions = 16
    async with database() as session:
        session.add(Setting(scope="global", key="model_config", value={"embedding_dimensions": 1024}))
        session.add(Document(
            source_id="indexed-source", filename="indexed.md", storage_path="/tmp/indexed.md",
            sag_source_id="indexed-document",
        ))
        await session.commit()

    await settings_service.apply_startup_overrides(database)

    assert isolate.stock.embedding_dimensions == 1024
    assert isolate.stock.effective_embedding_schema_dimensions == 16
    assert isolate.stock.effective_embedding_request_dimensions == 16
    response = await client.get("/api/v1/system/model-config")
    assert response.json()["embedding_dimensions"] == 16
    async with database() as session:
        row = await session.scalar(select(Setting).where(Setting.key == "model_config"))
        assert row.value == {"embedding_dimensions": 1024}


async def test_unrelated_card_save_keeps_legacy_indexed_embedding_space(card_client, isolate, database):
    client, runtime = card_client
    isolate.stock.embedding_dimensions = 1024
    isolate.stock.embedding_schema_dimensions = 16
    isolate.stock.embedding_request_dimensions = 16
    async with database() as session:
        session.add(Setting(scope="global", key="model_config", value={"embedding_dimensions": 1024}))
        session.add(Document(
            source_id="indexed-source", filename="indexed.md", storage_path="/tmp/indexed.md",
            sag_source_id="indexed-document",
        ))
        await session.commit()

    response = await client.put("/api/v1/system/model-config", json={"llm_model": "new-generation"})

    assert response.status_code == 200, response.text
    assert response.json()["config"]["embedding_dimensions"] == 16
    assert isolate.stock.effective_embedding_schema_dimensions == 16
    assert isolate.stock.effective_embedding_request_dimensions == 16
    assert runtime.applied[0][0]["embedding_schema_dimensions"] == 16
    assert runtime.applied[0][0]["embedding_request_dimensions"] == 16
    async with database() as session:
        row = await session.scalar(select(Setting).where(Setting.key == "model_config"))
        assert row.value == {"embedding_dimensions": 1024, "llm_model": "new-generation"}


@pytest.mark.parametrize(
    "alias,schema_dimensions,expected", [(1024, 16, 16), (None, 16, 16), (None, 1024, 1024), (None, None, None)],
)
async def test_model_page_dimensions_show_effective_schema_when_explicit(
    card_client, isolate, alias, schema_dimensions, expected,
):
    client, _ = card_client
    isolate.stock.embedding_dimensions = alias
    isolate.stock.embedding_schema_dimensions = schema_dimensions
    isolate.stock.embedding_request_dimensions = None

    response = await client.get("/api/v1/system/model-config")

    assert response.status_code == 200 and response.json()["embedding_dimensions"] == expected


async def test_generation_probe_never_echoes_provider_errors(card_client, isolate, database, monkeypatch):
    client, runtime = card_client

    async def fail(**kwargs):
        raise RuntimeError("provider echoed provider-secret")

    monkeypatch.setattr("sag_api.generation.llm._litellm_completion", fail)
    before = isolate.stock.model_dump()
    result = await client.post(
        "/api/v1/system/model-config/test", json={"llm_api_key": "draft-secret", "llm_model": "draft-model"}
    )
    assert result.status_code == 200 and result.json()["ok"] is False
    assert "provider-secret" not in result.text and "draft-secret" not in result.text
    assert isolate.stock.model_dump() == before and runtime.applied == []
    async with database() as session:
        assert await session.scalar(select(Setting)) is None


async def test_concurrent_card_saves_merge_both_patches(card_client, isolate, database, monkeypatch):
    client, _ = card_client
    initial = await client.put(
        "/api/v1/system/model-config",
        json={"llm_model": "saved-generation", "embedding_base_url": "https://saved-embedding.invalid/v1"},
    )
    assert initial.status_code == 200
    first_read, release_first, second_read = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_load = settings_service._load_row
    reads = []

    async def controlled_load(session, key="model_config"):
        row = await original_load(session, key)
        if key == "model_config":
            reads.append(copy.deepcopy(row.value))
            if len(reads) == 1:
                first_read.set()
                await release_first.wait()
            else:
                second_read.set()
        return row

    monkeypatch.setattr(settings_service, "_load_row", controlled_load)
    generation = asyncio.create_task(client.put("/api/v1/system/model-config", json={"llm_model": "new-generation"}))
    await asyncio.wait_for(first_read.wait(), timeout=2)
    embedding = asyncio.create_task(client.put(
        "/api/v1/system/model-config", json={"embedding_base_url": "https://new-embedding.invalid/v1"}
    ))
    try:
        # Before serialization, both real DB sessions read the old row here. With
        # the save lock, the second request waits until the first one commits.
        await asyncio.wait_for(second_read.wait(), timeout=0.2)
    except TimeoutError:
        pass
    finally:
        release_first.set()
    results = await asyncio.gather(generation, embedding)
    assert all(response.status_code == 200 for response in results)
    assert isolate.stock.llm_model == "new-generation"
    assert isolate.stock.embedding_base_url == "https://new-embedding.invalid/v1"
    async with database() as session:
        row = await session.scalar(select(Setting).where(Setting.key == "model_config"))
        assert row.value == {
            "llm_model": "new-generation", "embedding_base_url": "https://new-embedding.invalid/v1",
        }
    assert reads[1]["llm_model"] == "new-generation"


async def test_failed_card_commit_keeps_state_and_releases_the_save_lock(card_client, isolate, database, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    client, runtime = card_client
    result = await client.put("/api/v1/system/model-config", json={"llm_model": "saved-generation"})
    assert result.status_code == 200
    runtime.applied.clear()
    before = isolate.stock.model_dump()

    async def failed_commit(self):
        raise RuntimeError("synthetic commit failure")

    with monkeypatch.context() as failure:
        failure.setattr(AsyncSession, "commit", failed_commit)
        with pytest.raises(RuntimeError, match="synthetic commit failure"):
            await client.put("/api/v1/system/model-config", json={"llm_model": "must-not-publish"})
    assert isolate.stock.model_dump() == before and runtime.applied == []
    async with database() as session:
        row = await session.scalar(select(Setting).where(Setting.key == "model_config"))
        assert row.value == {"llm_model": "saved-generation"}
    result = await asyncio.wait_for(
        client.put("/api/v1/system/model-config", json={"llm_model": "after-failure"}), timeout=2
    )
    assert result.status_code == 200 and isolate.stock.llm_model == "after-failure"
