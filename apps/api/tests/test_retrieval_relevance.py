"""Retrieval answers may only see evidence that survives query-aware reranking."""

import pytest

from sag_api.sag import RetrievedSection
from sag_api.services.retrieval_service import (
    fallback_search_answer,
    rerank_sections,
    synthesize_search_answer,
)


def section(chunk_id: str, heading: str, content: str, score: float) -> RetrievedSection:
    return RetrievedSection(
        chunk_id=chunk_id,
        heading=heading,
        content=content,
        score=score,
        source_config_id="source-1",
    )


def test_rerank_prefers_direct_query_evidence_and_filters_unrelated_candidates():
    result = rerank_sections(
        "张杰最近有什么公益动态",
        [
            section("noise", "平台首页", "这是与体育赛事有关的热门内容。", 0.96),
            section("answer", "张杰公益行动", "张杰为乡村儿童建设音乐教室。", 0.74),
            section("other", "其他歌手", "另一位歌手发布了新专辑。", 0.7),
        ],
        limit=8,
    )

    assert [item.chunk_id for item in result.sections] == ["answer"]
    assert result.filtered_count == 2


def test_rerank_uses_semantic_floor_when_no_lexical_signal_exists():
    result = rerank_sections(
        "如何改善配送劳动者的保障",
        [
            section("strong", "劳动研究", "报告讨论了工作时长、技能与收入。", 0.82),
            section("weak", "无关附录", "网页页脚与版权信息。", 0.12),
        ],
        limit=8,
    )

    assert [item.chunk_id for item in result.sections] == ["strong"]


def test_fallback_answer_cites_only_selected_sections():
    selected = [
        section("one", "公益行动", "张杰为乡村儿童建设音乐教室。", 0.9),
        section("two", "赈灾捐助", "团队向受灾地区捐赠物资。", 0.8),
    ]

    answer = fallback_search_answer("张杰有哪些公益行动", selected)

    assert "张杰为乡村儿童建设音乐教室" in answer
    assert "[1]" in answer and "[2]" in answer
    assert "[3]" not in answer


@pytest.mark.asyncio
async def test_invalid_llm_citation_falls_back_to_selected_evidence():
    class InvalidCitationLLM:
        configured = True

        async def complete(self, _messages):
            return "模型引用了不存在的证据 [9]"

    selected = [section("one", "相关证据", "实际入选的事实。", 0.9)]
    answer = await synthesize_search_answer(
        "问题",
        selected,
        llm=InvalidCitationLLM(),
    )

    assert "实际入选的事实" in answer
    assert "[1]" in answer
    assert "[9]" not in answer
