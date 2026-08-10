"use client";

import * as React from "react";
import { FlaskConical, GitCompareArrows, Search, Trophy } from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { useApp } from "@/components/features/app-shell";
import {
  api,
  ApiError,
  type EvalCompareResponse,
  type EvalStrategyResult,
} from "@/lib/api";
import {
  DEFAULT_SEARCH_STRATEGY,
  SEARCH_STRATEGIES,
  type SearchStrategy,
} from "@/lib/retrieval-config";
import type { Section } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";

export function RetrievalTestDialog({
  sourceId,
  sourceName,
  open,
  onOpenChange,
}: {
  sourceId: string;
  sourceName: string;
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const t = useTranslations("RetrievalTest");
  const strategies = useTranslations("SearchStrategies");
  const { capabilities } = useApp();
  const disabledMap = capabilities?.search_strategies_disabled ?? {};

  const enabledStrategies = SEARCH_STRATEGIES.filter(
    (item) => !disabledMap[item.value],
  ).map((item) => item.value);
  const fallbackA: SearchStrategy =
    enabledStrategies[0] ?? DEFAULT_SEARCH_STRATEGY;
  const fallbackB: SearchStrategy =
    enabledStrategies.find((value) => value !== fallbackA) ??
    fallbackA;

  const [query, setQuery] = React.useState("");
  const [topK, setTopK] = React.useState(8);
  const [compare, setCompare] = React.useState(false);
  const [strategyA, setStrategyA] = React.useState<SearchStrategy>(fallbackA);
  const [strategyB, setStrategyB] = React.useState<SearchStrategy>(fallbackB);
  const [judge, setJudge] = React.useState(true);
  const [results, setResults] = React.useState<Section[] | null>(null);
  const [compareResults, setCompareResults] =
    React.useState<EvalCompareResponse | null>(null);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    if (!open) return;
    setResults(null);
    setCompareResults(null);
    setQuery("");
    // Keep the current mode toggle so re-opening isn't jarring, but re-seed
    // strategy choices in case capabilities changed between opens.
    setStrategyA(fallbackA);
    setStrategyB(fallbackB);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  async function run(e?: React.FormEvent) {
    e?.preventDefault();
    if (!query.trim()) return;
    if (compare && strategyA === strategyB) {
      toast.error(t("sameStrategyError"));
      return;
    }
    setBusy(true);
    try {
      if (compare) {
        const response = await api.evalCompare({
          query,
          source_ids: [sourceId],
          top_k: topK,
          strategies: [strategyA, strategyB],
          judge,
        });
        setCompareResults(response);
        setResults(null);
      } else {
        const response = await api.globalSearch({
          query,
          source_ids: [sourceId],
          top_k: topK,
        });
        setResults(response.sections);
        setCompareResults(null);
      }
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : t("failed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className={cn("max-w-2xl", compare && "max-w-5xl")}>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <FlaskConical className="size-4 text-foreground" />
            {t("title", { source: sourceName })}
          </DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>

        <div className="flex items-center justify-between rounded-md border px-3 py-2">
          <label
            htmlFor="rt-compare"
            className="flex items-center gap-2 text-xs text-muted-foreground"
          >
            <GitCompareArrows className="size-3.5" />
            {t("compareMode")}
          </label>
          <Switch
            id="rt-compare"
            checked={compare}
            onCheckedChange={setCompare}
          />
        </div>

        <form onSubmit={run} className="flex flex-col gap-3">
          <div className="flex items-end gap-2">
            <div className="flex flex-1 flex-col gap-1.5">
              <Label htmlFor="rt-q">{t("query")}</Label>
              <Input
                id="rt-q"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t("queryPlaceholder")}
                autoFocus
              />
            </div>
            <div className="flex w-20 flex-col gap-1.5">
              <Label htmlFor="rt-k">top_k</Label>
              <Input
                id="rt-k"
                type="number"
                min={1}
                max={50}
                value={topK}
                onChange={(e) =>
                  setTopK(
                    Math.max(1, Math.min(50, Number(e.target.value) || 1)),
                  )
                }
              />
            </div>
            <Button type="submit" disabled={busy || !query.trim()}>
              {busy ? <Spinner /> : <Search className="size-4" />}
              {compare ? t("runCompare") : t("run")}
            </Button>
          </div>

          {compare && (
            <div className="grid grid-cols-2 gap-3 rounded-md border p-3">
              <StrategyPicker
                idPrefix="rt-a"
                label={t("strategyA")}
                value={strategyA}
                onChange={setStrategyA}
                disabledMap={disabledMap}
                otherValue={strategyB}
              />
              <StrategyPicker
                idPrefix="rt-b"
                label={t("strategyB")}
                value={strategyB}
                onChange={setStrategyB}
                disabledMap={disabledMap}
                otherValue={strategyA}
              />
              <label
                htmlFor="rt-judge"
                className="col-span-2 mt-1 flex items-center justify-between gap-2 text-xs text-muted-foreground"
              >
                <span className="flex items-center gap-2">
                  <Trophy className="size-3.5" />
                  {t("judgeToggle")}
                </span>
                <Switch id="rt-judge" checked={judge} onCheckedChange={setJudge} />
              </label>
            </div>
          )}
        </form>

        {!compare && results && (
          <SingleColumn
            results={results}
            sectionLabel={t("section")}
            emptyLabel={t("empty")}
            countLabel={t("resultCount", { count: results.length })}
          />
        )}

        {compare && compareResults && (
          <CompareColumns
            data={compareResults}
            sectionLabel={t("section")}
            emptyLabel={t("empty")}
            judgeMissingLabel={t("judgeMissing")}
            strategyLabelFor={(value: SearchStrategy) => {
              const entry = SEARCH_STRATEGIES.find((s) => s.value === value);
              return entry ? strategies(entry.labelKey) : value;
            }}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

function StrategyPicker({
  idPrefix,
  label,
  value,
  onChange,
  disabledMap,
  otherValue,
}: {
  idPrefix: string;
  label: string;
  value: SearchStrategy;
  onChange: (value: SearchStrategy) => void;
  disabledMap: Partial<
    Record<SearchStrategy, { reason: string; message: string }>
  >;
  otherValue: SearchStrategy;
}) {
  const strategies = useTranslations("SearchStrategies");
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={`${idPrefix}-select`} className="text-xs">
        {label}
      </Label>
      <Select
        value={value}
        onValueChange={(next) => {
          const strategy = next as SearchStrategy;
          if (disabledMap[strategy]) return;
          onChange(strategy);
        }}
      >
        <SelectTrigger id={`${idPrefix}-select`}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {SEARCH_STRATEGIES.map(({ value: strategy, labelKey }) => {
            const info = disabledMap[strategy];
            const sameAsOther = strategy === otherValue;
            return (
              <SelectItem
                key={strategy}
                value={strategy}
                disabled={Boolean(info) || sameAsOther}
              >
                <span className="flex items-center gap-1.5">
                  {strategies(labelKey)}
                  {info && (
                    <span className="text-[10px] text-muted-foreground">
                      {strategies("disabledSuffix")}
                    </span>
                  )}
                </span>
              </SelectItem>
            );
          })}
        </SelectContent>
      </Select>
    </div>
  );
}

function SingleColumn({
  results,
  sectionLabel,
  emptyLabel,
  countLabel,
}: {
  results: Section[];
  sectionLabel: string;
  emptyLabel: string;
  countLabel: string;
}) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>{countLabel}</span>
      </div>

      <div className="max-h-[22rem] overflow-y-auto rounded-lg border">
        {results.length === 0 ? (
          <p className="px-3 py-8 text-center text-sm text-muted-foreground">
            {emptyLabel}
          </p>
        ) : (
          results.map((section, i) => (
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

function CompareColumns({
  data,
  sectionLabel,
  emptyLabel,
  judgeMissingLabel,
  strategyLabelFor,
}: {
  data: EvalCompareResponse;
  sectionLabel: string;
  emptyLabel: string;
  judgeMissingLabel: string;
  strategyLabelFor: (value: SearchStrategy) => string;
}) {
  const [left, right] = data.results;
  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-3">
        <ColumnResult
          result={left}
          label={left ? strategyLabelFor(left.strategy) : ""}
          sectionLabel={sectionLabel}
          emptyLabel={emptyLabel}
        />
        <ColumnResult
          result={right}
          label={right ? strategyLabelFor(right.strategy) : ""}
          sectionLabel={sectionLabel}
          emptyLabel={emptyLabel}
        />
      </div>

      <div className="rounded-md border px-3 py-2 text-xs">
        {data.judges.length === 0 ? (
          <p className="text-muted-foreground">
            {data.judge_reason || judgeMissingLabel}
          </p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {data.judges.map((verdict, i) => {
              const winner =
                verdict.winner === "A"
                  ? strategyLabelFor(verdict.a_strategy)
                  : verdict.winner === "B"
                    ? strategyLabelFor(verdict.b_strategy)
                    : "tie";
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
    </div>
  );
}

function ColumnResult({
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

function StatsPill({ stats }: { stats: Record<string, unknown> }) {
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

function pickNumber(stats: Record<string, unknown>, key: string): number | null {
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

function SectionRow({
  index,
  section,
  sectionLabel,
}: {
  index: number;
  section: Section;
  sectionLabel: string;
}) {
  return (
    <div className="border-t p-3 first:border-t-0">
      <div className="mb-1 flex items-center gap-2">
        <span className="grid size-5 shrink-0 place-items-center rounded-[6px] bg-muted text-[11px] font-semibold text-foreground">
          {index + 1}
        </span>
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">
          {section.heading || sectionLabel}
        </span>
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
    </div>
  );
}
