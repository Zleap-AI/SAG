from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from sag_agent.store import MemoryRunStore, RunStore
from sag_agent.tools import AgentTool, ToolContext, ToolRegistry
from sag_agent.types import (
    AgentError,
    AgentEvent,
    AgentMessage,
    CancellationToken,
    EventType,
    ModelProvider,
    ModelRequest,
    RunResult,
    RunStatus,
    RuntimeStatus,
    ToolCall,
    ToolDecision,
    ToolDecisionAction,
    ToolExecutionMode,
    ToolProgress,
    ToolResult,
    Usage,
    normalize_messages,
    utc_now,
)


class AgentRuntimeError(RuntimeError):
    pass


class MaxTurnsExceeded(AgentRuntimeError):
    pass


class RunStoreError(AgentRuntimeError):
    pass


@dataclass(slots=True)
class RunContext:
    run_id: str
    data: Any
    messages: list[AgentMessage]
    cancellation: CancellationToken
    usage: Usage = field(default_factory=Usage)
    metadata: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0


TransformContext = Callable[
    [tuple[AgentMessage, ...], RunContext],
    Sequence[AgentMessage | Mapping[str, Any]] | Awaitable[Sequence[AgentMessage | Mapping[str, Any]]],
]
BeforeToolCall = Callable[
    [ToolCall, AgentTool, RunContext],
    ToolDecision | None | Awaitable[ToolDecision | None],
]
AfterToolCall = Callable[
    [ToolCall, AgentTool, ToolResult | None, AgentError | None, RunContext],
    ToolResult | None | Awaitable[ToolResult | None],
]
EventListener = Callable[[AgentEvent], None | Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Agent:
    name: str
    model: ModelProvider
    instructions: str = ""
    tools: tuple[AgentTool, ...] = ()
    # Hosts may use provider `auto`/`required` or force one named function on
    # the first turn. Later turns return to `auto` so evidence can be used.
    initial_tool_choice: str | Mapping[str, Any] | None = None
    max_turns: int | None = None
    transform_context: TransformContext | None = None
    finalize_on_max_turns: bool = False
    final_instructions: str = (
        "The tool-call limit has been reached. Give the best final answer from the available "
        "tool results. Do not call more tools, and state any missing information clearly."
    )
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    max_turns: int = 6
    tool_timeout_seconds: float = 30.0
    tool_execution: ToolExecutionMode = ToolExecutionMode.PARALLEL
    before_tool_call: BeforeToolCall | None = None
    after_tool_call: AfterToolCall | None = None
    store: RunStore | None = None
    fail_on_store_error: bool = True


@dataclass(slots=True)
class _ToolOutcome:
    call: ToolCall
    tool: AgentTool | None
    result: ToolResult | None = None
    error: AgentError | None = None
    duration_ms: int = 0

    @property
    def model_content(self) -> str:
        if self.result is not None:
            return self.result.content
        assert self.error is not None
        return f"Tool execution failed: {self.error.message}"


class _ApprovalGate:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[tuple[bool, str]]] = {}

    def open(self, call_id: str) -> asyncio.Future[tuple[bool, str]]:
        if call_id in self._pending:
            raise AgentRuntimeError(f"duplicate pending approval: {call_id}")
        future: asyncio.Future[tuple[bool, str]] = asyncio.get_running_loop().create_future()
        self._pending[call_id] = future
        return future

    async def wait(
        self,
        call_id: str,
        future: asyncio.Future[tuple[bool, str]],
    ) -> tuple[bool, str]:
        try:
            return await future
        finally:
            if self._pending.get(call_id) is future:
                self._pending.pop(call_id, None)

    def discard(self, call_id: str) -> None:
        future = self._pending.pop(call_id, None)
        if future is not None and not future.done():
            future.cancel()

    def resolve(self, call_id: str, *, approved: bool, reason: str = "") -> bool:
        future = self._pending.get(call_id)
        if future is None or future.done():
            return False
        future.set_result((approved, reason))
        return True

    def cancel_all(self) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.cancel()


