from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

import sag_agent
from sag_agent import (
    Agent,
    AgentRuntime,
    AgentRuntimeError,
    AgentTool,
    EventType,
    MemoryRunStore,
    ModelChunk,
    RunStatus,
    RuntimeConfig,
    ToolCall,
    ToolResult,
    ToolSpec,
)


class ScriptedModel:
    def __init__(self, *turns: list[ModelChunk]) -> None:
        self.turns = list(turns)
        self.requests = []

    async def stream_turn(self, request, cancellation):
        self.requests.append(request)
        if not self.turns:
            raise AssertionError("unexpected model call")
        for chunk in self.turns.pop(0):
            cancellation.raise_if_cancelled()
            yield chunk


async def collect(handle):
    return [event async for event in handle]


@pytest.mark.asyncio
async def test_runtime_streams_one_model_turn_and_replays_events():
    model = ScriptedModel([ModelChunk(text_delta="hello "), ModelChunk(text_delta="world")])
    runtime = AgentRuntime()
    with pytest.raises(AgentRuntimeError):
        runtime.run(Agent(name="test", model=model), "hello")

    await runtime.start()
    handle = runtime.run(Agent(name="test", model=model), "hello", run_id="run-direct")
    events = await collect(handle)
    result = await handle.result()

    assert result.status == RunStatus.COMPLETED
    assert result.output == "hello world"
    assert len(model.requests) == 1
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[0].type == EventType.RUN_STARTED
    assert events[-1].type == EventType.RUN_COMPLETED
    assert await runtime.store.events("run-direct") == events
    await runtime.stop()


@pytest.mark.asyncio
async def test_tool_loop_uses_one_model_call_per_turn_and_preserves_messages():
    model = ScriptedModel(
        [ModelChunk(tool_calls=(ToolCall(id="call-1", name="echo", arguments={"text": "hi"}),))],
        [ModelChunk(text_delta="done")],
    )

    async def execute(arguments, context):
        return ToolResult(content=f"echo:{arguments['text']}", details={"count": 1})

    tool = AgentTool(
        ToolSpec(
            name="echo",
            label="Echo",
            description="Echo text",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        ),
        execute,
    )
    async with AgentRuntime() as runtime:
        handle = runtime.run(Agent(name="tools", model=model, tools=(tool,)), "go")
        events = await collect(handle)
        result = await handle.result()

    assert result.output == "done"
    assert len(model.requests) == 2
    assert [message.role for message in result.messages] == ["user", "assistant", "tool", "assistant"]
    assert result.messages[2].content == "echo:hi"
    assert EventType.TOOL_STARTED in [event.type for event in events]
    assert EventType.TOOL_COMPLETED in [event.type for event in events]


@pytest.mark.asyncio
async def test_initial_required_tool_choice_only_applies_to_first_turn():
    model = ScriptedModel(
        [ModelChunk(tool_calls=(ToolCall(id="call-1", name="echo"),))],
        [ModelChunk(text_delta="grounded")],
    )

    async def execute(arguments, context):
        return ToolResult(content="evidence")

    tool = AgentTool(ToolSpec(name="echo", description="Search evidence"), execute)
    async with AgentRuntime() as runtime:
        handle = runtime.run(
            Agent(
                name="research",
                model=model,
                tools=(tool,),
                initial_tool_choice="required",
            ),
            "research this",
        )
        await collect(handle)
        result = await handle.result()

    assert result.output == "grounded"
    assert model.requests[0].tool_choice == "required"
    assert model.requests[1].tool_choice is None


@pytest.mark.asyncio
async def test_named_initial_tool_choice_only_forces_that_first_function():
    model = ScriptedModel(
        [ModelChunk(tool_calls=(ToolCall(id="clock", name="get_time"),))],
        [ModelChunk(text_delta="grounded in time")],
    )

    async def execute(arguments, context):
        return ToolResult(content="2026-07-13")

    tool = AgentTool(ToolSpec(name="get_time", description="Current time"), execute)
    choice = {"type": "function", "function": {"name": "get_time"}}
    async with AgentRuntime() as runtime:
        handle = runtime.run(
            Agent(
                name="temporal research",
                model=model,
                tools=(tool,),
                initial_tool_choice=choice,
            ),
            "latest updates",
        )
        await collect(handle)
        result = await handle.result()

    assert result.output == "grounded in time"
    assert model.requests[0].tool_choice == choice
    assert model.requests[1].tool_choice is None


@pytest.mark.asyncio
async def test_invalid_named_initial_tool_choice_is_rejected_before_run():
    model = ScriptedModel([])

    async def execute(arguments, context):
        return ToolResult(content="ok")

    tool = AgentTool(ToolSpec(name="get_time", description="Current time"), execute)
    async with AgentRuntime() as runtime:
        with pytest.raises(ValueError, match="unknown tool"):
            runtime.run(
                Agent(
                    name="bad tool",
                    model=model,
                    tools=(tool,),
                    initial_tool_choice={"type": "function", "function": {"name": "missing"}},
                ),
                "go",
            )
        with pytest.raises(ValueError, match="string, mapping, or None"):
            runtime.run(
                Agent(
                    name="bad type",
                    model=model,
                    tools=(tool,),
                    initial_tool_choice=42,  # type: ignore[arg-type]
                ),
                "go",
            )


