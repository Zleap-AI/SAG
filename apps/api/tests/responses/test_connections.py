import asyncio
import json

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.chatbot_config import Environment
from sag_api.core.config import Settings
from sag_api.core.errors import ConfigurationError
from sag_api.db.models import Setting
from sag_api.generation.chatbot import QueryLLM
from sag_api.generation.llm import LLMClient
from sag_api.generation.responses.routing import register
from sag_api.services import chatbot_service as rt
from sag_api.services import settings_service


def original():
    return Settings(_env_file=None, llm_provider="openai", llm_model="original-model", llm_api_key="original-key")


@pytest.mark.parametrize("provider", ["openai", "azure", "bedrock_runtime", "bedrock_mantle"])
@pytest.mark.parametrize("key", ["query-key", ""])
async def test_independent_responses_and_original_calls_do_not_share_credentials(monkeypatch, provider, key):
    import litellm

    endpoint = "https://query.invalid/" + (
        "openai/v1/responses" if provider in {"azure", "bedrock_runtime"} else "v1/responses"
    )
    stock = original()
    manager = rt.Manager(
        Environment(
            {
                "SAG_CHATBOT_LLM_ENABLED": "true",
                "SAG_CHATBOT_LLM_PROVIDER": "responses",
                "SAG_CHATBOT_LLM_MODEL": "query-model",
                "SAG_CHATBOT_LLM_API_KEY": key,
                "SAG_CHATBOT_LLM_RESPONSES_PROVIDER": provider,
                "SAG_CHATBOT_LLM_RESPONSES_ENDPOINT": endpoint,
                "SAG_CHATBOT_LLM_EXTRA_BODY": '{"reasoning":{"effort":"high"}}',
            }
        ),
        stock,
    )
    monkeypatch.setattr(rt, "manager", manager)
    extraction = Settings(
        _env_file=None,
        llm_provider="responses",
        llm_model="extract-model",
        llm_api_key="extract-key",
        llm_responses_endpoint="https://extraction.invalid/v1/responses",
    )
    calls = []

    def transport(request):
        body = json.loads(request.content)
        calls.append((request, body))
        if request.url.host == "query.invalid":
            assert body["model"] == "query-model" and body["reasoning"] == {"effort": "high"}
            assert (
                request.headers.get("api-key" if provider == "azure" else "authorization")
                == (key if provider == "azure" else "Bearer " + key)
                if key
                else not ({"authorization", "api-key"} & set(request.headers))
            )
        else:
            assert request.url.host == "extraction.invalid" and body["model"] == "extract-model"
            assert request.headers["authorization"] == "Bearer extract-key"
            assert "reasoning" not in body
        result = {
            "id": "r",
            "status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}],
        }
        if body["stream"]:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="data: " + json.dumps({"type": "response.completed", "response": result}) + "\n\n",
            )
        return httpx.Response(200, json=result)

    register()
    router = next(
        entry["custom_handler"] for entry in litellm.custom_provider_map if entry["provider"] == "sag_responses"
    )
    monkeypatch.setattr(router, "async_transport", httpx.MockTransport(transport))
    llm = QueryLLM(stock)
    messages = [{"role": "user", "content": "ping"}]
    assert await asyncio.gather(llm.complete(messages), LLMClient(extraction).complete(messages)) == ["done", "done"]
    assert "".join([part async for part in llm.stream_complete(messages)]) == "done"
    assert stock.llm_provider == "openai" and stock.llm_api_key == "original-key"
    assert len(calls) == 3 and rt.operation.get() is None


async def test_original_responses_validates_before_commit_and_clears_keys_on_endpoint_changes(monkeypatch, tmp_path):
    stock = original()
    monkeypatch.setattr(settings_service, "_settings", stock)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/config.db")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Setting.__table__.create(sync))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            with pytest.raises(ConfigurationError):
                await settings_service.save_model_config(
                    session,
                    {
                        "llm_provider": "responses",
                        "llm_responses_endpoint": "https://private:secret@host.invalid/responses",
                    },
                )
            assert await session.scalar(select(Setting)) is None
            assert stock.llm_provider == "openai" and stock.llm_api_key == "original-key"
            config = await settings_service.save_model_config(
                session,
                {
                    "llm_provider": "responses",
                    "llm_model": "native-model",
                    "llm_responses_endpoint": "https://first.invalid/v1/responses",
                },
            )
            assert config["llm_api_key_set"] is False
            await settings_service.save_model_config(session, {"llm_api_key": "native-key"})
            config = await settings_service.save_model_config(
                session, {"llm_responses_endpoint": "https://second.invalid/v1/responses"}
            )
            assert not config["llm_api_key_set"] and stock.llm_api_key == ""
    finally:
        await engine.dispose()


