from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import litellm
import pytest

from sag_api.core.config import Settings
from sag_api.core.litellm_policy import install_litellm_policy, uninstall_litellm_policy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call_kind",
    ["generation", "stream", "tool_none", "tool_required", "extraction", "extraction_stream"],
)
async def test_deepseek_flash_sends_non_thinking_http_request(
    monkeypatch: pytest.MonkeyPatch, call_kind: str
) -> None:
    """Inspect serialized HTTP after real generation/dependency and LiteLLM routing."""
    from zleap.sag.core.ai.factory import create_llm_client
    from zleap.sag.core.ai.models import LLMMessage

    from sag_agent import AgentMessage, CancellationToken, ModelRequest
    from sag_api.generation.llm import LLMClient
    from sag_api.sag.config_builder import build_engine_config

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        response = {
            "id": "deepseek-policy-test",
            "object": "chat.completion",
            "created": 1,
            "model": "deepseek-flash",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }
        if body.get("stream"):
            response["object"] = "chat.completion.chunk"
            response["choices"] = [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=f"data: {json.dumps(response)}\n\ndata: [DONE]\n\n",
            )
        return httpx.Response(200, json=response)

    async def send(_self: httpx.AsyncClient, request: httpx.Request, **_kwargs: Any) -> httpx.Response:
        response = respond(request)
        response.request = request
        return response

    # LiteLLM can select either HTTPX or aiohttp transports; intercept the
    # fully serialized request before either transport can access the network.
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    configured = Settings(
        _env_file=None,
        llm_provider="openai",
        llm_base_url="https://api.deepseek.com",
        llm_model="deepseek-flash",
        llm_api_key="deepseek-policy-test-key",
        llm_extra_body=None,
        llm_max_retries=0,
    )
    client = LLMClient(configured)
    messages = [{"role": "user", "content": "hello"}]
    if call_kind == "generation":
        assert await client.complete(messages) == "ok"
    elif call_kind == "stream":
        assert "".join([part async for part in client.stream_complete(messages)]) == "ok"
    elif call_kind.startswith("tool_"):
        request = ModelRequest(
            messages=(AgentMessage(role="user", content="hello"),),
            tools=({"type": "function", "function": {"name": "search_context", "parameters": {"type": "object"}}},),
            tool_choice=call_kind.removeprefix("tool_"),
            turn=1,
        )
        chunks = [chunk async for chunk in client.stream_turn(request, CancellationToken())]
        assert "".join(chunk.text_delta or "" for chunk in chunks) == "ok"
    else:
        engine = build_engine_config(configured)
        extraction_client = await create_llm_client(scenario="extract", model_config=engine.llm.model_dump())
        handle = install_litellm_policy(configured)
        try:
            extraction_messages = [LLMMessage(role="user", content="hello")]
            if call_kind == "extraction":
                result = await extraction_client.chat(extraction_messages)
                assert result.content == "ok"
            else:
                parts = [part async for part in extraction_client.chat_stream(extraction_messages)]
                assert "".join(part[0] for part in parts) == "ok"
        finally:
            uninstall_litellm_policy(handle)

    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.deepseek.com/chat/completions"
    body = json.loads(requests[0].content)
    assert body["model"] == "deepseek-flash"
    assert body.get("thinking") == {"type": "disabled"}
    assert "reasoning_effort" not in body
    assert "enable_thinking" not in body
    if call_kind.startswith("tool_"):
        assert body["tool_choice"] == call_kind.removeprefix("tool_")


