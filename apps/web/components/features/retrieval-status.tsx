"use client";

import { useLocale, useTranslations } from "next-intl";
import {
  getSearchStrategy,
  isJevScoredStrategy,
  isSearchStrategy,
  type SearchStrategy,
} from "@/lib/retrieval-config";

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

function usedFallback(stats: Record<string, unknown>): boolean {
  return stats.fallback_used === true || Boolean(record(stats.ranking).fallback_reason);
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Origins worth a line of their own: the user did not pick the mode and it is
 * not simply the global default, so say where it came from. "request" and
 * "global" stay silent unless the execution changed.
 */
const EXPLAINED_ORIGINS = {
  persona: "originPersona",
  source_default: "originSourceDefault",
} as const;

function explainedOrigin(value: unknown): keyof typeof EXPLAINED_ORIGINS | null {
  return typeof value === "string" && Object.hasOwn(EXPLAINED_ORIGINS, value)
    ? value as keyof typeof EXPLAINED_ORIGINS
    : null;
}

/** Explain execution changes without treating same-profile coarse ranking as JEV success. */
export function RetrievalStatus({ stats, requestedStrategy }: {
  stats: Record<string, unknown>;
  requestedStrategy?: SearchStrategy;
}) {
  const t = useTranslations("RetrievalStatus");
  const strategies = useTranslations("SearchStrategies");
  const locale = useLocale();
  const requested = isSearchStrategy(stats.requested_strategy) ? stats.requested_strategy : requestedStrategy;
  const effective = typeof stats.effective_strategy === "string" ? stats.effective_strategy : requested;
  const fallback = usedFallback(stats) || Object.values(record(stats.source_stats)).some(value => usedFallback(record(value)));
  const jevFallback = (isJevScoredStrategy(requested) || isJevScoredStrategy(effective)) && fallback;
  const partial = stats.partial === true;
  const changed = requested && effective && requested !== effective;
  // Paid JEV calls are reported per run so users can see what a query cost.
  const usage = record(stats.jev_usage);
  const usageRequests = finiteNumber(usage.requests);
  const usageJudgments = finiteNumber(usage.judgments);
  const usageInputTokens = finiteNumber(usage.input_tokens);
  const format = (value: number) => value.toLocaleString(locale);
  // Only report usage the backend actually measured; never fabricate a zero.
  const usageText = usageRequests !== null && usageJudgments !== null
    ? t("jevUsage", {
      requests: format(usageRequests),
      judgments: format(usageJudgments),
      tokens: usageInputTokens === null ? "" : t("jevUsageTokens", { tokens: format(usageInputTokens) }),
    })
    : null;
  const executionChanged = jevFallback || partial || changed;
  const origin = explainedOrigin(stats.strategy_origin);
  // The origin describes the resolved (requested) mode; a changed execution is reported separately.
  const originMode = requested ?? effective;
  if (!executionChanged && usageText === null && !(origin && originMode)) return null;
  function label(mode: string) {
    return isSearchStrategy(mode) ? strategies(getSearchStrategy(mode).labelKey) : mode;
  }
  return <div role="status" className="flex flex-col gap-1 rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
    {origin && originMode && <p data-testid="retrieval-origin">{t("origin", { mode: label(originMode), origin: t(EXPLAINED_ORIGINS[origin]) })}</p>}
    {executionChanged && requested && <p>{t("requested", { mode: label(requested) })}{" · "}{t("actual", { mode: jevFallback ? t("coarse") : label(effective ?? requested) })}</p>}
    {jevFallback && <p>{t("jevFallback")}</p>}
    {partial && <p>{t("partial")}</p>}
    {usageText !== null && <p>{usageText}</p>}
  </div>;
}