def test_chatbot_responses_endpoint_identity_clears_encrypted_key_and_preserves_embedding():
    stock = original()
    environment = Environment({"SAG_CHATBOT_CONFIG_ENCRYPTION_KEY": Fernet.generate_key().decode()})
    manager = rt.Manager(environment, stock)
    saved, snapshot = manager.draft(
        {
            "llm": {
                "enabled": True,
                "provider": "responses",
                "model": "chat-model",
                "api_key": "query-key",
                "responses_endpoint": "https://first.invalid/v1/responses",
            }
        },
        persist=True,
    )
    assert "query-key" not in json.dumps(saved) and snapshot.settings.llm_provider == "responses"
    manager.persisted = saved
    _, changed = manager.draft({"llm": {"responses_endpoint": "https://second.invalid/v1/responses"}}, persist=True)
    assert changed.connections["llm"].api_key == ""
    assert changed.stock.embedding_model == stock.embedding_model


async def test_independent_agent_replays_reasoning_and_releases_each_concurrent_run(monkeypatch):
    import litellm

    from sag_agent import Agent, AgentTool, RunStatus, ToolResult, ToolSpec
    from sag_api.generation.chatbot import QueryAgentRuntime
    from sag_api.generation.responses.codec import replay
    from sag_api.generation.responses.routing import run_replay

    stock = original()
    environment = Environment(
        {
            "SAG_CHATBOT_LLM_ENABLED": "true",
            "SAG_CHATBOT_LLM_PROVIDER": "responses",
            "SAG_CHATBOT_LLM_MODEL": "chat-model",
            "SAG_CHATBOT_LLM_API_KEY": "chat-key",
            "SAG_CHATBOT_LLM_RESPONSES_ENDPOINT": "https://chat.invalid/v1/responses",
        }
    )
    monkeypatch.setattr(rt, "manager", rt.Manager(environment, stock))
    states = []

    def transport(request):
        body = json.loads(request.content)
        marker = next(item["content"][0]["text"] for item in body["input"] if item.get("role") == "user")
        states.append(replay.get())
        if any(item.get("type") == "function_call_output" for item in body["input"]):
            assert any(item.get("encrypted_content") == marker for item in body["input"])
            output = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}]
        else:
            output = [
                {"type": "reasoning", "id": "rs", "summary": [], "encrypted_content": marker},
                {"type": "function_call", "id": "fc", "call_id": "call", "name": "echo", "arguments": "{}"},
            ]
        result = {"id": "r", "status": "completed", "output": output}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: " + json.dumps({"type": "response.completed", "response": result}) + "\n\n",
        )

    register()
    router = next(
        entry["custom_handler"] for entry in litellm.custom_provider_map if entry["provider"] == "sag_responses"
    )
    monkeypatch.setattr(router, "async_transport", httpx.MockTransport(transport))

    async def echo(arguments, context):
        assert replay.get() is None
        return ToolResult(content="echo")

    tool = AgentTool(ToolSpec(name="echo", label="Echo", description="Echo", parameters={"type": "object"}), echo)
    async with QueryAgentRuntime() as runtime:
        agent = Agent(name="chat", model=QueryLLM(stock), tools=[tool])
        runs = [runtime.run(agent, "first"), runtime.run(agent, "second")]
        results = await asyncio.gather(*(run.result() for run in runs))
        assert all(result.status == RunStatus.COMPLETED and result.output == "done" for result in results)
    assert all(state == {} for state in states)
    assert run_replay.get() is None and replay.get() is None and rt.operation.get() is None
