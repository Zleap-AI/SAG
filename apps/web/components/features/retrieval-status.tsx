"use client";

import { useTranslations } from "next-intl";
import { getSearchStrategy, isSearchStrategy, type SearchStrategy } from "@/lib/retrieval-config";

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

export function RetrievalStatus({ stats, requestedStrategy }: {
  stats: Record<string, unknown>;
  requestedStrategy?: SearchStrategy;
}) {
  const t = useTranslations("RetrievalStatus");
  const strategies = useTranslations("SearchStrategies");
  const requested = isSearchStrategy(stats.requested_strategy) ? stats.requested_strategy : requestedStrategy;
  const effective = isSearchStrategy(stats.effective_strategy) ? stats.effective_strategy : requested;
  const sources = stats.source_stats && typeof stats.source_stats === "object" ? Object.values(stats.source_stats) : [];
  const aggregateRanking = record(stats.ranking);
  const sourceRankings = sources.map(source => record(record(source).ranking));
  const rankings = [aggregateRanking, ...sourceRankings].filter(ranking => Object.keys(ranking).length);
  const candidateCount = (key: string) => {
    const validCount = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
    if (validCount(aggregateRanking[key])) return aggregateRanking[key];
    return sourceRankings.reduce((total, ranking) => total + (validCount(ranking[key]) ? ranking[key] : 0), 0);
  };
  const scoredChunks = candidateCount("scored_chunks");
  const unscoredChunks = candidateCount("query_chunk_fallback_chunks");
  const reasons = rankings.map(ranking => ranking.fallback_reason).filter((reason): reason is string => typeof reason === "string" && Boolean(reason));
  const fallback = stats.fallback_used === true || reasons.length > 0 || sources.some(source => record(source).fallback_used === true);
  const isJev = requested === "multi_jev" || effective === "multi_jev";
  const completed = isJev && !fallback && scoredChunks > 0 && rankings.every(ranking => ranking.method === "jev");
  const unscored = isJev && unscoredChunks > 0;
  const jevFallback = isJev && fallback;
  const changed = requested && effective && requested !== effective;
  if (!completed && !unscored && !jevFallback && !changed && stats.partial !== true) return null;
  const label = (strategy: SearchStrategy) => strategies(getSearchStrategy(strategy).labelKey);
  const timeout = reasons.some(reason => /timeout|deadline/i.test(reason));
  return <div role="status" className="flex min-w-0 flex-col gap-1 rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
    {requested && effective && <p>{t("requested", { mode: label(requested) })}{" · "}{t("actual", { mode: jevFallback ? t("coarse") : unscored && scoredChunks === 0 ? t("unscored") : label(effective) })}</p>}
    {completed && <p>{t("jevComplete", { count: scoredChunks })}</p>}
    {unscored && <p>{t("unscoredCandidates", { count: unscoredChunks })}</p>}
    {jevFallback && <p>{t("jevFallback")} {t(timeout ? "fallbackTimeout" : "fallbackUnavailable")}</p>}
    {stats.partial === true && <p>{t("partial")}</p>}
  </div>;
}
