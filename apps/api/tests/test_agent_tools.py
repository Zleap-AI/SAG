"""Agent 工具循环：桩 LLM 触发 tool_call → 派发工具 → 汇总 citations → 收尾。

用 FakeLLM（configured=True + 脚本化 stream_turn）在离线下确定性驱动循环；注册一个
测试用 echo 工具验证「派发 + 引用回填」。走**无状态的 OpenAI 端点**（thread_id=None）以
避免触发后台记忆任务（引擎构建）带来的 DB 争用。向后兼容由 test_agents/test_experience 覆盖。
"""

import json
from types import SimpleNamespace

import httpx
import pytest

from sag_agent import ModelChunk, ToolCall
from sag_api.sag import GraphEventInfo, RetrievedSection, SearchOutcome, SourceGraphInfo
from sag_api.services.agent_service import _adapt_tool, _enabled_tool_names
from sag_api.tools import registry
from sag_api.tools.base import Tool, ToolContext, ToolMeta, ToolResult
from sag_api.tools.builtin import GetTimeTool, OpenWebPageTool, SearchContextTool, WebSearchTool

ECHO_CITATION = {
    "n": 1,
    "chunk_id": "c1",
    "heading": "H",
    "snippet": "S",
    "score": 0.9,
    "source_id": "src",
    "source_name": "回声源",
}


class EchoTool(Tool):
    meta = ToolMeta(
        name="echo",
        description="测试工具：回显参数并附一条引用。",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}},
    )

    async def invoke(self, args, ctx):
        return ToolResult(content=f"echoed:{args.get('q', '')}", citations=[ECHO_CITATION])


registry.register(EchoTool())


class ExternalEvidenceTool(Tool):
    meta = ToolMeta(
        name="external_evidence",
        description="测试工具：搜索外部资料并返回可追溯网页来源。",
        parameters={"type": "object", "properties": {}},
    )

    async def invoke(self, args, ctx):
        del args, ctx
        return ToolResult(
            content="官方发布确认了更新。",
            data={
                "external_references": [
                    {
                        "title": "Official release",
                        "url": "https://example.com/official-release",
                        "source": "example.com",
                        "snippet": "The official release confirms the update.",
                    },
                    {
                        "title": "Duplicate release",
                        "url": "https://example.com/official-release#summary",
                        "source": "reader",
                    },
                    {
                        "title": "Unsafe result",
                        "url": "javascript:alert(1)",
                        "source": "untrusted",
                    },
                ]
            },
        )


registry.register(ExternalEvidenceTool())


class StubWebSearchTool(Tool):
    meta = ToolMeta(
        name="web_search",
        description="测试工具：返回一条互联网搜索结果。",
        parameters={"type": "object", "properties": {}},
    )

    async def invoke(self, args, ctx):
        del args, ctx
        return ToolResult(
            content="网页 1：广州天气预报",
            data={"section_count": 1},
        )


