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
from sag_api.services import chatbot_service as rt


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


async def test_all_routes_require_user_auth(client):
    _, client = client
    for method, path, body in (("GET", "", None), ("PUT", "", {}), ("POST", "/test", {"target": "llm"})):
        response = await client.request(
            method, "/api/v1/system/chatbot-config" + path, **({"json": body} if body else {})
        )
        assert response.status_code == 401


async def test_save_public_and_secret_safe_validation(client):
    app, client = client
    app.dependency_overrides[get_current_user] = lambda: object()
    body = {"llm": {"enabled": True, "model": "m", "api_key": "ui-secret"}}
    response = await client.put("/api/v1/system/chatbot-config", json=body)
    assert response.status_code == 200
    assert response.json()["config"]["llm"]["api_key_set"]
    assert "ui-secret" not in response.text
    response = await client.get("/api/v1/system/chatbot-config")
    assert "ui-secret" not in response.text
    for body in (
        {"embedding": {"model": "another-model", "api_key": "bad-secret"}},
        {"llm": {"api_key": {"bad": "bad-secret"}}},
    ):
        response = await client.put("/api/v1/system/chatbot-config", json=body)
        assert response.status_code == 422 and "bad-secret" not in response.text


async def test_unsaved_test_sends_draft_not_active_and_sanitizes_errors(client, isolate, monkeypatch):
    from litellm import ModelResponse

    app, client = client
    app.dependency_overrides[get_current_user] = lambda: object()
    isolate.environment = Environment({})
    requests = []

    async def complete(**kwargs):
        requests.append(kwargs)
        return ModelResponse(model=kwargs["model"], choices=[{"message": {"role": "assistant", "content": "ok"}}])

    monkeypatch.setattr("sag_api.generation.llm._litellm_completion", complete)
    body = {
        "target": "llm",
        "llm": {"enabled": True, "model": "draft", "api_key": "draft-secret", "base_url": "https://draft.invalid/v1"},
    }
    response = await client.post("/api/v1/system/chatbot-config/test", json=body)
    assert response.json() == {"ok": True, "message": "连接成功 · openai / draft"}
    assert requests[0]["api_key"] == "draft-secret"
    assert requests[0]["model"] == "openai/draft" and isolate.persisted == {}

    async def fail(**kwargs):
        raise RuntimeError("provider echoed draft-secret")

    monkeypatch.setattr("sag_api.generation.llm._litellm_completion", fail)
    response = await client.post("/api/v1/system/chatbot-config/test", json=body)
    assert not response.json()["ok"] and "draft-secret" not in response.text


async def test_lock_and_independent_embedding_test(client, isolate):
    app, client = client
    app.dependency_overrides[get_current_user] = lambda: object()
    isolate.environment = Environment({"SAG_LOCK_CHATBOT_CONFIG": "true"})
    response = await client.put("/api/v1/system/chatbot-config", json={"llm": {"model": "draft"}})
    assert response.status_code == 403
    response = await client.get("/api/v1/system/chatbot-config")
    assert response.json()["config"]["locked"]
    response = await client.post("/api/v1/system/chatbot-config/test", json={"target": "embedding"})
    assert response.status_code == 400


async def test_credential_source_environment_key_never_persisted(client, isolate):
    app, client = client
    app.dependency_overrides[get_current_user] = lambda: object()
    isolate.environment = Environment({"SAG_CHATBOT_LLM_API_KEY": "env-secret", "SAG_CHATBOT_LLM_MODEL": "env-model"})
    response = await client.put("/api/v1/system/chatbot-config", json={"llm": {"enabled": True, "api_key": ""}})
    assert response.status_code == 200
    assert response.json()["config"]["llm"]["credential_source"] == "environment"
    assert "env-secret" not in json.dumps(isolate.persisted)