@pytest.mark.asyncio
async def test_tool_failure_is_structured_and_returned_to_model():
    model = ScriptedModel(
        [ModelChunk(tool_calls=(ToolCall(id="call-bad", name="broken"),))],
        [ModelChunk(text_delta="recovered")],
    )

    async def fail(arguments, context):
        raise RuntimeError("service unavailable")

    tool = AgentTool(ToolSpec(name="broken", description="Always fails"), fail)
    async with AgentRuntime() as runtime:
        handle = runtime.run(Agent(name="tools", model=model, tools=(tool,)), "go")
        events = await collect(handle)
        result = await handle.result()

    assert result.status == RunStatus.COMPLETED
    assert result.output == "recovered"
    assert "service unavailable" in model.requests[1].messages[-1].content
    failed = [event for event in events if event.type == EventType.TOOL_FAILED]
    assert failed[0].payload["error"]["code"] == "tool_error"


@pytest.mark.asyncio
async def test_tool_context_exposes_run_scope_and_progress():
    model = ScriptedModel(
        [ModelChunk(tool_calls=(ToolCall(id="call-context", name="inspect"),))],
        [ModelChunk(text_delta="done")],
    )
    seen = {}

    async def execute(arguments, context):
        seen.update(
            run_id=context.run_id,
            tool_call_id=context.tool_call_id,
            data=context.data,
        )
        await context.progress("halfway", {"percent": 50})
        return ToolResult(content="inspected")

    tool = AgentTool(ToolSpec(name="inspect", description="Inspect context"), execute)
    async with AgentRuntime() as runtime:
        handle = runtime.run(
            Agent(name="context", model=model, tools=(tool,)),
            "go",
            context={"tenant": "acme"},
            run_id="run-context",
        )
        events = await collect(handle)
        await handle.result()

    assert seen == {
        "run_id": "run-context",
        "tool_call_id": "call-context",
        "data": {"tenant": "acme"},
    }
    progress = next(event for event in events if event.type == EventType.TOOL_PROGRESS)
    assert progress.payload["message"] == "halfway"
    assert progress.payload["details"] == {"percent": 50}


@pytest.mark.asyncio
async def test_required_tool_approval_can_resume_the_run():
    model = ScriptedModel(
        [ModelChunk(tool_calls=(ToolCall(id="call-write", name="write"),))],
        [ModelChunk(text_delta="approved")],
    )
    executed = False

    async def execute(arguments, context):
        nonlocal executed
        executed = True
        return ToolResult(content="written")

    tool = AgentTool(
        ToolSpec(name="write", description="Write data", requires_approval=True),
        execute,
    )
    async with AgentRuntime() as runtime:
        handle = runtime.run(Agent(name="approval", model=model, tools=(tool,)), "go")
        events = []
        async for event in handle:
            events.append(event)
            if event.type == EventType.TOOL_APPROVAL_REQUIRED:
                assert handle.approve("call-write")
        result = await handle.result()

    assert executed is True
    assert result.output == "approved"
    assert EventType.TOOL_APPROVAL_REQUIRED in [event.type for event in events]


@pytest.mark.asyncio
async def test_approval_is_registered_before_event_listeners_run():
    model = ScriptedModel(
        [ModelChunk(tool_calls=(ToolCall(id="call-write", name="write"),))],
        [ModelChunk(text_delta="approved")],
    )

    async def execute(arguments, context):
        return ToolResult(content="written")

    tool = AgentTool(ToolSpec(name="write", description="Write", requires_approval=True), execute)
    async with AgentRuntime() as runtime:
        handle = None

        def approve_immediately(event):
            if event.type == EventType.TOOL_APPROVAL_REQUIRED:
                assert handle is not None
                assert handle.approve(event.payload["tool_call_id"])

        runtime.subscribe(approve_immediately)
        handle = runtime.run(Agent(name="approval", model=model, tools=(tool,)), "go")
        await collect(handle)
        result = await handle.result()

    assert result.status == RunStatus.COMPLETED
    assert result.output == "approved"


@pytest.mark.asyncio
async def test_rejected_tool_is_not_executed_and_returns_failure_to_model():
    model = ScriptedModel(
        [ModelChunk(tool_calls=(ToolCall(id="call-write", name="write"),))],
        [ModelChunk(text_delta="handled")],
    )
    executed = False

    async def execute(arguments, context):
        nonlocal executed
        executed = True
        return ToolResult(content="written")

    tool = AgentTool(ToolSpec(name="write", description="Write", requires_approval=True), execute)
    async with AgentRuntime() as runtime:
        handle = runtime.run(Agent(name="approval", model=model, tools=(tool,)), "go")
        events = []
        async for event in handle:
            events.append(event)
            if event.type == EventType.TOOL_APPROVAL_REQUIRED:
                assert handle.reject("call-write", "not now")
        result = await handle.result()

    assert executed is False
    assert result.output == "handled"
    failed = next(event for event in events if event.type == EventType.TOOL_FAILED)
    assert failed.payload["error"]["code"] == "tool_rejected"


