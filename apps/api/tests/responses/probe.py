"""Fresh-process native generation, extraction and Agent integration probes."""

# ruff: noqa: E402 -- configure an isolated process before application imports.
import asyncio
import json
import os
import sys

model = sys.argv[2] if len(sys.argv) > 2 else "vendor/exact-model"
effort = sys.argv[3] if len(sys.argv) > 3 else "unset"
os.environ.update(
    {
        "SAG_LLM_PROVIDER": "responses",
        "SAG_LLM_RESPONSES_PROVIDER": "openai",
        "SAG_LLM_RESPONSES_ENDPOINT": "https://responses.invalid/v1/responses",
        "SAG_LLM_API_KEY": "probe-key",
        "SAG_LLM_MODEL": model,
        "SAG_LLM_EXTRA_BODY": json.dumps({"reasoning": {"effort": effort}} if effort != "unset" else {}),
        "SAG_LOCK_LLM_CONFIG": "true",
        "SAG_LLM_BASE_URL": "https://unused.invalid/v1",
    }
)

from sag_api.core.config import settings
from sag_api.core.responses import Config
from sag_api.generation.responses.routing import register, run_replay

config = Config.from_settings(settings)
case = sys.argv[1]
register()
register()

import httpx
import litellm
from test_protocol import function, response, sse

from sag_api.generation.llm import LLMClient
from sag_api.generation.responses.codec import replay

# The singleton and draft share the same isolated configuration.
assert settings.routed_llm_model == f"sag_responses/{model}"
assert settings.effective_embedding_api_key != "probe-key"
settings.embedding_base_url = None
assert settings.effective_embedding_base_url is None
from sag_api.services.settings_service import apply_overrides

apply_overrides(settings, {"llm_model": "wrong-model", "llm_api_key": "wrong-key"})
assert settings.llm_model == model and settings.llm_api_key == "probe-key"
provider = next(e["custom_handler"] for e in litellm.custom_provider_map if e["provider"] == "sag_responses")
requests = []
seen_states = []


def handler(request):
    body = json.loads(request.content)
    requests.append(body)
    assert body["model"] == model
    assert not {"thinking", "enable_thinking", "reasoning_effort"} & body.keys()
    if len(sys.argv) > 2:
        expected = sys.argv[4] if len(sys.argv) > 4 else (effort if effort != "unset" else "none")
        assert body["reasoning"] == {"effort": expected}
    assert request.url == config.endpoint
    assert request.headers["authorization"] == "Bearer probe-key"
    assert body["store"] is False
    if effort == "unsupported":
        return httpx.Response(400, json={"error": {"message": "reasoning effort not supported"}})
    if case == "schema" and len(requests) == 1:
        return httpx.Response(
            400, json={"error": {"message": "json_schema not supported; do not leak probe-key", "param": "text.format"}}
        )
    if case == "retry" and len(requests) == 1:
        return httpx.Response(429, json={"error": {"message": "limited"}})
    raw = response('{"ok":true}' if "text" in body else "hello")
    if case == "agent" and body.get("tools"):
        seen_states.append(replay.get())
        marker = next(item["content"][0]["text"] for item in body["input"] if item.get("role") == "user")
        if any(item.get("type") == "function_call_output" for item in body["input"]):
            assert any(item.get("encrypted_content") == marker for item in body["input"])
        else:
            raw = response("", [function()])
            raw["output"].insert(0, {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": marker})
    if body["stream"]:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse([{"type": "response.completed", "response": raw}]),
        )
    return httpx.Response(200, json=raw)


provider.async_transport = httpx.MockTransport(handler)
provider.sync_transport = httpx.MockTransport(handler)


async def main():
    llm = LLMClient(settings)
    if case in {"generation", "retry"}:
        assert await llm.complete([{"role": "user", "content": "hello"}]) == "hello"
        if case == "retry":
            assert len(requests) == 2
        assert "".join([part async for part in llm.stream_complete([{"role": "user", "content": "hello"}])]) == "hello"
        from sag_api.core.litellm_policy import apply_litellm_completion_policy

        request = {
            "model": settings.routed_llm_model,
            "messages": [{"role": "user", "content": "hello"}],
            "api_key": settings.llm_api_key,
        }
        assert (
            litellm.completion(**apply_litellm_completion_policy(settings, request)).choices[0].message.content
            == "hello"
        )
    elif case in {"extraction", "schema"}:
        from zleap.sag.core.ai.factory import create_llm_client
        from zleap.sag.core.ai.models import LLMMessage

        from sag_api.core.litellm_policy import install_litellm_policy, uninstall_litellm_policy
        from sag_api.sag.config_builder import build_engine_config

        engine_config = build_engine_config(settings)
        engine_llm = await create_llm_client(scenario="extract", model_config=engine_config.llm.model_dump())
        policy = install_litellm_policy(settings)
        try:
            result = await engine_llm.chat(
                [LLMMessage(role="user", content="return JSON")],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "result",
                        "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                    },
                },
            )
            assert json.loads(result.content) == {"ok": True}
            if case == "schema":
                assert [r["text"]["format"]["type"] for r in requests] == ["json_schema", "json_object"]
        finally:
            uninstall_litellm_policy(policy)
    elif case == "agent":
        from sag_agent import Agent, AgentTool, RunStatus, ToolResult, ToolSpec
        from sag_api.generation.chatbot import QueryAgentRuntime

        async def execute(arguments, context):
            assert arguments == {"text": "hi"}
            assert replay.get() is None  # Nested generation in tools cannot corrupt agent state.
            assert await llm.complete([{"role": "user", "content": "nested generation"}]) == "hello"
            return ToolResult(content="echo:hi")

        tool = AgentTool(
            ToolSpec(
                name="echo",
                label="Echo",
                description="Echo text",
                parameters={"type": "object", "properties": {"text": {"type": "string"}}},
            ),
            execute,
        )
        async with QueryAgentRuntime() as runtime:
            runs = [
                runtime.run(Agent(name="test", model=llm, tools=[tool]), f"hello-{i}", run_id=f"run-{i}")
                for i in range(2)
            ]
            results = await asyncio.gather(*(run.result() for run in runs))
            assert all(r.status == RunStatus.COMPLETED and r.output == "hello" for r in results), results
            assert all(
                r.usage.input_tokens == 14 and r.usage.output_tokens == 6 and r.usage.reasoning_tokens == 2
                for r in results
            ), [r.usage for r in results]
        assert replay.get() is None and run_replay.get() is None
        assert all(state == {} for state in seen_states)
        assert len(requests) == 6


from sag_api.core.litellm_policy import install_litellm_policy, uninstall_litellm_policy

# A running application's lifespan installs this callback, so chat gets policy
# once during request construction and again before LiteLLM dispatch.
policy = install_litellm_policy(settings) if case in {"generation", "agent"} else None
try:
    if effort == "unsupported":
        try:
            asyncio.run(main())
        except Exception as error:
            assert type(error).__name__ in {"UpstreamError", "LLMConfigurationError", "LLMInvalidRequestError"}, error
            assert len(requests) == 1  # No retry or silent downgrade of reasoning.
        else:
            raise AssertionError("Unsupported native effort was silently accepted")
    else:
        asyncio.run(main())
finally:
    if policy:
        uninstall_litellm_policy(policy)