_EVENTS_DONE = object()
_TERMINAL_EVENTS = {
    EventType.RUN_COMPLETED,
    EventType.RUN_FAILED,
    EventType.RUN_CANCELLED,
}
log = logging.getLogger("sag_agent.runtime")


class RunHandle:
    """A running agent operation with one event consumer and explicit control methods."""

    def __init__(self, run_id: str, context: RunContext, gate: _ApprovalGate) -> None:
        self.run_id = run_id
        self.context = context
        self._gate = gate
        self._queue: asyncio.Queue[AgentEvent | object] = asyncio.Queue()
        self._result: asyncio.Future[RunResult] = asyncio.get_running_loop().create_future()
        self._task: asyncio.Task[None] | None = None
        self._status = RunStatus.QUEUED
        self._consumer_started = False

    @property
    def status(self) -> RunStatus:
        return self._status

    @property
    def done(self) -> bool:
        return self._result.done()

    def cancel(self) -> None:
        if self.done:
            return
        self.context.cancellation.cancel()
        self._gate.cancel_all()
        if self._task is not None:
            asyncio.get_running_loop().call_soon(self._task.cancel)

    def approve(self, tool_call_id: str) -> bool:
        approved = self._gate.resolve(tool_call_id, approved=True)
        if approved:
            self._status = RunStatus.RUNNING
        return approved

    def reject(self, tool_call_id: str, reason: str = "Rejected by user") -> bool:
        rejected = self._gate.resolve(tool_call_id, approved=False, reason=reason)
        if rejected:
            self._status = RunStatus.RUNNING
        return rejected

    async def result(self) -> RunResult:
        return await asyncio.shield(self._result)

    async def wait_for_idle(self) -> RunResult:
        return await self.result()

    def __aiter__(self):
        return self.events()

    async def events(self):
        if self._consumer_started:
            raise AgentRuntimeError("RunHandle supports one live event consumer; use RunStore for replay")
        self._consumer_started = True
        while True:
            item = await self._queue.get()
            if item is _EVENTS_DONE:
                return
            assert isinstance(item, AgentEvent)
            yield item

    def _bind_task(self, task: asyncio.Task[None]) -> None:
        self._task = task

    def _push(self, event: AgentEvent) -> None:
        self._queue.put_nowait(event)

    def _finish(self, result: RunResult) -> None:
        self._status = result.status
        if not self._result.done():
            self._result.set_result(result)
        self._queue.put_nowait(_EVENTS_DONE)


