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


def test_rerank_accepts_split_evidence_for_contiguous_chinese_query():
    result = rerank_sections(
        "肉类清汤",
        [section("target", "烹饪说明", "肉类需要炖煮；清汤只需短时加热。", 0.1)],
        limit=8,
    )

    assert [item.chunk_id for item in result.sections] == ["target"]


@pytest.mark.asyncio
async def test_contiguous_and_spaced_chinese_queries_return_same_core_evidence():
    from uuid import uuid4

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Source
    from sag_api.sag import SearchOutcome
    from sag_api.services.retrieval_service import retrieve_relevant_sections

    class LexicalEngine:
        def __init__(self):
            self.semantic_queries: list[str] = []
            self.grep_calls: list[tuple[str, str]] = []

        async def search_many(self, _targets, query, **_kwargs):
            self.semantic_queries.append(query)
            return SearchOutcome(query=query, sections=[], stats={})

        async def grep_chunks(self, source_config_id, term, **_kwargs):
            self.grep_calls.append((source_config_id, term))
            rows = {
                "肉类": [
                    {
                        "chunk_id": "chunk-meat",
                        "heading": "炖煮肉类",
                        "snippet": "肉类需要六十到一百二十分钟。",
                        "source_id": "article-meat",
                    }
                ],
                "清汤": [
                    {
                        "chunk_id": "chunk-soup",
                        "heading": "快速清汤",
                        "snippet": "清汤只需要五到十分钟。",
                        "source_id": "article-soup",
                    }
                ],
            }
            return rows.get(term, [])

    await init_db()
    engine = LexicalEngine()
    async with SessionLocal() as session:
        source = Source(
            name="chinese-query-equivalence",
            sag_source_config_id=(f"chinese-query-{uuid4().hex}")[:36],
        )
        session.add(source)
        await session.commit()

        contiguous = await retrieve_relevant_sections(engine, [source], "肉类清汤", top_k=8)
        spaced = await retrieve_relevant_sections(engine, [source], "肉类 清汤", top_k=8)

    assert engine.semantic_queries == ["肉类清汤", "肉类 清汤"]
    assert [item.chunk_id for item in contiguous.sections] == [item.chunk_id for item in spaced.sections]
    assert {item.chunk_id for item in contiguous.sections} == {"chunk-meat", "chunk-soup"}
    assert contiguous.stats["chinese_segmentation_used"] is True
    assert contiguous.stats["lexical_term_count"] == 3


@pytest.mark.asyncio
async def test_natural_chinese_question_recalls_trailing_topic_term():
    from uuid import uuid4

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Source
    from sag_api.sag import SearchOutcome
    from sag_api.services.retrieval_service import retrieve_relevant_sections

    class LexicalEngine:
        async def search_many(self, _targets, query, **_kwargs):
            return SearchOutcome(query=query, sections=[], stats={})

        async def grep_chunks(self, _source_config_id, term, **_kwargs):
            if term != "清汤":
                return []
            return [
                {
                    "chunk_id": "chunk-soup",
                    "heading": "快速清汤",
                    "snippet": "清汤只需要五到十分钟。",
                    "source_id": "article-soup",
                }
            ]

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="natural-chinese-query",
            sag_source_config_id=(f"natural-chinese-query-{uuid4().hex}")[:36],
        )
        session.add(source)
        await session.commit()

        outcome = await retrieve_relevant_sections(
            LexicalEngine(),
            [source],
            "如何制作肉类清汤",
            top_k=8,
        )

    assert [item.chunk_id for item in outcome.sections] == ["chunk-soup"]
    assert outcome.stats["lexical_term_count"] == 4


