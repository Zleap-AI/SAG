"""检索读侧 —— 从 `EngineManager` 拆出的召回路径。

含单源检索（`search`，带精确→快速回退）、多源并发检索（`search_many`）、
跨源分块向量召回（`_search_chunk_vectors`）与事件向量召回（`search_event_scores`）。
策略判定（门面名 -> 引擎名、能力回退）仍由 `EngineManager` 持有，经
`EngineAccess` 注入，避免把配置耦合带进检索实现。
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import TYPE_CHECKING, Any

from zleap.sag.pipeline import SearchOptions, SearchOutputOptions, SearchRequest, SearchScope

from sag_api.core.error_taxonomy import ErrorStage
from sag_api.core.logging import get_logger
from sag_api.sag._timings_probe import capture_scope as _timings_capture_scope
from sag_api.sag._timings_probe import release_scope as _timings_release_scope
from sag_api.sag.dto import RetrievedSection, SearchOutcome
from sag_api.sag.errors import map_sag_errors

if TYPE_CHECKING:
    from sag_api.db.models import Source
    from sag_api.sag.engine_access import EngineAccess

log = get_logger("sag")


class SearchReader:
    """检索召回路径。"""

    def __init__(self, access: EngineAccess) -> None:
        self._access = access

    async def _search_raw(
        self,
        source_config_id: str,
        query: str,
        *,
        source: Source | None,
        strategy: str,
        top_k: int,
        include_ranked_candidates: bool = False,
    ) -> SearchOutcome:
        """单次检索（带每源时限）。超时抛 asyncio.TimeoutError。

        strategy 是门面名(vector/multi/multi_es_fast);进入 zleap 前翻译成引擎名。
        """
        timeout = max(1.0, self._access.settings.search_source_timeout)
        engine_strategy = self._access.zleap_engine_strategy(strategy)

        async def run() -> Any:
            # The timeout must include waiting for the per-source lock. Otherwise
            # concurrent searches can queue forever before the timed region starts.
            with map_sag_errors(stage=ErrorStage.RETRIEVE):
                async with self._access.use(source_config_id, source) as engine:
                    return await engine.search(
                        SearchRequest(
                            query=query,
                            scope=SearchScope(data_source_ids=(source_config_id,)),
                            options=SearchOptions(
                                strategy=engine_strategy,
                                top_k=top_k,
                                return_type="chunk",
                                output=SearchOutputOptions(
                                    include_ranked_candidates=include_ranked_candidates,
                                ),
                            ),
                        )
                    )

        # 开一个 timings 桶,捕获 zleap 检索链内部的每 step 耗时(见 _timings_probe)。
        bucket, token = _timings_capture_scope()
        try:
            result = await asyncio.wait_for(run(), timeout)
        finally:
            _timings_release_scope(token)
        outcome = SearchOutcome.from_result(result)
        if bucket:
            merged_stats = dict(outcome.stats)
            merged_stats["engine_timings"] = dict(bucket)
            outcome = SearchOutcome(query=outcome.query, sections=outcome.sections, stats=merged_stats)
        return outcome

    async def search(
        self,
        source_config_id: str,
        query: str,
        *,
        source: Source | None = None,
        strategy: str | None = None,
        top_k: int | None = None,
        include_ranked_candidates: bool = False,
    ) -> SearchOutcome:
        """检索（韧性版）：精确模式超时/失败/空结果时回退快速模式。

        精确模式的查询侧含 LLM 实体抽取（慢且可能失败重试）；事件向量层缺失的源也会空转。
        回退把这类退化收敛为一次快速向量检索，可经 `search_fallback_vector=false` 关闭。
        """
        strategy = self._access.effective_search_strategy(strategy)
        top_k = top_k or self._access.settings.search_top_k
        search_options: dict[str, Any] = {
            "source": source,
            "top_k": top_k,
        }
        if include_ranked_candidates:
            search_options["include_ranked_candidates"] = True
        try:
            outcome = await self._access.search_raw(
                source_config_id,
                query,
                strategy=strategy,
                **search_options,
            )
            if outcome.sections or strategy == "vector" or not self._access.settings.search_fallback_vector:
                return SearchOutcome(
                    query=outcome.query,
                    sections=outcome.sections,
                    stats={
                        **outcome.stats,
                        "requested_strategy": strategy,
                        "effective_strategy": strategy,
                        "fallback_used": False,
                    },
                )
            log.info("精确检索空结果，回退快速检索 source_config_id=%s", source_config_id)
        except TimeoutError:
            if strategy == "vector" or not self._access.settings.search_fallback_vector:
                raise
            log.warning(
                "检索超时(%.0fs) 回退 vector source_config_id=%s strategy=%s",
                self._access.settings.search_source_timeout,
                source_config_id,
                strategy,
            )
        except Exception as e:  # noqa: BLE001
            if strategy == "vector" or not self._access.settings.search_fallback_vector:
                raise
            log.warning(
                "检索失败回退 vector source_config_id=%s strategy=%s err=%s",
                source_config_id,
                strategy,
                getattr(e, "message", None) or e,
            )
        outcome = await self._access.search_raw(
            source_config_id,
            query,
            strategy="vector",
            **search_options,
        )
        return SearchOutcome(
            query=outcome.query,
            sections=outcome.sections,
            stats={
                **outcome.stats,
                "requested_strategy": strategy,
                "effective_strategy": "vector",
                "fallback_used": True,
            },
        )

    async def search_many(
        self,
        targets: list[tuple[str, Source | None]],
        query: str,
        *,
        strategy: str | None = None,
        top_k: int | None = None,
        exclude_source_ids_by_config: dict[str, tuple[str, ...]] | None = None,
        include_ranked_candidates: bool = False,
    ) -> SearchOutcome:
        """在统一候选与并发边界内检索；单源失败不影响整体结果。"""
        strategy = self._access.effective_search_strategy(strategy)
        top_k = top_k or self._access.settings.search_top_k
        per_source_k = max(top_k, 4)
        requested_sources = len(targets)
        targets = targets[: self._access.settings.search_source_candidate_limit]
        has_exclusions = any(source_ids for source_ids in (exclude_source_ids_by_config or {}).values())

        ranked_candidates_unavailable = include_ranked_candidates and has_exclusions

        def with_ranked_candidates_diagnostic(stats: dict[str, Any]) -> dict[str, Any]:
            if not ranked_candidates_unavailable:
                return stats
            return {
                **stats,
                "ranked_candidates": [],
                "ranked_candidates_unavailable_reason": "document_source_exclusions",
            }

        if ((strategy == "vector" and not include_ranked_candidates) or has_exclusions) and targets:
            try:
                outcome = await self._access.search_chunk_vectors(
                    targets,
                    query,
                    top_k=top_k,
                    requested_sources=requested_sources,
                    exclude_source_ids_by_config=exclude_source_ids_by_config,
                )
                if has_exclusions and strategy != "vector":
                    return SearchOutcome(
                        query=outcome.query,
                        sections=outcome.sections,
                        stats=with_ranked_candidates_diagnostic(
                            {
                                **outcome.stats,
                                "requested_strategy": strategy,
                                "effective_strategy": "vector",
                                "fallback_used": True,
                            }
                        ),
                    )
                if ranked_candidates_unavailable:
                    return SearchOutcome(
                        query=outcome.query,
                        sections=outcome.sections,
                        stats=with_ranked_candidates_diagnostic(dict(outcome.stats)),
                    )
                return outcome
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                # The lexical branch in ``retrieve_relevant_sections`` is already
                # running in parallel. Return control to it instead of paying a
                # second timeout in the legacy per-source vector path.
                log.warning("批量块向量召回超时，保留并行词法结果")
                return SearchOutcome(
                    query=query,
                    sections=[],
                    stats=with_ranked_candidates_diagnostic(
                        {
                            "sources": len(targets),
                            "sources_requested": requested_sources,
                            "source_limit_applied": requested_sources > len(targets),
                            "candidates": 0,
                            "chunk_recall": (
                                "batch-vector-prefilter-timeout" if has_exclusions else "batch-vector-timeout"
                            ),
                        }
                    ),
                )
            except Exception as error:  # noqa: BLE001
                if has_exclusions:
                    log.warning(
                        "带删除屏障的批量块召回失败，不回退未过滤检索：%s",
                        error,
                    )
                    return SearchOutcome(
                        query=query,
                        sections=[],
                        stats=with_ranked_candidates_diagnostic(
                            {
                                "sources": len(targets),
                                "sources_requested": requested_sources,
                                "source_limit_applied": requested_sources > len(targets),
                                "candidates": 0,
                                "requested_strategy": strategy,
                                "effective_strategy": "vector",
                                "fallback_used": True,
                                "chunk_recall": "batch-vector-prefilter-failed",
                            }
                        ),
                    )
                # Keep the established per-source engine path as a compatibility
                # fallback for storage providers that cannot do a filtered batch kNN.
                log.warning("批量块向量召回失败，回退逐信源检索：%s", error)

        semaphore = asyncio.Semaphore(self._access.settings.search_source_concurrency)

        async def _one(scid: str, source: Source | None):
            async with semaphore:
                try:
                    outcome = await self._access.search(
                        scid,
                        query,
                        source=source,
                        strategy=strategy,
                        top_k=per_source_k,
                        include_ranked_candidates=include_ranked_candidates,
                    )
                    return scid, outcome
                except Exception as e:  # noqa: BLE001
                    log.warning("fan-out 检索失败 %s：%s", scid, getattr(e, "message", None) or e)
                    return None

        results = await asyncio.gather(*(_one(scid, src) for scid, src in targets))

        best: dict[tuple[str, str], RetrievedSection] = {}
        loose: list[RetrievedSection] = []
        for result in results:
            if result is None:
                continue
            scid, outcome = result
            for sec in outcome.sections:
                # 部分向量后端不会回填来源；跨源聚合时补齐，供 MCP/UI 正确标注。
                if not sec.source_config_id:
                    sec.source_config_id = scid
                if sec.chunk_id:
                    key = (sec.source_config_id, sec.chunk_id)
                    prev = best.get(key)
                    if prev is None or sec.score > prev.score:
                        best[key] = sec
                else:
                    loose.append(sec)
        merged = sorted([*best.values(), *loose], key=lambda x: x.score, reverse=True)[:top_k]
        ranked_candidates: list[dict[str, Any]] = []
        if include_ranked_candidates:
            for result in results:
                if result is None:
                    continue
                scid, outcome = result
                for item in outcome.stats.get("ranked_candidates", []):
                    candidate = dict(item)
                    candidate.setdefault("source_config_id", scid)
                    ranked_candidates.append(candidate)
        stats: dict[str, Any] = {
            "sources": len(targets),
            "sources_requested": requested_sources,
            "source_limit_applied": requested_sources > len(targets),
            "candidates": len(best) + len(loose),
            "requested_strategy": strategy,
            "effective_strategy": next(
                (
                    outcome.stats.get("effective_strategy", strategy)
                    for result in results
                    if result is not None
                    for _, outcome in [result]
                ),
                strategy,
            ),
            "fallback_used": any(
                bool(outcome.stats.get("fallback_used"))
                for result in results
                if result is not None
                for _, outcome in [result]
            ),
        }
        if include_ranked_candidates:
            stats["ranked_candidates"] = ranked_candidates
        return SearchOutcome(
            query=query,
            sections=merged,
            stats=stats,
        )

    async def _search_chunk_vectors(
        self,
        targets: list[tuple[str, Source | None]],
        query: str,
        *,
        top_k: int,
        requested_sources: int,
        exclude_source_ids_by_config: dict[str, tuple[str, ...]] | None = None,
    ) -> SearchOutcome:
        """Recall chunks across all selected sources with one query embedding."""

        sources_by_config = {source_config_id: source for source_config_id, source in targets}
        source_config_ids = list(sources_by_config)
        await self._access.ensure_read_runtime(sources_by_config)
        from zleap.sag.core.adapters.models import Filter, VectorQuery

        async def recall() -> tuple[list[Any], dict[str, float]]:
            timings: dict[str, float] = {}
            t0 = time.perf_counter()
            # 0.8.2:向量检索经引擎注入的 VectorStore,payload 键为 data_source_id。
            primary_config_id = source_config_ids[0]
            slot = await self._access.slot(primary_config_id, sources_by_config[primary_config_id])
            engine = slot.engine
            query_vector = await engine.resources.embedding.generate(query)
            timings["vector.embedding"] = round((time.perf_counter() - t0) * 1000, 2)
            t1 = time.perf_counter()
            exclusions = {
                source_config_id: tuple(
                    sorted({value.strip() for value in source_ids if isinstance(value, str) and value.strip()})
                )
                for source_config_id in source_config_ids
                if (
                    source_ids := (exclude_source_ids_by_config or {}).get(
                        source_config_id,
                        (),
                    )
                )
            }
            exclusions = {
                source_config_id: source_ids for source_config_id, source_ids in exclusions.items() if source_ids
            }
            filters = [Filter.one_of("data_source_id", tuple(source_config_ids))]
            if exclusions:
                exclusion_pairs = [
                    Filter.all(
                        Filter.eq("data_source_id", source_config_id),
                        Filter.one_of("source_id", tuple(source_ids)),
                    )
                    for source_config_id, source_ids in exclusions.items()
                ]
                filters.append(Filter.negate(Filter.any(*exclusion_pairs)))
            hits = await engine.resources.vector.query(
                "source_chunks",
                VectorQuery(
                    vector=query_vector,
                    vector_field="content_vector",
                    filters=Filter.all(*filters),
                    limit=top_k,
                ),
            )
            timings["vector.search"] = round((time.perf_counter() - t1) * 1000, 2)
            timings["vector.total"] = round((time.perf_counter() - t0) * 1000, 2)
            return hits, timings

        hits, batch_timings = await asyncio.wait_for(
            recall(),
            timeout=max(1.0, self._access.settings.search_source_timeout),
        )
        allowed_sources = set(source_config_ids)
        sections: dict[tuple[str, str], RetrievedSection] = {}
        loose: list[RetrievedSection] = []
        for hit in hits:
            payload = dict(getattr(hit, "payload", None) or {})
            source_config_id = str(payload.get("data_source_id") or "").strip()
            if source_config_id not in allowed_sources:
                continue
            score = float(getattr(hit, "score", 0.0) or 0.0)
            if not math.isfinite(score):
                continue
            section = RetrievedSection(
                chunk_id=str(getattr(hit, "id", "") or "").strip() or None,
                heading=str(payload.get("heading") or "").strip(),
                content=str(payload.get("content") or "").strip(),
                score=max(0.0, min(1.0, score)),
                rank=int(payload.get("rank") or 0),
                source_id=str(payload.get("source_id") or "").strip() or None,
                source_config_id=source_config_id,
            )
            if not section.chunk_id:
                loose.append(section)
                continue
            key = (source_config_id, section.chunk_id)
            previous = sections.get(key)
            if previous is None or section.score > previous.score:
                sections[key] = section

        merged = sorted(
            [*sections.values(), *loose],
            key=lambda section: (-section.score, section.source_config_id or "", section.chunk_id or ""),
        )[:top_k]
        return SearchOutcome(
            query=query,
            sections=merged,
            stats={
                "sources": len(targets),
                "sources_requested": requested_sources,
                "source_limit_applied": requested_sources > len(targets),
                "candidates": len(sections) + len(loose),
                "chunk_recall": "batch-vector",
                "engine_timings": batch_timings,
            },
        )

    async def search_event_scores(
        self,
        query: str,
        sources_by_config: dict[str, Source | None],
        *,
        limit: int | None = None,
    ) -> dict[tuple[str, str], float]:
        """Recall extracted events directly from their title/content vectors.

        Chunk retrieval remains the evidence path used by answers and citations. This
        independent event path prevents long documents with sparse event-bearing
        chunks from degrading into chunk-only search results.
        """

        query = query.strip()
        source_config_ids = sorted(
            source_config_id.strip() for source_config_id in sources_by_config if source_config_id.strip()
        )
        if not query or not source_config_ids:
            return {}

        bounded_limit = max(
            1,
            min(int(limit or self._access.settings.search_top_k), 50),
        )
        candidate_limit = min(200, max(bounded_limit * 4, 24))

        await self._access.ensure_read_runtime(sources_by_config)
        from zleap.sag.core.adapters.models import Filter, VectorQuery

        async def recall_vectors() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            slot = await self._access.slot(source_config_ids[0], sources_by_config[source_config_ids[0]])
            engine = slot.engine
            query_vector = await engine.resources.embedding.generate(query)
            store = engine.resources.vector
            filters = Filter.one_of("data_source_id", tuple(source_config_ids))

            async def channel(field: str, k: int) -> list[dict[str, Any]]:
                hits = await store.query(
                    "event_vectors_wide",
                    VectorQuery(vector=query_vector, vector_field=field, filters=filters, limit=k),
                )
                return [
                    {
                        "event_id": str(getattr(hit, "id", "")),
                        "source_config_id": str((getattr(hit, "payload", None) or {}).get("data_source_id") or ""),
                        "score": float(getattr(hit, "score", 0.0) or 0.0),
                    }
                    for hit in hits
                ]

            return await asyncio.gather(
                channel("title_vector", candidate_limit),
                channel("content_vector", candidate_limit),
            )

        title_hits, content_hits = await asyncio.wait_for(
            recall_vectors(),
            timeout=max(1.0, self._access.settings.search_source_timeout),
        )

        allowed_sources = set(source_config_ids)
        scores: dict[tuple[str, str], float] = {}
        channels: dict[tuple[str, str], int] = {}
        for channel, weight, hits in (
            (1, 1.0, title_hits),
            (2, 0.95, content_hits),
        ):
            for hit in hits:
                event_id = str(hit.get("event_id") or hit.get("id") or "").strip()
                source_config_id = str(hit.get("source_config_id") or "").strip()
                if not event_id or source_config_id not in allowed_sources:
                    continue
                try:
                    score_value = hit.get("_score")
                    if score_value is None:
                        score_value = hit.get("score", 0.0)
                    raw_score = float(score_value or 0.0)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(raw_score):
                    continue
                key = (source_config_id, event_id)
                score = max(0.0, min(1.0, raw_score)) * weight
                scores[key] = max(scores.get(key, 0.0), score)
                channels[key] = channels.get(key, 0) | channel

        if not scores:
            return {}

        # Agreement between title and content vectors is a small confidence signal,
        # while the raw cosine score remains the dominant ordering factor.
        for key, channel_mask in channels.items():
            if channel_mask == 3:
                scores[key] = min(1.0, scores[key] + 0.03)

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        strongest = ranked[0][1]
        relevance_floor = max(0.20, strongest * 0.55)
        return {key: round(score, 6) for key, score in ranked[:bounded_limit] if score >= relevance_floor}
