"use client";

import * as React from "react";
import { FlaskConical, GitCompareArrows, Search, Trophy } from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { useApp } from "@/components/features/app-shell";
import { api, ApiError, type EvalCompareResponse } from "@/lib/api";
import {
  DEFAULT_SEARCH_STRATEGY,
  getSearchStrategiesDisabled,
  SEARCH_STRATEGIES,
  type SearchStrategy,
} from "@/lib/retrieval-config";
import type { Section } from "@/lib/types";
import {
  ColumnResult,
  JudgeVerdicts,
  SectionRow,
} from "@/components/features/retrieval-compare-parts";
import { RetrievalStatus } from "@/components/features/retrieval-status";
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
  const disabledMap = getSearchStrategiesDisabled(capabilities, strategies("unsupportedBackend"));

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
  const [resultStats, setResultStats] = React.useState<Record<string, unknown>>({});
  const [compareResults, setCompareResults] =
    React.useState<EvalCompareResponse | null>(null);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    if (!open) return;
    setResults(null);
    setResultStats({});
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
        setResultStats(response.stats);
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
          <div className="flex flex-col gap-2">
            <RetrievalStatus stats={resultStats} />
            <SingleColumn
              results={results}
              sectionLabel={t("section")}
              emptyLabel={t("empty")}
              countLabel={t("resultCount", { count: results.length })}
            />
          </div>
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
                <span className="flex min-w-0 flex-col gap-0.5">
                  <span className="flex items-center gap-1.5">
                    {strategies(labelKey)}
                    {info && (
                      <span className="text-[10px] text-muted-foreground">
                        {strategies("disabledSuffix")}
                      </span>
                    )}
                  </span>
                  {info && <span className="max-w-72 text-[11px] leading-4 text-muted-foreground">{info.message}</span>}
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

      <JudgeVerdicts
        judges={data.judges}
        judgeReason={data.judge_reason}
        missingLabel={judgeMissingLabel}
        strategyLabelFor={strategyLabelFor}
      />
    </div>
  );
}