@pytest.mark.asyncio
async def test_lexical_recall_is_bounded_to_four_terms_per_source(monkeypatch):
    from uuid import uuid4

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Source
    from sag_api.sag import SearchOutcome
    from sag_api.services import retrieval_service
    from sag_api.services.query_analysis import QueryAnalysis

    class RecordingEngine:
        def __init__(self):
            self.grep_calls: list[tuple[str, str]] = []

        async def search_many(self, _targets, query, **_kwargs):
            return SearchOutcome(query=query, sections=[], stats={})

        async def grep_chunks(self, source_config_id, term, **_kwargs):
            self.grep_calls.append((source_config_id, term))
            return []

    analysis = QueryAnalysis(
        normalized_phrase="甲乙丙丁戊己庚辛",
        scoring_terms=("甲乙", "丙丁", "戊己", "庚辛"),
        lookup_terms=("甲乙丙丁戊己庚辛", "甲乙", "丙丁", "戊己"),
        chinese_segmentation_used=True,
    )
    monkeypatch.setattr(
        retrieval_service,
        "analyze_query",
        lambda *_args, **_kwargs: analysis,
        raising=False,
    )
    await init_db()
    engine = RecordingEngine()
    async with SessionLocal() as session:
        sources = [
            Source(name=f"bounded-{index}", sag_source_config_id=(f"bounded-{index}-{uuid4().hex}")[:36])
            for index in range(2)
        ]
        session.add_all(sources)
        await session.commit()

        outcome = await retrieval_service.retrieve_relevant_sections(
            engine,
            sources,
            "甲乙丙丁戊己庚辛",
        )

    assert len(engine.grep_calls) == len(sources) * 4
    assert outcome.stats["lexical_term_count"] == 4


@pytest.mark.asyncio
async def test_total_latency_includes_query_analysis(monkeypatch):
    from types import SimpleNamespace

    from sag_api.core.db import init_db
    from sag_api.sag import SearchOutcome
    from sag_api.services import query_analysis, retrieval_service

    clock = [0.0]

    def delayed_segmenter(_text: str):
        clock[0] += 0.25
        return ["肉类", "清汤"]

    class EmptyEngine:
        async def search_many(self, _targets, query, **_kwargs):
            return SearchOutcome(query=query, sections=[], stats={})

    monkeypatch.setattr(query_analysis, "_jieba_segment", delayed_segmenter)
    monkeypatch.setattr(
        retrieval_service,
        "time",
        SimpleNamespace(perf_counter=lambda: clock[0]),
    )
    await init_db()

    outcome = await retrieval_service.retrieve_relevant_sections(
        EmptyEngine(),
        [],
        "肉类清汤",
    )

    assert outcome.stats["latency_total_ms"] == 250.0


@pytest.mark.asyncio
async def test_tokenizer_failure_preserves_semantic_retrieval(monkeypatch):
    from uuid import uuid4

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Source
    from sag_api.sag import SearchOutcome
    from sag_api.services import query_analysis
    from sag_api.services.retrieval_service import retrieve_relevant_sections

    class SemanticEngine:
        async def search_many(self, _targets, query, **_kwargs):
            return SearchOutcome(
                query=query,
                sections=[section("semantic", "烹饪说明", "肉类需要炖煮，清汤需要控温。", 0.9)],
                stats={},
            )

        async def grep_chunks(self, *_args, **_kwargs):
            return []

    def broken_segmenter(_text: str):
        raise RuntimeError("tokenizer unavailable")

    monkeypatch.setattr(query_analysis, "_jieba_segment", broken_segmenter)
    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="tokenizer-fallback",
            sag_source_config_id=(f"tokenizer-fallback-{uuid4().hex}")[:36],
        )
        session.add(source)
        await session.commit()
        outcome = await retrieve_relevant_sections(SemanticEngine(), [source], "肉类清汤")

    assert [item.chunk_id for item in outcome.sections] == ["semantic"]
    assert outcome.stats["chinese_segmentation_used"] is False


def test_fallback_answer_cites_only_selected_sections():
    selected = [
        section("one", "公益行动", "张杰为乡村儿童建设音乐教室。", 0.9),
        section("two", "赈灾捐助", "团队向受灾地区捐赠物资。", 0.8),
    ]

    answer = fallback_search_answer("张杰有哪些公益行动", selected)

    assert "张杰为乡村儿童建设音乐教室" in answer
    assert "[1]" in answer and "[2]" in answer
    assert "[3]" not in answer


