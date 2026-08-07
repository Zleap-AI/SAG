"""Host adapters connecting SAG Agent Core to the knowledge application."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import async_sessionmaker

from sag_agent import (
    Agent as RuntimeAgent,
)
from sag_agent import (
    AgentEvent as RuntimeEvent,
)
from sag_agent import (
    AgentRuntime,
    AgentTool,
    EventType,
    ToolExecutionMode,
    ToolRisk,
    ToolSpec,
)
from sag_agent import (
    ToolResult as RuntimeToolResult,
)
from sag_api.enums import MessageStatus
from sag_api.generation import LLMClient, build_prompt_preview
from sag_api.sag import EngineManager, SourceGraphInfo
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

_KNOWLEDGE_TOOLS = ["search_context", "get_entity"]
_WEB_TOOLS = ["web_search", "open_webpage"]
_ALWAYS_TOOLS = ["get_time"]
_TOOL_LABELS = {
    "search_context": "检索知识库",
    "get_entity": "查询实体",
    "get_time": "查询时间",
    "web_search": "联网搜索",
    "open_webpage": "打开网页",
}

_DIRECT_INTENT = re.compile(
    r"^(?:你好(?:呀|啊)?|您好(?:呀|啊)?|嗨|哈[啰罗喽]|hi|hello|hey|"
    r"早上好|下午好|晚上好|在吗|你在吗|谢谢(?:你)?|感谢(?:你)?|多谢|"
    r"再见|拜拜|你是谁(?:呀|啊)?|你叫什么(?:名字)?|who are you|"
    r"what(?:'|’)s your name)[!！?？。,.，\s]*$",
    re.IGNORECASE,
)
_DIRECT_RESPONSE_HINT = re.compile(
    r"(?:翻译|改写|润色|续写|纠错|起名|写一首|写一段|生成文案|头脑风暴)"
    r"|(?:总结|概括)(?:以下|这段|这篇|我提供的)",
    re.IGNORECASE,
)
_CLARIFICATION_FIRST_INTENT = re.compile(
    r"^(?:(?:请|帮我|麻烦你|能否|可以|please)\s*)?"
    r"(?:分析|总结|介绍|推荐|比较|对比|规划|看看|说说|查一下|analyse|analyze|summarize|"
    r"recommend|compare|plan)(?:一下)?[!！?？。,.，\s]*$"
    r"|^(?:最近|现在|目前|这个|那个|昨天|上周|上个月|去年|过去(?:一段时间)?)?"
    r"(?:怎么样|怎么办|如何)[!！?？。,.，\s]*$",
    re.IGNORECASE,
)
_SIMPLE_ARITHMETIC = re.compile(r"^[\d\s+\-*/().=？?]+$")
_TIME_INTENT = re.compile(
    r"(?:现在|今天|当前).{0,6}(?:几点|时间|日期|星期)|(?:时区|timezone|what time|current time)",
    re.IGNORECASE,
)
_RELATIVE_TIME_INTENT = re.compile(
    r"(?:过去|近)\s*(?:[一二三四五六七八九十百两\d]+|几|数)\s*(?:天|日|周|星期|个?月|季度|年)"
    r"|(?:昨天|前天|明天|后天|上周|本周|下周|上个?月|本月|下个?月|去年|今年|明年)"
    r"|\b(?:yesterday|tomorrow)\b"
    r"|\b(?:last|past|next)\s+(?:(?:\d+|one|two|three|several|few)\s+)?"
    r"(?:days?|weeks?|months?|quarters?|years?)\b",
    re.IGNORECASE,
)
_TIME_SENSITIVE_INTENT = re.compile(
    r"(?:最近|最新|近期|当前|现行|实时|截至|今日|今天|本周|本月|今年|刚刚|"
    r"latest|recent|current|today|this (?:week|month|year)|as of)"
    r".{0,40}(?:新闻|更新|版本|发布|价格|行情|天气|趋势|发展|政策|法规|赛程|日程|状态|变化|"
    r"news|updates?|versions?|releases?|price|weather|trends?|policy|schedule|status|changes?)",
    re.IGNORECASE,
)
_EXPLICIT_RESEARCH_INTENT = re.compile(
    r"(?:资料|知识库|文档|来源|引用|证据|数据|统计|研究|报告|调研|查找|搜索|检索|核实|"
    r"sources?|citations?|evidence|data|statistics|research|report|find|search|look up|verify)",
    re.IGNORECASE,
)
_RESEARCH_TOOL = re.compile(
    r"(?:search|find|query|browse|web|news|weather|price|knowledge|document|lookup|read|"
    r"检索|搜索|查询|新闻|天气|行情|知识|文档|网页)",
    re.IGNORECASE,
)
# Numeric knowledge markers are plain ``[n]``. A numeric Markdown link label
# (``[n](https://...)``) is an external link and must not be stripped.
_CITATION_REFERENCE = re.compile(r"\[(\d+)](?!\()")
_ANSWER_URL = re.compile(r"https?://[^\s<>\"'`，。；：！？、（）【】《》]+", re.IGNORECASE)
_MAX_EXTERNAL_CITATIONS = 12
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}，。；：！？、"


def _tool_supports_research(tool: AgentTool) -> bool:
    if tool.spec.name in {"search_context", "get_entity"}:
        return True
    return bool(_RESEARCH_TOOL.search(f"{tool.spec.name} {tool.spec.description}"))


def _named_tool_choice(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name}}


def _initial_tool_choice(
    query: str,
    tools: tuple[AgentTool, ...],
    *,
    knowledge_only: bool,
    scoped: bool,
) -> str | dict[str, Any]:
    """Apply only high-confidence first-turn routing.

    High-confidence social requests, complete arithmetic, and requests that
    require clarification cannot use tools on the first turn. Explicit temporal
    requests establish a dynamic time anchor; knowledge scopes and research
    requests still receive deterministic grounding.
    """

    if not tools:
        return "auto"
    names = {tool.spec.name for tool in tools}
    normalized = " ".join(query.strip().split())
    if _DIRECT_INTENT.fullmatch(normalized):
        return "none"
    if _CLARIFICATION_FIRST_INTENT.fullmatch(normalized) or _SIMPLE_ARITHMETIC.fullmatch(normalized):
        return "none"
    if not normalized or _DIRECT_RESPONSE_HINT.search(normalized):
        return "auto"

    temporal = bool(
        _TIME_INTENT.search(normalized)
        or _RELATIVE_TIME_INTENT.search(normalized)
        or _TIME_SENSITIVE_INTENT.search(normalized)
    )
    if temporal and "get_time" in names:
        return _named_tool_choice("get_time")
    if (knowledge_only or scoped) and "search_context" in names:
        return _named_tool_choice("search_context")
    if _EXPLICIT_RESEARCH_INTENT.search(normalized) and any(_tool_supports_research(tool) for tool in tools):
        return "required"
    if temporal and any(_tool_supports_research(tool) for tool in tools):
        return "required"
    return "auto"


def _append_current_scene(
    messages: list[dict[str, Any]],
    notes: list[str],
) -> list[dict[str, Any]]:
    """Keep dynamic run context inside the single system role."""

    if not notes:
        return list(messages)
    result = [dict(message) for message in messages]
    scene = "【当前场景】\n" + "\n".join(f"- {note}" for note in notes)
    for index, message in enumerate(result):
        if message.get("role") == "system":
            result[index] = {**message, "content": f"{message.get('content', '')}\n\n{scene}"}
            break
    else:
        result.insert(0, {"role": "system", "content": scene})
    return result


def _finalize_answer_citations(
    answer: str,
    citations: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Return one canonical answer whose numeric citations are all clickable.

    Model-invented or non-traceable numbers are removed. When grounded search
    evidence exists but the model forgot claim markers, return a small set of
    explicitly non-claim-mapped structured sources without modifying the answer.
    """

    traceable: dict[int, dict[str, Any]] = {}
    for citation in citations:
        number = citation.get("n")
        if (
            isinstance(number, int)
            and not isinstance(number, bool)
            and citation.get("chunk_id")
            and citation.get("source_id")
        ):
            traceable[number] = citation

    used: list[int] = []

    def replace_reference(match: re.Match[str]) -> str:
        number = int(match.group(1))
        if number not in traceable:
            return ""
        if number not in used:
            used.append(number)
        return match.group(0)

    canonical = _CITATION_REFERENCE.sub(replace_reference, answer).strip()
    canonical = re.sub(r"[ \t]+([，。！？；：,.!?;:])", r"\1", canonical)
    explicitly_mapped = set(used)
    if canonical and traceable and not used:
        used = list(traceable)[:3]
    normalized = []
    for number in used:
        citation = dict(traceable[number])
        mapped = number in explicitly_mapped
        citation.update(
            {
                "kind": "internal",
                "mapped": mapped,
                "claim_level": "claim" if mapped else "run",
            }
        )
        normalized.append(citation)
    return canonical, normalized


