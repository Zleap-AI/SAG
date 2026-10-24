"""Shared bounded retrieval, reranking, and evidence-grounded search answers."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from sag_api.core.config import settings
from sag_api.core.logging import get_logger
from sag_api.enums import JEV_SCORED_STRATEGIES, normalize_search_strategy
from sag_api.sag import RetrievedSection, SearchOutcome
from sag_api.services.query_analysis import (
    QueryAnalysis,
    analyze_query,
    normalize_lexical_text,
)

log = get_logger("retrieval")


class SearchSource(Protocol):
    id: str
    name: str
    sag_source_config_id: str


EventScoreMap = dict[tuple[str, str], float]
DocumentSourceScope = dict[str, tuple[str, ...]]


def event_scores_from_evidence(sections: list[RetrievedSection]) -> EventScoreMap:
    """Keep selected event IDs in evidence order, excluding unscored chunk fallback."""
    scores: EventScoreMap = {}
    for section in sections:
        if not section.source_config_id or section.metadata.get("score_source") == "query_chunk_fallback":
            continue
        for event_id in section.metadata.get("event_ids") or []:
            if isinstance(event_id, str) and event_id.strip():
                # Hydrated chunks expose aggregate scores, not raw event scores.
                scores.setdefault((section.source_config_id, event_id.strip()), section.score)
    return scores


async def published_document_inputs(sources: list[SearchSource]) -> DocumentSourceScope:
    """Resolve only current qualified publication pointers, including paused candidates."""
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Document, Source

    by_source = {source.id: source.sag_source_config_id for source in sources}
    values: dict[str, set[str]] = {source.sag_source_config_id: set() for source in sources}
    if not by_source:
        return {}
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Document.source_id, Document.sag_source_id).join(Source, Document.source_id == Source.id).where(
                    Document.source_id.in_(by_source),
                    Document.has_publication,
                    Source.is_deleting.is_(False),
                )
            )
        ).all()
    for source_id, input_id in rows:
        if input_id:
            values[by_source[source_id]].add(input_id)
    return {scid: tuple(sorted(ids)) for scid, ids in values.items()}


async def grep_published_chunks(engine_manager, source: SearchSource, pattern: str, *, limit: int) -> list[dict]:
    """Apply current publication pointers before LIMIT and recheck before returning evidence."""
    published = await published_document_inputs([source])
    input_ids = published.get(source.sag_source_config_id, ())
    if not input_ids:
        return []
    rows = await engine_manager.grep_chunks(
        source.sag_source_config_id,
        pattern,
        source=source,
        limit=limit,
        source_ids=input_ids,
    )
    current = await published_document_inputs([source])
    visible = current.get(source.sag_source_config_id, ())
    return [row for row in rows if row["source_id"] in visible]


async def get_published_chunk(engine_manager, source: SearchSource, chunk_id: str):
    published = await published_document_inputs([source])
    input_ids = published.get(source.sag_source_config_id, ())
    if not input_ids:
        return None
    chunk = await engine_manager.get_chunk(
        source.sag_source_config_id,
        chunk_id,
        source=source,
        source_ids=input_ids,
    )
    return chunk if published == await published_document_inputs([source]) else None


_BOILERPLATE = (
    "新浪首页",
    "权利保护声明",
    "阅读排行榜",
    "评论排行榜",
    "点击加载更多",
    "免责声明",
)
_CITATION_RE = re.compile(r"\[(\d+)]")


def _section_key(section: RetrievedSection) -> tuple[str, str]:
    source = (section.source_config_id or section.source_id or "").strip()
    chunk = (section.chunk_id or "").strip()
    if chunk:
        return source, chunk
    fingerprint = normalize_lexical_text(f"{section.heading}\n{section.content}")[:240]
    return source, fingerprint


def _lexical_relevance(
    query: str,
    section: RetrievedSection,
    *,
    analysis: QueryAnalysis | None = None,
) -> float:
    effective = analysis or analyze_query(
        query,
        segmentation_enabled=settings.search_chinese_segmentation_enabled,
    )
    heading = normalize_lexical_text(section.heading)
    content = normalize_lexical_text(section.content)
    text = f"{heading}{content}"
    if not text:
        return 0.0

    terms = [normalize_lexical_text(term) for term in effective.scoring_terms]
    terms = [term for term in terms if term]
    phrase = effective.normalized_phrase

    score = 0.0
    if phrase and len(phrase) >= 2 and phrase in text:
        score += 0.55
        if phrase in heading:
            score += 0.2
    if terms:
        matched = sum(term in text for term in terms)
        heading_matched = sum(term in heading for term in terms)
        score += 0.35 * matched / len(terms)
        score += 0.15 * heading_matched / len(terms)
    return min(1.0, score)


def _is_boilerplate(section: RetrievedSection) -> bool:
    text = f"{section.heading}\n{section.content}"
    return sum(marker in text for marker in _BOILERPLATE) >= 2


@dataclass(frozen=True, slots=True)
class RerankResult:
    sections: list[RetrievedSection]
    candidate_count: int
    relevant_count: int
    filtered_count: int
    lexical_count: int


def rerank_sections(
    query: str,
    semantic: list[RetrievedSection],
    *,
    lexical: list[RetrievedSection] | None = None,
    exact_lexical_keys: set[tuple[str, str]] | None = None,
    limit: int,
    analysis: QueryAnalysis | None = None,
) -> RerankResult:
    """Hybrid rerank with an explicit relevance gate before anything reaches an answer."""

    effective = analysis or analyze_query(
        query,
        segmentation_enabled=settings.search_chinese_segmentation_enabled,
    )
    semantic_keys = {_section_key(section) for section in semantic}
    lexical = lexical or []
    lexical_keys = {_section_key(section) for section in lexical}
    exact_keys = lexical_keys if exact_lexical_keys is None else lexical_keys.intersection(exact_lexical_keys)
    merged: dict[tuple[str, str], tuple[RetrievedSection, int]] = {}
    for index, section in enumerate([*semantic, *lexical]):
        key = _section_key(section)
        if not key[1]:
            continue
        previous = merged.get(key)
        if previous is None:
            merged[key] = (section, index)
            continue
        previous_section, previous_index = previous
        chosen = section if len(section.content.strip()) > len(previous_section.content.strip()) else previous_section
        merged[key] = (
            chosen.model_copy(update={"score": max(float(previous_section.score), float(section.score))}),
            min(previous_index, index),
        )

    candidates = list(merged.items())
    if not candidates:
        return RerankResult([], 0, 0, 0, len(lexical))

    raw_scores = [max(0.0, float(item[1][0].score or 0.0)) for item in candidates]
    top_raw = max(raw_scores, default=0.0)
    semantic_floor = max(0.35, top_raw * 0.68)
    denominator = max(1, len(candidates) - 1)
    lexical_scores = {
        key: _lexical_relevance(query, section, analysis=effective) for key, (section, _index) in candidates
    }
    has_lexical_signal = any(key in lexical_keys or score >= 0.2 for key, score in lexical_scores.items())
    has_strong_lexical_signal = any(score >= 0.35 for score in lexical_scores.values())
    ranked: list[tuple[float, float, int, RetrievedSection]] = []

    for position, (key, (section, original_index)) in enumerate(candidates):
        raw = max(0.0, min(1.0, float(section.score or 0.0)))
        lexical_score = lexical_scores[key]
        exact = key in exact_keys
        lexical_match = key in lexical_keys
        if _is_boilerplate(section) and not lexical_match and lexical_score < 0.35:
            continue
        rank_score = 1.0 - position / denominator
        combined = min(
            1.0,
            raw * 0.5 + rank_score * 0.2 + lexical_score * 0.3 + (0.15 if exact else 0.0),
        )
        if has_lexical_signal:
            relevant = lexical_match or lexical_score >= 0.2 or (
                not has_strong_lexical_signal and key in semantic_keys
                and raw >= max(0.8, semantic_floor) and not _is_boilerplate(section)
            )
        else:
            relevant = raw >= semantic_floor
        if not relevant:
            continue
        ranked.append((combined, raw, original_index, section))

    ranked.sort(key=lambda item: (-item[0], -item[1], item[2], _section_key(item[3])))
    selected = [
        section.model_copy(update={"score": round(score, 6), "rank": index})
        for index, (score, _raw, _original, section) in enumerate(ranked[: max(1, limit)])
    ]
    return RerankResult(
        sections=selected,
        candidate_count=len(candidates),
        relevant_count=len(ranked),
        filtered_count=len(candidates) - len(ranked),
        lexical_count=len(lexical),
    )


async def _lexical_sections(
    engine_manager: Any,
    sources: list[SearchSource],
    *,
    analysis: QueryAnalysis,
    source_ids_by_config: DocumentSourceScope,
) -> tuple[list[RetrievedSection], set[tuple[str, str]]]:
    grep_chunks = getattr(engine_manager, "grep_chunks", None)
    terms = analysis.lookup_terms
    if not callable(grep_chunks) or not terms:
        return [], set()

    semaphore = asyncio.Semaphore(max(1, settings.search_source_concurrency))
    expanded_keys = {normalize_lexical_text(term) for term in analysis.expanded_terms}

    async def one(
        source: SearchSource,
        term: str,
    ) -> tuple[list[RetrievedSection], bool]:
        async with semaphore:
            try:
                options: dict[str, Any] = {
                    "source": source,
                    "limit": 2,
                }
                options["source_ids"] = source_ids_by_config[source.sag_source_config_id]
                rows = await grep_chunks(
                    source.sag_source_config_id,
                    term,
                    **options,
                )
            except Exception:  # noqa: BLE001
                return [], False
        sections = [
            RetrievedSection(
                chunk_id=row.get("chunk_id"),
                heading=row.get("heading") or "精确匹配",
                content=row.get("snippet") or "",
                score=max(0.8, 1.0 - index * 0.02),
                rank=index,
                source_id=row.get("source_id"),
                source_config_id=source.sag_source_config_id,
            )
            for index, row in enumerate(rows)
        ]
        return sections, normalize_lexical_text(term) not in expanded_keys

    groups = await asyncio.gather(*(one(source, term) for source in sources for term in terms))
    sections = [section for group, _strong in groups for section in group]
    exact_keys = {_section_key(section) for group, strong in groups if strong for section in group}
    return sections, exact_keys


async def retrieve_relevant_sections(
    engine_manager: Any,
    sources: list[SearchSource],
    query: str,
    *,
    strategy: str | None = None,
    top_k: int | None = None,
    include_ranked_candidates: bool = False,
) -> SearchOutcome:
    """One retrieval contract for search UI and the Agent's search_context tool."""

    total_start = time.perf_counter()
    configuration = getattr(engine_manager, "_settings", settings)
    requested_strategy = strategy or configuration.search_strategy
    mode = normalize_search_strategy(requested_strategy)
    if mode in JEV_SCORED_STRATEGIES:
        # The JEV-scored profile owns recall and final scoring. Adding application
        # lexical hits afterwards would mix unscored evidence into the ranked list.
        requested_limit = max(1, min(int(top_k or configuration.search_top_k), 50))
        published = await published_document_inputs(sources)
        visible_sources = [source for source in sources if published.get(source.sag_source_config_id)]
        options: dict[str, Any] = {
            "strategy": mode,
            "top_k": requested_limit,
            "source_ids_by_config": published,
        }
        if include_ranked_candidates:
            options["include_ranked_candidates"] = True
        engine_start = time.perf_counter()
        outcome = await engine_manager.search_many(
            [(source.sag_source_config_id, source) for source in visible_sources], query, **options,
        )
        current = await published_document_inputs(visible_sources)
        visible_sections = [
            section for section in outcome.sections
            if section.source_id and section.source_id in current.get(section.source_config_id or "", ())
        ]
        return SearchOutcome(query=outcome.query or query, sections=visible_sections[:requested_limit], stats={
            **outcome.stats,
            "requested_strategy": requested_strategy,
            "requested_top_k": requested_limit,
            "candidate_top_k": requested_limit,
            "candidates": len(outcome.sections),
            "relevant": len(visible_sections),
            "filtered_irrelevant": 0,
            "lexical_candidates": 0,
            "has_more": len(visible_sections) > requested_limit,
            "publication_filtered": len(outcome.sections) - len(visible_sections),
            "latency_engine_ms": round((time.perf_counter() - engine_start) * 1000, 2),
            "latency_rerank_ms": 0.0,
            "latency_total_ms": round((time.perf_counter() - total_start) * 1000, 2),
        })

    analysis = analyze_query(
        query,
        segmentation_enabled=settings.search_chinese_segmentation_enabled,
    )
    requested_limit = max(1, min(int(top_k or settings.search_top_k), 50))
    candidate_limit = min(50, max(requested_limit * 3, requested_limit + 8))
    published = await published_document_inputs(sources)
    sources = [source for source in sources if published.get(source.sag_source_config_id)]
    targets = [(source.sag_source_config_id, source) for source in sources]
    engine_start = time.perf_counter()
    search_options: dict[str, Any] = {
        "strategy": mode,
        "top_k": candidate_limit,
    }
    if include_ranked_candidates:
        search_options["include_ranked_candidates"] = True
    search_options["source_ids_by_config"] = published
    outcome, lexical_recall = await asyncio.gather(
        engine_manager.search_many(targets, query, **search_options),
        _lexical_sections(
            engine_manager,
            sources,
            analysis=analysis,
            source_ids_by_config=published,
        ),
    )
    lexical, exact_lexical_keys = lexical_recall
    # A publication switch or delete can commit while recall is in flight.
    # Recheck current pointers before returning evidence.
    current = await published_document_inputs(sources)
    engine_latency_ms = round((time.perf_counter() - engine_start) * 1000, 2)

    def visible(section: RetrievedSection) -> bool:
        return bool(section.source_id) and section.source_id in current.get(section.source_config_id or "", ())

    semantic_sections = [section for section in outcome.sections if visible(section)]
    lexical_sections = [section for section in lexical if visible(section)]
    hidden_count = len(outcome.sections) + len(lexical) - len(semantic_sections) - len(lexical_sections)
    rerank_start = time.perf_counter()
    reranked = rerank_sections(
        query,
        semantic_sections,
        lexical=lexical_sections,
        exact_lexical_keys=exact_lexical_keys,
        limit=requested_limit,
        analysis=analysis,
    )
    rerank_latency_ms = round((time.perf_counter() - rerank_start) * 1000, 2)
    total_latency_ms = round((time.perf_counter() - total_start) * 1000, 2)
    stats = {
        **outcome.stats,
        "requested_strategy": requested_strategy,
        "requested_top_k": requested_limit,
        "candidate_top_k": candidate_limit,
        "candidates": reranked.candidate_count,
        "relevant": reranked.relevant_count,
        "filtered_irrelevant": reranked.filtered_count,
        "lexical_candidates": reranked.lexical_count,
        "chinese_segmentation_used": analysis.chinese_segmentation_used,
        "lexical_term_count": len(analysis.lookup_terms),
        "has_more": reranked.relevant_count > len(reranked.sections),
        "publication_filtered": hidden_count,
        "latency_engine_ms": engine_latency_ms,
        "latency_rerank_ms": rerank_latency_ms,
        "latency_total_ms": total_latency_ms,
    }
    return SearchOutcome(
        query=outcome.query or query,
        sections=reranked.sections,
        stats=stats,
    )