def test_fallback_excerpt_honors_segmentation_rollback(monkeypatch):
    from sag_api.core.config import settings

    selected = [
        section(
            "target",
            "烹饪说明",
            "无关说明很长。肉类需要较长时间炖煮，清汤只需要五到十分钟。",
            0.9,
        )
    ]
    monkeypatch.setattr(settings, "search_chinese_segmentation_enabled", False)

    answer = fallback_search_answer("肉类清汤", selected)

    assert "无关说明很长" in answer
    assert "肉类需要较长时间炖煮" not in answer


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


@pytest.mark.asyncio
async def test_retrieval_excludes_logically_deleted_document_before_physical_cleanup():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Source
    from sag_api.enums import DocumentStatus
    from sag_api.sag import SearchOutcome
    from sag_api.services.retrieval_service import retrieve_relevant_sections

    class FakeEngine:
        async def search_many(self, _targets, query, **_kwargs):
            return SearchOutcome(
                query=query,
                sections=[
                    RetrievedSection(
                        chunk_id="hidden",
                        heading="删除文档",
                        content="目标主题来自已删除文档。",
                        score=0.99,
                        source_id="engine-hidden",
                        source_config_id="visibility-config",
                    ),
                    RetrievedSection(
                        chunk_id="visible",
                        heading="保留文档",
                        content="目标主题来自保留文档。",
                        score=0.9,
                        source_id="engine-visible",
                        source_config_id="visibility-config",
                    ),
                ],
                stats={},
            )

        async def grep_chunks(self, *_args, **_kwargs):
            return []

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="visibility", sag_source_config_id="visibility-config"[:36])
        session.add(source)
        await session.flush()
        session.add_all(
            [
                Document(
                    source_id=source.id,
                    filename="hidden.md",
                    content_type="text/markdown",
                    size_bytes=10,
                    storage_path="/tmp/hidden.md",
                    status=DocumentStatus.DELETING,
                    sag_source_id="engine-hidden",
                ),
                Document(
                    source_id=source.id,
                    filename="visible.md",
                    content_type="text/markdown",
                    size_bytes=10,
                    storage_path="/tmp/visible.md",
                    status=DocumentStatus.READY,
                    sag_source_id="engine-visible",
                ),
            ]
        )
        await session.commit()

        outcome = await retrieve_relevant_sections(
            FakeEngine(),
            [source],
            "目标主题",
            top_k=8,
        )

    assert [item.chunk_id for item in outcome.sections] == ["visible"]


@pytest.mark.asyncio
async def test_lexical_retrieval_keeps_visible_peer_during_logical_delete():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Source
    from sag_api.enums import DocumentStatus
    from sag_api.sag import SearchOutcome
    from sag_api.services.retrieval_service import retrieve_relevant_sections

    class FakeEngine:
        async def search_many(self, _targets, query, **_kwargs):
            return SearchOutcome(query=query, sections=[], stats={})

        async def grep_chunks(self, *_args, **_kwargs):
            return [
                {"chunk_id": "hidden", "heading": "目标主题", "snippet": "隐藏", "source_id": "engine-hidden"},
                {"chunk_id": "visible", "heading": "目标主题", "snippet": "保留", "source_id": "engine-visible"},
            ]

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="lexical-visibility", sag_source_config_id="lexical-config"[:36])
        session.add(source)
        await session.flush()
        session.add(
            Document(
                source_id=source.id,
                filename="hidden.md",
                content_type="text/markdown",
                size_bytes=10,
                storage_path="/tmp/hidden.md",
                status=DocumentStatus.DELETING,
                sag_source_id="engine-hidden",
            )
        )
        await session.commit()
        outcome = await retrieve_relevant_sections(FakeEngine(), [source], "目标主题")

    assert [item.chunk_id for item in outcome.sections] == ["visible"]


