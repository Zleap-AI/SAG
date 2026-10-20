from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sag_agent.types import (
    CancellationToken,
    ToolExecutionMode,
    ToolProgress,
    ToolResult,
    ToolRisk,
    ToolSpec,
)

ProgressCallback = Callable[[ToolProgress], Awaitable[None]]
ToolExecutor = Callable[
    [Mapping[str, Any], "ToolContext"],
    Awaitable[ToolResult] | ToolResult,
]
ToolValidator = Callable[[Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Execution-scoped capabilities provided to a tool by the runtime."""

    run_id: str
    tool_call_id: str
    data: Any
    cancellation: CancellationToken
    _on_progress: ProgressCallback
    _on_executor_task: Callable[[asyncio.Task[Any] | None], None] | None = None

    async def progress(
        self,
        message: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.cancellation.raise_if_cancelled()
        await self._on_progress(ToolProgress(message=message, details=dict(details or {})))


@dataclass(frozen=True, slots=True)
class AgentTool:
    """Provider-neutral tool definition and executor."""

    spec: ToolSpec
    executor: ToolExecutor
    validator: ToolValidator | None = None

    def validate(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.validator is None:
            return dict(arguments)
        return self.validator(arguments)

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        value = self.executor(arguments, context)
        if isinstance(value, asyncio.Task) and context._on_executor_task is not None:
            # A synchronous executor may return a Task with its own cleanup.
            # Track that task only while awaiting it; observers remain separate.
            context._on_executor_task(value)
            try:
                value = await value
            finally:
                context._on_executor_task(None)
        elif inspect.isawaitable(value):
            value = await value
        if not isinstance(value, ToolResult):
            raise TypeError(f"tool {self.spec.name!r} must return ToolResult")
        return value


class ToolRegistry:
    def __init__(self, tools: Sequence[AgentTool] = ()) -> None:
        self._tools: dict[str, AgentTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: AgentTool) -> None:
        name = tool.spec.name.strip()
        if not name:
            raise ValueError("tool name cannot be empty")
        if name in self._tools:
            raise ValueError(f"duplicate tool name: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> AgentTool | None:
        return self._tools.get(name)

    def require(self, name: str) -> AgentTool:
        tool = self.get(name)
        if tool is None:
            raise KeyError(f"unknown tool: {name}")
        return tool

    def all(self) -> tuple[AgentTool, ...]:
        return tuple(self._tools.values())

    def schemas(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(tool.spec.to_model_schema() for tool in self._tools.values())


def function_tool(
    *,
    name: str,
    description: str,
    parameters: Mapping[str, Any],
    label: str | None = None,
    risk: ToolRisk = ToolRisk.READ_ONLY,
    requires_approval: bool = False,
    execution_mode: ToolExecutionMode | None = None,
    timeout_seconds: float | None = None,
    validator: ToolValidator | None = None,
) -> Callable[[ToolExecutor], AgentTool]:
    """Decorator for small integrations that already have a JSON schema."""

    def wrap(executor: ToolExecutor) -> AgentTool:
        return AgentTool(
            spec=ToolSpec(
                name=name,
                label=label,
                description=description,
                parameters=parameters,
                risk=risk,
                requires_approval=requires_approval,
                execution_mode=execution_mode,
                timeout_seconds=timeout_seconds,
            ),
            executor=executor,
            validator=validator,
        )

    return wrap
