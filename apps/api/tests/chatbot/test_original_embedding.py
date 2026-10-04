import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from sag_api.api.v1.chatbot import router
from sag_api.core.chatbot_config import Environment
from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user
from sag_api.core.errors import ApiError


@pytest.fixture
async def client(database):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/system")

    @app.exception_handler(ApiError)
    async def errors(request, error):
        return JSONResponse(error.to_envelope(), status_code=error.status_code)

    async def session():
        async with database() as value:
            yield value

    app.dependency_overrides[get_session] = session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as value:
        yield app, value


async def test_original_embedding_probe_requires_user_auth(client):
    _, http = client
    assert (await http.post("/api/v1/system/model-config/embedding/test", json={})).status_code == 401


@pytest.fixture
def embedding_transport(monkeypatch):
    from openai import AsyncOpenAI

    calls, clients = [], []
    response = {"data": [{"index": 0, "embedding": [1.0, 2.0, 3.0]}]}

    def handle(request):
        calls.append((str(request.url), request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(200, json=response)

    def create(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        clients.append(client)
        return AsyncOpenAI(**kwargs, http_client=client)

    monkeypatch.setattr("openai.AsyncOpenAI", create)
    return calls, clients, response


async def test_original_embedding_draft_bypasses_query_routing_and_never_saves(
    client, isolate, embedding_transport, database
):
    from sqlalchemy import select

    from sag_api.db.models import Setting

    app, client = client
    app.dependency_overrides[get_current_user] = lambda: object()
    isolate.environment = Environment(
        {
            "SAG_CHATBOT_EMBEDDING_ENABLED": "true",
            "SAG_CHATBOT_EMBEDDING_API_KEY": "query-key",
            "SAG_CHATBOT_EMBEDDING_BASE_URL": "https://query.invalid/v1",
        }
    )
    isolate.stock.embedding_schema_dimensions = None
    isolate.stock.embedding_request_dimensions = None
    before = isolate.stock.model_dump()
    calls, clients, payload = embedding_transport
    payload["data"][0]["embedding"] = [1.0, 2.0, 3.0, 4.0]
    async with isolate.scope(query=True):
        response = await client.post(
            "/api/v1/system/model-config/embedding/test",
            json={
                "embedding_model": "draft-model",
                "embedding_base_url": "https://draft.invalid/v1",
                "embedding_api_key": "draft-key",
                "embedding_dimensions": 4,
            },
        )
    assert response.status_code == 200 and response.json()["ok"]
    assert response.json()["dimensions"] == 4
    assert len(calls) == 1 and calls[0][:2] == ("https://draft.invalid/v1/embeddings", "Bearer draft-key")
    assert calls[0][2]["model"] == "draft-model" and calls[0][2]["dimensions"] == 4
    assert clients[0].is_closed
    assert isolate.stock.model_dump() == before and isolate.persisted == {}
    async with database() as session:
        assert (await session.scalar(select(Setting))) is None


@pytest.mark.parametrize(
    "draft,expected_url,expected_key",
    [
        ({"embedding_api_key": ""}, "https://stock-embedding.invalid/v1/embeddings", "stock-embedding-key"),
        ({"embedding_base_url": "", "embedding_api_key": ""}, "https://stock.invalid/v1/embeddings", "stock-key"),
        (
            {"embedding_base_url": "", "llm_base_url": "https://draft-llm.invalid/v1", "llm_api_key": "draft-llm-key"},
            "https://draft-llm.invalid/v1/embeddings",
            "draft-llm-key",
        ),
    ],
)
async def test_original_embedding_retains_keys_and_stock_provider_reuse(
    client, isolate, embedding_transport, draft, expected_url, expected_key
):
    app, client = client
    app.dependency_overrides[get_current_user] = lambda: object()
    if draft.get("embedding_base_url") == "":
        isolate.stock.embedding_api_key = None
    response = await client.post("/api/v1/system/model-config/embedding/test", json=draft)
    assert response.json()["ok"]
    calls, clients, _ = embedding_transport
    assert calls[0][:2] == (expected_url, "Bearer " + expected_key)
    assert clients[0].is_closed


@pytest.mark.parametrize("provider", ["anthropic", "gemini", "responses"])
async def test_original_embedding_non_openai_provider_needs_separate_key(
    client, isolate, embedding_transport, provider
):
    app, client = client
    app.dependency_overrides[get_current_user] = lambda: object()
    isolate.stock.embedding_api_key = None
    response = await client.post("/api/v1/system/model-config/embedding/test", json={"llm_provider": provider})
    assert not response.json()["ok"]
    assert not embedding_transport[0] and not embedding_transport[1]


async def test_original_embedding_does_not_reuse_saved_responses_key_after_protocol_change(
    client, isolate, embedding_transport
):
    app, client = client
    app.dependency_overrides[get_current_user] = lambda: object()
    isolate.stock.llm_provider = "responses"
    isolate.stock.embedding_api_key = None
    body = {"llm_provider": "openai", "embedding_base_url": "", "llm_base_url": "https://draft.invalid/v1"}
    result = await client.post("/api/v1/system/model-config/embedding/test", json=body)
    assert not result.json()["ok"] and not embedding_transport[0]
    result = await client.post(
        "/api/v1/system/model-config/embedding/test", json={**body, "llm_api_key": "explicit-chat-key"}
    )
    assert result.json()["ok"]
    assert embedding_transport[0][0][:2] == ("https://draft.invalid/v1/embeddings", "Bearer explicit-chat-key")
    assert isolate.stock.llm_api_key == "stock-key" and isolate.stock.llm_provider == "responses"


async def test_original_embedding_omits_unsupported_request_dimensions(client, isolate, embedding_transport):
    app, client = client
    app.dependency_overrides[get_current_user] = lambda: object()
    isolate.stock.embedding_request_dimensions = None
    isolate.stock.embedding_dimensions = None
    response = await client.post(
        "/api/v1/system/model-config/embedding/test",
        json={
            "embedding_model": "BAAI/bge-m3",
            "embedding_base_url": "https://api.siliconflow.cn/v1",
            "embedding_dimensions": None,
        },
    )
    assert response.json()["ok"]
    assert "dimensions" not in embedding_transport[0][0][2]


@pytest.mark.parametrize(
    "data",
    [
        [],
        [{"index": 0, "embedding": []}],
        [{"index": 0, "embedding": [1.0, 2.0]}],
        [{"index": 0, "embedding": ["nan", 2.0, 3.0]}],
    ],
)
async def test_original_embedding_rejects_bad_vectors_and_closes_client(client, embedding_transport, data):
    app, client = client
    app.dependency_overrides[get_current_user] = lambda: object()
    calls, clients, payload = embedding_transport
    payload["data"] = data
    response = await client.post("/api/v1/system/model-config/embedding/test", json={})
    assert response.status_code == 200 and not response.json()["ok"]
    assert len(calls) == 1 and clients[0].is_closed


@pytest.mark.parametrize(
    "draft",
    [
        {"embedding_api_key": {"key": "validation-secret"}},
        {"embedding_dimensions": 0, "embedding_api_key": "validation-secret"},
        {"embedding_model": None, "embedding_api_key": "validation-secret"},
        {"embedding_base_url": "https://validation-secret@host.invalid/v1"},
        {"llm_provider": None, "embedding_api_key": "validation-secret"},
        {"unknown": "validation-secret"},
    ],
)
async def test_original_embedding_validation_hides_secrets(client, embedding_transport, draft):
    app, client = client
    app.dependency_overrides[get_current_user] = lambda: object()
    response = await client.post("/api/v1/system/model-config/embedding/test", json=draft)
    assert response.status_code == 422 and "validation-secret" not in response.text
    assert not embedding_transport[0]


@pytest.mark.parametrize("cancel", [False, True])
async def test_original_embedding_provider_failure_and_cancellation_close_client(client, monkeypatch, cancel):
    from zleap.sag.core.adapters.defaults import OpenAIEmbeddingAdapter

    app, client = client
    app.dependency_overrides[get_current_user] = lambda: object()
    closed = []

    async def fail(self, text):
        if cancel:
            raise asyncio.CancelledError()
        raise RuntimeError("provider echoed provider-secret")

    async def close(self):
        closed.append(True)

    monkeypatch.setattr(OpenAIEmbeddingAdapter, "generate", fail)
    monkeypatch.setattr(OpenAIEmbeddingAdapter, "close", close)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await client.post("/api/v1/system/model-config/embedding/test", json={})
    else:
        response = await client.post("/api/v1/system/model-config/embedding/test", json={})
        assert not response.json()["ok"] and "provider-secret" not in response.text
    assert closed == [True]
