import json

import httpx
import pytest
from litellm import ModelResponse
from sag_agent import Agent, AgentRuntime, AgentTool, ToolResult, ToolSpec
from sag_chatbot import runtime as rt
from sag_chatbot.config import KEYLESS_API_KEY, Environment
from sag_chatbot.provider import QueryLLM, QueryResponsesProvider
from sag_chatbot.responses.provider import ResponsesProvider


class Stream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False
    def __aiter__(self): return self
    async def __anext__(self):
        try:
            return next(self.chunks)
        except StopIteration:
            raise StopAsyncIteration from None
    async def aclose(self): self.closed = True


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
@pytest.mark.parametrize("key", ["query-key", ""])
async def test_protocol_stream_tool_turns_and_snapshot_without_http(isolate, monkeypatch, provider, key):
    isolate.environment = Environment({
        "SAG_CHATBOT_LLM_ENABLED": "true", "SAG_CHATBOT_LLM_MODEL": "query-model",
        "SAG_CHATBOT_LLM_API_KEY": key, "SAG_CHATBOT_LLM_PROVIDER": provider,
    })
    requests, streams = [], []
    async def completion(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            delta = {"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "echo", "arguments": '{"text":"hi"}'}}]}
            stream = Stream([{"choices": [{"delta": delta, "finish_reason": "tool_calls"}]}])
        else:
            stream = Stream([{"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}])
        streams.append(stream)
        return stream
    monkeypatch.setattr("sag_api.generation.llm._litellm_completion", completion)
    async def echo(arguments, context):
        # A validated new configuration becomes visible to future operations only.
        isolate.persisted = {"llm": {"model": "new-model"}}
        return ToolResult(content=arguments["text"])
    tool = AgentTool(ToolSpec(name="echo", label="Echo", description="echo", parameters={"type": "object"}), echo)
    async with AgentRuntime() as runtime:
        handle = runtime.run(Agent(name="test", model=QueryLLM(isolate.stock), tools=(tool,)), "hello")
        events = [event async for event in handle]
        assert (await handle.result()).output == "done"
    assert events
    assert len(requests) == 2 and all(r["model"] == provider + "/query-model" for r in requests)
    assert all(r["api_key"] == (key or KEYLESS_API_KEY) for r in requests)
    assert all(r["temperature"] == (1 if provider == "anthropic" else .3) for r in requests)
    assert requests[1]["messages"][-1]["role"] == "tool"
    assert all(stream.closed for stream in streams)
    assert rt.operation.get() is None


def response(text="", calls=()):
    return {"id": "r", "status": "completed", "output": list(calls) + (
        [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}] if text else []
    ), "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}}


@pytest.mark.parametrize("provider,endpoint", [
    ("openai", "https://response-query.invalid/v1/responses"),
    ("azure", "https://response-query.invalid/openai/v1/responses"),
    ("bedrock_runtime", "https://response-query.invalid/openai/v1/responses"),
    ("bedrock_mantle", "https://response-query.invalid/v1/responses"),
])
@pytest.mark.parametrize("key", ["responses-query-key", ""])
async def test_responses_transport_reasoning_and_replay(isolate, monkeypatch, provider, endpoint, key):
    isolate.environment = Environment({
        "SAG_CHATBOT_LLM_ENABLED": "true", "SAG_CHATBOT_LLM_MODEL": "qwen3.6-flash",
        "SAG_CHATBOT_LLM_API_KEY": key, "SAG_CHATBOT_LLM_PROVIDER": "responses",
        "SAG_CHATBOT_LLM_RESPONSES_PROVIDER": provider, "SAG_CHATBOT_LLM_RESPONSES_ENDPOINT": endpoint,
        "SAG_CHATBOT_LLM_EXTRA_BODY": '{"reasoning":{"effort":"high"}}',
    })
    requests, failures = [], []
    def transport(request):
        body = json.loads(request.content)
        requests.append((request, body))
        assert body["reasoning"] == {"effort": "high"} and body["store"] is False
        if len(requests) == 1:
            result = response(calls=[
                {"type": "reasoning", "id": "rs_1", "encrypted_content": "query-opaque", "summary": []},
                {"type": "function_call", "id": "fc1", "call_id": "query-call", "name": "echo", "arguments": '{"text":"hi"}'},
            ])
        else:
            assert any(item.get("encrypted_content") == "query-opaque" for item in body["input"])
            assert any(item.get("type") == "function_call_output" and item["call_id"] == "query-call" for item in body["input"])
            result = response("done")
        events = [{"type": "response.completed", "response": result}]
        wire = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=wire)
    from sag_api.core.litellm_policy import apply_litellm_completion_policy
    async def completion(**kwargs):
        snapshot = rt.operation.get()
        snapshot.responses_handler = snapshot.responses_handler or QueryResponsesProvider(
            snapshot.responses_config, async_transport=httpx.MockTransport(transport),
        )
        normalized = apply_litellm_completion_policy(snapshot.settings, kwargs)
        provider = snapshot.responses_handler
        class Translate(Stream):
            def __init__(self):
                    self.iterator = provider.astreaming(
                        model=normalized["model"].split("/", 1)[-1], messages=normalized["messages"], api_key=normalized["api_key"],
                        optional_params={key: value for key, value in normalized.items() if key not in {"model", "messages", "api_key", "api_base", "timeout", "num_retries"}},
                )
            async def __anext__(self):
                try:
                    chunk = await self.iterator.__anext__()
                except StopAsyncIteration:
                    raise
                except Exception as exc:
                    failures.append(repr(exc))
                    raise
                delta = {"content": chunk["text"]}
                if chunk["tool_use"]:
                    delta["tool_calls"] = [chunk["tool_use"]]
                return {"choices": [{"delta": delta, "finish_reason": chunk["finish_reason"] if chunk["is_finished"] else None}]}
            async def aclose(self): await self.iterator.aclose()
        return Translate()
    monkeypatch.setattr("sag_api.generation.llm._litellm_completion", completion)
    async def echo(arguments, context): return ToolResult(content=arguments["text"])
    tool = AgentTool(ToolSpec(name="echo", label="Echo", description="echo", parameters={"type": "object"}), echo)
    async with AgentRuntime() as runtime:
        handle = runtime.run(Agent(name="responses", model=QueryLLM(isolate.stock), tools=(tool,)), "go")
        [event async for event in handle]
        assert (await handle.result()).output == "done", (failures, requests)
    header = "api-key" if provider == "azure" else "authorization"
    assert all(request.headers.get(header) == ((key if provider == "azure" else f"Bearer {key}") if key else None) for request, _ in requests)
    assert all(str(request.url).startswith(endpoint) for request, _ in requests)
    assert rt.chat_replay.get() is None


async def test_plain_stream_and_complete_keep_selected_route(isolate, monkeypatch):
    isolate.environment = Environment({"SAG_CHATBOT_LLM_ENABLED": "true", "SAG_CHATBOT_LLM_MODEL": "selected", "SAG_CHATBOT_LLM_API_KEY": "key"})
    calls = []
    async def completion(**kwargs):
        calls.append(kwargs)
        if kwargs.get("stream"):
            return Stream([{"choices": [{"delta": {"content": "hello"}}]}])
        return ModelResponse(model=kwargs["model"], choices=[{"message": {"role": "assistant", "content": "summary"}}])
    monkeypatch.setattr("sag_api.generation.llm._litellm_completion", completion)
    llm = QueryLLM(isolate.stock)
    assert await llm.complete([{"role": "user", "content": "summarize"}]) == "summary"
    output = []
    async for chunk in llm.stream_complete([{"role": "user", "content": "answer"}]):
        assert not rt.query_scope.get()  # Scope cannot leak into stream consumers or their durable work.
        output.append(chunk)
    assert output == ["hello"]
    assert all(call["model"] == "openai/selected" for call in calls)


@pytest.mark.parametrize("key", ["query-key", ""])
async def test_registered_responses_route_uses_real_litellm_boundary(isolate, key):
    isolate.environment = Environment({
        "SAG_CHATBOT_LLM_ENABLED": "true", "SAG_CHATBOT_LLM_PROVIDER": "responses",
        "SAG_CHATBOT_LLM_MODEL": "test-model", "SAG_CHATBOT_LLM_API_KEY": key,
        "SAG_CHATBOT_LLM_RESPONSES_ENDPOINT": "https://query.invalid/v1/responses",
    })
    requests = []
    def transport(request):
        requests.append(request)
        return httpx.Response(200, json=response("actual-route"))
    async with isolate.scope(query=True) as snapshot:
        from sag_api.api.v1.system import _capabilities
        assert _capabilities()["llm_provider"] == "openai"
        assert _capabilities()["llm_model"] == "test-model"
        snapshot.responses_handler = QueryResponsesProvider(snapshot.responses_config, async_transport=httpx.MockTransport(transport))
        assert await QueryLLM(isolate.stock).complete([{"role": "user", "content": "hello"}]) == "actual-route"
    assert requests[0].headers.get("authorization") == ("Bearer query-key" if key else None)
    assert json.loads(requests[0].content)["model"] == "test-model"


async def test_disabled_query_llm_preserves_ambient_replay_state(isolate, monkeypatch):
    from sag_chatbot.responses.codec import replay
    state = {"stock-call": {"encrypted_content": "stock-opaque"}}
    async def completion(**kwargs):
        assert replay.get() is state
        assert not rt.query_scope.get()
        assert kwargs["api_key"] == "stock-key"
        return ModelResponse(model=kwargs["model"], choices=[{"message": {"role": "assistant", "content": "stock"}}])
    monkeypatch.setattr("sag_api.generation.llm._litellm_completion", completion)
    token = replay.set(state)
    try:
        assert await QueryLLM(isolate.stock).complete([{"role": "user", "content": "hi"}]) == "stock"
        assert replay.get() is state and state["stock-call"]["encrypted_content"] == "stock-opaque"
    finally:
        replay.reset(token)


@pytest.mark.parametrize("stream", [False, True])
async def test_separate_responses_retry_budget_at_litellm_boundary(isolate, stream):
    isolate.environment = Environment({
        "SAG_CHATBOT_LLM_ENABLED": "true", "SAG_CHATBOT_LLM_PROVIDER": "responses",
        "SAG_CHATBOT_LLM_MODEL": "retry-model", "SAG_CHATBOT_LLM_API_KEY": "query-key",
        "SAG_CHATBOT_LLM_RESPONSES_ENDPOINT": "https://query.invalid/v1/responses",
        "SAG_CHATBOT_LLM_MAX_RETRIES": "1",
    })
    calls = []
    def transport(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": {"message": "private query-key detail"}})
        if stream:
            event = {"type": "response.completed", "response": response("retried")}
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="data: " + json.dumps(event) + "\n\n")
        return httpx.Response(200, json=response("retried"))
    async with isolate.scope() as snapshot:
        snapshot.responses_handler = ResponsesProvider(snapshot.responses_config, async_transport=httpx.MockTransport(transport))
        llm = QueryLLM(isolate.stock)
        messages = [{"role": "user", "content": "retry"}]
        if stream:
            assert "".join([chunk async for chunk in llm.stream_complete(messages)]) == "retried"
        else:
            assert await llm.complete(messages) == "retried"
    assert len(calls) == 2
    assert all(call.url.host == "query.invalid" and call.headers["authorization"] == "Bearer query-key" for call in calls)


async def test_query_engine_responses_replay_is_separate_from_stock(isolate):
    from sag_chatbot.responses.codec import replay
    from zleap.sag.core.ai.models import LLMMessage
    isolate.environment = Environment({
        "SAG_CHATBOT_LLM_ENABLED": "true", "SAG_CHATBOT_LLM_PROVIDER": "responses",
        "SAG_CHATBOT_LLM_MODEL": "test-model", "SAG_CHATBOT_LLM_API_KEY": "query-key",
        "SAG_CHATBOT_LLM_RESPONSES_ENDPOINT": "https://query.invalid/v1/responses",
    })
    def transport(request):
        return httpx.Response(200, json=response(calls=[
            {"type": "reasoning", "id": "rs_query", "encrypted_content": "query-opaque", "summary": []},
            {"type": "function_call", "id": "fc_query", "call_id": "query-call", "name": "echo", "arguments": "{}"},
        ]))
    stock_state = {"stock-call": []}
    token = replay.set(stock_state)
    try:
        async with isolate.scope(query=True) as snapshot:
            snapshot.responses_handler = ResponsesProvider(snapshot.responses_config, async_transport=httpx.MockTransport(transport))
            await snapshot.adapter("llm").chat([LLMMessage(role="user", content="query")])
            assert ("query-call",) in snapshot.replay
            assert stock_state == {"stock-call": []} and replay.get() is stock_state
    finally:
        replay.reset(token)