@pytest.mark.asyncio
async def test_event_recall_excludes_events_from_logically_deleted_document():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.retrieval_service import recall_event_scores

    class FakeEngine:
        async def search_event_scores(self, *_args, **_kwargs):
            return {
                ("event-config", "hidden-event"): 0.99,
                ("event-config", "visible-event"): 0.9,
            }

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="event-visibility", sag_source_config_id="event-config"[:36])
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="hidden.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/hidden.md",
            status=DocumentStatus.DELETING,
            sag_source_id="engine-hidden",
        )
        session.add(document)
        await session.flush()
        session.add(
            Job(
                type=JobType.PROCESS_DOCUMENT,
                status=JobStatus.SUCCEEDED,
                source_id=source.id,
                document_id=document.id,
                payload={"process_checkpoint": {"event_ids": ["hidden-event"]}},
            )
        )
        await session.commit()
        scores = await recall_event_scores(
            FakeEngine(),
            "目标主题",
            {source.sag_source_config_id: source},
        )

    assert scores == {("event-config", "visible-event"): 0.9}


@pytest.mark.asyncio
async def test_delete_failed_source_is_filtered_before_vector_top_k():
    """A large hidden document must not starve a healthy peer from retrieval."""
    from uuid import uuid4

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Source
    from sag_api.enums import DocumentStatus
    from sag_api.sag import SearchOutcome
    from sag_api.services.retrieval_service import retrieve_relevant_sections

    await init_db()
    config_id = f"prefilter-{uuid4().hex}"[:36]
    async with SessionLocal() as session:
        source = Source(name="prefilter", sag_source_config_id=config_id)
        session.add(source)
        await session.flush()
        session.add_all(
            [
                Document(
                    source_id=source.id,
                    filename="hidden.pdf",
                    content_type="application/pdf",
                    size_bytes=10,
                    storage_path="/tmp/prefilter-hidden.pdf",
                    status=DocumentStatus.DELETE_FAILED,
                    sag_source_id="engine-hidden-large",
                ),
                Document(
                    source_id=source.id,
                    filename="visible.pdf",
                    content_type="application/pdf",
                    size_bytes=10,
                    storage_path="/tmp/prefilter-visible.pdf",
                    status=DocumentStatus.READY,
                    sag_source_id="engine-visible-peer",
                ),
            ]
        )
        await session.commit()

        class VisibilityAwareFakeEngine:
            supports_document_source_exclusions = True

            async def search_many(
                self,
                _targets,
                query,
                *,
                strategy=None,
                top_k=None,
                exclude_source_ids_by_config=None,
            ):
                del strategy
                rows = [
                    RetrievedSection(
                        chunk_id=f"hidden-{index}",
                        heading="目标主题",
                        content="目标主题来自删除失败的大文档。",
                        score=0.99 - index * 0.001,
                        source_id="engine-hidden-large",
                        source_config_id=config_id,
                    )
                    for index in range(25)
                ]
                rows.append(
                    RetrievedSection(
                        chunk_id="visible-26",
                        heading="目标主题",
                        content="目标主题来自仍然可用的健康文档。",
                        score=0.8,
                        source_id="engine-visible-peer",
                        source_config_id=config_id,
                    )
                )
                excluded = set(
                    (exclude_source_ids_by_config or {}).get(config_id, ())
                )
                rows = [row for row in rows if row.source_id not in excluded]
                return SearchOutcome(
                    query=query,
                    sections=rows[: int(top_k or 8)],
                    stats={},
                )

            async def grep_chunks(self, *_args, **_kwargs):
                return []

        outcome = await retrieve_relevant_sections(
            VisibilityAwareFakeEngine(),
            [source],
            "目标主题",
            strategy="multi_es_fast",
            top_k=8,
        )

    assert [section.chunk_id for section in outcome.sections] == ["visible-26"]
