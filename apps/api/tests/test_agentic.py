"""Agentic 基建：默认工具、全局证据编号、历史压缩、token 估算。全离线。"""

import pytest

from sag_agent import AgentTool, ToolResult, ToolSpec
from sag_api.generation.prompt import build_agent_messages, build_prompt_preview, estimate_tokens
from sag_api.sag import RetrievedSection, SearchOutcome
from sag_api.services.agent_domain import compress_history
from sag_api.services.agent_service import (
    _append_current_scene,
    _build_external_citations,
    _enabled_tool_names,
    _finalize_answer_citations,
    _initial_tool_choice,
)
from sag_api.tools.base import ToolContext
from sag_api.tools.builtin import SearchContextTool


class _A:
    def __init__(self, is_default=False, tools=None):
        self.is_default = is_default
        self.persona = {"tools": tools} if tools is not None else {}


def test_default_agent_gets_builtin_tools():
    assert _enabled_tool_names(_A(is_default=True)) == [
        "get_time",
        "search_context",
        "get_entity",
        "web_search",
        "open_webpage",
    ]
    assert _enabled_tool_names(_A(is_default=False)) == [
        "get_time",
        "web_search",
        "open_webpage",
    ]
    assert _enabled_tool_names(_A(is_default=True, tools=["echo"])) == [
        "get_time",
        "search_context",
        "get_entity",
        "web_search",
        "open_webpage",
        "echo",
    ]
    assert _enabled_tool_names(_A(is_default=True), knowledge_only=True) == [
        "get_time",
        "search_context",
        "get_entity",
    ]


def test_estimate_tokens_cjk_aware():
    assert estimate_tokens("你好世界") == 4  # CJK 每字 1
    assert estimate_tokens("abcdefgh") == 2  # ASCII 每 4 字符 1
    assert estimate_tokens("") == 0


def test_agent_prompt_uses_static_timezone_rule_and_time_tool_guidance():
    messages = build_agent_messages(
        "测试助手",
        {},
        "现在几点",
        timezone="Asia/Shanghai",
    )
    system = messages[0]["content"]
    assert "Asia/Shanghai" in system
    assert "get_time" in system
    assert "当前日期和时间是动态事实" in system
    assert "绝对日期、合适的时间窗口和查询对象" in system
    assert "不得沿用旧对话、模型记忆或用户示例中的年份" in system


def test_agent_prompt_guides_clarification_progress_and_delivery_in_both_languages():
    zh = build_agent_messages("测试助手", {}, "推荐一下")[0]["content"]
    en = build_agent_messages(
        "Test Assistant",
        {},
        "Recommend something",
        language="en",
        timezone="UTC",
    )[0]["content"]

    assert "实质改变结论或交付物" in zh
    assert "合理默认值" in zh
    assert "可直接使用的结果" in zh
    assert "不要输出冗长的内部思维过程" in zh
    assert "官方公告、产品文档、原始数据" in zh
    assert "至少两个相互独立的来源交叉核验" in zh
    assert "关键外部事实就必须在对应论断附近附上可点击的直接来源" in zh
    assert "get_entity 做实体消歧和形成后续检索词" in zh
    assert "寒暄、致谢、告别、身份询问应直接回答，不调用检索" in zh
    assert "不能用搜索代替澄清" in zh
    assert "materially change the conclusion or deliverable" in en
    assert "reasonable assumptions and proceed" in en
    assert "directly usable result" in en
    assert "Do not expose lengthy hidden reasoning" in en
    assert "first-party announcements, product documentation, original data" in en
    assert "at least two independent sources" in en
    assert "clickable direct source near each key external claim" in en
    assert "get_entity only to disambiguate entities" in en
    assert "Answer greetings, thanks, farewells, and identity questions directly" in en
    assert "search is not a substitute for clarification" in en


def test_search_context_description_has_explicit_non_retrieval_boundary():
    description = SearchContextTool.meta.description

    assert "仅当回答依赖已挂载知识库" in description
    assert "不要用于寒暄、致谢、身份询问、纯创作、简单计算" in description
    assert "不能用检索代替澄清" in description


def test_agent_messages_keep_system_history_and_current_user_separate():
    messages = build_agent_messages(
        "测试助手",
        {},
        "当前问题",
        history=[
            {"role": "user", "content": "历史问题"},
            {"role": "assistant", "content": "历史回答"},
        ],
    )

    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert "历史回答" not in messages[0]["content"]
    preview = build_prompt_preview(messages)
    assert "【系统指令】" in preview
    assert "【历史 · 助手】\n历史回答" in preview
    assert "【当前问题】\n当前问题" in preview


def test_dynamic_scene_stays_inside_single_system_message():
    messages = build_agent_messages(
        "测试助手",
        {},
        "查知识库",
        history=[{"role": "assistant", "content": "历史回答"}],
    )
    with_scene = _append_current_scene(messages, ["限定本地知识库", "限定产品资料"])

    assert [message["role"] for message in with_scene] == ["system", "assistant", "user"]
    assert sum(message["role"] == "system" for message in with_scene) == 1
    assert "【当前场景】" in with_scene[0]["content"]
    assert "限定本地知识库" in with_scene[0]["content"]
    assert with_scene[1]["content"] == "历史回答"
    assert messages[0]["content"] != with_scene[0]["content"]