def _normalize_external_url(value: Any) -> str | None:
    """Return a safe, canonical HTTP(S) URL, or ``None``.

    External citations are persisted as clickable links, so accepting a broad
    string prefix is not sufficient. Credentials, whitespace, malformed ports,
    and non-web schemes are rejected before anything reaches the API payload.
    Fragments are dropped for stable de-duplication.
    """

    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or any(character.isspace() for character in raw):
        return None
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    netloc = parsed.hostname.lower()
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "", parsed.query, ""))


def _build_external_citations(
    answer: str,
    references: list[dict[str, Any]],
    *,
    start_n: int = 1,
) -> list[dict[str, Any]]:
    """Build bounded, de-duplicated citations from this run's tool artifacts.

    A URL is claim-mapped only when the model placed that observed URL in its
    answer. Other observed sources remain explicitly run-level so the citation
    UI can present them without pretending they support a particular claim.
    """

    answer_urls = {
        normalized
        for match in _ANSWER_URL.findall(answer)
        if (normalized := _normalize_external_url(match.rstrip(_URL_TRAILING_PUNCTUATION)))
    }
    citations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for reference in references:
        url = _normalize_external_url(reference.get("url"))
        if url is None or url in seen:
            continue
        seen.add(url)
        raw_title = reference.get("title")
        title = " ".join(str(raw_title or "").split())[:200]
        raw_source = reference.get("source")
        source = " ".join(str(raw_source or "").split())[:120]
        raw_snippet = next(
            (
                reference.get(key)
                for key in ("snippet", "summary", "description", "content")
                if isinstance(reference.get(key), str) and str(reference.get(key)).strip()
            ),
            "",
        )
        snippet = " ".join(str(raw_snippet or "").split())[:720]
        summary = snippet[:160].strip()
        if len(snippet) > 160:
            sentence_end = next(
                (index + 1 for index, character in enumerate(summary) if index >= 5 and character in "。！？.!?"),
                0,
            )
            if sentence_end:
                summary = summary[:sentence_end].strip()
            elif not summary.endswith(("。", "！", "？", ".", "!", "?")):
                summary = summary.rstrip("…") + "…"
        hostname = urlsplit(url).hostname or ""
        mapped = url in answer_urls
        citation = {
            "kind": "external",
            "n": start_n + len(citations),
            "url": url,
            "title": title or hostname or url,
            "source": source or hostname,
            "mapped": mapped,
            "claim_level": "claim" if mapped else "run",
        }
        if snippet:
            citation["summary"] = summary
            citation["snippet"] = snippet
        citations.append(citation)
        if len(citations) >= _MAX_EXTERNAL_CITATIONS:
            break
    return citations