async def recall_event_scores(
    engine_manager: Any,
    query: str,
    sources_by_config: dict[str, SearchSource],
    *,
    limit: int | None = None,
) -> EventScoreMap:
    """Best-effort direct event recall shared by Search and the Agent.

    Chunks remain the traceable evidence path.  Event recall supplies the
    semantic result layer (title + summary) so a long document does not lose
    its extracted events merely because the best matching chunks are located
    elsewhere in the document.
    """

    search = getattr(engine_manager, "search_event_scores", None)
    if not callable(search):
        return {}
    published = await published_document_inputs(list(sources_by_config.values()))
    sources_by_config = {scid: source for scid, source in sources_by_config.items() if published.get(scid)}
    if not sources_by_config:
        return {}
    try:
        result = await search(query, sources_by_config, limit=limit, source_ids_by_config=published)
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001
        log.warning("事项向量召回失败，继续使用原文块结果：%s", error)
        return {}
    if not isinstance(result, dict):
        return {}
    current = await published_document_inputs(list(sources_by_config.values()))
    if any(current.get(scid) != published.get(scid) for scid in sources_by_config):
        return {}

    scores: EventScoreMap = {}
    for raw_key, raw_score in result.items():
        if not isinstance(raw_key, tuple) or len(raw_key) != 2:
            continue
        source_config_id = str(raw_key[0] or "").strip()
        event_id = str(raw_key[1] or "").strip()
        if not source_config_id or not event_id:
            continue
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            continue
        scores[(source_config_id, event_id)] = max(0.0, min(1.0, score))
    return scores


