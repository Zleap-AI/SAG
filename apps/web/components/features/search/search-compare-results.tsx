"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import { Search as SearchIcon, Trophy } from "lucide-react";

import type { EvalCompareResponse, EvalStrategyResult } from "@/lib/api";
import {
  getSearchStrategy,
  isJevScoredStrategy,
  SEARCH_STRATEGY_ICONS,
  type SearchStrategy,
} from "@/lib/retrieval-config";
import type { Section } from "@/lib/types";
import { cn } from "@/lib/utils";
import { EmptyState } from "@/components/features/empty-state";
import {
  JudgeVerdicts,
  pickNumber,
  SectionRow,
  strategyBadgeClassName,
} from "@/components/features/retrieval-compare-parts";
import { RetrievalStatus } from "@/components/features/retrieval-status";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

/** True when the server ran something other than the requested mode. */
function executionChanged(result: EvalStrategyResult): boolean {
  const { stats } = result;
  if (stats.fallback_used === true || Boolean(record(stats.ranking).fallback_reason)) return true;
  const requested = typeof stats.requested_strategy === "string" ? stats.requested_strategy : result.strategy;
  const effective = typeof stats.effective_strategy === "string" ? stats.effective_strategy : requested;
  return requested !== effective;
}

/** Count how many columns retrieved each chunk so shared hits can be marked. */
export function countChunkColumns(results: ReadonlyArray<EvalStrategyResult>): Map<string, number> {
  const counts = new Map<string, number>();
  for (const column of results) {
    const seen = new Set<string>();
    for (const section of column.sections) {
      if (section.chunk_id) seen.add(section.chunk_id);
    }
    for (const chunkId of seen) counts.set(chunkId, (counts.get(chunkId) ?? 0) + 1);
  }
  return counts;
}

function gridClassName(count: number, compact: boolean): string {
  if (compact || count <= 1) return "grid-cols-1";
  return count >= 3 ? "grid-cols-1 md:grid-cols-3" : "grid-cols-1 md:grid-cols-2";
}

function ColumnHeading({ strategy }: { strategy: SearchStrategy }) {
  const strategies = useTranslations("SearchStrategies");
  const entry = getSearchStrategy(strategy);
  const Icon = SEARCH_STRATEGY_ICONS[entry.value];
  return (
    <span className="flex min-w-0 items-center gap-1.5 text-sm font-medium text-foreground">
      <Icon className="size-3.5 shrink-0" />
      <span className="truncate">{strategies(entry.labelKey)}</span>
      {isJevScoredStrategy(entry.value) && (
        <span className={strategyBadgeClassName}>{strategies("paid")}</span>
      )}
    </span>
  );
}

function MetricsStrip({ result }: { result: EvalStrategyResult }) {
  const t = useTranslations("Search");
  const locale = useLocale();
  const format = (value: number) => value.toLocaleString(locale);
  const latency = pickNumber(result.stats, "latency_total_ms") ?? pickNumber(result.stats, "latency_engine_ms");
  const usage = record(result.stats.jev_usage);
  const requests = pickNumber(usage, "requests");
  const judgments = pickNumber(usage, "judgments");
  const inputTokens = pickNumber(usage, "input_tokens");
  const parts: string[] = [];
  if (latency !== null) parts.push(t("compareLatency", { seconds: (latency / 1000).toFixed(1) }));
  parts.push(t("compareHits", { count: result.sections.length }));
  if (requests !== null && judgments !== null) {
    parts.push(t("compareUsage", {
      requests: format(requests),
      judgments: format(judgments),
      tokens: inputTokens === null ? "" : t("compareUsageTokens", { tokens: format(inputTokens) }),
    }));
  }
  return (
    <p className="flex flex-wrap items-center gap-x-1.5 text-[11px] tabular-nums text-muted-foreground" data-testid="compare-metrics">
      {parts.map((part, index) => (
        <React.Fragment key={index}>
          {index > 0 && <span aria-hidden="true">·</span>}
          <span>{part}</span>
        </React.Fragment>
      ))}
    </p>
  );
}