@pytest.mark.asyncio
async def test_search_tool_prefers_exact_body_window_over_semantic_boilerplate():
    class HybridEngine:
        graph_calls = 0

        async def search_many(self, targets, query, *, strategy=None, top_k=None):
            assert strategy == "vector"
            return SearchOutcome(
                query=query,
                sections=[
                    RetrievedSection(
                        chunk_id="nav",
                        heading="版权声明",
                        content="新浪首页 阅读排行榜 评论排行榜",
                        score=0.64,
                        source_config_id="sc-1",
                    )
                ],
            )

        async def grep_chunks(self, source_config_id, pattern, *, source=None, limit=20):
            assert pattern == "林俊杰"
            return [
                {
                    "chunk_id": "body",
                    "heading": "林俊杰官宣恋情",
                    "snippet": "12月29日晚林俊杰官宣恋情，与女友七七相差21岁。",
                }
            ]

        async def graph_for_sections(self, sections, sources_by_config, **kwargs):
            self.graph_calls += 1
            assert kwargs["event_limit"] == max(12, len(sections))
            assert sources_by_config["sc-1"].id == "source-1"
            return SimpleNamespace(
                events=[
                    SimpleNamespace(
                        id="event-1",
                        source_config_id="sc-1",
                        chunk_id=sections[0].chunk_id,
                        title="林俊杰官宣恋情",
                        summary="林俊杰于 12 月 29 日公开恋情。",
                        content="12 月 29 日，林俊杰公开确认恋情，并介绍双方交往情况。",
                        category="娱乐",
                        score=0.95,
                    )
                ],
                entities=[],
                associations=[],
            )

    engine = HybridEngine()
    source = SimpleNamespace(id="source-1", name="娱乐新闻", sag_source_config_id="sc-1")
    host_context = ToolContext(engine_manager=engine, sources=[source])
    result = await SearchContextTool().invoke(
        {"query": "关于林俊杰最新动态 2024 2025", "top_k": 4},
        host_context,
    )

    assert result.citations[0]["chunk_id"] == "body"
    assert result.citations[0]["event_refs"][0]["title"] == "林俊杰官宣恋情"
    assert result.citations[0]["event_refs"][0]["content"].startswith("12 月 29 日")
    assert "summary" not in result.citations[0]
    assert "12月29日晚" in result.content
    assert result.data["lexical_count"] == 1
    assert result.data["section_count"] == 1
    assert result.data["_graph"] is not None
    assert result.data["_graph"].events[0].id == "event-1"
    assert engine.graph_calls == 1

    # The runtime adapter must reuse SearchContextTool's graph result instead
    # of issuing a second graph query while constructing universe artifacts.
    collected_citations: list[dict] = []
    adapter_engine = HybridEngine()
    adapter_context = ToolContext(engine_manager=adapter_engine, sources=[source])
    adapted = _adapt_tool(SearchContextTool(), adapter_context, collected_citations)
    runtime_result = await adapted.execute(
        {"query": "关于林俊杰最新动态 2024 2025", "top_k": 4},
        SimpleNamespace(
            cancellation=SimpleNamespace(raise_if_cancelled=lambda: None),
        ),
    )

    assert adapter_engine.graph_calls == 1
    assert collected_citations[0]["event_refs"][0]["id"] == "event-1"
    assert runtime_result.details["sources"] == [{"id": "source-1", "name": "娱乐新闻"}]
    assert runtime_result.artifacts["citations"][0]["event_refs"][0]["summary"] == ("林俊杰于 12 月 29 日公开恋情。")
    assert runtime_result.artifacts["citations"][0]["event_refs"][0]["content"].startswith(
        "12 月 29 日"
    )
    assert runtime_result.details["matches"][0]["event_refs"][0]["category"] == "娱乐"


@pytest.mark.asyncio
async def test_web_search_trace_uses_internet_scope_instead_of_mounted_knowledge_sources():
    mounted_sources = [
        SimpleNamespace(id="source-1", name="西游记"),
        SimpleNamespace(id="source-2", name="SAG"),
    ]
    adapted = _adapt_tool(
        StubWebSearchTool(),
        ToolContext(engine_manager=SimpleNamespace(), sources=mounted_sources),
        [],
    )

    result = await adapted.execute(
        {"query": "广州明天天气"},
        SimpleNamespace(cancellation=SimpleNamespace(raise_if_cancelled=lambda: None)),
    )

    assert result.details["scope"] == "internet"
    assert "sources" not in result.details


@pytest.mark.asyncio
async def test_search_tool_graph_capacity_covers_every_returned_section():
    class ManySectionEngine:
        graph_calls = 0
        event_limit = 0

        async def search_many(self, targets, query, *, strategy=None, top_k=None):
            source_config_id = targets[0][0]
            return SearchOutcome(
                query=query,
                sections=[
                    RetrievedSection(
                        chunk_id=f"chunk-{index}",
                        heading=f"共同主题 {index}",
                        content=f"共同主题的可核验证据 {index}",
                        score=1.0 - index / 100,
                        source_config_id=source_config_id,
                    )
                    for index in range(20)
                ],
            )

        async def graph_for_sections(self, sections, sources_by_config, **kwargs):
            self.graph_calls += 1
            self.event_limit = kwargs["event_limit"]
            return SourceGraphInfo(
                events=[
                    GraphEventInfo(
                        id=f"event-{index}",
                        source_id="document-1",
                        source_config_id=section.source_config_id or "",
                        chunk_id=section.chunk_id,
                        title=f"真实事件 {index}",
                        summary=f"真实事件摘要 {index}",
                        category="测试",
                    )
                    for index, section in enumerate(sections)
                ]
            )

    engine = ManySectionEngine()
    source = SimpleNamespace(id="source-1", name="测试资料", sag_source_config_id="sc-1")
    result = await SearchContextTool().invoke(
        {"query": "共同主题", "top_k": 20},
        ToolContext(engine_manager=engine, sources=[source]),
    )

    assert len(result.citations) == 20
    assert engine.graph_calls == 1
    assert engine.event_limit == 20
    assert all(len(citation["event_refs"]) == 1 for citation in result.citations)


