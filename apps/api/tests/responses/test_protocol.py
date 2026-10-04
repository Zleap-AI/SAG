import asyncio
import json

import httpx
import pytest
from openai import APIError

from sag_api.core.config import Settings
from sag_api.core.responses import Config
from sag_api.generation.responses.codec import decode_response, messages_input, replay, request_body
from sag_api.generation.responses.provider import ResponsesProvider


def response(text="hello", calls=()):
    return {
        "id": "resp_test",
        "status": "completed",
        "output": (
            [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]
            if text
            else []
        )
        + list(calls),
        "usage": {
            "input_tokens": 7,
            "output_tokens": 3,
            "total_tokens": 10,
            "input_tokens_details": {"cached_tokens": 2},
            "output_tokens_details": {"reasoning_tokens": 1},
        },
    }


def function(call_id="call_1"):
    return {"type": "function_call", "id": "fc_1", "call_id": call_id, "name": "echo", "arguments": '{"text":"hi"}'}


def sse(events):
    return "".join(
        "event: " + event["type"] + "\r\ndata: " + json.dumps(event, ensure_ascii=False) + "\r\n\r\n"
        for event in events
    ).encode()


def kwargs(**extra):
    return {
        "model": "vendor/model",
        "messages": [{"role": "user", "content": "hello"}],
        "api_key": "unit-test-secret",
        "optional_params": {},
        **extra,
    }


def test_request_maps_images_schema_tools_and_options():
    params = {
        "max_tokens": 123,
        "temperature": 0.3,
        "reasoning_effort": "low",
        "tools": [
            {"type": "function", "function": {"name": "echo", "parameters": {"type": "object", "properties": {}}}}
        ],
        "tool_choice": {"type": "function", "function": {"name": "echo"}},
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "answer", "schema": {"type": "object"}, "strict": True},
        },
    }
    messages = [
        {"role": "system", "content": "instructions"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        },
    ]
    body = request_body("exact/id", messages, params, stream=True, send_temperature=False)
    assert body["model"] == "exact/id"
    assert body["max_output_tokens"] == 123 and "temperature" not in body
    assert body["input"][1]["content"][1]["type"] == "input_image"
    assert body["tools"][0]["strict"] is False
    assert body["tool_choice"] == {"type": "function", "name": "echo"}
    assert body["text"]["format"]["name"] == "answer"
    assert body["reasoning"] == {"effort": "low"}
    assert body["store"] is False and "previous_response_id" not in body


@pytest.mark.parametrize(
    "params",
    [
        {"stop": ["x"]},
        {"extra_body": {"store": True}},
        {"extra_body": {"enable_thinking": False}},
        {"tools": [{"type": "web_search"}]},
    ],
)
def test_unsupported_features_fail_explicitly(params):
    with pytest.raises(APIError):
        request_body("model", kwargs()["messages"], params, stream=False, send_temperature=False)


def test_reasoning_replay_follows_retained_history():
    state = {}
    token = replay.set(state)
    try:
        raw = response("", [function()])
        raw["output"].insert(0, {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque", "summary": []})
        text, calls, usage = decode_response(raw)
        messages = [
            {"role": "assistant", "content": text, "tool_calls": calls},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ]
        wire = messages_input(messages)
        assert wire[0]["encrypted_content"] == "opaque"
        assert wire[1]["call_id"] == wire[2]["call_id"] == "call_1"
        assert usage["prompt_tokens_details"]["cached_tokens"] == 2
        messages_input([{"role": "user", "content": "new context"}])
        assert state == {}
    finally:
        replay.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,header",
    [
        ("openai", "authorization"),
        ("azure", "api-key"),
        ("bedrock_runtime", "authorization"),
        ("bedrock_mantle", "authorization"),
    ],
)
async def test_transport_auth_and_exact_model(provider, header):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=response())

    p = ResponsesProvider(
        Config(provider, "https://example.test/v1/responses?api-version=v1"),
        async_transport=httpx.MockTransport(handler),
    )
    result = await p.acompletion(**kwargs())
    assert result.choices[0].message.content == "hello"
    assert "unit-test-secret" in seen[0].headers[header]
    assert json.loads(seen[0].content)["model"] == "vendor/model"
    assert seen[0].url.params["api-version"] == "v1"
    assert result.usage.completion_tokens_details.reasoning_tokens == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    [
        "https://resource.openai.azure.com/openai/v1/responses",
        "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/responses",
        "https://bedrock-mantle.us-east-1.api.aws/v1/responses",
        "https://private.example/tenant/responses?route=one",
    ],
)
async def test_default_connection_uses_exact_endpoint_and_bearer_without_vendor_selection(endpoint):
    settings = Settings(
        _env_file=None, llm_provider="responses", llm_model="vendor/model", llm_responses_endpoint=endpoint
    )
    config = Config.from_settings(settings)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=response())

    provider = ResponsesProvider(config, async_transport=httpx.MockTransport(handler))
    assert (await provider.acompletion(**kwargs())).choices[0].message.content == "hello"
    assert config.provider == "openai"
    assert seen[0].url == httpx.URL(endpoint)
    assert seen[0].headers["authorization"] == "Bearer unit-test-secret"
    assert "api-key" not in seen[0].headers and "api-version" not in seen[0].url.params
    assert json.loads(seen[0].content)["store"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429, 500, 503])
