"use client";

import { useTranslations } from "next-intl";
import { getSearchStrategy, isSearchStrategy, type SearchStrategy } from "@/lib/retrieval-config";

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

function usedFallback(stats: Record<string, unknown>): boolean {
  return stats.fallback_used === true || Boolean(record(stats.ranking).fallback_reason);
}

/** Explain execution changes without treating same-profile coarse ranking as JEV success. */
export function RetrievalStatus({ stats, requestedStrategy }: {
  stats: Record<string, unknown>;
  requestedStrategy?: SearchStrategy;
}) {
  const t = useTranslations("RetrievalStatus");
  const strategies = useTranslations("SearchStrategies");
  const requested = isSearchStrategy(stats.requested_strategy) ? stats.requested_strategy : requestedStrategy;
  const effective = typeof stats.effective_strategy === "string" ? stats.effective_strategy : requested;
  const fallback = usedFallback(stats) || Object.values(record(stats.source_stats)).some(value => usedFallback(record(value)));
  const jevFallback = (requested === "multi_jev" || effective === "multi_jev") && fallback;
  const partial = stats.partial === true;
  const changed = requested && effective && requested !== effective;
  if (!jevFallback && !partial && !changed) return null;
  function label(mode: string) {
    return isSearchStrategy(mode) ? strategies(getSearchStrategy(mode).labelKey) : mode;
  }
  return <div role="status" className="flex flex-col gap-1 rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
    {requested && <p>{t("requested", { mode: label(requested) })}{" · "}{t("actual", { mode: jevFallback ? t("coarse") : label(effective ?? requested) })}</p>}
    {jevFallback && <p>{t("jevFallback")}</p>}
    {partial && <p>{t("partial")}</p>}
  </div>;
}
