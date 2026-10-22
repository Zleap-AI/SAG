import pytest

from sag_api.core.config import Settings
from sag_api.core.litellm_policy import install_litellm_policy, uninstall_litellm_policy
from sag_api.schemas.system import ModelConfigUpdate


@pytest.mark.parametrize("mode", ["auto", "json_schema", "json_object", "prompt_only"])
def test_mode_survives_schema_and_effective_settings(mode):
    from sag_api.services import settings_service

    assert ModelConfigUpdate(llm_structured_output_mode=mode).model_dump(exclude_unset=True) == {
        "llm_structured_output_mode": mode
    }
    assert "llm_structured_output_mode" in settings_service._FIELDS


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 429, 500])
async def test_bounded_guided_grammar_fallback(monkeypatch, status):
    import litellm

    calls = []

    class ProviderError(Exception):
        status_code = status

    async def completion(**kwargs):
        calls.append(kwargs["response_format"]["type"])
        if calls[-1] == "json_schema":
            raise ProviderError("guided_grammar is not supported")
        return {"ok": True}

    monkeypatch.setattr(litellm, "acompletion", completion)
    handle = install_litellm_policy(Settings(_env_file=None))
    try:
        if status == 400:
            assert await litellm.acompletion(response_format={"type": "json_schema"}) == {"ok": True}
            assert calls == ["json_schema", "json_object"]
        else:
            with pytest.raises(ProviderError):
                await litellm.acompletion(response_format={"type": "json_schema"})
            assert calls == ["json_schema"]
    finally:
        uninstall_litellm_policy(handle)


@pytest.mark.parametrize("status,invalid", [(400, False), (400, True), (401, False), (429, False), (500, False)])
async def test_model_probe_http_guided_grammar_fallback_and_local_validation(monkeypatch, status, invalid):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    calls = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            mode = body.get("response_format", {}).get("type")
            if mode == "json_schema":
                self.send_response(status)
                result = {"error": {"message": "guided_grammar is not supported", "type": "invalid_request_error"}}
            else:
                self.send_response(200)
                content = "pong" if mode is None else ('{"ok": "wrong-type"}' if invalid else '{"ok": true}')
                result = {
                    "id": "probe",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "probe-model",
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                    ],
                }
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import httpx
        from fastapi import FastAPI

        from sag_api.api.v1.system import router
        from sag_api.core.config import settings
        from sag_api.core.deps import get_current_user

        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        app.dependency_overrides[get_current_user] = lambda: object()
        payload = {
            "llm_provider": "openai",
            "llm_api_key": "test-only-key",
            "llm_base_url": f"http://127.0.0.1:{server.server_port}/v1",
            "llm_model": "probe-model",
            "llm_structured_output_mode": "auto",
            "llm_max_retries": 0,
        }
        original = settings.model_dump()
        handle = install_litellm_policy(settings)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                result = await client.post("/api/v1/system/model-config/test", json=payload)
                assert result.status_code == 200
                assert result.json()["ok"] is (status == 400 and not invalid)
                assert result.json()["completion"]["ok"] is True
                assert result.json()["structured"]["ok"] is (status == 400 and not invalid)
                assert settings.model_dump() == original
        finally:
            uninstall_litellm_policy(handle)
        modes = [call.get("response_format", {}).get("type") for call in calls]
        assert modes == ([None, "json_schema", "json_object"] if status == 400 else [None, "json_schema"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['auto', 'json_schema', 'json_object', 'prompt_only'])
async def test_tenant_structured_mode_survives_database_reload(tmp_path, monkeypatch, mode):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sag_api.db.base import Base
    from sag_api.services import settings_service
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "settings.sqlite"}')
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    configuration = Settings(_env_file=None)
    monkeypatch.setattr(settings_service, '_settings', configuration)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            saved = await settings_service.save_model_config(session, {'llm_structured_output_mode': mode})
        assert saved['llm_structured_output_mode'] == mode
        configuration.llm_structured_output_mode = 'auto'
        await settings_service.apply_startup_overrides(sessions)
        assert configuration.llm_structured_output_mode == mode
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_capability_cache_preserves_endpoint_model_case_and_credential(monkeypatch):
    import litellm

    calls = []

    class Unsupported(Exception):
        status_code = 400

    async def completion(**kwargs):
        mode = kwargs["response_format"]["type"]
        calls.append(mode)
        if mode == "json_schema":
            raise Unsupported("guided_grammar is not supported")
        return {"ok": True}

    monkeypatch.setattr(litellm, "acompletion", completion)
    handle = install_litellm_policy(Settings(_env_file=None))
    try:
        for endpoint, model, key in [
            ("https://example.test/Team", "openai/Model", "one"),
            ("https://example.test/team", "openai/Model", "one"),
            ("https://example.test/team", "openai/model", "one"),
            ("https://example.test/team", "openai/model", "two"),
        ]:
            await litellm.acompletion(
                api_base=endpoint, model=model, api_key=key, response_format={"type": "json_schema"}
            )
        assert calls == ["json_schema", "json_object"] * 4
    finally:
        uninstall_litellm_policy(handle)
