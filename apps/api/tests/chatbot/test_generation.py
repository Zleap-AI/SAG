import pytest
from litellm import ModelResponse

from sag_agent import Agent, AgentTool, ToolResult, ToolSpec
from sag_api.core.chatbot_config import KEYLESS_API_KEY, Environment
from sag_api.generation.chatbot import QueryAgentRuntime, QueryLLM
from sag_api.services import chatbot_service as rt


class Stream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.chunks)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
@pytest.mark.parametrize("key", ["query-key", ""])
async def test_protocol_stream_tool_turns_and_snapshot_without_http(isolate, monkeypatch, provider, key):
    isolate.environment = Environment(
        {
            "SAG_CHATBOT_LLM_ENABLED": "true",
            "SAG_CHATBOT_LLM_MODEL": "query-model",
            "SAG_CHATBOT_LLM_API_KEY": key,
            "SAG_CHATBOT_LLM_PROVIDER": provider,
        }
    )
    requests, streams = [], []

    async def completion(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            delta = {
                "tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "echo", "arguments": '{"text":"hi"}'}}]
            }
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
    async with QueryAgentRuntime() as runtime:
        handle = runtime.run(Agent(name="test", model=QueryLLM(isolate.stock), tools=(tool,)), "hello")
        events = [event async for event in handle]
        assert (await handle.result()).output == "done"
    assert events
    assert len(requests) == 2 and all(r["model"] == provider + "/query-model" for r in requests)
    assert all(r["api_key"] == (key or KEYLESS_API_KEY) for r in requests)
    assert all(r["temperature"] == (1 if provider == "anthropic" else 0.3) for r in requests)
    assert requests[1]["messages"][-1]["role"] == "tool"
    assert all(stream.closed for stream in streams)
    assert rt.operation.get() is None


async def test_plain_stream_and_complete_keep_selected_route(isolate, monkeypatch):
    isolate.environment = Environment(
        {"SAG_CHATBOT_LLM_ENABLED": "true", "SAG_CHATBOT_LLM_MODEL": "selected", "SAG_CHATBOT_LLM_API_KEY": "key"}
    )
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
