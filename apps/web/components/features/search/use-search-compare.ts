"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

import { api, ApiError, type EvalCompareResponse } from "@/lib/api";
import {
  isSearchStrategy,
  SEARCH_STRATEGIES,
  type SearchStrategy,
} from "@/lib/retrieval-config";

export const COMPARE_MIN_STRATEGIES = 2;
export const COMPARE_MAX_STRATEGIES = 3;

/** The alternative that differs most from a typical pick, in order of preference. */
const PREFERRED_ALTERNATIVES: readonly SearchStrategy[] = ["sag2", "multi_jev", "multi"];

/** Keep chips and columns in catalog order regardless of click order. */
export function sortCompareStrategies(
  values: ReadonlyArray<SearchStrategy>,
): SearchStrategy[] {
  const picked = new Set(values);
  return SEARCH_STRATEGIES.map((item) => item.value).filter((value) => picked.has(value));
}

/**
 * Seed the compare selection: the mode the user already has, plus the enabled
 * mode that behaves most differently from it (sag2 → multi_jev → multi).
 */
export function defaultCompareStrategies(
  current: SearchStrategy,
  enabled: ReadonlyArray<SearchStrategy>,
): SearchStrategy[] {
  const picks: SearchStrategy[] = enabled.includes(current) ? [current] : [];
  const alternative = PREFERRED_ALTERNATIVES.find(
    (value) => value !== current && enabled.includes(value),
  ) ?? enabled.find((value) => value !== current);
  if (alternative) picks.push(alternative);
  return sortCompareStrategies(picks);
}

/**
 * Apply a toggle-group change while enforcing 2–3 picks: a change that would
 * leave the group outside that range is ignored and the previous picks stay.
 */
export function selectCompareStrategies(
  current: ReadonlyArray<SearchStrategy>,
  next: ReadonlyArray<string>,
  enabled: ReadonlyArray<SearchStrategy>,
): SearchStrategy[] {
  const valid = sortCompareStrategies(
    next.filter((value): value is SearchStrategy => isSearchStrategy(value) && enabled.includes(value)),
  );
  if (valid.length < COMPARE_MIN_STRATEGIES || valid.length > COMPARE_MAX_STRATEGIES) {
    return sortCompareStrategies(current);
  }
  return valid;
}

export interface SearchCompareRunOptions {
  query: string;
  sourceIds?: string[];
  topK?: number;
}

export interface SearchCompareState {
  /** Compare mode is on: the search box runs eval-compare instead of a normal search. */
  enabled: boolean;
  /** Selected modes, filtered to what the deployment can serve. */
  strategies: SearchStrategy[];
  /** Whether an LLM judge can run at all on this deployment. */
  judgeAvailable: boolean;
  /** Effective judge flag sent with the request. */
  judge: boolean;
  busy: boolean;
  /** Modes of the request in flight, so busy columns can be drawn. */
  pendingStrategies: SearchStrategy[];
  result: EvalCompareResponse | null;
  error: string | null;
  lastQuery: string;
  canRun: boolean;
  enable: (current: SearchStrategy) => void;
  disable: () => void;
  selectStrategies: (values: ReadonlyArray<string>) => void;
  setJudge: (judge: boolean) => void;
  run: (options: SearchCompareRunOptions) => Promise<EvalCompareResponse | null>;
  cancel: () => void;
  reset: () => void;
}

/**
 * Local state for the side-by-side compare on the search page. It never
 * touches the normal search lifecycle in SearchProvider, so toggling compare
 * off brings the previous normal result straight back.
 */
export function useSearchCompare({
  enabledStrategies,
  judgeAvailable,
}: {
  enabledStrategies: ReadonlyArray<SearchStrategy>;
  judgeAvailable: boolean;
}): SearchCompareState {
  const t = useTranslations("Search");
  const [enabled, setEnabled] = React.useState(false);
  const [selected, setSelected] = React.useState<SearchStrategy[]>([]);
  const [judgeChoice, setJudgeChoice] = React.useState(true);
  const [busy, setBusy] = React.useState(false);
  const [pendingStrategies, setPendingStrategies] = React.useState<SearchStrategy[]>([]);
  const [result, setResult] = React.useState<EvalCompareResponse | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [lastQuery, setLastQuery] = React.useState("");
  const abortRef = React.useRef<AbortController | null>(null);

  const enabledKey = enabledStrategies.join("\u0000");
  const strategies = React.useMemo(
    () => selected.filter((value) => enabledStrategies.includes(value)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [selected, enabledKey],
  );
  const judge = judgeAvailable && judgeChoice;

  const cancel = React.useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setBusy(false);
  }, []);

  React.useEffect(() => () => {
    abortRef.current?.abort();
  }, []);

  const enable = React.useCallback((current: SearchStrategy) => {
    setSelected(defaultCompareStrategies(current, enabledStrategies));
    setEnabled(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabledKey]);

  const disable = React.useCallback(() => {
    cancel();
    setEnabled(false);
  }, [cancel]);

  const selectStrategies = React.useCallback((values: ReadonlyArray<string>) => {
    setSelected((current) => selectCompareStrategies(current, values, enabledStrategies));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabledKey]);

  const reset = React.useCallback(() => {
    cancel();
    setResult(null);
    setError(null);
    setLastQuery("");
    setPendingStrategies([]);
  }, [cancel]);

  const run = React.useCallback(async ({ query, sourceIds, topK }: SearchCompareRunOptions) => {
    const text = query.trim();
    if (!text || strategies.length < COMPARE_MIN_STRATEGIES) return null;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setBusy(true);
    setError(null);
    setResult(null);
    setLastQuery(text);
    setPendingStrategies(strategies);
    try {
      const response = await api.evalCompare({
        query: text,
        strategies,
        source_ids: sourceIds,
        top_k: topK,
        judge,
      }, controller.signal);
      if (abortRef.current !== controller) return null;
      setResult(response);
      return response;
    } catch (caught) {
      if (abortRef.current !== controller) return null;
      if (!(caught instanceof ApiError && caught.code === "aborted")) {
        setError(caught instanceof ApiError ? caught.message : t("compareFailed"));
      }
      return null;
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
        setBusy(false);
      }
    }
  }, [judge, strategies, t]);

  return {
    enabled,
    strategies,
    judgeAvailable,
    judge,
    busy,
    pendingStrategies,
    result,
    error,
    lastQuery,
    canRun: !busy && strategies.length >= COMPARE_MIN_STRATEGIES,
    enable,
    disable,
    selectStrategies,
    setJudge: setJudgeChoice,
    run,
    cancel,
    reset,
  };
}
