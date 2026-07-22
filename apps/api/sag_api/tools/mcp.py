"""MCP 客户端适配 —— 把远端 MCP 工具适配成 sag 的 `Tool`。

Agent 循环对「内置工具」与「远端 MCP 工具」一视同仁：本模块把一个远端 MCP
server 暴露的每个工具包成 `MCPTool`（命名空间前缀避免撞名），其 `invoke` 转成
一次 `session.call_tool`。连接生命周期由 `open_agent_mcp_tools` 的异步上下文托管
——在工具循环期间保持打开，循环结束即断开。传输（stdio / Streamable-HTTP）在
`_open_session` 内按绑定 config 选择，`MCPTool` 本身与传输无关（接一个就绪的
`ClientSession` 即可，便于用进程内内存传输做离线测试）。
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from sag_api.core.logging import get_logger
from sag_api.tools.base import Tool, ToolContext, ToolMeta, ToolResult

log = get_logger("mcp.client")


class MCPToolExecutionError(RuntimeError):
    """A remote MCP tool returned an application-level error result."""

    code = "mcp_tool_error"

    def __init__(self, *, tool_name: str, message: str) -> None:
        self.tool_name = tool_name
        self.remote_message = message
        super().__init__(f"MCP tool {tool_name!r} failed: {message}")

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "tool_name": self.tool_name,
            "message": self.remote_message,
        }


@dataclass(slots=True)
class MCPToolsBundle:
    """MCP tools plus non-sensitive connection warnings for the current run."""

    tools: list[MCPTool] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)


_URL_PATTERN = re.compile(r"https?://[^\s<>\"'`，。；：！？、（）【】《》]+", re.IGNORECASE)
_JSON_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_URL_KEYS = frozenset(
    {
        "url",
        "uri",
        "href",
        "link",
        "source_url",
        "sourceurl",
        "external_url",
        "externalurl",
    }
)
_TITLE_KEYS = ("title", "name", "label", "headline")
_SOURCE_KEYS = ("source", "publisher", "site", "domain", "provider")
_SNIPPET_KEYS = ("snippet", "description", "summary", "excerpt", "content")
_SNIPPET_MAX_LENGTH = 320
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}，。；：！？、"


def _namespace(label: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in (label or "mcp")).strip("_")
    return (safe or "mcp")[:32]


def _content_to_text(result: Any) -> str:
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts).strip() or "（无返回）"


def _plain_value(value: Any) -> Any:
    """Convert Pydantic-like structured content into JSON-shaped values."""
    if isinstance(value, (dict, list, tuple, str, int, float, bool)) or value is None:
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(by_alias=True)
    return value


def _clean_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.rstrip(_URL_TRAILING_PUNCTUATION)
    if not url or any(character.isspace() for character in url):
        return None
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return url


def _string_field(node: dict[str, Any], keys: tuple[str, ...]) -> str:
    lower = {str(key).lower().replace("-", "_"): value for key, value in node.items()}
    for key in keys:
        value = lower.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _short_snippet(value: Any) -> str:
    """Return a bounded human-readable excerpt, never a structured payload dump."""
    if not isinstance(value, str):
        return ""
    snippet = " ".join(value.split())
    if not snippet:
        return ""

    # Some search MCPs put their complete JSON response in a field named
    # ``content``.  That payload is transport data, not a citation excerpt.
    json_candidate = snippet
    fenced = _JSON_FENCE_PATTERN.fullmatch(snippet)
    if fenced:
        json_candidate = fenced.group(1).strip()
    try:
        parsed = json.loads(json_candidate)
    except (TypeError, ValueError):
        pass
    else:
        if isinstance(parsed, (dict, list)):
            return ""

    if len(snippet) <= _SNIPPET_MAX_LENGTH:
        return snippet
    return snippet[: _SNIPPET_MAX_LENGTH - 1].rstrip() + "…"


def _external_references(result: Any) -> list[dict[str, str]]:
    """Collect de-duplicated web references from MCP structured/text output."""
    references: dict[str, dict[str, str]] = {}

    def add(raw_url: Any, *, title: str = "", source: str = "", snippet: str = "") -> None:
        url = _clean_url(raw_url)
        if not url:
            return
        parsed = urlsplit(url)
        fallback_source = parsed.hostname or parsed.netloc
        current = references.setdefault(
            url,
            {
                "url": url,
                "title": title or fallback_source or url,
                "source": source or fallback_source,
            },
        )
        if title and (not current["title"] or current["title"] in {fallback_source, url}):
            current["title"] = title
        if source and (not current["source"] or current["source"] == fallback_source):
            current["source"] = source
        clean_snippet = _short_snippet(snippet)
        if clean_snippet and not current.get("snippet"):
            current["snippet"] = clean_snippet

    def walk(value: Any) -> None:
        value = _plain_value(value)
        if isinstance(value, dict):
            title = _string_field(value, _TITLE_KEYS)
            source = _string_field(value, _SOURCE_KEYS)
            snippet = _string_field(value, _SNIPPET_KEYS)
            for key, child in value.items():
                normalized_key = str(key).lower().replace("-", "_")
                if normalized_key in _URL_KEYS:
                    add(child, title=title, source=source, snippet=snippet)
                    continue
                # Metadata strings may contain URLs or even a serialized copy
                # of the complete tool payload.  They describe the current
                # result and must not recursively become unrelated citations.
                if isinstance(child, str) and normalized_key in {
                    *_TITLE_KEYS,
                    *_SOURCE_KEYS,
                    *_SNIPPET_KEYS,
                }:
                    continue
                walk(child)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
            return
        if isinstance(value, str):
            for match in _URL_PATTERN.findall(value):
                add(match)

    structured_values = [
        getattr(result, "structuredContent", None),
        getattr(result, "structured_content", None),
    ]
    for structured in structured_values:
        if structured is not None:
            walk(structured)

    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str) or not text.strip():
            continue
        stripped_text = text.strip()
        full_text_is_json = False
        candidates = [stripped_text, *_JSON_FENCE_PATTERN.findall(text)]
        for index, candidate in enumerate(candidates):
            try:
                parsed = json.loads(candidate)
            except (TypeError, ValueError):
                continue
            if index == 0 and isinstance(parsed, (dict, list)):
                full_text_is_json = True
            walk(parsed)
        if not full_text_is_json:
            # JSON fences were already processed structurally.  Excluding them
            # from the raw URL scan prevents links embedded in serialized
            # snippets from leaking out as standalone references.
            walk(_JSON_FENCE_PATTERN.sub("", text))
    return list(references.values())


class MCPTool(Tool):
    """把某个远端 MCP 工具适配成 sag 的 `Tool`。"""

    def __init__(
        self,
        session: ClientSession,
        *,
        remote_name: str,
        local_name: str,
        description: str,
        parameters: dict[str, Any],
    ) -> None:
        self._session = session
        self._remote_name = remote_name
        self.meta = ToolMeta(name=local_name, description=description, parameters=parameters)

    async def invoke(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = await self._session.call_tool(self._remote_name, args or {})
        text = _content_to_text(result)
        if getattr(result, "isError", False) or getattr(result, "is_error", False):
            raise MCPToolExecutionError(tool_name=self._remote_name, message=text)
        return ToolResult(content=text, data={"external_references": _external_references(result)})


async def tools_from_session(session: ClientSession, *, namespace: str) -> list[MCPTool]:
    """列出远端工具并逐个适配（本地名 = mcp__<namespace>__<remote>）。"""
    listed = await session.list_tools()
    tools: list[MCPTool] = []
    for t in listed.tools:
        params = t.inputSchema or {"type": "object", "properties": {}}
        tools.append(
            MCPTool(
                session,
                remote_name=t.name,
                local_name=f"mcp__{namespace}__{t.name}",
                description=t.description or f"远端 MCP 工具 {t.name}",
                parameters=params,
            )
        )
    return tools


@contextlib.asynccontextmanager
async def _open_session(config: dict) -> AsyncIterator[ClientSession]:
    """按 config 选择传输并建立就绪的 ClientSession（url → HTTP；command → stdio）。"""
    url = config.get("url")
    command = config.get("command")
    if url:
        headers = config.get("headers") or None
        async with streamablehttp_client(url, headers=headers) as (read, write, _sid):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    elif command:
        params = StdioServerParameters(
            command=command,
            args=list(config.get("args") or []),
            env=config.get("env") or None,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:
        raise ValueError("MCP 绑定缺少 url 或 command")


@contextlib.asynccontextmanager
async def open_agent_mcp_tools(specs: list[tuple[str, dict]]) -> AsyncIterator[MCPToolsBundle]:
    """打开 MCP 连接，产出工具和可安全展示的连接告警；退出即断开连接。

    `specs`：`[(label, config), …]`。单个 server 连接失败不会影响其余连接；完整
    异常仅进入服务端日志，bundle 中的 warning 不包含 URL、请求头或异常详情。
    """
    tools: list[MCPTool] = []
    warnings: list[dict[str, str]] = []
    async with AsyncExitStack() as stack:
        for label, config in specs:
            try:
                session = await stack.enter_async_context(_open_session(config))
                tools.extend(await tools_from_session(session, namespace=_namespace(label)))
            except Exception:  # noqa: BLE001
                log.warning("MCP 连接失败 %s", label, exc_info=True)
                warnings.append(
                    {
                        "code": "mcp_connection_failed",
                        "server": _namespace(label),
                        "message": "MCP 服务连接失败，本轮已跳过该服务。",
                    }
                )
        yield MCPToolsBundle(tools=tools, warnings=warnings)