class GatewayError(RuntimeError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def _schema_request(**overrides: Any) -> dict[str, Any]:
    return {
        "model": "openai/qwen-test",
        "api_base": "https://gateway.example/v1",
        "messages": [{"role": "user", "content": "extract"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "answer", "schema": {"type": "object"}},
        },
        **overrides,
    }


@pytest.mark.asyncio
async def test_auto_downgrades_once_and_caches_by_provider_base_url_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if kwargs["response_format"]["type"] == "json_schema":
            raise GatewayError("response_format json_schema is not supported", 400)
        return {"ok": True}

    monkeypatch.setattr(litellm, "acompletion", completion)
    original = litellm.acompletion
    handle = install_litellm_policy(
        Settings(_env_file=None, llm_structured_output_mode="auto")
    )
    try:
        assert await litellm.acompletion(**_schema_request()) == {"ok": True}
        assert await litellm.acompletion(**_schema_request()) == {"ok": True}
        assert [call["response_format"]["type"] for call in calls] == [
            "json_schema",
            "json_object",
            "json_object",
        ]

        await litellm.acompletion(
            **_schema_request(api_base="https://another.example/v1")
        )
        assert calls[-2]["response_format"]["type"] == "json_schema"
    finally:
        uninstall_litellm_policy(handle)
    assert litellm.acompletion is original


@pytest.mark.asyncio
async def test_auto_downgrades_deepseek_unavailable_response_format_wording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if kwargs["response_format"]["type"] == "json_schema":
            raise GatewayError(
                "OpenAIException - This response_format type is unavailable now",
                400,
            )
        return {"ok": True}

    monkeypatch.setattr(litellm, "acompletion", completion)
    handle = install_litellm_policy(
        Settings(
            _env_file=None,
            llm_provider="openai",
            llm_base_url="https://api.deepseek.com",
            llm_model="deepseek-v4-flash",
            llm_structured_output_mode="auto",
        )
    )
    try:
        assert await litellm.acompletion(
            **_schema_request(
                model="openai/deepseek-v4-flash",
                api_base="https://api.deepseek.com",
            )
        ) == {"ok": True}
    finally:
        uninstall_litellm_policy(handle)

    assert [call["response_format"]["type"] for call in calls] == [
        "json_schema",
        "json_object",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "status"),
    [
        ("invalid request body", 400),
        ("response_format json_schema is not supported", 401),
        ("response_format json_schema rate limit", 429),
    ],
)
async def test_auto_never_downgrades_unrelated_or_non_capability_errors(
    monkeypatch: pytest.MonkeyPatch, message: str, status: int
) -> None:
    calls = 0

    async def completion(**_kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        raise GatewayError(message, status)

    monkeypatch.setattr(litellm, "acompletion", completion)
    handle = install_litellm_policy(Settings(_env_file=None))
    try:
        with pytest.raises(GatewayError, match=message):
            await litellm.acompletion(**_schema_request())
    finally:
        uninstall_litellm_policy(handle)
    assert calls == 1


@pytest.mark.asyncio
async def test_auto_serializes_first_probe_for_same_capability_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_calls = 0
    object_calls = 0

    async def completion(**kwargs: Any) -> dict[str, Any]:
        nonlocal schema_calls, object_calls
        if kwargs["response_format"]["type"] == "json_schema":
            schema_calls += 1
            await asyncio.sleep(0.01)
            raise GatewayError("json_schema response_format unsupported", 422)
        object_calls += 1
        return {"ok": True}

    monkeypatch.setattr(litellm, "acompletion", completion)
    handle = install_litellm_policy(Settings(_env_file=None))
    try:
        results = await asyncio.gather(
            litellm.acompletion(**_schema_request()),
            litellm.acompletion(**_schema_request()),
        )
    finally:
        uninstall_litellm_policy(handle)
    assert results == [{"ok": True}, {"ok": True}]
    assert schema_calls == 1
    assert object_calls == 2


@pytest.mark.asyncio
async def test_explicit_mode_does_not_auto_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def completion(**_kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        raise GatewayError("response_format json_schema is not supported", 400)

    monkeypatch.setattr(litellm, "acompletion", completion)
    handle = install_litellm_policy(
        Settings(_env_file=None, llm_structured_output_mode="json_schema")
    )
    try:
        with pytest.raises(GatewayError):
            await litellm.acompletion(**_schema_request())
    finally:
        uninstall_litellm_policy(handle)
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", ["minimal", "rich"])
async def test_engine_extraction_sends_required_fields_through_app_policy(monkeypatch, contract):
    """Exercise the installed engine and SAG request policy together for #206."""
    from pathlib import Path
    from types import SimpleNamespace

    import zleap.sag
    from zleap.sag.core.adapters.defaults import OpenAILLMAdapter
    from zleap.sag.modules.extract.config import ExtractConfig, ExtractionWritePlan
    from zleap.sag.modules.extract.processor import EventProcessor
    from zleap.sag.pipeline import RelatedEventContextOptions

    from sag_api.sag.config_builder import build_engine_config

    requests = []

    async def send(_self, request, **_kwargs):
        body = json.loads(request.content)
        requests.append(body)
        schema = body["response_format"]["json_schema"]["schema"]
        for definition in [schema, *schema["$defs"].values()]:
            assert set(definition["required"]) == set(definition["properties"])
        event = {
            "title": "Acme 融资", "content": "Acme 完成融资。",
            "entities": [{"type": "organization", "name": "Acme", "description": "融资方"}],
        }
        if contract == "rich":
            event.update(reason="文档描述融资事项。", references=[1])
        # Local defaults remain valid even if a compatible provider omits them.
        payload = {"type": "response", "data": {"items": [event]}}
        return httpx.Response(200, request=request, json={
            "id": "schema-test", "object": "chat.completion", "created": 1, "model": "gpt-6-luna",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(payload)},
                         "finish_reason": "stop"}],
        })

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    configured = Settings(
        _env_file=None, llm_provider="openai", llm_model="gpt-6-luna",
        llm_api_key="schema-test-key", llm_base_url="https://gateway.example/v1",
        llm_structured_output_mode="json_schema", llm_max_retries=0, llm_temperature=1,
    )
    processor = EventProcessor(
        llm_client=OpenAILLMAdapter(scenario="extract", config=build_engine_config(configured).llm),
        embedding=object(), session_factory=object(), repositories=object(),
        prompt_manager=SimpleNamespace(prompts_dir=Path(zleap.sag.__file__).parent / "prompts", language="zh"),
        config=ExtractConfig(
            storage_mode="normal", write_plan=ExtractionWritePlan.from_storage_mode("normal"),
            data_source_id="ds", source_type="ARTICLE", source_id="source", source_version="v1",
            chunk_ids=("chunk",), related_events=RelatedEventContextOptions(enabled=False),
            contract=contract, max_retries=0,
        ),
    )
    await processor.initialize([SimpleNamespace(type="organization", name="Organization", description="Organization")])
    handle = install_litellm_policy(configured)
    try:
        result = await processor.process([SimpleNamespace(content="Acme 完成融资。")], {}, "ARTICLE")
    finally:
        uninstall_litellm_policy(handle)

    assert len(requests) == 1
    assert result["data"]["items"][0]["children"] == []
    assert result["data"]["items"][0]["is_valid"] is True
