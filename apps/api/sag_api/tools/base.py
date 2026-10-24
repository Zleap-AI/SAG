"""工具抽象 —— Agent 可挂载的能力单元。

设计对齐 `connectors/`：一个「工具」自描述（name + JSON-Schema 参数），
经注册表登记；Agent 循环把工具 schema 交给 LLM（native function-calling），
再把 LLM 的 tool_call 派发到对应工具执行。检索只是内置的其中一个工具，
外部 MCP 工具适配成同一接口后与内置工具对 Agent 完全一致。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sag_api.db.models import Agent, Source
    from sag_api.sag import EngineManager


@dataclass
class ToolMeta:
    """工具自描述。`parameters` 为 JSON-Schema（object），供模型 function-calling。"""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolContext:
    """工具执行所需的运行时上下文（由 Agent 循环注入，类比 job handler 的单例注入）。"""

    engine_manager: EngineManager
    sources: list[Source] = field(default_factory=list)
    persona: dict[str, Any] = field(default_factory=dict)
    agent: Agent | None = None
    # 本轮显式选择的检索模式；优先于人格与信源默认。
    requested_strategy: str | None = None
    # 全局证据编号偏移：循环在每次派发前设置，保证 [n] 跨轮递增不重号
    citation_offset: int = 0


@dataclass
class ToolResult:
    """工具执行结果。`content` 回填给模型；`citations` 供 UI 溯源；`data` 结构化附带。"""

    content: str
    citations: list[dict] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


class Tool(ABC):
    """所有工具的基类。新增工具 = 继承 + 实现 invoke + 在 registry 注册。"""

    meta: ToolMeta

    @abstractmethod
    async def invoke(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行工具。参数已由模型给出（或自动播种），返回可回填的结果。"""
        raise NotImplementedError