class AgentRuntime:
    """Long-lived execution runtime. It is framework and provider agnostic."""

    def __init__(self, config: RuntimeConfig | None = None) -> None:
        self.config = config or RuntimeConfig()
        if self.config.max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        if self.config.tool_timeout_seconds <= 0:
            raise ValueError("tool_timeout_seconds must be positive")
        self.store = self.config.store or MemoryRunStore()
        self.status = RuntimeStatus.CREATED
        self._active: dict[str, RunHandle] = {}
        self._listeners: list[EventListener] = []

    async def __aenter__(self) -> AgentRuntime:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    async def start(self) -> None:
        if self.status == RuntimeStatus.RUNNING:
            return
        if self.status == RuntimeStatus.STOPPING:
            raise AgentRuntimeError("runtime is stopping")
        self.status = RuntimeStatus.RUNNING

    async def stop(self, *, cancel_active: bool = True) -> None:
        if self.status in (RuntimeStatus.CREATED, RuntimeStatus.STOPPED):
            self.status = RuntimeStatus.STOPPED
            return
        self.status = RuntimeStatus.STOPPING
        handles = tuple(self._active.values())
        if cancel_active:
            for handle in handles:
                handle.cancel()
        if handles:
            await asyncio.gather(*(handle.result() for handle in handles), return_exceptions=True)
        self.status = RuntimeStatus.STOPPED

    async def wait_for_idle(self) -> None:
        handles = tuple(self._active.values())
        if handles:
            await asyncio.gather(*(handle.result() for handle in handles))

    def subscribe(self, listener: EventListener) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def get_run(self, run_id: str) -> RunHandle | None:
        return self._active.get(run_id)

    def run(
        self,
        agent: Agent,
        input: str | AgentMessage | Mapping[str, Any] | None = None,
        *,
        history: Sequence[AgentMessage | Mapping[str, Any]] = (),
        context: Any = None,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RunHandle:
        if self.status != RuntimeStatus.RUNNING:
            raise AgentRuntimeError("runtime is not running; call await runtime.start() first")
        if not agent.name.strip():
            raise ValueError("agent name cannot be empty")
        if not isinstance(agent.model, ModelProvider):
            raise TypeError("agent.model must implement ModelProvider.stream_turn")
        registry = ToolRegistry(agent.tools)
        choice = agent.initial_tool_choice
        if isinstance(choice, str):
            if choice not in ("auto", "required", "none"):
                raise ValueError("agent.initial_tool_choice must be auto, required, none, or None")
        elif choice is not None:
            if not isinstance(choice, Mapping):
                raise ValueError("agent.initial_tool_choice must be a string, mapping, or None")
            function = choice.get("function")
            name = function.get("name") if isinstance(function, Mapping) else None
            if choice.get("type") != "function" or not isinstance(name, str) or not name.strip():
                raise ValueError("named initial_tool_choice must select a function name")
            if registry.get(name) is None:
                raise ValueError(f"initial_tool_choice references unknown tool: {name}")

        messages = normalize_messages(history)
        if agent.instructions:
            messages.insert(0, AgentMessage(role="system", content=agent.instructions))
        if input is not None:
            if isinstance(input, str):
                messages.append(AgentMessage(role="user", content=input))
            elif isinstance(input, AgentMessage):
                messages.append(input)
            else:
                messages.append(AgentMessage.from_dict(input))
        if not messages or messages[-1].role not in ("user", "tool"):
            raise ValueError("a run must end with a user or tool message")

        rid = run_id or uuid.uuid4().hex
        if rid in self._active:
            raise ValueError(f"run already active: {rid}")
        cancellation = CancellationToken()
        run_context = RunContext(
            run_id=rid,
            data=context,
            messages=messages,
            cancellation=cancellation,
            metadata={**dict(agent.metadata), **dict(metadata or {})},
        )
        gate = _ApprovalGate()
        handle = RunHandle(rid, run_context, gate)
        self._active[rid] = handle
        task = asyncio.create_task(
            self._drive(agent, handle, ToolRegistry(agent.tools)),
            name=f"agent-run-{rid[:12]}",
        )
        handle._bind_task(task)
        return handle

    async def _drive(self, agent: Agent, handle: RunHandle, tools: ToolRegistry) -> None:
        ctx = handle.context
        result: RunResult
        try:
            try:
                await self.store.create(ctx.run_id)
            except Exception as exc:
                raise RunStoreError(f"failed to create run in store: {exc}") from exc
            await self._emit(handle, EventType.RUN_STARTED, payload={"agent": agent.name})
            output = await self._execute_loop(agent, handle, tools)
            result = RunResult(
                run_id=ctx.run_id,
                status=RunStatus.COMPLETED,
                output=output,
                messages=tuple(ctx.messages),
                usage=ctx.usage,
                metadata=dict(ctx.metadata),
            )
            await self._emit(
                handle,
                EventType.RUN_COMPLETED,
                payload={
                    "output": output,
                    "usage": ctx.usage.to_dict(),
                    "store_errors": tuple(ctx.metadata.get("store_errors", ())),
                },
                tolerate_store_error=True,
            )
        except asyncio.CancelledError:
            ctx.cancellation.cancel()
            result = RunResult(
                run_id=ctx.run_id,
                status=RunStatus.CANCELLED,
                output="",
                messages=tuple(ctx.messages),
                usage=ctx.usage,
                error=AgentError(code="cancelled", message="Run cancelled"),
                metadata=dict(ctx.metadata),
            )
            await self._emit(
                handle,
                EventType.RUN_CANCELLED,
                payload={"error": result.error.to_dict()},
                tolerate_store_error=True,
            )
        except Exception as exc:  # noqa: BLE001
            error = AgentError(
                code=(
                    "max_turns_exceeded"
                    if isinstance(exc, MaxTurnsExceeded)
                    else "run_store_error"
                    if isinstance(exc, RunStoreError)
                    else "run_failed"
                ),
                message=str(exc) or exc.__class__.__name__,
                retryable=not isinstance(exc, (ValueError, TypeError, MaxTurnsExceeded)),
            )
            result = RunResult(
                run_id=ctx.run_id,
                status=RunStatus.FAILED,
                output="",
                messages=tuple(ctx.messages),
                usage=ctx.usage,
                error=error,
                metadata=dict(ctx.metadata),
            )
            await self._emit(
                handle,
                EventType.RUN_FAILED,
                payload={"error": error.to_dict()},
                tolerate_store_error=True,
            )
        finally:
            result = replace(result, metadata=dict(ctx.metadata))
            try:
                await self.store.finish(result)
            except Exception as exc:
                message = f"failed to finish run in store: {exc}"
                ctx.metadata.setdefault("store_errors", []).append(message)
                result = replace(result, metadata=dict(ctx.metadata))
                log.exception("%s", message)
            finally:
                handle._finish(result)
                self._active.pop(ctx.run_id, None)

    async def _execute_loop(self, agent: Agent, handle: RunHandle, tools: ToolRegistry) -> str:
        max_turns = agent.max_turns if agent.max_turns is not None else self.config.max_turns
        if max_turns < 1:
            raise ValueError("agent.max_turns must be at least 1")

        for turn in range(1, max_turns + 1):
            assistant, duration_ms = await self._model_turn(agent, handle, tools, turn)
            if not assistant.tool_calls:
                await self._emit(
                    handle,
                    EventType.TURN_COMPLETED,
                    turn=turn,
                    payload={"tool_call_ids": [], "duration_ms": duration_ms},
                )
                return str(assistant.content or "")

            outcomes = await self._execute_tool_batch(handle, tools, assistant.tool_calls, turn)
            for outcome in outcomes:
                message = AgentMessage(
                    role="tool",
                    content=outcome.model_content,
                    tool_call_id=outcome.call.id,
                    name=outcome.call.name,
                    metadata={
                        "is_error": outcome.error is not None,
                        "details": dict(outcome.result.details) if outcome.result else {},
                    },
                )
                await self._emit(
                    handle,
                    EventType.MESSAGE_STARTED,
                    turn=turn,
                    payload={"role": "tool", "tool_call_id": outcome.call.id},
                )
                handle.context.messages.append(message)
                await self._emit(
                    handle,
                    EventType.MESSAGE_COMPLETED,
                    turn=turn,
                    payload={"message": message.to_model_dict(), "is_error": outcome.error is not None},
                )
            await self._emit(
                handle,
                EventType.TURN_COMPLETED,
                turn=turn,
                payload={
                    "tool_call_ids": [outcome.call.id for outcome in outcomes],
                    "duration_ms": duration_ms + sum(outcome.duration_ms for outcome in outcomes),
                },
            )
            if any(outcome.result and outcome.result.terminate for outcome in outcomes):
                return str(assistant.content or "") or "\n".join(outcome.model_content for outcome in outcomes)

        if agent.finalize_on_max_turns:
            handle.context.messages.append(AgentMessage(role="system", content=agent.final_instructions))
            assistant, duration_ms = await self._model_turn(
                agent,
                handle,
                ToolRegistry(),
                max_turns + 1,
            )
            await self._emit(
                handle,
                EventType.TURN_COMPLETED,
                turn=max_turns + 1,
                payload={"tool_call_ids": [], "duration_ms": duration_ms, "forced_final": True},
            )
            return str(assistant.content or "")
        raise MaxTurnsExceeded(f"agent exceeded the maximum of {max_turns} turns")

    async def _model_turn(
        self,
        agent: Agent,
        handle: RunHandle,
        tools: ToolRegistry,
        turn: int,
    ) -> tuple[AgentMessage, int]:
        ctx = handle.context
        ctx.cancellation.raise_if_cancelled()
        model_messages: Sequence[AgentMessage | Mapping[str, Any]] = tuple(ctx.messages)
        if agent.transform_context is not None:
            transformed = agent.transform_context(tuple(ctx.messages), ctx)
            model_messages = await transformed if inspect.isawaitable(transformed) else transformed
        prepared = tuple(normalize_messages(model_messages))
        request = ModelRequest(
            messages=prepared,
            tools=tools.schemas(),
            tool_choice=(agent.initial_tool_choice if turn == 1 and tools.all() else None),
            run_id=ctx.run_id,
            turn=turn,
            metadata=dict(ctx.metadata),
        )
        await self._emit(
            handle,
            EventType.TURN_STARTED,
            turn=turn,
            payload={"message_count": len(prepared), "tool_count": len(request.tools)},
        )
        await self._emit(
            handle,
            EventType.MESSAGE_STARTED,
            turn=turn,
            payload={"role": "assistant"},
        )

        started = time.perf_counter()
        content: list[str] = []
        calls: dict[str, ToolCall] = {}
        ctx.usage = ctx.usage.plus(Usage(requests=1))
        async for chunk in agent.model.stream_turn(request, ctx.cancellation):
            ctx.cancellation.raise_if_cancelled()
            if chunk.usage is not None:
                ctx.usage = ctx.usage.plus(chunk.usage)
            if chunk.text_delta:
                content.append(chunk.text_delta)
                await self._emit(
                    handle,
                    EventType.MESSAGE_DELTA,
                    turn=turn,
                    payload={"role": "assistant", "delta": chunk.text_delta},
                )
            for call in chunk.tool_calls:
                call_id = call.id or f"call-{turn}-{len(calls) + 1}"
                calls[call_id] = call if call.id else replace(call, id=call_id)

        duration_ms = int((time.perf_counter() - started) * 1000)
        assistant = AgentMessage(
            role="assistant",
            content="".join(content),
            tool_calls=tuple(calls.values()),
        )
        ctx.messages.append(assistant)
        await self._emit(
            handle,
            EventType.MESSAGE_COMPLETED,
            turn=turn,
            payload={
                "message": assistant.to_model_dict(),
                "duration_ms": duration_ms,
                "has_tool_calls": bool(assistant.tool_calls),
            },
        )
        return assistant, duration_ms

    async def _execute_tool_batch(
        self,
        handle: RunHandle,
        registry: ToolRegistry,
        calls: tuple[ToolCall, ...],
        turn: int,
    ) -> list[_ToolOutcome]:
        prepared: list[tuple[ToolCall, AgentTool, Mapping[str, Any]] | _ToolOutcome] = []
        for call in calls:
            value = await self._preflight_tool(handle, registry, call, turn)
            prepared.append(value)

        executable = [value for value in prepared if isinstance(value, tuple)]
        sequential = self.config.tool_execution == ToolExecutionMode.SEQUENTIAL or any(
            tool.spec.execution_mode == ToolExecutionMode.SEQUENTIAL for _, tool, _ in executable
        )
        executed: dict[str, _ToolOutcome] = {}
        if sequential:
            for call, tool, arguments in executable:
                executed[call.id] = await self._execute_tool(handle, call, tool, arguments, turn)
        else:
            tasks = [
                asyncio.create_task(
                    self._execute_tool(handle, call, tool, arguments, turn),
                    name=f"agent-tool-{call.id[:12]}",
                )
                for call, tool, arguments in executable
            ]
            results = await asyncio.gather(*tasks)
            executed.update((outcome.call.id, outcome) for outcome in results)

        outcomes: list[_ToolOutcome] = []
        for value in prepared:
            if isinstance(value, _ToolOutcome):
                outcomes.append(value)
            else:
                outcomes.append(executed[value[0].id])
        return outcomes

    async def _preflight_tool(
        self,
        handle: RunHandle,
        registry: ToolRegistry,
        call: ToolCall,
        turn: int,
    ) -> tuple[ToolCall, AgentTool, Mapping[str, Any]] | _ToolOutcome:
        tool = registry.get(call.name)
        if tool is None:
            return await self._tool_failure(
                handle,
                call,
                None,
                AgentError(code="unknown_tool", message=f"Unknown tool: {call.name}"),
                turn,
            )
        if call.parse_error:
            return await self._tool_failure(
                handle,
                call,
                tool,
                AgentError(code="invalid_tool_arguments", message=call.parse_error),
                turn,
            )
        try:
            arguments = tool.validate(call.arguments)
        except Exception as exc:  # noqa: BLE001
            return await self._tool_failure(
                handle,
                call,
                tool,
                AgentError(code="invalid_tool_arguments", message=str(exc)),
                turn,
            )

        decision = None
        if self.config.before_tool_call is not None:
            value = self.config.before_tool_call(call, tool, handle.context)
            decision = await value if inspect.isawaitable(value) else value
        if decision is None:
            decision = ToolDecision.require_approval() if tool.spec.requires_approval else ToolDecision.allow()
        if decision.action == ToolDecisionAction.DENY:
            return await self._tool_failure(
                handle,
                call,
                tool,
                AgentError(code="tool_denied", message=decision.reason or "Tool call denied"),
                turn,
            )
        if decision.action == ToolDecisionAction.REQUIRE_APPROVAL:
            approval = handle._gate.open(call.id)
            try:
                await self._emit(
                    handle,
                    EventType.TOOL_APPROVAL_REQUIRED,
                    turn=turn,
                    payload={
                        "tool_call_id": call.id,
                        "name": call.name,
                        "label": tool.spec.label or call.name,
                        "arguments": dict(arguments),
                        "risk": tool.spec.risk.value,
                        "reason": decision.reason,
                    },
                )
                approved, reason = await handle._gate.wait(call.id, approval)
            except BaseException:
                handle._gate.discard(call.id)
                raise
            if not approved:
                return await self._tool_failure(
                    handle,
                    call,
                    tool,
                    AgentError(code="tool_rejected", message=reason or "Tool call rejected"),
                    turn,
                )

        await self._emit(
            handle,
            EventType.TOOL_STARTED,
            turn=turn,
            payload={
                "tool_call_id": call.id,
                "name": call.name,
                "label": tool.spec.label or call.name,
                "arguments": dict(arguments),
                "risk": tool.spec.risk.value,
            },
        )
        return call, tool, arguments

    async def _execute_tool(
        self,
        handle: RunHandle,
        call: ToolCall,
        tool: AgentTool,
        arguments: Mapping[str, Any],
        turn: int,
    ) -> _ToolOutcome:
        started = time.perf_counter()

        async def on_progress(progress: ToolProgress) -> None:
            handle.context.cancellation.raise_if_cancelled()
            await self._emit(
                handle,
                EventType.TOOL_PROGRESS,
                turn=turn,
                payload={
                    "tool_call_id": call.id,
                    "name": call.name,
                    "message": progress.message,
                    "details": dict(progress.details),
                },
            )

        try:
            timeout = tool.spec.timeout_seconds or self.config.tool_timeout_seconds
            result = await asyncio.wait_for(
                tool.execute(
                    arguments,
                    ToolContext(
                        run_id=handle.run_id,
                        tool_call_id=call.id,
                        data=handle.context.data,
                        cancellation=handle.context.cancellation,
                        _on_progress=on_progress,
                    ),
                ),
                timeout=timeout,
            )
            if self.config.after_tool_call is not None:
                value = self.config.after_tool_call(call, tool, result, None, handle.context)
                replacement = await value if inspect.isawaitable(value) else value
                if replacement is not None:
                    result = replacement
            duration_ms = int((time.perf_counter() - started) * 1000)
            await self._emit(
                handle,
                EventType.TOOL_COMPLETED,
                turn=turn,
                payload={
                    "tool_call_id": call.id,
                    "name": call.name,
                    "duration_ms": duration_ms,
                    "details": dict(result.details),
                    "artifacts": dict(result.artifacts),
                    "terminate": result.terminate,
                },
            )
            return _ToolOutcome(call=call, tool=tool, result=result, duration_ms=duration_ms)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            error = AgentError(
                code="tool_timeout",
                message=f"Tool {call.name} timed out",
                retryable=True,
            )
        except Exception as exc:  # noqa: BLE001
            error = AgentError(code="tool_error", message=str(exc) or exc.__class__.__name__)

        if self.config.after_tool_call is not None:
            value = self.config.after_tool_call(call, tool, None, error, handle.context)
            replacement = await value if inspect.isawaitable(value) else value
            if replacement is not None:
                duration_ms = int((time.perf_counter() - started) * 1000)
                await self._emit(
                    handle,
                    EventType.TOOL_COMPLETED,
                    turn=turn,
                    payload={
                        "tool_call_id": call.id,
                        "name": call.name,
                        "duration_ms": duration_ms,
                        "details": dict(replacement.details),
                        "artifacts": dict(replacement.artifacts),
                        "terminate": replacement.terminate,
                    },
                )
                return _ToolOutcome(call=call, tool=tool, result=replacement, duration_ms=duration_ms)
        return await self._tool_failure(handle, call, tool, error, turn, started=started)

    async def _tool_failure(
        self,
        handle: RunHandle,
        call: ToolCall,
        tool: AgentTool | None,
        error: AgentError,
        turn: int,
        *,
        started: float | None = None,
    ) -> _ToolOutcome:
        duration_ms = int((time.perf_counter() - started) * 1000) if started is not None else 0
        await self._emit(
            handle,
            EventType.TOOL_FAILED,
            turn=turn,
            payload={
                "tool_call_id": call.id,
                "name": call.name,
                "label": tool.spec.label if tool else call.name,
                "duration_ms": duration_ms,
                "error": error.to_dict(),
            },
        )
        return _ToolOutcome(call=call, tool=tool, error=error, duration_ms=duration_ms)

    async def _emit(
        self,
        handle: RunHandle,
        event_type: EventType,
        *,
        turn: int = 0,
        payload: Mapping[str, Any] | None = None,
        tolerate_store_error: bool = False,
    ) -> AgentEvent:
        ctx = handle.context
        ctx.sequence += 1
        event = AgentEvent(
            type=event_type,
            run_id=ctx.run_id,
            sequence=ctx.sequence,
            timestamp=utc_now(),
            turn=turn,
            payload=dict(payload or {}),
        )
        if event_type == EventType.RUN_STARTED:
            handle._status = RunStatus.RUNNING
        elif event_type == EventType.TOOL_APPROVAL_REQUIRED:
            handle._status = RunStatus.WAITING_APPROVAL
        store_error: Exception | None = None
        try:
            await self.store.append(event)
        except Exception as exc:  # noqa: BLE001
            store_error = exc
            ctx.metadata.setdefault("store_errors", []).append(f"failed to append {event.type.value}: {exc}")
        handle._push(event)
        for listener in tuple(self._listeners):
            try:
                value = listener(event)
                if inspect.isawaitable(value):
                    await value
            except Exception as exc:  # noqa: BLE001
                ctx.metadata.setdefault("listener_errors", []).append(str(exc))
        if (
            store_error is not None
            and self.config.fail_on_store_error
            and not tolerate_store_error
            and event_type not in _TERMINAL_EVENTS
        ):
            raise RunStoreError(f"failed to append {event.type.value}: {store_error}") from store_error
        return event