async def test_errors_are_redacted_and_status_preserved(status):
    p = ResponsesProvider(
        Config("openai", "https://example.test/v1/responses"),
        async_transport=httpx.MockTransport(
            lambda r: httpx.Response(status, json={"error": {"message": "unit-test-secret private prompt"}})
        ),
    )
    with pytest.raises(APIError) as error:
        await p.acompletion(**kwargs())
    assert error.value.status_code == status
    assert "unit-test-secret" not in str(error.value) and "private prompt" not in str(error.value)


class Fragmented(httpx.AsyncByteStream):
    def __init__(self, data, block=False):
        self.data, self.block, self.closed = data, block, False

    async def __aiter__(self):
        for index in range(0, len(self.data), 3):
            yield self.data[index : index + 3]
        if self.block:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_stream_fragmentation_tools_usage_and_cleanup():
    events = [
        {"type": "response.output_text.delta", "delta": "你好"},
        {"type": "response.function_call_arguments.delta", "delta": '{"te'},
        {"type": "response.function_call_arguments.delta", "delta": 'xt":"hi"}'},
        {"type": "response.completed", "response": response("你好", [function(), function("call_2")])},
    ]
    stream = Fragmented(sse(events))
    p = ResponsesProvider(
        Config("openai", "https://example.test/v1/responses"),
        async_transport=httpx.MockTransport(
            lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
        ),
    )
    chunks = [c async for c in p.astreaming(**kwargs())]
    assert "".join(c["text"] for c in chunks) == "你好"
    assert [c["tool_use"]["id"] for c in chunks if c["tool_use"]] == ["call_1", "call_2"]
    assert chunks[-1]["usage"]["completion_tokens_details"]["reasoning_tokens"] == 1
    assert chunks[-1]["finish_reason"] == "tool_calls" and stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", [None, "response.failed", "response.incomplete", "response.refusal.delta"])
async def test_failed_stream_never_emits_tools(ending):
    events = [{"type": "response.function_call_arguments.delta", "delta": "{}"}]
    if ending:
        events.append({"type": ending})
    stream = Fragmented(sse(events))
    p = ResponsesProvider(
        Config("openai", "https://example.test/v1/responses"),
        async_transport=httpx.MockTransport(
            lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
        ),
    )
    chunks = []
    with pytest.raises(APIError):
        async for c in p.astreaming(**kwargs()):
            chunks.append(c)
    assert not chunks and stream.closed


@pytest.mark.asyncio
async def test_cancellation_closes_http_stream():
    stream = Fragmented(sse([{"type": "response.output_text.delta", "delta": "partial"}]), block=True)
    p = ResponsesProvider(
        Config("openai", "https://example.test/v1/responses"),
        async_transport=httpx.MockTransport(
            lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
        ),
    )

    async def consume():
        async for _ in p.astreaming(**kwargs()):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed


def test_sync_completion_and_stream():
    def handler(request):
        if json.loads(request.content)["stream"]:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse([{"type": "response.completed", "response": response()}]),
            )
        return httpx.Response(200, json=response())

    p = ResponsesProvider(
        Config("openai", "https://example.test/v1/responses"), sync_transport=httpx.MockTransport(handler)
    )
    assert p.completion(**kwargs()).choices[0].message.content == "hello"
    assert "".join(c["text"] for c in p.streaming(**kwargs())) == "hello"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 422, 429, 503])
async def test_retry_budget_and_permanent_errors(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "private provider detail"}})

    p = ResponsesProvider(
        Config("openai", "https://example.test/v1/responses"), async_transport=httpx.MockTransport(handler)
    )
    with pytest.raises(APIError):
        await p.acompletion(**kwargs(optional_params={"max_retries": 1}))
    assert len(calls) == (2 if status in {429, 503} else 1)


@pytest.mark.asyncio
async def test_partial_stream_is_never_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse([{"type": "response.output_text.delta", "delta": "partial"}]),
        )

    p = ResponsesProvider(
        Config("openai", "https://example.test/v1/responses"), async_transport=httpx.MockTransport(handler)
    )
    with pytest.raises(APIError):
        _ = [c async for c in p.astreaming(**kwargs(optional_params={"max_retries": 2}))]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_stream_retry_before_output():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "limited"}})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse([{"type": "response.completed", "response": response()}]),
        )

    p = ResponsesProvider(
        Config("openai", "https://example.test/v1/responses"), async_transport=httpx.MockTransport(handler)
    )
    chunks = [c async for c in p.astreaming(**kwargs(optional_params={"max_retries": 1}))]
    assert len(calls) == 2 and "".join(c["text"] for c in chunks) == "hello"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
async def test_network_errors_never_leak_request(failure):
    def handler(request):
        raise failure("unit-test-secret", request=request)

    p = ResponsesProvider(
        Config("openai", "https://example.test/v1/responses"), async_transport=httpx.MockTransport(handler)
    )
    with pytest.raises(APIError) as error:
        await p.acompletion(**kwargs())
    assert error.value.status_code == (408 if failure is httpx.ReadTimeout else 503)
    assert "unit-test-secret" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        b"data: not-json\n\n",
        b"data: [DONE]\n\n",
        sse([{"type": "response.completed", "response": {"status": "incomplete"}}]),
        sse([{"type": "response.completed", "response": response("", [{"type": "web_search_call"}])}]),
    ],
)
async def test_malformed_and_unsupported_streams(data):
    p = ResponsesProvider(
        Config("openai", "https://example.test/v1/responses"),
        async_transport=httpx.MockTransport(
            lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)
        ),
    )
    with pytest.raises(APIError):
        _ = [c async for c in p.astreaming(**kwargs())]
