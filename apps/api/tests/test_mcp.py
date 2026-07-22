"""MCP 三件事，全程离线：

1. 信源即 MCP —— 经进程内内存客户端列出并调用 search/get_entity/get_chunk（空库 → 结构化结果）。
2. 远端 MCP 工具适配成 sag 的 `Tool`（命名空间前缀 + call_tool 往返）。
3. 绑定与描述端点 —— agent 挂载外部 MCP 的校验、信源的 MCP 连接描述。
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session as connect

from sag_api.mcp.server import MCP_TOOL_DETAILS, MCP_TOOL_NAMES, build_source_mcp, use_scope
from sag_api.tools import mcp as mcp_module
from sag_api.tools import registry
from sag_api.tools.base import Tool, ToolContext, ToolMeta, ToolResult
from sag_api.tools.mcp import (
    MCPTool,
    MCPToolExecutionError,
    _clean_url,
    open_agent_mcp_tools,
    tools_from_session,
)


async def _register(c, email):
    r = await c.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_source_mcp_lists_and_calls_tools_over_engine():
    """知识库 MCP server：真实引擎 + 全库作用域，探索与检索工具均可调用。"""
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Source
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            A = await _register(c, "mcpsrv@t.com")
            src = (await c.post("/api/v1/sources", headers=A, json={"name": "MCP 源"})).json()
            src2 = (await c.post("/api/v1/sources", headers=A, json={"name": "第二个 MCP 源"})).json()
            async with SessionLocal() as s:
                sources = tuple(
                    (
                        await s.execute(
                            select(Source)
                            .where(Source.id.in_([src["id"], src2["id"]]))
                            .order_by(Source.created_at, Source.id)
                        )
                    )
                    .scalars()
                    .all()
                )

            mcp = build_source_mcp()
            # 作用域须在 connect（起服务任务）之前设置，任务会复制含作用域的上下文
            with use_scope(app.state.engine_manager, sources):
                async with connect(mcp) as client:
                    await client.initialize()
                    listed = await client.list_tools()
                    tools_by_name = {tool.name: tool for tool in listed.tools}
                    names = set(tools_by_name)
                    assert {
                        "search",
                        "get_entity",
                        "get_chunk",
                        "list_sources",
                        "list_documents",
                        "outline",
                        "grep",
                        "read",
                    } <= names
                    for detail in MCP_TOOL_DETAILS:
                        tool = tools_by_name[detail["name"]]
                        assert tool.title == detail["label"]
                        assert tool.description == detail["description"]
                        assert tool.annotations is not None
                        assert tool.annotations.readOnlyHint is True
                        assert tool.annotations.destructiveHint is False
                    search_properties = tools_by_name["search"].inputSchema["properties"]
                    assert search_properties["query"]["description"]
                    assert search_properties["source_id"]["description"]

                    r_sources = await client.call_tool("list_sources", {})
                    assert "MCP 源" in r_sources.content[0].text
                    assert "第二个 MCP 源" in r_sources.content[0].text

                    # 探索原语（离线）：先上传一个 md
                    up = await c.post(
                        f"/api/v1/sources/{src['id']}/documents",
                        headers=A,
                        files={"file": ("probe.md", b"# Title\n\nhello mcp world", "text/markdown")},
                    )
                    doc = up.json()
                    r_ls = await client.call_tool("list_documents", {})
                    assert "probe.md" in r_ls.content[0].text
                    r_read = await client.call_tool("read", {"document_id": doc["id"]})
                    assert "hello mcp world" in r_read.content[0].text
                    r_out = await client.call_tool("outline", {"document_id": doc["id"]})
                    assert isinstance(r_out.content[0].text, str)  # 处理中→占位文案亦可
                    r_grep = await client.call_tool("grep", {"pattern": "不存在的串xyz", "source_id": src["id"]})
                    assert "未匹配" in r_grep.content[0].text or "chunk_id" in r_grep.content[0].text

                    r_chunk = await client.call_tool(
                        "get_chunk", {"chunk_id": "does-not-exist", "source_id": src["id"]}
                    )
                    assert not r_chunk.isError
                    assert "未找到" in r_chunk.content[0].text

                    r_entity = await client.call_tool("get_entity", {"name": "查无此实体", "source_id": src["id"]})
                    assert not r_entity.isError
                    assert "未找到" in r_entity.content[0].text

                    # 检索走真实引擎（离线下 SAG 需 LLM 抽取实体 → 结构化报错）；
                    # 关键是工具正确派发并返回结构化 MCP 响应，不使 server 崩溃
                    r_search = await client.call_tool("search", {"query": "任意问题", "source_id": src["id"]})
                    assert r_search.content and isinstance(r_search.content[0].text, str)


@pytest.mark.asyncio
async def test_remote_mcp_tool_adapted_as_sag_tool():
    """远端 MCP 工具 → MCPTool：命名空间前缀 + invoke 往返回文本。"""
    stub = FastMCP("stub")

    @stub.tool(description="回显输入")
    async def echo(text: str) -> str:
        return f"echo:{text}"

    async with connect(stub) as client:
        await client.initialize()
        tools = await tools_from_session(client, namespace="stub")
        assert len(tools) == 1
        tool = tools[0]
        assert tool.meta.name == "mcp__stub__echo"
        assert tool.meta.parameters.get("type") == "object"
        result = await tool.invoke({"text": "hi"}, ToolContext(engine_manager=None))
        assert result.content == "echo:hi"
        assert result.data == {"external_references": []}


class _StubCallSession:
    def __init__(self, result):
        self.result = result

    async def call_tool(self, name, args):
        return self.result


def _adapt_stub_result(result) -> MCPTool:
    return MCPTool(
        _StubCallSession(result),
        remote_name="lookup",
        local_name="mcp__stub__lookup",
        description="stub",
        parameters={"type": "object", "properties": {}},
    )


def test_external_reference_url_validation_rejects_unsafe_authority_and_whitespace():
    unsafe_urls = (
        " https://example.com/leading-space",
        "https://example.com/path with space",
        "https://user:secret@example.com/private",
        "https://example.com:not-a-port/result",
        "https:///missing-host",
        "ftp://example.com/file",
    )

    assert all(_clean_url(url) is None for url in unsafe_urls)
    safe_url = "https://example.com:8443/report?q=agent#section"
    assert _clean_url(safe_url) == safe_url


@pytest.mark.asyncio
async def test_remote_mcp_error_result_raises_structured_exception():
    """MCP isError 必须进入运行时失败分支，不能伪装成成功的文本结果。"""
    tool = _adapt_stub_result(
        SimpleNamespace(
            isError=True,
            content=[SimpleNamespace(text="upstream rejected the query")],
        )
    )

    with pytest.raises(MCPToolExecutionError) as raised:
        await tool.invoke({}, ToolContext(engine_manager=None))

    assert raised.value.to_dict() == {
        "code": "mcp_tool_error",
        "tool_name": "lookup",
        "message": "upstream rejected the query",
    }
    assert "lookup" in str(raised.value)


@pytest.mark.asyncio
async def test_remote_mcp_extracts_and_deduplicates_external_references():
    """structured content、文本 JSON 与普通 URL 都能形成可渲染外部来源。"""
    tool = _adapt_stub_result(
        SimpleNamespace(
            isError=False,
            structuredContent={
                "results": [
                    {
                        "url": "https://news.example/a",
                        "title": "Alpha report",
                        "source": "Example News",
                        "snippet": "  Alpha   launch\n details.  ",
                    }
                ]
            },
            structured_content={
                "items": [
                    {
                        "href": "https://docs.example/b",
                        "name": "Beta docs",
                        "publisher": "Example Docs",
                        "description": "Beta documentation summary.",
                    }
                ]
            },
            content=[
                SimpleNamespace(
                    text=(
                        '{"results":[{"link":"https://third.example/c","title":"Gamma",'
                        '"site":"Third","summary":"Gamma release notes."}]}'
                    )
                ),
                SimpleNamespace(text="重复来源 https://news.example/a；忽略 ftp://files.example/x"),
            ],
        )
    )

    result = await tool.invoke({}, ToolContext(engine_manager=None))

    assert result.data["external_references"] == [
        {
            "url": "https://news.example/a",
            "title": "Alpha report",
            "source": "Example News",
            "snippet": "Alpha launch details.",
        },
        {
            "url": "https://docs.example/b",
            "title": "Beta docs",
            "source": "Example Docs",
            "snippet": "Beta documentation summary.",
        },
        {
            "url": "https://third.example/c",
            "title": "Gamma",
            "source": "Third",
            "snippet": "Gamma release notes.",
        },
    ]


@pytest.mark.asyncio
async def test_remote_mcp_reference_snippets_are_bounded_and_merge_richer_duplicates():
    """重复 URL 补全后续元数据；摘要有边界且不会展开 JSON 工具载荷。"""
    long_summary = "  ".join(["detail"] * 100)
    serialized_payload = (
        '{"results":[{"url":"https://nested.example/private",'
        '"content":"complete upstream payload"}]}'
    )
    tool = _adapt_stub_result(
        SimpleNamespace(
            isError=False,
            structuredContent={
                "results": [
                    {"url": "https://merge.example/report"},
                    {
                        "url": "https://safe.example/result",
                        "title": "Safe result",
                        "content": serialized_payload,
                    },
                ]
            },
            structured_content={
                "results": [
                    {
                        "url": "https://merge.example/report",
                        "headline": "Complete report",
                        "provider": "Merge News",
                        "content": long_summary,
                    }
                ]
            },
            content=[
                SimpleNamespace(
                    text=(
                        '{"results":[{"link":"https://text.example/item",'
                        '"title":"Text JSON","description":"  concise\\nsummary  "}]}'
                    )
                )
            ],
        )
    )

    result = await tool.invoke({}, ToolContext(engine_manager=None))
    references = result.data["external_references"]

    assert references[0]["title"] == "Complete report"
    assert references[0]["source"] == "Merge News"
    assert len(references[0]["snippet"]) == 320
    assert references[0]["snippet"].endswith("…")
    assert references[1] == {
        "url": "https://safe.example/result",
        "title": "Safe result",
        "source": "safe.example",
    }
    assert references[2] == {
        "url": "https://text.example/item",
        "title": "Text JSON",
        "source": "text.example",
        "snippet": "concise summary",
    }
    assert all(reference["url"] != "https://nested.example/private" for reference in references)


@pytest.mark.asyncio
async def test_open_agent_mcp_tools_returns_safe_connection_warning(monkeypatch):
    """连接失败会反馈给调用方，但 warning 不泄露配置、凭据或完整异常。"""

    @asynccontextmanager
    async def broken_session(config):
        del config
        raise RuntimeError("Bearer top-secret at https://private.example/mcp")
        yield  # pragma: no cover

    monkeypatch.setattr(mcp_module, "_open_session", broken_session)

    async with open_agent_mcp_tools(
        [("private-search", {"url": "https://private.example/mcp", "token": "top-secret"})]
    ) as bundle:
        assert bundle.tools == []
        assert bundle.warnings == [
            {
                "code": "mcp_connection_failed",
                "server": "private_search",
                "message": "MCP 服务连接失败，本轮已跳过该服务。",
            }
        ]
        warning_text = str(bundle.warnings)
        assert "top-secret" not in warning_text
        assert "private.example" not in warning_text


def test_registry_overlay_does_not_pollute_global():
    """叠加层含内置 + MCP 工具；全局单例不被污染。"""

    class _Stub(Tool):
        meta = ToolMeta(
            name="mcp__ext__ping",
            description="stub",
            parameters={"type": "object", "properties": {}},
        )

        async def invoke(self, args, ctx):
            return ToolResult(content="pong")

    child = registry.overlay([_Stub()])
    assert child.has("mcp__ext__ping")
    assert child.has("search_context")  # 内置工具继承
    assert not registry.has("mcp__ext__ping")  # 全局不受影响


@pytest.mark.asyncio
async def test_mcp_binding_validation_and_source_descriptor():
    """agent 挂载外部 MCP 的校验 + 信源 MCP 连接描述端点。"""
    from sag_api.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            A = await _register(c, "mcpbind@t.com")
            src = (await c.post("/api/v1/sources", headers=A, json={"name": "描述源"})).json()

            desc = await c.get(f"/api/v1/sources/{src['id']}/mcp", headers=A)
            assert desc.status_code == 200, desc.text
            body = desc.json()
            assert src["id"] in body["http"]["url"]
            assert body["stdio"]["env"]["SAG_MCP_SOURCE_ID"] == src["id"]
            assert set(body["tools"]) == set(MCP_TOOL_NAMES)
            assert body["tool_details"] == list(MCP_TOOL_DETAILS)

            knowledge = await c.get("/api/v1/system/mcp", headers=A)
            assert knowledge.status_code == 200, knowledge.text
            global_body = knowledge.json()
            assert global_body["scope"] == "knowledge_base"
            assert global_body["source_count"] >= 1
            assert "source_id" not in global_body["http"]["url"]
            assert global_body["http"]["url"].endswith("/mcp/")
            assert global_body["stdio"]["env"] == {}
            assert set(global_body["tools"]) == set(MCP_TOOL_NAMES)
            assert global_body["tool_details"] == list(MCP_TOOL_DETAILS)

            unauthorized = await c.get("/mcp/")
            assert unauthorized.status_code == 401

            initialized = await c.post(
                "/mcp/",
                headers={
                    **A,
                    "Host": "192.168.1.20:8000",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "lan-host-test", "version": "1.0"},
                    },
                },
            )
            assert initialized.status_code == 200, initialized.text

            agent = (await c.post("/api/v1/agents", headers=A, json={"name": "挂载助手"})).json()
            ok = await c.post(
                f"/api/v1/agents/{agent['id']}/bindings",
                headers=A,
                json={"target_type": "mcp_server", "config": {"name": "fs", "url": "http://x/mcp"}},
            )
            assert ok.status_code == 201, ok.text
            assert ok.json()["target_type"] == "mcp_server"
            assert ok.json()["config"]["url"] == "http://x/mcp"

            bad = await c.post(
                f"/api/v1/agents/{agent['id']}/bindings",
                headers=A,
                json={"target_type": "mcp_server", "config": {"name": "缺少连接"}},
            )
            assert bad.status_code == 422, bad.text