def _best_excerpt(
    query: str,
    section: RetrievedSection,
    limit: int = 260,
    *,
    analysis: QueryAnalysis | None = None,
) -> str:
    effective = analysis or analyze_query(
        query,
        segmentation_enabled=settings.search_chinese_segmentation_enabled,
    )
    content = re.sub(r"\s+", " ", section.content).strip()
    if not content:
        return section.heading.strip()
    sentences = [part.strip() for part in re.split(r"(?<=[。！？.!?])", content) if part.strip()]
    terms = [normalize_lexical_text(term) for term in effective.scoring_terms]
    best = max(
        sentences or [content],
        key=lambda sentence: sum(term in normalize_lexical_text(sentence) for term in terms),
    )
    return best[:limit] + ("…" if len(best) > limit else "")


def fallback_search_answer(query: str, sections: list[RetrievedSection]) -> str:
    if not sections:
        return ""
    analysis = analyze_query(
        query,
        segmentation_enabled=settings.search_chinese_segmentation_enabled,
    )
    lines = [
        f"- {_best_excerpt(query, section, analysis=analysis)} [{index}]"
        for index, section in enumerate(sections[:4], 1)
    ]
    return "根据与问题直接相关的证据：\n" + "\n".join(lines)


def _validated_answer(answer: str, section_count: int) -> str | None:
    text = answer.strip()
    if not text:
        return None
    references = [int(value) for value in _CITATION_RE.findall(text)]
    if not references or any(value < 1 or value > section_count for value in references):
        return None
    return text


