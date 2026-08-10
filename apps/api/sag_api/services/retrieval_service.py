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
from sag_api.sag import RetrievedSection, SearchOutcome

log = get_logger("retrieval")


class SearchSource(Protocol):
    id: str
    name: str
    sag_source_config_id: str


EventScoreMap = dict[tuple[str, str], float]
DocumentSourceExclusions = dict[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _HiddenDerivatives:
    source_ids: frozenset[str] = frozenset()
    source_keys: frozenset[tuple[str, str]] = frozenset()
    source_config_ids: frozenset[str] = frozenset()
    event_keys: frozenset[tuple[str, str]] = frozenset()


async def _hidden_document_derivatives(
    sources: list[SearchSource],
) -> _HiddenDerivatives:
    """Read the persisted logical-delete/reprocess barrier for retrieval."""
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Document, Job
    from sag_api.enums import DocumentStatus, JobStatus, JobType

    source_ids = [source.id for source in sources]
    if not source_ids:
        return _HiddenDerivatives()
    config_by_source = {
        source.id: source.sag_source_config_id for source in sources
    }
    hidden_document_ids: set[str] = set()
    hidden_source_ids: set[str] = set()
    hidden_source_keys: set[tuple[str, str]] = set()
    hidden_configs: set[str] = set()
    hidden_event_keys: set[tuple[str, str]] = set()
    async with SessionLocal() as session:
        documents = list(
            (
                await session.scalars(
                    select(Document).where(
                        Document.source_id.in_(source_ids),
                        Document.status.in_(
                            [DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED]
                        ),
                    )
                )
            ).all()
        )
        active_control_jobs = list(
            (
                await session.scalars(
                    select(Job).where(
                        Job.source_id.in_(source_ids),
                        Job.type.in_([JobType.DELETE_DOCUMENT, JobType.REPROCESS_DOCUMENT]),
                        Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                    )
                )
            ).all()
        )
        failed_control_jobs = list(
            (
                await session.scalars(
                    select(Job)
                    .join(Document, Job.document_id == Document.id)
                    .where(
                        Job.source_id.in_(source_ids),
                        Job.type.in_([JobType.DELETE_DOCUMENT, JobType.REPROCESS_DOCUMENT]),
                        Job.status == JobStatus.FAILED,
                        Document.status.in_([DocumentStatus.FAILED, DocumentStatus.DELETE_FAILED]),
                    )
                )
            ).all()
        )
        active_control_jobs.extend(failed_control_jobs)
        hidden_document_ids.update(document.id for document in documents)
        hidden_document_ids.update(
            job.document_id for job in active_control_jobs if job.document_id
        )
        if hidden_document_ids:
            histories = list(
                (
                    await session.scalars(
                        select(Job).where(Job.document_id.in_(hidden_document_ids))
                    )
                ).all()
            )
        else:
            histories = []

    for document in documents:
        config_id = config_by_source.get(document.source_id)
        if document.sag_source_id:
            hidden_source_ids.add(document.sag_source_id)
            if config_id:
                hidden_source_keys.add((config_id, document.sag_source_id))
        if config_id:
            hidden_configs.add(config_id)
    for job in [*active_control_jobs, *histories]:
        payload = job.payload or {}
        checkpoint = payload.get("process_checkpoint")
        if isinstance(checkpoint, dict):
            value = checkpoint.get("source_id")
            if isinstance(value, str) and value.strip():
                normalized_source_id = value.strip()
                hidden_source_ids.add(normalized_source_id)
                config_id = config_by_source.get(job.source_id or "")
                if config_id:
                    hidden_source_keys.add((config_id, normalized_source_id))
            config_id = config_by_source.get(job.source_id or "")
            if config_id:
                hidden_event_keys.update(
                    (config_id, event_id.strip())
                    for event_id in checkpoint.get("event_ids", [])
                    if isinstance(event_id, str) and event_id.strip()
                )
        derived_source_ids = {
            value.strip()
            for value in payload.get("derived_source_ids", [])
            if isinstance(value, str) and value.strip()
        }
        hidden_source_ids.update(derived_source_ids)
        config_id = config_by_source.get(job.source_id or "")
        if config_id:
            hidden_source_keys.update(
                (config_id, source_id) for source_id in derived_source_ids
            )
            hidden_configs.add(config_id)
    return _HiddenDerivatives(
        source_ids=frozenset(hidden_source_ids),
        source_keys=frozenset(hidden_source_keys),
        source_config_ids=frozenset(hidden_configs),
        event_keys=frozenset(hidden_event_keys),
    )


def _document_source_exclusions(
    hidden: _HiddenDerivatives,
) -> DocumentSourceExclusions:
    """Scope hidden engine article IDs to their owning source config."""
    values: dict[str, set[str]] = {}
    for source_config_id, source_id in hidden.source_keys:
        values.setdefault(source_config_id, set()).add(source_id)
    return {
        source_config_id: tuple(sorted(source_ids))
        for source_config_id, source_ids in values.items()
    }


_QUERY_NOISE = (
    "知识库",
    "资料库",
    "资料中",
    "文档中",
    "告诉我",
    "帮我查",
    "搜索",
    "查询",
    "请问",
    "关于",
    "最新",
    "最近",
    "动态",
    "消息",
    "新闻",
    "内容",
    "资料",
    "一下",
    "是什么",
    "有哪些",
    "有什么",
)
_BOILERPLATE = (
    "新浪首页",
    "权利保护声明",
    "阅读排行榜",
    "评论排行榜",
    "点击加载更多",
    "免责声明",
)
_CITATION_RE = re.compile(r"\[(\d+)]")


def _normalized(value: str) -> str:
    return "".join(re.findall(r"[a-z0-9\u3400-\u9fff]+", value.lower()))


def query_terms(query: str) -> list[str]:
    """Extract a small, deterministic lexical signal without pretending to segment Chinese."""

    cleaned = query.strip().lower()
    for phrase in _QUERY_NOISE:
        cleaned = cleaned.replace(phrase, " ")
    candidates = re.findall(
        r"[a-z0-9][a-z0-9_.+-]{1,31}|[\u3400-\u9fff]{2,16}",
        cleaned,
    )
    terms: list[str] = []
    for candidate in candidates:
        value = candidate.strip()
        if value and not value.isdigit() and value not in terms:
            terms.append(value)
    return terms[:4]


def _section_key(section: RetrievedSection) -> tuple[str, str]:
    source = (section.source_config_id or section.source_id or "").strip()
    chunk = (section.chunk_id or "").strip()
    if chunk:
        return source, chunk
    fingerprint = _normalized(f"{section.heading}\n{section.content}")[:240]
    return source, fingerprint


def _lexical_relevance(query: str, section: RetrievedSection) -> float:
    heading = _normalized(section.heading)
    content = _normalized(section.content)
    text = f"{heading}{content}"
    if not text:
        return 0.0

    terms = [_normalized(term) for term in query_terms(query)]
    terms = [term for term in terms if term]
    cleaned_query = query
    for phrase in _QUERY_NOISE:
        cleaned_query = cleaned_query.replace(phrase, " ")
    phrase = _normalized(cleaned_query)

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
    limit: int,
) -> RerankResult:
    """Hybrid rerank with an explicit relevance gate before anything reaches an answer."""

    lexical = lexical or []
    exact_keys = {_section_key(section) for section in lexical}
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
    lexical_scores = {key: _lexical_relevance(query, section) for key, (section, _index) in candidates}
    has_lexical_signal = any(key in exact_keys or score >= 0.2 for key, score in lexical_scores.items())
    ranked: list[tuple[float, float, int, RetrievedSection]] = []

    for position, (key, (section, original_index)) in enumerate(candidates):
        raw = max(0.0, min(1.0, float(section.score or 0.0)))
        lexical_score = lexical_scores[key]
        exact = key in exact_keys
        if _is_boilerplate(section) and not exact and lexical_score < 0.35:
            continue
        rank_score = 1.0 - position / denominator
        combined = min(
            1.0,
            raw * 0.5 + rank_score * 0.2 + lexical_score * 0.3 + (0.15 if exact else 0.0),
        )
        if has_lexical_signal:
            relevant = exact or lexical_score >= 0.2
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
    query: str,
    *,
    exclude_source_ids_by_config: DocumentSourceExclusions | None = None,
) -> list[RetrievedSection]:
    grep_chunks = getattr(engine_manager, "grep_chunks", None)
    terms = query_terms(query)
    if not callable(grep_chunks) or not terms:
        return []

    semaphore = asyncio.Semaphore(max(1, settings.search_source_concurrency))

    async def one(source: SearchSource, term: str) -> list[RetrievedSection]:
        async with semaphore:
            try:
                options: dict[str, Any] = {
                    "source": source,
                    "limit": 2,
                }
                if (
                    getattr(
                        engine_manager,
                        "supports_document_source_exclusions",
                        False,
                    )
                    and exclude_source_ids_by_config
                ):
                    options["exclude_source_ids"] = (
                        exclude_source_ids_by_config.get(
                            source.sag_source_config_id,
                            (),
                        )
                    )
                rows = await grep_chunks(
                    source.sag_source_config_id,
                    term,
                    **options,
                )
            except Exception:  # noqa: BLE001
                return []
        return [
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

    groups = await asyncio.gather(*(one(source, term) for source in sources for term in terms))
    return [section for group in groups for section in group]


async def retrieve_relevant_sections(
    engine_manager: Any,
    sources: list[SearchSource],
    query: str,
    *,
    strategy: str | None = None,
    top_k: int | None = None,
) -> SearchOutcome:
    """One retrieval contract for search UI and the Agent's search_context tool."""

    requested_limit = max(1, min(int(top_k or settings.search_top_k), 50))
    candidate_limit = min(50, max(requested_limit * 3, requested_limit + 8))
    targets = [(source.sag_source_config_id, source) for source in sources]
    total_start = time.perf_counter()
    engine_start = time.perf_counter()
    hidden = await _hidden_document_derivatives(sources)
    exclusions = _document_source_exclusions(hidden)
    search_options: dict[str, Any] = {
        "strategy": strategy,
        "top_k": candidate_limit,
    }
    if (
        getattr(
            engine_manager,
            "supports_document_source_exclusions",
            False,
        )
        and exclusions
    ):
        search_options["exclude_source_ids_by_config"] = exclusions
    outcome, lexical = await asyncio.gather(
        engine_manager.search_many(targets, query, **search_options),
        _lexical_sections(
            engine_manager,
            sources,
            query,
            exclude_source_ids_by_config=exclusions,
        ),
    )
    # A delete can commit while either engine query is in flight. Re-read the
    # persisted barrier before returning evidence so the delete is linearized.
    hidden = await _hidden_document_derivatives(sources)
    engine_latency_ms = round((time.perf_counter() - engine_start) * 1000, 2)
    def visible(section: RetrievedSection) -> bool:
        if section.source_id:
            return section.source_id not in hidden.source_ids
        return section.source_config_id not in hidden.source_config_ids

    semantic_sections = [section for section in outcome.sections if visible(section)]
    lexical_sections = [section for section in lexical if visible(section)]
    hidden_count = len(outcome.sections) + len(lexical) - len(semantic_sections) - len(lexical_sections)
    rerank_start = time.perf_counter()
    reranked = rerank_sections(
        query,
        semantic_sections,
        lexical=lexical_sections,
        limit=requested_limit,
    )
    rerank_latency_ms = round((time.perf_counter() - rerank_start) * 1000, 2)
    total_latency_ms = round((time.perf_counter() - total_start) * 1000, 2)
    stats = {
        **outcome.stats,
        "requested_top_k": requested_limit,
        "candidate_top_k": candidate_limit,
        "candidates": reranked.candidate_count,
        "relevant": reranked.relevant_count,
        "filtered_irrelevant": reranked.filtered_count,
        "lexical_candidates": reranked.lexical_count,
        "has_more": reranked.relevant_count > len(reranked.sections),
        "logically_deleted_filtered": hidden_count,
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
    hidden = await _hidden_document_derivatives(list(sources_by_config.values()))
    try:
        result = await search(query, sources_by_config, limit=limit)
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001
        log.warning("事项向量召回失败，继续使用原文块结果：%s", error)
        return {}
    if not isinstance(result, dict):
        return {}

    scores: EventScoreMap = {}
    for raw_key, raw_score in result.items():
        if not isinstance(raw_key, tuple) or len(raw_key) != 2:
            continue
        source_config_id = str(raw_key[0] or "").strip()
        event_id = str(raw_key[1] or "").strip()
        if not source_config_id or not event_id:
            continue
        if (source_config_id, event_id) in hidden.event_keys:
            continue
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            continue
        scores[(source_config_id, event_id)] = max(0.0, min(1.0, score))
    return scores


def _best_excerpt(query: str, section: RetrievedSection, limit: int = 260) -> str:
    content = re.sub(r"\s+", " ", section.content).strip()
    if not content:
        return section.heading.strip()
    sentences = [part.strip() for part in re.split(r"(?<=[。！？.!?])", content) if part.strip()]
    terms = [_normalized(term) for term in query_terms(query)]
    best = max(
        sentences or [content],
        key=lambda sentence: sum(term in _normalized(sentence) for term in terms),
    )
    return best[:limit] + ("…" if len(best) > limit else "")


def fallback_search_answer(query: str, sections: list[RetrievedSection]) -> str:
    if not sections:
        return ""
    lines = [f"- {_best_excerpt(query, section)} [{index}]" for index, section in enumerate(sections[:4], 1)]
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