@pytest.mark.asyncio
async def test_parallel_tool_batch_executes_concurrently():
    model = ScriptedModel(
        [
            ModelChunk(
                tool_calls=(
                    ToolCall(id="call-a", name="a"),
                    ToolCall(id="call-b", name="b"),
                )
            )
        ],
        [ModelChunk(text_delta="both")],
    )
    started: set[str] = set()
    both_started = asyncio.Event()

    def make_tool(name):
        async def execute(arguments, context):
            started.add(name)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.5)
            return ToolResult(content=name)

        return AgentTool(ToolSpec(name=name, description=name), execute)

    async with AgentRuntime() as runtime:
        handle = runtime.run(
            Agent(name="parallel", model=model, tools=(make_tool("a"), make_tool("b"))),
            "go",
        )
        await collect(handle)
        result = await handle.result()

    assert result.output == "both"
    assert started == {"a", "b"}


@pytest.mark.asyncio
async def test_cancel_emits_terminal_event_and_result():
    started = asyncio.Event()

    class BlockingModel:
        async def stream_turn(self, request, cancellation):
            started.set()
            await cancellation.wait()
            cancellation.raise_if_cancelled()
            if False:
                yield ModelChunk()

    async with AgentRuntime() as runtime:
        handle = runtime.run(Agent(name="cancel", model=BlockingModel()), "go")
        await started.wait()
        handle.cancel()
        events = await collect(handle)
        result = await handle.result()

    assert result.status == RunStatus.CANCELLED
    assert events[-1].type == EventType.RUN_CANCELLED


@pytest.mark.asyncio
async def test_finalize_after_turn_limit_disables_tools():
    model = ScriptedModel(
        [ModelChunk(tool_calls=(ToolCall(id="again", name="echo"),))],
        [ModelChunk(text_delta="forced final")],
    )

    async def execute(arguments, context):
        return ToolResult(content="result")

    tool = AgentTool(ToolSpec(name="echo", description="echo"), execute)
    async with AgentRuntime() as runtime:
        handle = runtime.run(
            Agent(
                name="limit",
                model=model,
                tools=(tool,),
                max_turns=1,
                finalize_on_max_turns=True,
            ),
            "go",
        )
        await collect(handle)
        result = await handle.result()

    assert result.output == "forced final"
    assert model.requests[1].tools == ()


@pytest.mark.asyncio
async def test_store_append_failure_still_delivers_one_terminal_result():
    class FailingStore:
        async def create(self, run_id):
            return None

        async def append(self, event):
            raise OSError("disk unavailable")

        async def finish(self, result):
            self.finished = result

        async def events(self, run_id, *, after=0):
            return []

        async def result(self, run_id):
            return None

    runtime = AgentRuntime(RuntimeConfig(store=FailingStore()))
    async with runtime:
        handle = runtime.run(Agent(name="store", model=ScriptedModel([ModelChunk(text_delta="x")])), "go")
        events = await collect(handle)
        result = await handle.result()

    terminals = [
        event for event in events if event.type.value.startswith("run.") and event.type != EventType.RUN_STARTED
    ]
    assert len(terminals) == 1
    assert terminals[0].type == EventType.RUN_FAILED
    assert result.status == RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "run_store_error"


@pytest.mark.asyncio
async def test_store_finish_failure_does_not_orphan_completed_run():
    class FinishFailingStore(MemoryRunStore):
        async def finish(self, result):
            raise OSError("commit failed")

    runtime = AgentRuntime(RuntimeConfig(store=FinishFailingStore()))
    async with runtime:
        handle = runtime.run(Agent(name="store", model=ScriptedModel([ModelChunk(text_delta="ok")])), "go")
        events = await collect(handle)
        result = await handle.result()

    assert events[-1].type == EventType.RUN_COMPLETED
    assert result.status == RunStatus.COMPLETED
    assert "commit failed" in result.metadata["store_errors"][-1]


@pytest.mark.asyncio
async def test_memory_store_evicts_old_completed_runs():
    store = MemoryRunStore(max_runs=1)
    runtime = AgentRuntime(RuntimeConfig(store=store))
    async with runtime:
        first = runtime.run(
            Agent(name="one", model=ScriptedModel([ModelChunk(text_delta="one")])),
            "go",
            run_id="one",
        )
        await collect(first)
        await first.result()
        second = runtime.run(
            Agent(name="two", model=ScriptedModel([ModelChunk(text_delta="two")])),
            "go",
            run_id="two",
        )
        await collect(second)
        await second.result()

    assert await store.events("one") == []
    assert await store.result("one") is None
    assert (await store.result("two")).output == "two"


def test_package_has_no_host_framework_imports():
    root = Path(sag_agent.__file__).parent
    forbidden = {"fastapi", "sqlalchemy", "sag_api", "openai"}
    found: set[str] = set()
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names if alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])
    assert not (found & forbidden)