@pytest.mark.parametrize("key_field", [{}, {"api_key": ""}])
async def test_keyless_optional_connections_save_test_restart_and_never_inherit_keys(
    client, isolate, database, monkeypatch, key_field
):
    import litellm
    from openai import AsyncOpenAI

    app, client = client
    app.dependency_overrides[get_current_user] = lambda: object()
    isolate.environment = Environment({})  # Keyless persistence does not need credential encryption.
    monkeypatch.setenv("OPENAI_API_KEY", "sdk-environment-secret")
    monkeypatch.setattr(litellm, "api_key", "sdk-global-secret")
    calls, clients = [], []
    reject = False

    def handle(request):
        calls.append(request)
        assert "authorization" not in request.headers
        if reject:
            return httpx.Response(401, json={"error": {"message": "provider echoed sdk-environment-secret"}})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0, 3.0]}]})
        return httpx.Response(
            200,
            json={
                "id": "keyless",
                "object": "chat.completion",
                "created": 0,
                "model": "local-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            },
        )

    def create(**kwargs):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        clients.append(http)
        return AsyncOpenAI(**kwargs, http_client=http)

    monkeypatch.setattr("openai.AsyncOpenAI", create)

    async def complete(**kwargs):
        # Keep the real LiteLLM/SDK path while injecting only its supported client/transport seam.
        async with create(api_key=kwargs["api_key"], base_url=kwargs["api_base"]) as sdk:
            return await litellm.acompletion(**kwargs, client=sdk)

    monkeypatch.setattr("sag_api.generation.llm._litellm_completion", complete)
    drafts = {
        "llm": {"enabled": True, "model": "local-model", "base_url": "https://keyless-llm.invalid/v1", **key_field},
        "embedding": {"enabled": True, "base_url": "https://keyless-embedding.invalid/v1", **key_field},
    }
    for target, draft in drafts.items():
        result = await client.post("/api/v1/system/chatbot-config/test", json={"target": target, target: draft})
        assert result.status_code == 200 and result.json()["ok"], result.text
        expected_message = (
            "连接成功 · openai / local-model"
            if target == "llm"
            else "Embedding connection successful · 3 dimensions"
        )
        assert result.json()["message"] == expected_message
        assert isolate.persisted == {}
    result = await client.put("/api/v1/system/chatbot-config", json=drafts)
    assert result.status_code == 200, result.text
    assert all(not result.json()["config"][target]["api_key_set"] for target in drafts)
    assert all("api_key_encrypted" not in value for value in isolate.persisted.values())
    restarted = rt.Manager(Environment({}), isolate.stock)
    await restarted.load(database)
    monkeypatch.setattr(rt, "manager", restarted)
    for target in drafts:
        result = await client.post("/api/v1/system/chatbot-config/test", json={"target": target})
        assert result.status_code == 200 and result.json()["ok"], result.text
    assert {request.url.host for request in calls} == {"keyless-llm.invalid", "keyless-embedding.invalid"}
    reject = True
    result = await client.post("/api/v1/system/chatbot-config/test", json={"target": "llm"})
    assert result.status_code == 200 and not result.json()["ok"]
    assert "sdk-environment-secret" not in result.text
    assert all(http.is_closed for http in clients)


async def test_original_model_probe_uses_original_connection(isolate, monkeypatch):
    from litellm import ModelResponse

    from sag_api.api.v1 import system

    isolate.environment = Environment(
        {
            "SAG_CHATBOT_LLM_ENABLED": "true",
            "SAG_CHATBOT_LLM_MODEL": "query-model",
            "SAG_CHATBOT_LLM_API_KEY": "query-key",
        }
    )
    requests = []

    async def complete(**kwargs):
        requests.append(kwargs)
        return ModelResponse(model=kwargs["model"], choices=[{"message": {"role": "assistant", "content": "ok"}}])

    monkeypatch.setattr(system, "settings", isolate.stock)
    monkeypatch.setattr("sag_api.generation.llm._litellm_completion", complete)
    app = FastAPI()
    app.include_router(system.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: object()
    app.add_middleware(rt.CaptureMiddleware)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        result = await client.post("/api/v1/system/model-config/test")
    assert result.status_code == 200 and result.json()["ok"]
    assert requests[0]["api_key"] == "stock-key" and requests[0]["model"] == "openai/stock-model"