@dataclass(frozen=True, slots=True)
class SearchAnswerUpdate:
    kind: Literal["delta", "completed"]
    text: str


def _search_answer_messages(
    query: str,
    sections: list[RetrievedSection],
) -> tuple[list[dict[str, str]], int]:
    evidence_blocks: list[str] = []
    used = 0
    for index, section in enumerate(sections, 1):
        block = f"[{index}] {section.heading or '相关资料'}\n{section.content.strip()}"
        remaining = 12000 - used
        if remaining <= 0:
            break
        block = block[:remaining]
        evidence_blocks.append(block)
        used += len(block)
    return (
        [
            {
                "role": "system",
                "content": (
                    "你是检索结果回答器。只回答用户提出的具体问题，不要概括候选集合。"
                    "只能使用给定证据；忽略与问题无关的内容。每个事实性结论必须标注"
                    "对应的 [编号]，编号只能来自证据。证据不足时明确说明不足，不得补充"
                    "常识或猜测。回答简洁、直接。"
                ),
            },
            {
                "role": "user",
                "content": (f"问题：{query}\n\n已通过相关性重排的证据：\n" + "\n\n".join(evidence_blocks)),
            },
        ],
        len(evidence_blocks),
    )


async def synthesize_search_answer(
    query: str,
    sections: list[RetrievedSection],
    *,
    llm: Any | None,
) -> str:
    """Answer the actual question from selected evidence; never summarize the raw candidate pool."""

    fallback = fallback_search_answer(query, sections)
    if not sections or llm is None or not getattr(llm, "configured", False):
        return fallback

    messages, evidence_count = _search_answer_messages(query, sections)
    try:
        answer = await llm.complete(messages)
    except Exception as error:  # noqa: BLE001
        log.warning("搜索答案生成失败，回退证据摘要：%s", error)
        return fallback
    return _validated_answer(answer, evidence_count) or fallback


async def stream_synthesize_search_answer(
    query: str,
    sections: list[RetrievedSection],
    *,
    llm: Any | None,
) -> AsyncIterator[SearchAnswerUpdate]:
    """Yield true provider deltas followed by one citation-validated answer."""

    fallback = fallback_search_answer(query, sections)
    if not sections or llm is None or not getattr(llm, "configured", False):
        yield SearchAnswerUpdate(kind="completed", text=fallback)
        return

    messages, evidence_count = _search_answer_messages(query, sections)
    parts: list[str] = []
    try:
        async for delta in llm.stream_complete(messages):
            if not delta:
                continue
            parts.append(delta)
            yield SearchAnswerUpdate(kind="delta", text=delta)
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001
        log.warning("搜索答案流生成失败，回退证据摘要：%s", error)
        yield SearchAnswerUpdate(kind="completed", text=fallback)
        return

    answer = _validated_answer("".join(parts), evidence_count) or fallback
    yield SearchAnswerUpdate(kind="completed", text=answer)
