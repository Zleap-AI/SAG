"""Fresh native application lifecycle with isolated storage and mocked HTTP providers."""

# ruff: noqa: E402 -- settings must read the isolated environment before application imports.
# Set environment before importing the application's settings singleton.
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import httpx
from cryptography.fernet import Fernet

mode = sys.argv[1]
assert mode in {"disabled", "query"}
root = tempfile.TemporaryDirectory(prefix="sag-chatbot-smoke-")
for name in list(os.environ):
    if name.startswith("SAG_CHATBOT_") or name == "SAG_LOCK_CHATBOT_CONFIG":
        del os.environ[name]
os.environ.update(
    {
        "SAG_DATABASE_URL": f"sqlite+aiosqlite:///{root.name}/app.db",
        "SAG_DATA_DIR": f"{root.name}/engine",
        "SAG_UPLOAD_DIR": f"{root.name}/uploads",
        "SAG_DSH_CONNECTION_FILE": f"{root.name}/connection.json",
        "SAG_ENGINE_WARMUP_COUNT": "0",
        "SAG_AUTH_MODE": "password",
        "SAG_ALLOW_REGISTRATION": "true",
        "SAG_LLM_PROVIDER": "openai",
        "SAG_LLM_MODEL": "stock-model",
        "SAG_LLM_API_KEY": "stock-key",
        "SAG_LLM_BASE_URL": "https://stock.invalid/v1",
        "SAG_LOCK_LLM_CONFIG": "true",
        "SAG_EMBEDDING_API_KEY": "stock-embedding-key",
        "SAG_EMBEDDING_MODEL": "shared-model",
        "SAG_EMBEDDING_BASE_URL": "https://stock-embedding.invalid/v1",
        "SAG_EMBEDDING_DIMENSIONS": "3",
        "SAG_MINERU_API_KEY": "",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "SAG_CHATBOT_CONFIG_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }
)
if mode == "query":
    os.environ.update(
        {
            "SAG_CHATBOT_LLM_ENABLED": "true",
            "SAG_CHATBOT_LLM_MODEL": "query-model",
            "SAG_CHATBOT_LLM_API_KEY": "query-key",
            "SAG_CHATBOT_LLM_BASE_URL": "https://query.invalid/v1",
            "SAG_CHATBOT_LLM_CONTEXT_WINDOW": "111000",
            "SAG_CHATBOT_EMBEDDING_ENABLED": "true",
            "SAG_CHATBOT_EMBEDDING_BASE_URL": "https://query-embedding.invalid/v1",
            "SAG_CHATBOT_EMBEDDING_API_KEY": "query-embedding-key",
        }
    )

# .env must not supply developer credentials or storage paths to this subprocess.
os.chdir(root.name)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from unittest.mock import patch

import litellm
from openai import AsyncOpenAI

from sag_api.api.v1.system import _capabilities
from sag_api.core.config import settings
from sag_api.main import create_app
from sag_api.services import chatbot_service as rt

requests = []
clients = []


def handle(request):
    requests.append(request)
    if request.url.path.endswith("/embeddings"):
        assert request.headers["authorization"] == "Bearer query-embedding-key"
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0, 3.0]}]})
    assert request.headers["authorization"] == "Bearer query-key"
    return httpx.Response(
        200,
        json={
            "id": "smoke",
            "object": "chat.completion",
            "created": 0,
            "model": "query-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "smoke-ok"}, "finish_reason": "stop"}],
        },
    )


def sdk(**kwargs):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    clients.append(http)
    return AsyncOpenAI(**kwargs, http_client=http)


async def completion(**kwargs):
    async with sdk(api_key=kwargs["api_key"], base_url=kwargs["api_base"]) as client:
        return await litellm.acompletion(**kwargs, client=client)


async def run():
    app = create_app()
    async with app.router.lifespan_context(app):
        assert rt.manager.loaded and app.state.llm.configured
        from zleap.sag.config import EmbeddingConfig, LLMConfig
        from zleap.sag.core.adapters import registry

        durable = registry.create_llm(
            config=LLMConfig(
                provider="litellm",
                model=settings.routed_llm_model,
                api_key=settings.llm_api_key,
            )
        )
        embedding = registry.create_embedding(
            config=EmbeddingConfig(
                model=settings.embedding_model,
                api_key=settings.embedding_api_key,
                schema_dimensions=3,
            )
        )
        assert isinstance(durable, rt.ScopedAdapter)
        assert durable.original._model_config["api_key"] == "stock-key"
        assert embedding.original._config.api_key == "stock-embedding-key"
        if mode == "disabled":
            assert _capabilities()["llm_model"] == "stock-model"
            assert rt.manager.snapshot().settings.routed_llm_model == settings.routed_llm_model
        else:
            with patch("openai.AsyncOpenAI", sdk), patch("sag_api.generation.llm._litellm_completion", completion):
                async with rt.manager.scope(query=True):
                    assert await app.state.llm.complete([{"role": "user", "content": "ping"}]) == "smoke-ok"
                    assert await embedding.generate("query") == [1.0, 2.0, 3.0]
                    assert _capabilities()["context_window"] == 111000
                assert {request.url.host for request in requests} == {"query.invalid", "query-embedding.invalid"}
                assert all(client.is_closed for client in clients)
            assert settings.llm_api_key == "stock-key"
        await durable.close()
        await embedding.close()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://smoke") as client:
            assert (await client.get("/api/v1/system/chatbot-config")).status_code == 401
            registered = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": "chatbot-smoke@example.com",
                    "password": "smoke-password-123",
                },
            )
            assert registered.status_code == 201, registered.text
            headers = {"authorization": "Bearer " + registered.json()["access_token"]}
            saved = await client.put(
                "/api/v1/system/chatbot-config",
                headers=headers,
                json={
                    "llm": {"enabled": False, "api_key": "encrypted-smoke-credential"},
                },
            )
            assert saved.status_code == 200 and "encrypted-smoke-credential" not in saved.text
            public = await client.get("/api/v1/system/chatbot-config", headers=headers)
            assert public.json()["config"]["llm"]["credential_source"] == "ui"
        from sag_api.core.db import SessionLocal

        restarted = rt.Manager(rt.manager.environment, settings)
        await restarted.load(SessionLocal)
        assert restarted.connections()["llm"].api_key == "encrypted-smoke-credential"
    assert app.state.llm is None


try:
    asyncio.run(run())
finally:
    root.cleanup()
print(f"Native chatbot {mode} lifecycle smoke passed")
