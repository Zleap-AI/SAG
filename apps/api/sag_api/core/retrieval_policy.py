"""Strategy inheritance and finite budgets shared by retrieval entry points."""

from __future__ import annotations

import math

from sag_api.core.config import Settings


def knowledge_search_strategy(persona: dict | None, configuration: Settings) -> str:
    explicit = (persona or {}).get("search_strategy")
    return explicit or ("multi_jev" if configuration.search_strategy == "multi_jev" else "vector")


def search_source_timeout_seconds(
    configuration: Settings,
    strategy: str | None,
    *,
    jev_ranking_timeout_seconds: float | None = None,
) -> float:
    base = max(1.0, configuration.search_source_timeout)
    ranking_timeout = configuration.jev_ranking_timeout_seconds
    if jev_ranking_timeout_seconds is not None:
        ranking_timeout = min(ranking_timeout, jev_ranking_timeout_seconds)
    return base + ranking_timeout if (
        strategy or configuration.search_strategy
    ) == "multi_jev" else base


def search_request_timeout_seconds(
    configuration: Settings,
    strategy: str | None,
    source_count: int,
    *,
    jev_ranking_timeout_seconds: float | None = None,
) -> float:
    count = min(max(0, source_count), configuration.search_source_candidate_limit)
    return math.ceil(count / configuration.search_source_concurrency) * search_source_timeout_seconds(
        configuration, strategy, jev_ranking_timeout_seconds=jev_ranking_timeout_seconds,
    )
