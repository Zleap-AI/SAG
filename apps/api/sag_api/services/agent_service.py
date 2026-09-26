"""Host adapters connecting SAG Agent Core to the knowledge application.

测试会替换本模块的模块级绑定（`resolve_mcp_specs`、`open_agent_mcp_tools`、
`WebSearchTool`），这些名字只在 `generate_stream` 内被引用，因此它保留在此；
工具适配、引用组装与意图判定已外迁至 `agent_tooling` 并在下方重新导出，
对外接口保持不变。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from sag_agent import (
    Agent as RuntimeAgent,
)
from sag_agent import (
    AgentRuntime,
    EventType,
)
from sag_api.enums import MessageStatus
from sag_api.generation import LLMClient, build_prompt_preview
from sag_api.sag import EngineManager
from sag_api.services.agent_domain import (
    AskPlan,
    persist_answer,
    resolve_mcp_specs,
    resolve_sources,
)
from sag_api.tools import ToolContext as HostToolContext
from sag_api.tools import ToolRegistry
from sag_api.tools.builtin import WebSearchTool
from sag_api.tools.mcp import open_agent_mcp_tools

AGENT_MAX_STEPS = 4


async def generate_stream(
    session_factory: async_sessionmaker,
    *,
    plan: AskPlan,
    agent,
    thread_id: str | None,
    engine_manager: EngineManager,
    llm: LLMClient,
    tool_registry: ToolRegistry,
    runtime: AgentRuntime | None = None,
    knowledge_only: bool = False,
) -> AsyncIterator[AgentStreamEvent]:
    """Run one request and expose the SDK event contract to the host transport."""

    from sag_api.core.config import settings

    owns_runtime = runtime is None
    active_runtime = runtime or AgentRuntime()
    if owns_runtime:
        await active_runtime.start()

    citations = list(plan.citations)
    external_references: list[dict[str, Any]] = []
    external_reference_urls: set[str] = set()
    trace: list[dict] = []
    tool_inputs: dict[str, dict[str, Any]] = {}
    partial_answer: list[str] = []  # 失败/取消时保留已流出的正文
    handle = None
    terminal = False

    async with session_factory() as session:
        sources = await resolve_sources(session, agent, plan.source_ids)
        mcp_specs = [] if knowledge_only else await resolve_mcp_specs(session, agent)
    host_context = HostToolContext(
        engine_manager=engine_manager,
        sources=sources,
        persona=agent.persona or {},
        agent=agent,
    )

    try:
        async with open_agent_mcp_tools(mcp_specs) as mcp_bundle:
            names = _enabled_tool_names(
                agent,
                has_sources=bool(sources),
                knowledge_only=knowledge_only,
            )
            host_tools = [tool_registry.get(name) for name in names if tool_registry.has(name)]
            host_tools.extend(mcp_bundle.tools)
            tools = tuple(_adapt_tool(tool, host_context, citations) for tool in host_tools)
            scene_notes: list[str] = []
            if mcp_bundle.warnings:
                unavailable = "、".join(warning.get("server", "MCP") for warning in mcp_bundle.warnings)
                scene_notes.append(
                    f"部分挂载工具本轮不可用：{unavailable}。若当前任务依赖这些能力，"
                    "必须明确说明暂时无法核验，不得用模型记忆替代实时或外部事实。"
                )
            if knowledge_only:
                offline_rule = (
                    "本轮联网已关闭，只能使用已挂载的本地知识库和必要系统工具；"
                    "不得调用或声称使用网页、MCP 或其他外部搜索。联网关闭不代表每轮都要检索；"
                    "仅当回答依赖知识性事实时，必须先调用 search_context，只根据工具返回的原文证据"
                    "回答并保留引用；"
                    "证据不足时明确说明知识库中没有足够依据，不得使用模型自身知识补充。"
                )
                if "search_context" not in names:
                    offline_rule = (
                        "本轮联网已关闭，且当前 Agent 没有可检索的本地知识库；只允许使用必要系统工具。"
                        "不得调用或声称使用网页、MCP、其他外部搜索或模型自身知识来补充知识性事实；"
                        "问题依赖外部或知识库资料时，应明确说明当前没有可用依据。"
                    )
                scene_notes.append(offline_rule)
            elif WebSearchTool.configured():
                scene_notes.append(
                    "本轮联网已开启。凡回答依赖实时、最新或外部事实，必须调用 web_search 获取网页证据，"
                    "并在结论附近保留可点击的 Markdown 来源链接；search_context 只用于用户的本地知识库，"
                    "不得用它代替互联网搜索。需要最新信息时，查询中应包含绝对日期，并将 time_range 设为 "
                    "day 或 week；搜索摘要不足以核验精确结论时，必须用 open_webpage 打开最相关的可信来源。"
                    "web_search 或 open_webpage 已成功返回结果后，不得声称无法联网、无法访问实时信息或无法访问网页；"
                    "如果结果不够新或不足以支持结论，只能明确说明本次搜索没有找到足以核验的结果，并说明证据日期，"
                    "不得把证据不足描述成系统能力不足。"
                )
            if plan.source_ids and sources:
                scene_notes.append(
                    "用户已通过 @ 将本轮知识范围限定为："
                    + "、".join(source.name for source in sources)
                    + "。问题涉及资料时必须先调用 search_context，并只依据返回证据作答。"
                )
            run_messages = _append_current_scene(list(plan.messages), scene_notes)
            # Freeze the actual initial input before the runtime appends model
            # output and tool-result messages. This is the only content the UI
            # may describe as the model's starting context.
            frozen_prompt_preview = build_prompt_preview(run_messages)
            initial_tool_choice = _initial_tool_choice(
                plan.query,
                tools,
                knowledge_only=knowledge_only,
                scoped=bool(plan.source_ids),
            )
            max_turns = max(1, int(getattr(settings, "agent_max_steps", AGENT_MAX_STEPS)))
            definition = RuntimeAgent(
                name=agent.name,
                model=llm,
                tools=tools,
                initial_tool_choice=initial_tool_choice,
                max_turns=max_turns,
                finalize_on_max_turns=True,
                metadata={
                    "agent_id": agent.id,
                    "initial_tool_choice": initial_tool_choice,
                    "web_enabled": not knowledge_only,
                    "knowledge_only": knowledge_only,
                },
            )
            handle = active_runtime.run(
                definition,
                history=run_messages,
                context=host_context,
                metadata={
                    "thread_id": thread_id,
                    "source_ids": [source.id for source in sources],
                    "source_names": [source.name for source in sources],
                    "web_enabled": not knowledge_only,
                    "knowledge_only": knowledge_only,
                },
            )

            async for event in handle:
                payload = event.payload
                output_payload: Mapping[str, Any] = payload

                if event.type == EventType.RUN_STARTED:
                    output_payload = {
                        **payload,
                        "user_message_id": plan.user_message_id,
                        "citations": citations,
                        "sources": [{"id": source.id, "name": source.name} for source in sources],
                        "tools": [tool.spec.name for tool in tools],
                        "tool_warnings": mcp_bundle.warnings,
                        "web_enabled": not knowledge_only,
                        "knowledge_only": knowledge_only,
                    }
                elif event.type in (
                    EventType.TOOL_APPROVAL_REQUIRED,
                    EventType.TOOL_STARTED,
                ):
                    tool_inputs[str(payload.get("tool_call_id") or "")] = {
                        "label": payload.get("label") or payload.get("name"),
                        "arguments": dict(payload.get("arguments") or {}),
                    }
                elif event.type == EventType.TOOL_COMPLETED:
                    details = payload.get("details") or {}
                    artifacts = payload.get("artifacts") or {}
                    observed_references = artifacts.get("external_references")
                    if isinstance(observed_references, list):
                        for reference in observed_references:
                            if not isinstance(reference, Mapping):
                                continue
                            url = reference.get("url")
                            if not isinstance(url, str) or url in external_reference_urls:
                                continue
                            external_reference_urls.add(url)
                            external_references.append(dict(reference))
                    activation = artifacts.get("universe_activation")
                    if isinstance(activation, Mapping):
                        activation_event = event.to_dict()
                        activation_event["type"] = "universe.activation"
                        activation_event["payload"] = dict(activation)
                        yield AgentStreamEvent(
                            type="universe.activation",
                            data=activation_event,
                        )
                    tool_call_id = str(payload.get("tool_call_id") or "")
                    started = tool_inputs.pop(tool_call_id, {})
                    trace.append(
                        {
                            "kind": "tool",
                            "step": event.turn,
                            "name": payload["name"],
                            "label": started.get("label") or payload.get("name"),
                            "arguments": started.get("arguments") or {},
                            "ms": payload.get("duration_ms", 0),
                            "count": details.get("count", 0),
                            "details": details,
                        }
                    )
                elif event.type == EventType.TOOL_FAILED:
                    error = payload.get("error") or {}
                    tool_call_id = str(payload.get("tool_call_id") or "")
                    started = tool_inputs.pop(tool_call_id, {})
                    trace.append(
                        {
                            "kind": "tool",
                            "step": event.turn,
                            "name": payload["name"],
                            "label": started.get("label") or payload.get("label") or payload.get("name"),
                            "arguments": started.get("arguments") or {},
                            "ms": payload.get("duration_ms", 0),
                            "count": 0,
                            "error": error.get("message", "工具执行失败"),
                        }
                    )
                elif event.type == EventType.MESSAGE_DELTA and payload.get("role") == "assistant":
                    delta = payload.get("delta")
                    if isinstance(delta, str):
                        partial_answer.append(delta)
                elif (
                    event.type == EventType.MESSAGE_COMPLETED and payload.get("message", {}).get("role") == "assistant"
                ):
                    duration = int(payload.get("duration_ms") or 0)
                    if payload.get("has_tool_calls"):
                        trace.append({"kind": "thinking", "step": event.turn, "ms": duration})
                    else:
                        trace.append({"kind": "answer", "step": event.turn, "ms": duration})
                elif event.type == EventType.RUN_COMPLETED:
                    canonical_answer, internal_citations = _finalize_answer_citations(
                        str(payload.get("output") or ""),
                        citations,
                    )
                    external_start = (
                        max(
                            (
                                citation["n"]
                                for citation in internal_citations
                                if isinstance(citation.get("n"), int) and not isinstance(citation.get("n"), bool)
                            ),
                            default=0,
                        )
                        + 1
                    )
                    external_citations = _build_external_citations(
                        canonical_answer,
                        external_references,
                        start_n=external_start,
                    )
                    canonical_citations = [*internal_citations, *external_citations]
                    message_id = None
                    if thread_id is not None:
                        message_id = await persist_answer(
                            session_factory,
                            thread_id,
                            canonical_answer,
                            canonical_citations,
                            steps=trace,
                            prompt_preview=frozen_prompt_preview,
                        )
                    output_payload = {
                        **payload,
                        "output": canonical_answer,
                        "message_id": message_id,
                        "citations": canonical_citations,
                        "prompt_preview": frozen_prompt_preview,
                    }
                    terminal = True
                elif event.type in (EventType.RUN_FAILED, EventType.RUN_CANCELLED):
                    # 失败/取消也写入 assistant 消息，保留会话错误历史（含已流出的 partial answer）。
                    partial_text = "".join(partial_answer)
                    canonical_answer, internal_citations = _finalize_answer_citations(
                        partial_text,
                        citations,
                    )
                    error_payload = payload.get("error") if isinstance(payload, Mapping) else None
                    status = MessageStatus.CANCELLED if event.type == EventType.RUN_CANCELLED else MessageStatus.FAILED
                    message_id = None
                    if thread_id is not None:
                        message_id = await persist_answer(
                            session_factory,
                            thread_id,
                            canonical_answer,
                            internal_citations,
                            steps=trace,
                            prompt_preview=frozen_prompt_preview,
                            status=status,
                            error=dict(error_payload) if isinstance(error_payload, Mapping) else None,
                        )
                    output_payload = {
                        **payload,
                        "message_id": message_id,
                        "partial_output": canonical_answer,
                        "prompt_preview": frozen_prompt_preview,
                    }
                    terminal = True

                yield _stream_event(event, payload=output_payload)
    finally:
        if handle is not None and not terminal and not handle.done:
            handle.cancel()
            await handle.result()
        if owns_runtime:
            await active_runtime.stop()


# --- 已外迁至 agent_tooling，在此重新导出，对外接口不变 ---
from sag_api.services.agent_tooling import (  # noqa: E402
    AgentStreamEvent,
    _adapt_tool,
    _append_current_scene,
    _build_external_citations,
    _enabled_tool_names,
    _finalize_answer_citations,
    _initial_tool_choice,
    _stream_event,
)

__all__ = [
    "AgentStreamEvent",
    "generate_stream",
]