def test_initial_tool_policy_anchors_time_and_preserves_clarification():
    async def execute(arguments, context):
        return ToolResult(content="ok")

    search = AgentTool(
        ToolSpec(name="search_context", description="检索知识库"),
        execute,
    )
    clock = AgentTool(ToolSpec(name="get_time", description="查询时间"), execute)
    tools = (clock, search)

    named_time = {"type": "function", "function": {"name": "get_time"}}
    named_search = {"type": "function", "function": {"name": "search_context"}}

    assert (
        _initial_tool_choice(
            "最近 Agent 有什么发展？",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == named_time
    )
    assert (
        _initial_tool_choice(
            "最近 ChatGPT 有哪些更新？",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == named_time
    )
    assert (
        _initial_tool_choice(
            "过去三个月 ChatGPT 有哪些更新？",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == named_time
    )
    assert (
        _initial_tool_choice(
            "What changed last week?",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == named_time
    )
    assert (
        _initial_tool_choice(
            "你好",
            tools,
            knowledge_only=True,
            scoped=False,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "推荐一下",
            tools,
            knowledge_only=True,
            scoped=False,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "最近怎么样",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "上周怎么样",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "(2 + 3) * 4 = ?",
            tools,
            knowledge_only=True,
            scoped=False,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "总结知识库里的发布流程",
            tools,
            knowledge_only=True,
            scoped=False,
        )
        == named_search
    )
    assert (
        _initial_tool_choice(
            "请搜索并核实这项数据",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == "required"
    )
    assert (
        _initial_tool_choice(
            "你好",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "把这段话润色一下",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == "auto"
    )


@pytest.mark.parametrize(
    "query",
    ["你好！", "Hello", "在吗？", "谢谢你", "你是谁呀？", "What's your name?"],
)
def test_high_confidence_social_intents_disable_tools(query):
    async def execute(arguments, context):
        return ToolResult(content="ok")

    tools = (
        AgentTool(ToolSpec(name="get_time", description="查询时间"), execute),
        AgentTool(ToolSpec(name="search_context", description="检索知识库"), execute),
    )

    assert (
        _initial_tool_choice(query, tools, knowledge_only=True, scoped=False)
        == "none"
    )


def test_answer_citations_are_canonical_and_traceable():
    citations = [
        {"n": 1, "chunk_id": "chunk-1", "source_id": "source-1", "heading": "一"},
        {"n": 2, "chunk_id": None, "source_id": "source-1", "heading": "不可打开"},
        {"n": 3, "chunk_id": "chunk-3", "source_id": "source-3", "heading": "三"},
    ]

    answer, used = _finalize_answer_citations("结论 [1]，虚构 [9]，坏引用 [2]。", citations)
    assert answer == "结论 [1]，虚构，坏引用。"
    assert [citation["n"] for citation in used] == [1]
    assert used[0]["kind"] == "internal"
    assert used[0]["mapped"] is True
    assert used[0]["claim_level"] == "claim"

    uncited, fallback = _finalize_answer_citations("模型忘了引用。", citations)
    assert uncited == "模型忘了引用。"
    assert [citation["n"] for citation in fallback] == [1, 3]
    assert all(citation["kind"] == "internal" for citation in fallback)
    assert all(citation["mapped"] is False for citation in fallback)
    assert all(citation["claim_level"] == "run" for citation in fallback)

    external_link = "外部来源 [9](https://example.com/release)。"
    preserved, none = _finalize_answer_citations(external_link, [])
    assert preserved == external_link
    assert none == []


def test_external_citations_are_safe_deduplicated_bounded_and_mapping_aware():
    references = [
        {
            "title": "Official release",
            "url": "HTTPS://Example.COM/release#details",
            "source": "OpenAI",
            "description": "  Product   update details.  ",
        },
        {"title": "duplicate", "url": "https://example.com/release"},
        {"title": "bad scheme", "url": "javascript:alert(1)"},
        {"title": "credentials", "url": "https://user:secret@example.com/private"},
        {"title": "whitespace", "url": "https://example.com/a b"},
        *[
            {"title": f"Result {index}", "url": f"https://source{index}.example/article"}
            for index in range(20)
        ],
    ]

    citations = _build_external_citations(
        "结论见 https://example.com/release。",
        references,
        start_n=6,
    )

    assert len(citations) == 12
    assert citations[0] == {
        "kind": "external",
        "n": 6,
        "url": "https://example.com/release",
        "title": "Official release",
        "source": "OpenAI",
        "mapped": True,
        "claim_level": "claim",
        "summary": "Product update details.",
        "snippet": "Product update details.",
    }
    assert citations[1]["n"] == 7
    assert citations[1]["mapped"] is False
    assert citations[1]["claim_level"] == "run"
    assert not any("javascript:" in citation["url"] for citation in citations)
    assert not any("secret" in citation["url"] for citation in citations)


@pytest.mark.asyncio
async def test_search_tool_uses_global_citation_offset():
    class _EM:
        async def search_many(self, targets, query, strategy=None, top_k=None):
            return SearchOutcome(
                query=query,
                sections=[
                    RetrievedSection(
                        heading="标题",
                        content="内容",
                        chunk_id="c1",
                        source_config_id="scid",
                        score=0.9,
                    )
                ],
            )

    class _Src:
        sag_source_config_id = "scid"
        id = "sid"
        name = "源"

    ctx = ToolContext(engine_manager=_EM(), sources=[_Src()], citation_offset=3)
    result = await SearchContextTool().invoke({"query": "q"}, ctx)
    assert "[4]" in result.content  # 编号从 offset+1 开始
    assert result.citations[0]["n"] == 4


@pytest.mark.asyncio
async def test_compress_history_trims_without_llm():
    history = [{"role": "user", "content": "字" * 200} for _ in range(10)]
    out = await compress_history(history, llm=None, budget_tokens=500)
    assert out and out == history[-len(out) :]  # 尾部保留
    assert sum(estimate_tokens(m["content"]) for m in out) <= 500 + 200
    # 预算内不动
    same = await compress_history(history[:2], llm=None, budget_tokens=10_000)
    assert same == history[:2]