@dataclass(frozen=True, slots=True)
class AgentStreamEvent:
    """Versioned runtime event ready for an HTTP/WebSocket transport."""

    type: str
    data: dict[str, Any]


def _enabled_tool_names(agent, *, has_sources: bool = False, knowledge_only: bool = False) -> list[str]:
    persona = agent.persona or {}
    names = persona.get("tools")
    configured = [name for name in names if isinstance(name, str)] if isinstance(names, list) else []
    # 检索是信源挂载带来的基础能力，不应依赖 persona 中是否碰巧保存了 tools 字段。
    knowledge_tools = _KNOWLEDGE_TOOLS if has_sources or getattr(agent, "is_default", False) else []
    if knowledge_only:
        # Keep local, read-only system utilities available while excluding
        # configured/MCP tools and model-knowledge fallbacks.
        return list(dict.fromkeys([*_ALWAYS_TOOLS, *knowledge_tools]))
    return list(dict.fromkeys([*_ALWAYS_TOOLS, *knowledge_tools, *_WEB_TOOLS, *configured]))


def _adapt_tool(host_tool, host_context: HostToolContext, citations: list[dict]) -> AgentTool:
    async def execute(
        arguments: Mapping[str, Any],
        context,
    ) -> RuntimeToolResult:
        context.cancellation.raise_if_cancelled()
        host_context.citation_offset = len(citations)
        result = await host_tool.invoke(dict(arguments), host_context)
        citations.extend(result.citations)
        count = int(result.data.get("section_count") or len(result.citations) or 0)
        raw_external_references = result.data.get("external_references")
        external_references = (
            [dict(reference) for reference in raw_external_references if isinstance(reference, Mapping)]
            if isinstance(raw_external_references, list)
            else []
        )
        matches = [
            {
                key: citation.get(key)
                for key in (
                    "n",
                    "chunk_id",
                    "heading",
                    "snippet",
                    "score",
                    "source_id",
                    "source_name",
                    "event_refs",
                )
            }
            for citation in result.citations[:6]
        ]
        details: dict[str, Any] = {"count": count}
        if host_tool.meta.name in _KNOWLEDGE_TOOLS:
            details["scope"] = "knowledge"
            details["sources"] = [
                {"id": source.id, "name": source.name} for source in host_context.sources
            ]
        elif host_tool.meta.name in _WEB_TOOLS:
            details["scope"] = "internet"
        if matches:
            details["matches"] = matches
        elif result.content:
            details["output_preview"] = result.content[:800]
        artifacts: dict[str, Any] = {"citations": result.citations}
        if external_references:
            details["external_references"] = external_references[:6]
            artifacts["external_references"] = external_references
        sections = result.data.get("sections")
        if host_tool.meta.name == "search_context" and isinstance(sections, list) and sections:
            sources_by_config = {source.sag_source_config_id: source for source in host_context.sources}
            graph_resolved = "_graph" in result.data
            graph = result.data.get("_graph")
            if not graph_resolved:
                graph_for_sections = getattr(host_context.engine_manager, "graph_for_sections", None)
                if callable(graph_for_sections):
                    graph = await graph_for_sections(
                        sections,
                        sources_by_config,
                        event_limit=max(12, len(sections)),
                        entity_limit=36,
                    )
            graph = graph or SourceGraphInfo()
            event_nodes = []
            for item in graph.events:
                source = sources_by_config.get(item.source_config_id)
                event_nodes.append(
                    {
                        "id": item.id,
                        "kind": "event",
                        "source_id": source.id if source else None,
                        "label": item.title,
                        "description": item.summary,
                        "category": item.category,
                        "chunk_id": item.chunk_id,
                        "importance": item.score,
                        "citation_numbers": [
                            citation.get("n")
                            for citation in result.citations
                            if citation.get("chunk_id") == item.chunk_id
                            and citation.get("source_id") == (source.id if source else None)
                        ],
                    }
                )
            event_source_by_id = {str(event["id"]): str(event["source_id"] or "") for event in event_nodes}
            artifacts["universe_activation"] = {
                "query": str(arguments.get("query") or ""),
                "nodes": [
                    *event_nodes,
                    *[
                        {
                            "id": item.id,
                            "kind": "entity",
                            "source_id": next(
                                (
                                    event["source_id"]
                                    for event in event_nodes
                                    if any(
                                        association.event_id == event["id"] and association.entity_id == item.id
                                        for association in graph.associations
                                    )
                                ),
                                None,
                            ),
                            "label": item.name,
                            "description": item.description,
                            "category": item.type,
                            "importance": min(1.0, 0.2 + item.heat / 10.0),
                        }
                        for item in graph.entities
                    ],
                ],
                "relations": [
                    {
                        "source_id": event_source_by_id.get(item.event_id, ""),
                        "from_id": item.event_id,
                        "to_id": item.entity_id,
                        "kind": "mentions",
                        "weight": item.weight,
                        "description": item.description,
                    }
                    for item in graph.associations
                ],
            }
        return RuntimeToolResult(
            content=result.content,
            details=details,
            artifacts=artifacts,
        )

    name = host_tool.meta.name
    return AgentTool(
        spec=ToolSpec(
            name=name,
            label=_TOOL_LABELS.get(name, name),
            description=host_tool.meta.description,
            parameters=host_tool.meta.parameters,
            risk=ToolRisk.READ_ONLY,
            # Citation numbering currently depends on source-order execution.
            execution_mode=ToolExecutionMode.SEQUENTIAL,
        ),
        executor=execute,
    )


def _stream_event(event: RuntimeEvent, *, payload: Mapping[str, Any] | None = None) -> AgentStreamEvent:
    data = event.to_dict()
    if payload is not None:
        data["payload"] = dict(payload)
    return AgentStreamEvent(type=event.type.value, data=data)


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
                elif (
                    event.type == EventType.MESSAGE_DELTA
                    and payload.get("role") == "assistant"
                ):
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
                    status = (
                        MessageStatus.CANCELLED
                        if event.type == EventType.RUN_CANCELLED
                        else MessageStatus.FAILED
                    )
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
