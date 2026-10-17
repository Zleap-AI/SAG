from __future__ import annotations

import asyncio
import socket

import pytest

from sag_agent import (
    Agent,
    AgentRuntime,
    AgentTool,
    EventType,
    MemoryRunStore,
    ModelChunk,
    RunStatus,
    RuntimeConfig,
    ToolCall,
    ToolExecutionMode,
    ToolResult,
    ToolSpec,
)

_TERMINAL_EVENTS = {
    EventType.RUN_COMPLETED,
    EventType.RUN_FAILED,
    EventType.RUN_CANCELLED,
}


class _ToolModel:
    def __init__(self, *names: str) -> None:
        self.calls = tuple(ToolCall(id=name, name=name) for name in names)

    async def stream_turn(self, _request, cancellation):
        cancellation.raise_if_cancelled()
        if _request.messages[-1].role == "tool":
            yield ModelChunk(text_delta="done")
            return
        yield ModelChunk(tool_calls=self.calls)


class _CountingStore(MemoryRunStore):
    def __init__(self) -> None:
        super().__init__()
        self.finished = []

    async def finish(self, result):
        self.finished.append(result)
        await super().finish(result)


class _SocketTool:
    """Keep real resources alive until asynchronous cleanup is allowed to finish."""

    def __init__(self, name: str, *, slow_cleanup: bool = True) -> None:
        self.name = name
        self.entered = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_allowed = asyncio.Event()
        self.cleanup_finished = asyncio.Event()
        self.blocked = asyncio.Event()
        self.sockets: tuple[socket.socket, socket.socket] | None = None
        self.task: asyncio.Task | None = None
        self.interrupted = False
        if not slow_cleanup:
            self.cleanup_allowed.set()

    def as_tool(self, *, timeout: float | None = None) -> AgentTool:
        return AgentTool(
            ToolSpec(name=self.name, description="Hold sockets", timeout_seconds=timeout),
            self.execute,
        )

    async def execute(self, _arguments, _context):
        self.task = asyncio.current_task()
        self.sockets = socket.socketpair()
        try:
            self.entered.set()
            await self.blocked.wait()
            return ToolResult(content="released")
        finally:
            self.cleanup_started.set()
            try:
                await self.cleanup_allowed.wait()
                for item in self.sockets:
                    item.close()
            except asyncio.CancelledError:
                self.interrupted = True
                raise
            finally:
                self.cleanup_finished.set()

    def assert_closed(self) -> None:
        assert self.sockets is not None
        assert [item.fileno() for item in self.sockets] == [-1, -1]
        assert not self.interrupted
        assert self.task is not None and self.task.done()


async def _checkpoint() -> None:
    """Give already queued callbacks a turn without waiting for elapsed time."""
    future = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(future.set_result, None)
    await future


async def _settle_callbacks() -> None:
    # Cancellation crosses the run, batch wrapper, and executor task boundaries.
    # The resource assertions below also detect any interruption after this check.
    for _ in range(4):
        await _checkpoint()


async def _cleanup(runtime, tools, before) -> None:
    """Release barriers and reclaim test resources even when the regression is RED."""
    for tool in tools:
        tool.cleanup_allowed.set()
        tool.blocked.set()
    await _settle_callbacks()
    try:
        await asyncio.wait_for(runtime.stop(), timeout=2)
    finally:
        remaining = asyncio.all_tasks() - before
        for task in remaining:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
        for tool in tools:
            if tool.sockets is not None:
                for item in tool.sockets:
                    item.close()


def _assert_pending(handle, events, store) -> None:
    assert not handle.done, "run became terminal before its tool cleanup finished"
    assert not any(event.type in _TERMINAL_EVENTS for event in events)
    assert store.finished == []


async def _assert_cancelled_once(handle, events, store) -> None:
    result = await asyncio.wait_for(handle.result(), timeout=2)
    assert result.status == RunStatus.CANCELLED
    assert [event.type for event in events if event.type in _TERMINAL_EVENTS] == [
        EventType.RUN_CANCELLED,
    ]
    assert store.finished == [result]
    assert await store.result(handle.run_id) == result


@pytest.mark.asyncio
async def test_parallel_cancel_waits_for_async_cleanup_without_cancelling_it_again():
    before = asyncio.all_tasks()
    fast = _SocketTool("fast", slow_cleanup=False)
    slow = _SocketTool("slow")
    store = _CountingStore()
    runtime = AgentRuntime(RuntimeConfig(store=store))
    events = []
    runtime.subscribe(events.append)
    await runtime.start()
    try:
        handle = runtime.run(
            Agent(name="parallel", model=_ToolModel("fast", "slow"), tools=(fast.as_tool(), slow.as_tool())),
            "go",
        )
        await asyncio.wait_for(asyncio.gather(fast.entered.wait(), slow.entered.wait()), timeout=2)
        handle.cancel()
        await asyncio.wait_for(
            asyncio.gather(fast.cleanup_finished.wait(), slow.cleanup_started.wait()),
            timeout=2,
        )
        await _settle_callbacks()
        _assert_pending(handle, events, store)

        slow.cleanup_allowed.set()
        await _assert_cancelled_once(handle, events, store)
        fast.assert_closed()
        slow.assert_closed()
    finally:
        await _cleanup(runtime, (fast, slow), before)