@pytest.mark.asyncio
async def test_search_tool_reuses_direct_event_recall_and_loads_traceable_evidence():
    class SparseEventEngine:
        event_score_calls = 0
        chunk_reads: list[tuple[str, str]] = []

        async def search_many(self, targets, query, *, strategy=None, top_k=None):
            return SearchOutcome(
                query=query,
                sections=[
                    RetrievedSection(
                        chunk_id="nearby-chunk",
                        heading="历史",
                        content="帝国发展相关的背景资料，但这个分块本身没有抽取事项。",
                        score=0.81,
                        source_config_id=targets[0][0],
                    )
                ],
            )

        async def search_event_scores(self, query, sources_by_config, *, limit=None):
            self.event_score_calls += 1
            assert query == "帝国发展"
            assert sources_by_config["sc-1"].id == "source-1"
            assert limit == 2
            return {("sc-1", "event-direct"): 0.97}

        async def graph_for_sections(self, sections, sources_by_config, **kwargs):
            assert kwargs["event_scores"] == {("sc-1", "event-direct"): 0.97}
            return SourceGraphInfo(
                events=[
                    GraphEventInfo(
                        id="event-direct",
                        source_id="document-1",
                        source_config_id="sc-1",
                        chunk_id="event-chunk",
                        title="帝国运转的信息需求与大脑存储局限",
                        summary="帝国依赖大规模信息处理体系维持扩张与治理。",
                        category="历史",
                        score=0.97,
                    )
                ]
            )

        async def get_chunk(self, source_config_id, chunk_id, *, source=None):
            self.chunk_reads.append((source_config_id, chunk_id))
            assert source.id == "source-1"
            return SimpleNamespace(
                chunk_id=chunk_id,
                heading="历史",
                content="维持复杂社会秩序需要存储并处理大量行政信息。",
                rank=12,
            )

    engine = SparseEventEngine()
    source = SimpleNamespace(id="source-1", name="人类简史", sag_source_config_id="sc-1")
    result = await SearchContextTool().invoke(
        {"query": "帝国发展", "top_k": 2},
        ToolContext(engine_manager=engine, sources=[source]),
    )

    assert engine.event_score_calls == 1
    assert engine.chunk_reads == [("sc-1", "event-chunk")]
    assert result.data["event_candidates"] == 1
    assert result.data["event_count"] == 1
    assert result.citations[0]["chunk_id"] == "event-chunk"
    assert result.citations[0]["source_id"] == "source-1"
    assert result.citations[0]["event_refs"] == [
        {
            "id": "event-direct",
            "title": "帝国运转的信息需求与大脑存储局限",
            "summary": "帝国依赖大规模信息处理体系维持扩张与治理。",
            "category": "历史",
        }
    ]
    assert result.content.startswith("[1] 事项：帝国运转的信息需求与大脑存储局限")
    assert "摘要：帝国依赖大规模信息处理体系维持扩张与治理。" in result.content
    assert "原文证据：\n维持复杂社会秩序需要存储并处理大量行政信息。" in result.content


def test_visible_sources_mount_builtin_knowledge_tools():
    agent = SimpleNamespace(persona={}, is_default=False)
    assert _enabled_tool_names(agent, has_sources=True) == [
        "get_time",
        "search_context",
        "get_entity",
        "web_search",
        "open_webpage",
    ]


@pytest.mark.asyncio
async def test_web_search_uses_302_endpoint_and_returns_traceable_sources(monkeypatch):
    from sag_api.core.config import settings
    from sag_api.tools import builtin

    requests: list[dict] = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "search_results": [
                    {
                        "url": "https://weather.example/guangzhou",
                        "title": "广州天气预报",
                        "content": "  7月15日有雷阵雨，出行请携带雨具。  ",
                        "published_at": "2026-07-14T06:30:00+08:00",
                    },
                    {
                        "url": "javascript:alert(1)",
                        "title": "不安全结果",
                    },
                    {
                        "url": "https://weather.example/guangzhou",
                        "title": "重复结果",
                    },
                ]
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            requests.append({"url": url, **kwargs})
            return Response()

    monkeypatch.setattr(settings, "llm_base_url", "https://api.302ai.cn/v1")
    monkeypatch.setattr(settings, "llm_api_key", "sk-private-test")
    monkeypatch.setattr(builtin.httpx, "AsyncClient", lambda **_kwargs: Client())

    result = await WebSearchTool().invoke(
        {"query": "广州明天（2026-07-15）天气", "count": 4},
        ToolContext(engine_manager=SimpleNamespace()),
    )

    assert requests[0]["url"] == "https://api.302ai.cn/302/general/search"
    assert requests[0]["json"] == {
        "query": "广州明天（2026-07-15）天气",
        "provider": "tavily",
        "max_results": 4,
        "time_range": "week",
    }
    assert requests[0]["headers"]["Authorization"] == "Bearer sk-private-test"
    assert result.data["section_count"] == 1
    assert result.data["external_references"] == [
        {
            "title": "广州天气预报",
            "url": "https://weather.example/guangzhou",
            "source": "weather.example",
            "snippet": "7月15日有雷阵雨，出行请携带雨具。",
        }
    ]
    assert "https://weather.example/guangzhou" in result.content
    assert "2026-07-14T06:30:00+08:00" in result.content
    assert "javascript:" not in result.content
    assert "sk-private-test" not in result.content


