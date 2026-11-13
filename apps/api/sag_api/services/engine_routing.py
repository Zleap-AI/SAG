"""Route automatic retrieval by immutable knowledge algorithm and fuse ranks."""

from __future__ import annotations

import asyncio

from sag_api.sag.dto import SearchOutcome


async def retrieve_automatic(sources, query, *, sag1_strategy, top_k, retrieve):
    groups = {
        kind: [source for source in sources if getattr(source, "engine_kind", "sag1") == kind]
        for kind in ("sag1", "sag2")
    }
    kinds = [kind for kind, group in groups.items() if group]
    results = await asyncio.gather(
        *(retrieve(groups[kind], "sag2" if kind == "sag2" else sag1_strategy) for kind in kinds), return_exceptions=True
    )
    if (len(kinds) == 1 and not isinstance(results[0], BaseException)
            and not results[0].stats.get("source_failures")):
        outcome = results[0]
        for section in outcome.sections:
            section.metadata["engine_kind"] = kinds[0]
        return outcome.model_copy(
            update={
                "stats": {
                    **outcome.stats,
                    "requested_strategy": "auto",
                    "effective_strategy": "auto",
                    "engine_kind": kinds[0],
                    "jev_fallback_used": sag1_strategy == "multi_jev" and bool(outcome.stats.get("fallback_used")),
                }
            }
        )
    failures = []
    candidates = {}
    usage = {}
    engine_stats = {}
    source_stats = {}
    fallback_used = False
    jev_fallback_used = False
    succeeded = 0
    for kind, result in zip(kinds, results, strict=True):
        if isinstance(result, BaseException):
            if not isinstance(result, Exception):
                raise result
            failures.append({"engine_kind": kind, "message": getattr(result, "message", None) or str(result)})
            continue
        succeeded += 1
        engine_stats[kind] = result.stats
        source_stats.update(result.stats.get("source_stats") or {})
        fallback_used |= bool(result.stats.get("fallback_used"))
        jev_fallback_used |= kind == "sag1" and sag1_strategy == "multi_jev" and bool(result.stats.get("fallback_used"))
        if result.stats.get("source_failures"):
            messages = list(dict.fromkeys(item["message"] for item in result.stats["source_failures"]))
            failures.append({"engine_kind": kind, "message": "; ".join(messages)})
        for key, value in (result.stats.get("jev_usage") or {}).items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
        for rank, section in enumerate(result.sections):
            key = (section.source_config_id, section.chunk_id or section.content)
            contribution = 1.0 / (60 + rank + 1)
            if key not in candidates:
                candidates[key] = [section, 0.0, len(candidates), kind]
            candidates[key][1] += contribution
    if kinds and not succeeded:
        raise next(result for result in results if isinstance(result, Exception))
    ranked = sorted(candidates.values(), key=lambda item: (-item[1], item[2]))[:top_k]
    sections = [
        section.model_copy(
            update={
                "score": score,
                "rank": rank,
                "metadata": {
                    **section.metadata,
                    "engine_kind": kind,
                    "original_score": section.score,
                    "score_source": "rrf",
                },
            }
        )
        for rank, (section, score, _, kind) in enumerate(ranked)
    ]
    return SearchOutcome(
        query=query,
        sections=sections,
        stats={
            "requested_strategy": "auto",
            "effective_strategy": "auto",
            "strategy_origin": "global",
            "sources": len(sources),
            "relevant": len(sections),
            "fusion": "rrf",
            "partial_results": bool(failures),
            "engine_failures": failures,
            "engine_stats": engine_stats,
            "source_stats": source_stats,
            "fallback_used": fallback_used,
            "jev_fallback_used": jev_fallback_used,
            **({"jev_usage": usage} if usage else {}),
        },
    )