function CompareColumn({
  result,
  chunkColumns,
  onAdopt,
  onOpenSection,
}: {
  result: EvalStrategyResult;
  chunkColumns: Map<string, number>;
  onAdopt: (strategy: SearchStrategy) => void;
  onOpenSection: (section: Section, index: number, column: EvalStrategyResult) => void;
}) {
  const t = useTranslations("Search");
  // The strip already reports JEV usage; keep the status box for execution changes only.
  const statusStats = React.useMemo(() => {
    const { jev_usage: _usage, ...rest } = result.stats;
    void _usage;
    return rest;
  }, [result.stats]);
  return (
    <section
      data-testid={`compare-column-${result.strategy}`}
      className="flex min-w-0 flex-col gap-2 rounded-lg border bg-card p-3 shadow-soft"
    >
      <header className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 flex-col gap-1">
          <ColumnHeading strategy={result.strategy} />
          <MetricsStrip result={result} />
        </div>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-7 shrink-0 px-2 text-xs"
          disabled={Boolean(result.error)}
          onClick={() => onAdopt(result.strategy)}
          data-testid={`compare-adopt-${result.strategy}`}
        >
          {t("adoptMode")}
        </Button>
      </header>
      {!result.error && executionChanged(result) && (
        <RetrievalStatus stats={statusStats} requestedStrategy={result.strategy} />
      )}
      {result.error ? (
        <p
          role="alert"
          className="rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground"
        >
          {result.error}
        </p>
      ) : result.sections.length === 0 ? (
        <p className="px-3 py-6 text-center text-sm text-muted-foreground">{t("compareEmpty")}</p>
      ) : (
        <div className="overflow-hidden rounded-lg border">
          {result.sections.map((section, index) => (
            <SectionRow
              key={`${section.chunk_id ?? "no-chunk"}-${index}`}
              index={index}
              section={section}
              sectionLabel={t("relatedMaterial")}
              marker={section.chunk_id && (chunkColumns.get(section.chunk_id) ?? 0) >= 2 ? (
                <span
                  data-testid="compare-common-hit"
                  className="shrink-0 rounded bg-primary/10 px-1 py-0.5 text-[10px] font-medium leading-none text-primary"
                >
                  {t("commonHit")}
                </span>
              ) : undefined}
              onClick={() => onOpenSection(section, index, result)}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function BusyColumn({ strategy }: { strategy: SearchStrategy }) {
  return (
    <section
      data-testid={`compare-column-${strategy}`}
      className="flex min-w-0 flex-col gap-2 rounded-lg border bg-card p-3 shadow-soft"
      aria-busy="true"
    >
      <ColumnHeading strategy={strategy} />
      <Skeleton className="h-3 w-32" />
      {Array.from({ length: 4 }).map((_, index) => (
        <Skeleton key={index} className="h-16 rounded-lg" />
      ))}
    </section>
  );
}

export interface SearchCompareResultsProps {
  data: EvalCompareResponse | null;
  busy: boolean;
  /** Modes in flight, used for the skeleton columns while busy. */
  pendingStrategies: ReadonlyArray<SearchStrategy>;
  /** Whole-request failure (as opposed to one mode's `error`). */
  error: string | null;
  compact: boolean;
  onAdopt: (strategy: SearchStrategy) => void;
  onOpenSection: (section: Section, index: number, column: EvalStrategyResult) => void;
  onRetry: () => void;
}

/** Side-by-side columns for 2–3 retrieval modes plus the pairwise LLM verdicts. */
export function SearchCompareResults({
  data,
  busy,
  pendingStrategies,
  error,
  compact,
  onAdopt,
  onOpenSection,
  onRetry,
}: SearchCompareResultsProps) {
  const t = useTranslations("Search");
  const strategies = useTranslations("SearchStrategies");
  const strategyLabelFor = React.useCallback(
    (value: SearchStrategy) => strategies(getSearchStrategy(value).labelKey),
    [strategies],
  );
  const chunkColumns = React.useMemo(
    () => countChunkColumns(data?.results ?? []),
    [data],
  );

  if (busy) {
    return (
      <div
        role="status"
        aria-live="polite"
        aria-label={t("compareRunning")}
        className={cn("grid gap-3", gridClassName(pendingStrategies.length, compact))}
      >
        {pendingStrategies.map((strategy) => (
          <BusyColumn key={strategy} strategy={strategy} />
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <EmptyState
        icon={SearchIcon}
        title={t("compareFailed")}
        description={error}
        action={(
          <button
            type="button"
            onClick={onRetry}
            className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground outline-none hover:bg-primary/90 focus-visible:ring-2 focus-visible:ring-ring"
          >
            {t("retry")}
          </button>
        )}
      />
    );
  }

  if (!data) return null;

  return (
    <div className="flex flex-col gap-3">
      <div className={cn("grid gap-3", gridClassName(data.results.length, compact))}>
        {data.results.map((result) => (
          <CompareColumn
            key={result.strategy}
            result={result}
            chunkColumns={chunkColumns}
            onAdopt={onAdopt}
            onOpenSection={onOpenSection}
          />
        ))}
      </div>
      <div className="flex flex-col gap-1.5">
        <h3 className="flex items-center gap-1.5 px-1 text-xs font-medium text-muted-foreground">
          <Trophy className="size-3.5" />
          {t("judgeToggle")}
        </h3>
        <JudgeVerdicts
          judges={data.judge_enabled ? data.judges : []}
          judgeReason={data.judge_reason}
          missingLabel={t("judgeMissing")}
          tieLabel={t("judgeTie")}
          strategyLabelFor={strategyLabelFor}
          className="bg-card"
        />
      </div>
    </div>
  );
}