@pytest.mark.asyncio
async def test_web_search_is_unavailable_without_302_configuration(monkeypatch):
    from sag_api.core.config import settings

    monkeypatch.setattr(settings, "llm_base_url", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")

    assert WebSearchTool.configured() is False
    result = await WebSearchTool().invoke(
        {"query": "最新消息"},
        ToolContext(engine_manager=SimpleNamespace()),
    )
    assert result.data["section_count"] == 0
    assert "尚未配置" in result.content


@pytest.mark.asyncio
async def test_open_web_page_extracts_public_html_as_traceable_evidence(monkeypatch):
    from sag_api.tools import builtin

    original_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://weather.example/guangzhou"
        assert request.headers["user-agent"].startswith("sag-bot/")
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><head><title>广州天气预报</title></head>"
                "<body><main><h1>7月15日</h1><p>广州有雷阵雨，最高温 32℃。</p></main></body></html>"
            ),
        )

    async def allow_test_url(url: str) -> str:
        return url

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(builtin, "_validated_public_web_url", allow_test_url)
    monkeypatch.setattr(
        builtin.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )

    result = await OpenWebPageTool().invoke(
        {"url": "https://weather.example/guangzhou"},
        ToolContext(engine_manager=SimpleNamespace()),
    )

    assert "广州有雷阵雨" in result.content
    assert result.data["section_count"] == 1
    assert result.data["external_references"][0]["url"] == ("https://weather.example/guangzhou")


@pytest.mark.asyncio
async def test_open_web_page_rejects_private_network_targets():
    with pytest.raises(RuntimeError, match="公开网页"):
        await OpenWebPageTool().invoke(
            {"url": "http://127.0.0.1:8000/api/v1/system/health"},
            ToolContext(engine_manager=SimpleNamespace()),
        )


@pytest.mark.asyncio
async def test_get_time_uses_system_timezone_and_returns_utc_instant(monkeypatch):
    from sag_api.core.config import settings

    monkeypatch.setattr(settings, "timezone", "Asia/Shanghai")
    result = await GetTimeTool().invoke(
        {},
        ToolContext(engine_manager=SimpleNamespace(), sources=[]),
    )
    assert result.data["ok"] is True
    assert result.data["timezone"] == "Asia/Shanghai"
    assert result.data["utc_offset"] == "+08:00"
    assert result.data["local_iso"].endswith("+08:00")
    assert result.data["utc_iso"].endswith("+00:00")


class FakeLLM:
    """脚本化：第一轮请求调 echo 工具，第二轮收尾；最终答案流式两 token。"""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def configured(self) -> bool:
        return True

    async def stream_turn(self, request, cancellation):
        self.calls += 1
        has_echo = any(tool.get("function", {}).get("name") == "echo" for tool in request.tools)
        if self.calls == 1 and has_echo:
            yield ModelChunk(
                tool_calls=(ToolCall(id="call_1", name="echo", arguments={"q": "hi"}),),
                finish_reason="tool_calls",
            )
            return
        for token in ["最终", "答案"]:
            cancellation.raise_if_cancelled()
            yield ModelChunk(text_delta=token)
        yield ModelChunk(finish_reason="stop")