@pytest.mark.asyncio
async def test_repeated_cancel_and_stop_join_the_same_async_tool_cleanup():
    before = asyncio.all_tasks()
    slow = _SocketTool("slow")
    store = _CountingStore()
    runtime = AgentRuntime(RuntimeConfig(store=store))
    events = []
    runtime.subscribe(events.append)
    await runtime.start()
    try:
        handle = runtime.run(
            Agent(name="cancel", model=_ToolModel("slow"), tools=(slow.as_tool(),)),
            "go",
        )
        await asyncio.wait_for(slow.entered.wait(), timeout=2)
        handle.cancel()
        await asyncio.wait_for(slow.cleanup_started.wait(), timeout=2)
        handle.cancel()
        handle.cancel()
        stopping = asyncio.create_task(runtime.stop())
        await _settle_callbacks()
        _assert_pending(handle, events, store)
        assert not stopping.done(), "runtime.stop returned while tool cleanup was pending"

        slow.cleanup_allowed.set()
        await _assert_cancelled_once(handle, events, store)
        await asyncio.wait_for(stopping, timeout=2)
        slow.assert_closed()
    finally:
        await _cleanup(runtime, (slow,), before)


@pytest.mark.asyncio
@pytest.mark.parametrize("late_cancel", ["none", "handle", "stop"])
async def test_store_failure_waits_for_peer_cleanup_and_preserves_the_original_error(late_cancel):
    before = asyncio.all_tasks()
    slow = _SocketTool("slow")

    class FailingStore(_CountingStore):
        async def append(self, event):
            if event.type == EventType.TOOL_FAILED:
                raise OSError("event store unavailable")
            await super().append(event)

    async def fail(_arguments, _context):
        await slow.entered.wait()
        raise ValueError("invalid tool request")

    store = FailingStore()
    runtime = AgentRuntime(RuntimeConfig(store=store))
    events = []
    runtime.subscribe(events.append)
    await runtime.start()
    try:
        handle = runtime.run(
            Agent(
                name="store failure",
                model=_ToolModel("fail", "slow"),
                tools=(AgentTool(ToolSpec(name="fail", description="Fail"), fail), slow.as_tool()),
            ),
            "go",
        )
        await asyncio.wait_for(slow.cleanup_started.wait(), timeout=2)
        stopping = None
        if late_cancel == "handle":
            handle.cancel()
        elif late_cancel == "stop":
            stopping = asyncio.create_task(runtime.stop())
        await _settle_callbacks()
        _assert_pending(handle, events, store)
        if stopping is not None:
            assert not stopping.done()

        slow.cleanup_allowed.set()
        result = await asyncio.wait_for(handle.result(), timeout=2)
        assert result.status == RunStatus.FAILED
        assert result.error is not None and result.error.code == "run_store_error"
        assert "event store unavailable" in result.error.message
        assert [event.type for event in events if event.type in _TERMINAL_EVENTS] == [
            EventType.RUN_FAILED,
        ]
        assert store.finished == [result]
        if stopping is not None:
            await asyncio.wait_for(stopping, timeout=2)
        slow.assert_closed()
    finally:
        await _cleanup(runtime, (slow,), before)


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_mode", [ToolExecutionMode.PARALLEL, ToolExecutionMode.SEQUENTIAL])
async def test_cancel_during_tool_timeout_cleanup_does_not_interrupt_resource_release(execution_mode):
    before = asyncio.all_tasks()
    slow = _SocketTool("slow")
    store = _CountingStore()
    runtime = AgentRuntime(RuntimeConfig(store=store, tool_execution=execution_mode))
    events = []
    runtime.subscribe(events.append)
    await runtime.start()
    try:
        handle = runtime.run(
            Agent(name="timeout", model=_ToolModel("slow"), tools=(slow.as_tool(timeout=0.01),)),
            "go",
        )
        await asyncio.wait_for(slow.cleanup_started.wait(), timeout=2)
        handle.cancel()
        await _settle_callbacks()
        _assert_pending(handle, events, store)

        slow.cleanup_allowed.set()
        await _assert_cancelled_once(handle, events, store)
        slow.assert_closed()
    finally:
        await _cleanup(runtime, (slow,), before)


@pytest.mark.asyncio
async def test_self_cancelled_tool_waits_for_peer_resource_cleanup():
    before = asyncio.all_tasks()
    slow = _SocketTool("slow")
    store = _CountingStore()

    async def cancel_self(_arguments, _context):
        await slow.entered.wait()
        raise asyncio.CancelledError

    runtime = AgentRuntime(RuntimeConfig(store=store))
    events = []
    runtime.subscribe(events.append)
    await runtime.start()
    try:
        handle = runtime.run(
            Agent(
                name="child cancellation",
                model=_ToolModel("cancel", "slow"),
                tools=(
                    AgentTool(ToolSpec(name="cancel", description="Cancel self"), cancel_self),
                    slow.as_tool(),
                ),
            ),
            "go",
        )
        await asyncio.wait_for(slow.cleanup_started.wait(), timeout=2)
        await _settle_callbacks()
        _assert_pending(handle, events, store)

        slow.cleanup_allowed.set()
        await _assert_cancelled_once(handle, events, store)
        slow.assert_closed()
    finally:
        await _cleanup(runtime, (slow,), before)


@pytest.mark.asyncio
async def test_expired_tool_timeout_does_not_start_the_executor():
    executed = []

    async def execute(_arguments, _context):
        executed.append(True)
        return ToolResult(content="side effect")

    events = []
    async with AgentRuntime() as runtime:
        runtime.subscribe(events.append)
        handle = runtime.run(
            Agent(
                name="expired timeout",
                model=_ToolModel("expired"),
                tools=(AgentTool(ToolSpec(name="expired", description="Expired", timeout_seconds=-1), execute),),
            ),
            "go",
        )
        result = await handle.result()

    assert result.status == RunStatus.COMPLETED
    assert executed == []
    failures = [event for event in events if event.type == EventType.TOOL_FAILED]
    assert len(failures) == 1
    assert failures[0].payload["error"]["code"] == "tool_timeout"
