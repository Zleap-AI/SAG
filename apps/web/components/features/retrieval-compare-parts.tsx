"use client";

import * as React from "react";
import { Trophy } from "lucide-react";

import type { EvalJudge, EvalStrategyResult } from "@/lib/api";
import type { SearchStrategy } from "@/lib/retrieval-config";
import type { Section } from "@/lib/types";
import { RetrievalStatus } from "@/components/features/retrieval-status";
import { cn } from "@/lib/utils";

/** Small muted badge used next to a mode label (paid / default markers). */
export const strategyBadgeClassName =
  "rounded bg-muted px-1 py-0.5 text-[10px] font-normal leading-none text-muted-foreground";

export function pickNumber(stats: Record<string, unknown>, key: string): number | null {
  const value = stats[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function pickTimings(stats: Record<string, unknown>, key: string): [string, number][] {
  const value = stats[key];
  if (!value || typeof value !== "object") return [];
  const entries: [string, number][] = [];
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    if (typeof v === "number" && Number.isFinite(v)) entries.push([k, v]);
  }
  return entries;
}

function shortStep(key: string): string {
  // "multi_es.step5_fast_expand" → "step5_fast_expand";vector.embedding → embedding
  const idx = key.indexOf(".");
  return idx >= 0 ? key.slice(idx + 1) : key;
}

/** One retrieved passage: rank, heading, score bar and a two-line excerpt. */
export function SectionRow({
  index,
  section,
  sectionLabel,
  marker,
  onClick,
}: {
  index: number;
  section: Section;
  sectionLabel: string;
  /** Optional inline marker rendered after the heading (e.g. a shared-hit tag). */
  marker?: React.ReactNode;
  /** When provided the row becomes a button that opens the passage. */
  onClick?: () => void;
}) {
  const body = (
    <>
      <div className="mb-1 flex items-center gap-2">
        <span className="grid size-5 shrink-0 place-items-center rounded-[6px] bg-muted text-[11px] font-semibold text-foreground">
          {index + 1}
        </span>
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">
          {section.heading || sectionLabel}
        </span>
        {marker}
        <span className="shrink-0 font-mono text-[11px] tabular-nums text-muted-foreground">
          {section.score.toFixed(4)}
        </span>
      </div>
      <div className="mb-1.5 ml-7 h-1 overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full bg-primary"
          style={{
            width: `${Math.max(4, Math.min(100, section.score * 100))}%`,
          }}
        />
      </div>
      <p className="ml-7 line-clamp-2 text-xs text-muted-foreground">
        {section.content}
      </p>
    </>
  );
  if (!onClick) {
    return <div className="border-t p-3 first:border-t-0">{body}</div>;
  }
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!section.chunk_id || !section.source_id}
      className="block w-full border-t p-3 text-left outline-none transition-colors first:border-t-0 hover:bg-muted/35 focus-visible:bg-muted/45 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:cursor-default disabled:hover:bg-transparent"
    >
      {body}
    </button>
  );
}

/** Compact latency / candidate summary for one strategy run. */
export function StatsPill({ stats }: { stats: Record<string, unknown> }) {
  const latency = pickNumber(stats, "latency_total_ms");
  const engineLatency = pickNumber(stats, "latency_engine_ms");
  const candidates = pickNumber(stats, "candidates");
  const relevant = pickNumber(stats, "relevant");
  const parts: string[] = [];
  if (latency != null) parts.push(`${latency.toFixed(0)}ms`);
  else if (engineLatency != null) parts.push(`${engineLatency.toFixed(0)}ms`);
  if (candidates != null) parts.push(`cand=${candidates}`);
  if (relevant != null) parts.push(`rel=${relevant}`);
  const engineTimings = pickTimings(stats, "engine_timings");
  const topSteps = engineTimings
    .filter(([key]) => !/\.total$/.test(key)) // total 已经在 pill 里
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3);
  if (parts.length === 0 && topSteps.length === 0) return null;
  return (
    <div className="flex flex-col gap-0.5">
      {parts.length > 0 && <span className="font-mono">{parts.join(" · ")}</span>}
      {topSteps.length > 0 && (
        <span className="font-mono text-muted-foreground" title={engineTimings.map(([k, v]) => `${k}=${v.toFixed(0)}ms`).join("\n")}>
          {topSteps.map(([k, v]) => `${shortStep(k)}=${v.toFixed(0)}ms`).join(" · ")}
        </span>
      )}
    </div>
  );
}

/** One strategy's column as shown in the per-source retrieval test dialog. */
export function ColumnResult({
  result,
  label,
  sectionLabel,
  emptyLabel,
}: {
  result: EvalStrategyResult | undefined;
  label: string;
  sectionLabel: string;
  emptyLabel: string;
}) {
  if (!result) return null;
  return (
    <div className="flex flex-col gap-1.5">
      <RetrievalStatus stats={result.stats} requestedStrategy={result.strategy} />
      <div className="flex items-center justify-between px-1 text-[11px] text-muted-foreground">
        <span className="font-medium text-foreground">{label}</span>
        <StatsPill stats={result.stats} />
      </div>
      <div className="max-h-[22rem] overflow-y-auto rounded-lg border">
        {result.error ? (
          <p className="px-3 py-6 text-center text-xs text-destructive">
            {result.error}
          </p>
        ) : result.sections.length === 0 ? (
          <p className="px-3 py-6 text-center text-sm text-muted-foreground">
            {emptyLabel}
          </p>
        ) : (
          result.sections.map((section, i) => (
            <SectionRow
              key={`${section.chunk_id ?? "no-chunk"}-${i}`}
              index={i}
              section={section}
              sectionLabel={sectionLabel}
            />
          ))
        )}
      </div>
    </div>
  );
}

/** Pairwise LLM verdicts, or the reason none were produced. */
export function JudgeVerdicts({
  judges,
  judgeReason,
  missingLabel,
  tieLabel = "tie",
  strategyLabelFor,
  className,
}: {
  judges: EvalJudge[];
  judgeReason: string | null;
  missingLabel: string;
  tieLabel?: string;
  strategyLabelFor: (value: SearchStrategy) => string;
  className?: string;
}) {
  return (
    <div className={cn("rounded-md border px-3 py-2 text-xs", className)} data-testid="compare-judges">
      {judges.length === 0 ? (
        <p className="text-muted-foreground">{judgeReason || missingLabel}</p>
      ) : (
        <ul className="flex flex-col gap-1.5">
          {judges.map((verdict, i) => {
            const winner =
              verdict.winner === "A"
                ? strategyLabelFor(verdict.a_strategy)
                : verdict.winner === "B"
                  ? strategyLabelFor(verdict.b_strategy)
                  : tieLabel;
            return (
              <li key={i} className="flex flex-col gap-0.5">
                <span className="text-xs font-medium text-foreground">
                  <Trophy className="mr-1 inline size-3.5" />
                  {winner}
                </span>
                <span className="text-[11px] text-muted-foreground">
                  {verdict.reason}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