class ExternalEvidenceLLM:
    """Call an external tool, then omit its URL to exercise run-level mapping."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def configured(self) -> bool:
        return True

    async def stream_turn(self, request, cancellation):
        self.calls += 1
        if self.calls == 1:
            yield ModelChunk(
                tool_calls=(ToolCall(id="external-1", name="external_evidence", arguments={}),),
                finish_reason="tool_calls",
            )
            return
        cancellation.raise_if_cancelled()
        yield ModelChunk(text_delta="已核实更新。", finish_reason="stop")


async def _register(c, email):
    r = await c.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_agent_tool_loop_dispatch_and_citations():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        app.state.llm = FakeLLM()  # 覆盖为桩（本用例范围内）
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            A = await _register(c, "agenttools@t.com")
            # 开启额外工具 echo；不绑定信源 → 不触发引擎，循环仍运行
            agent = (
                await c.post(
                    "/api/v1/agents",
                    headers=A,
                    json={"name": "工具助手", "persona": {"tools": ["echo"]}},
                )
            ).json()

            r = await c.post(
                f"/api/v1/openai/{agent['id']}/chat/completions",
                headers=A,
                json={"messages": [{"role": "user", "content": "你好"}]},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            # 工具循环跑完 → 最终答案
            assert body["choices"][0]["message"]["content"] == "最终答案"
            # 工具被派发（echo 执行）→ 其引用汇总进 sag.citations
            assert any(c.get("source_name") == "回声源" for c in body["sag"]["citations"])
            # 统一 provider 协议恰好两轮（工具决策 + 收尾）
            assert app.state.llm.calls == 2


@pytest.mark.asyncio
async def test_agent_external_tool_returns_structured_citation_when_model_omits_url():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        app.state.llm = ExternalEvidenceLLM()
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            A = await _register(c, "externalrefs@t.com")
            agent = (
                await c.post(
                    "/api/v1/agents",
                    headers=A,
                    json={"name": "外部资料助手", "persona": {"tools": ["external_evidence"]}},
                )
            ).json()

            response = await c.post(
                f"/api/v1/openai/{agent['id']}/chat/completions",
                headers=A,
                json={"messages": [{"role": "user", "content": "请搜索并核实更新"}]},
            )

            assert response.status_code == 200, response.text
            content = response.json()["choices"][0]["message"]["content"]
            assert content == "已核实更新。"
            assert response.json()["sag"]["citations"] == [
                {
                    "kind": "external",
                    "n": 1,
                    "url": "https://example.com/official-release",
                    "title": "Official release",
                    "source": "example.com",
                    "mapped": False,
                    "claim_level": "run",
                    "summary": "The official release confirms the update.",
                    "snippet": "The official release confirms the update.",
                }
            ]

            app.state.llm = ExternalEvidenceLLM()
            chunks: list[str] = []
            async with c.stream(
                "POST",
                f"/api/v1/openai/{agent['id']}/chat/completions",
                headers=A,
                json={
                    "messages": [{"role": "user", "content": "请搜索并核实更新"}],
                    "stream": True,
                },
            ) as streamed:
                assert streamed.status_code == 200
                async for line in streamed.aiter_lines():
                    if line.startswith("data:"):
                        chunks.append(line[len("data:") :].strip())
            streamed_content = "".join(
                json.loads(item)["choices"][0]["delta"].get("content", "") for item in chunks if item != "[DONE]"
            )
            assert streamed_content == "已核实更新。"

            # Stateful SSE exposes the same structured citation and persists it
            # for history playback without patching a source footer into prose.
            thread = (await c.post(f"/api/v1/agents/{agent['id']}/threads", headers=A, json={})).json()
            app.state.llm = ExternalEvidenceLLM()
            ask = await c.post(
                f"/api/v1/agents/{agent['id']}/threads/{thread['id']}/ask",
                headers=A,
                json={"query": "请搜索并核实更新", "web_enabled": True},
            )
            assert ask.status_code == 200, ask.text
            event_name = ""
            completed = None
            for line in ask.text.splitlines():
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and event_name == "run.completed":
                    completed = json.loads(line.split(":", 1)[1].strip())["payload"]
            assert completed is not None
            assert completed["citations"] == response.json()["sag"]["citations"]

            messages = (
                await c.get(
                    f"/api/v1/agents/{agent['id']}/threads/{thread['id']}/messages",
                    headers=A,
                )
            ).json()["items"]
            saved = next(message for message in messages if message["role"] == "assistant")
            assert saved["citations"] == completed["citations"]
            assert saved["citations"][0]["kind"] == "external"
            assert "javascript:" not in json.dumps(saved["citations"])


@pytest.mark.asyncio
async def test_no_tools_agent_uses_one_model_turn():
    """No-tool agents use the same provider protocol with exactly one model turn."""
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        fake = FakeLLM()
        app.state.llm = fake
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            A = await _register(c, "plainagent@t.com")
            agent = (await c.post("/api/v1/agents", headers=A, json={"name": "普通助手"})).json()
            r = await c.post(
                f"/api/v1/openai/{agent['id']}/chat/completions",
                headers=A,
                json={"messages": [{"role": "user", "content": "在吗"}]},
            )
            assert r.status_code == 200, r.text
            assert r.json()["choices"][0]["message"]["content"] == "最终答案"
            assert fake.calls == 1
