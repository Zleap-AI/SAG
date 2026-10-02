"""Real application startup and shutdown with isolated DBs and fake providers."""
# Bootstrap environment must precede provider/app imports.
import asyncio
import os
import sys
import tempfile

import httpx
from cryptography.fernet import Fernet

mode = sys.argv[1]
assert mode in {"disabled", "query"}
root = tempfile.mkdtemp(prefix="sag-chatbot-smoke-")
for name in list(os.environ):
    if name.startswith("SAG_CHATBOT_") or name == "SAG_LOCK_CHATBOT_CONFIG":
        del os.environ[name]
os.environ.update({
    "SAG_DATABASE_URL": f"sqlite+aiosqlite:///{root}/app.db",
    "SAG_DATA_DIR": f"{root}/engine", "SAG_UPLOAD_DIR": f"{root}/uploads",
    "SAG_DSH_CONNECTION_FILE": f"{root}/connection.json", "SAG_ENGINE_WARMUP_COUNT": "0",
    "SAG_AUTH_MODE": "password", "SAG_ALLOW_REGISTRATION": "true", "SAG_LLM_PROVIDER": "openai", "SAG_LLM_MODEL": "stock-model",
    "SAG_LLM_API_KEY": "stock-key", "SAG_LLM_BASE_URL": "https://stock.invalid/v1",
    "SAG_LOCK_LLM_CONFIG": "true", "SAG_EMBEDDING_API_KEY": "", "SAG_MINERU_API_KEY": "",
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    "SAG_CHATBOT_CONFIG_ENCRYPTION_KEY": Fernet.generate_key().decode(),
})
if mode != "disabled":
    os.environ.update({
        "SAG_CHATBOT_LLM_ENABLED": "true", "SAG_CHATBOT_LLM_PROVIDER": "responses",
        "SAG_CHATBOT_LLM_MODEL": "query-model", "SAG_CHATBOT_LLM_API_KEY": "query-key",
        "SAG_CHATBOT_LLM_RESPONSES_ENDPOINT": "https://query.invalid/v1/responses",
        "SAG_CHATBOT_LLM_CONTEXT_WINDOW": "111000",
    })

from sag_chatbot.bootstrap import create_app

app = create_app()
assert create_app() is app
from sag_api.api.v1.system import _capabilities
from sag_api.core.config import settings
from sag_chatbot import runtime as rt
from sag_chatbot.responses.provider import ResponsesProvider


async def run():
    async with app.router.lifespan_context(app):
        assert app.state.chatbot_installed and rt.manager.loaded
        assert app.state.llm.configured
        from zleap.sag.config import LLMConfig
        from zleap.sag.core.adapters import registry
        durable = registry.create_llm(config=LLMConfig(
            provider="litellm", model=settings.routed_llm_model, api_key=settings.llm_api_key,
        ))
        assert isinstance(durable, rt.ScopedAdapter)
        assert durable.original._model_config["api_key"] == "stock-key"
        if mode == "disabled":
            assert _capabilities()["llm_model"] == "stock-model"
            assert rt.manager.snapshot().settings.routed_llm_model == settings.routed_llm_model
        else:
            requests = []
            def transport(request):
                requests.append(request)
                assert request.headers["authorization"] == "Bearer query-key"
                return httpx.Response(200, json={"id": "r", "status": "completed", "output": [
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "smoke-ok"}]},
                ]})
            async with rt.manager.scope(query=True) as snapshot:
                snapshot.responses_handler = ResponsesProvider(snapshot.responses_config, async_transport=httpx.MockTransport(transport))
                assert await app.state.llm.complete([{"role": "user", "content": "ping"}]) == "smoke-ok"
                assert _capabilities()["context_window"] == 111000
                assert requests[0].url.host == "query.invalid"
            assert settings.llm_api_key == "stock-key"
        await durable.close()
        # Exercise real JWT authorization and encrypted API persistence against the application DB.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://smoke") as client:
            assert (await client.get("/api/v1/system/chatbot-config")).status_code == 401
            registered = await client.post("/api/v1/auth/register", json={
                "email": "chatbot-smoke@example.com", "password": "smoke-password-123",
            })
            assert registered.status_code == 201
            headers = {"authorization": "Bearer " + registered.json()["access_token"]}
            saved = await client.put("/api/v1/system/chatbot-config", headers=headers, json={
                "llm": {"enabled": False, "api_key": "encrypted-smoke-credential"},
            })
            assert saved.status_code == 200 and "encrypted-smoke-credential" not in saved.text
            public = await client.get("/api/v1/system/chatbot-config", headers=headers)
            assert public.json()["config"]["llm"]["credential_source"] == "ui"
        from sag_api.core.db import SessionLocal
        restarted = rt.Manager(rt.manager.environment, settings)
        await restarted.load(SessionLocal)
        assert restarted.connections()["llm"].api_key == "encrypted-smoke-credential"
    assert app.state.llm is None

asyncio.run(run())
