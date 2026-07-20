"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

import { api, ApiError } from "@/lib/api";
import type { SearchStrategy } from "@/lib/retrieval-config";
import type { ActivityItem, SearchResponse, Source } from "@/lib/types";
import {
  activationFromSearch,
  dispatchUniverseActivation,
  dispatchUniverseReset,
} from "@/lib/universe-events";
import {
  appendSearchSummary,
  beginSearchLifecycle,
  canLoadMoreSearch,
  completeSearchLifecycle,
  createSearchKey,
  DEFAULT_SEARCH_TOP_K,
  failSearchLifecycle,
  receiveSearchResult,
  resolveSearchRun,
  type SearchLifecycleState,
  type SearchRunIntent,
} from "@/components/features/search/search-state";

export type { SearchPhase, SearchRunIntent } from "@/components/features/search/search-state";

export interface SearchScope {
  id: string;
  name: string;
}

export interface SearchRunOptions {
  query?: string;
  topK?: number;
  strategy?: SearchStrategy;
  sourceIds?: string[];
  saveExploration?: boolean;
  intent?: SearchRunIntent;
}

export type SearchBrowseView = "activity" | "history";
export type SearchContentView = "results" | SearchBrowseView;

interface SearchWorkspaceState extends SearchLifecycleState {
  query: string;
  scoped: SearchScope[];
  sources: Source[];
  strategy: SearchStrategy;
  activity: ActivityItem[] | null;
  history: string[];
  contentView: SearchContentView;
  contentPreference: SearchBrowseView;
}

interface SearchWorkspaceContextValue extends SearchWorkspaceState {
  defaultStrategy: SearchStrategy;
  canLoadMore: boolean;
  setQuery: (query: string) => void;
  setStrategy: (strategy: SearchStrategy) => void;
  setScope: (scope: SearchScope[]) => void;
  toggleSource: (source: Source) => void;
  removeSource: (sourceId: string) => void;
  ensureSources: () => Promise<Source[]>;
  ensureActivity: (sourceIds?: string[]) => Promise<ActivityItem[]>;
  run: (options?: SearchRunOptions) => Promise<SearchResponse | null>;
  removeHistory: (query: string) => void;
  clearHistory: () => void;
  setContentView: (view: SearchContentView) => void;
  restoreContentPreference: () => void;
  clear: () => void;
  cancel: () => void;
}

const SearchWorkspaceContext = React.createContext<SearchWorkspaceContextValue | null>(null);
const SEARCH_HISTORY_KEY = "sag:search-history";
const SEARCH_HISTORY_LIMIT = 20;
const SEARCH_CONTENT_PREFERENCE_KEY = "sag:search-content-preference";

interface ActiveSearchRequest {
  id: number;
  rollback: SearchLifecycleState | null;
}

function captureSearchLifecycle(state: SearchLifecycleState): SearchLifecycleState {
  return {
    result: state.result,
    busy: state.busy,
    phase: state.phase,
    summaryStreaming: state.summaryStreaming,
    error: state.error,
    committedSearchKey: state.committedSearchKey,
    lastQuery: state.lastQuery,
    lastStrategy: state.lastStrategy,
    topK: state.topK,
    hasMore: state.hasMore,
  };
}

function readSearchContentPreference(): SearchBrowseView {
  if (typeof window === "undefined") return "activity";
  try {
    return window.localStorage.getItem(SEARCH_CONTENT_PREFERENCE_KEY) === "history"
      ? "history"
      : "activity";
  } catch {
    return "activity";
  }
}

function writeSearchContentPreference(view: SearchBrowseView) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(SEARCH_CONTENT_PREFERENCE_KEY, view);
  } catch {
    /* The in-memory preference still works when local storage is unavailable. */
  }
}

function readSearchHistory(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const value = JSON.parse(window.localStorage.getItem(SEARCH_HISTORY_KEY) ?? "[]");
    if (!Array.isArray(value)) return [];
    return value
      .filter((item): item is string => typeof item === "string" && Boolean(item.trim()))
      .map((item) => item.trim())
      .slice(0, SEARCH_HISTORY_LIMIT);
  } catch {
    return [];
  }
}

function writeSearchHistory(history: string[]) {
  if (typeof window === "undefined") return;
  try {
    if (history.length === 0) {
      window.localStorage.removeItem(SEARCH_HISTORY_KEY);
      return;
    }
    window.localStorage.setItem(SEARCH_HISTORY_KEY, JSON.stringify(history));
  } catch {
    /* The in-memory history still works when local storage is unavailable. */
  }
}

function rememberSearch(query: string, current: string[]): string[] {
  const normalized = query.trim();
  if (!normalized) return current;
  const next = [
    normalized,
    ...current.filter((item) => item !== normalized),
  ].slice(0, SEARCH_HISTORY_LIMIT);
  writeSearchHistory(next);
  return next;
}

export function SearchProvider({
  defaultStrategy,
  children,
}: {
  defaultStrategy: SearchStrategy;
  children: React.ReactNode;
}) {
  const t = useTranslations("Search");
  const [state, setState] = React.useState<SearchWorkspaceState>({
    query: "",
    scoped: [],
    sources: [],
    result: null,
    busy: false,
    phase: "idle",
    summaryStreaming: false,
    error: "",
    committedSearchKey: "",
    lastQuery: "",
    strategy: defaultStrategy,
    lastStrategy: defaultStrategy,
    topK: DEFAULT_SEARCH_TOP_K,
    hasMore: false,
    activity: null,
    history: [],
    contentView: "activity",
    contentPreference: "activity",
  });
  const stateRef = React.useRef(state);
  stateRef.current = state;
  const requestIdRef = React.useRef(0);
  const abortRef = React.useRef<AbortController | null>(null);
  const activeRequestRef = React.useRef<ActiveSearchRequest | null>(null);
  const sourcesLoadedRef = React.useRef(false);
  const sourcesPromiseRef = React.useRef<Promise<Source[]> | null>(null);
  const activityPromiseRef = React.useRef<{
    key: string;
    promise: Promise<ActivityItem[]>;
  } | null>(null);
  const loadedActivityKeyRef = React.useRef<string | null>(null);
  const requestedActivityKeyRef = React.useRef<string | null>(null);

  React.useEffect(() => {
    const history = readSearchHistory();
    const contentPreference = readSearchContentPreference();
    setState((current) => ({
      ...current,
      history,
      contentPreference,
      contentView:
        current.contentView === "results" ? "results" : contentPreference,
    }));
  }, []);

  React.useEffect(
    () => () => {
      abortRef.current?.abort();
    },
    [],
  );

  React.useEffect(() => {
    if (!stateRef.current.lastQuery) {
      setState((current) => ({
        ...current,
        strategy: defaultStrategy,
        lastStrategy: defaultStrategy,
      }));
    }
  }, [defaultStrategy]);

  const cancel = React.useCallback(() => {
    const activeRequest = activeRequestRef.current;
    requestIdRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    activeRequestRef.current = null;
    if (!activeRequest) return;
    setState((current) => ({
      ...current,
      ...failSearchLifecycle(current, "", activeRequest.rollback),
    }));
  }, []);

  const clear = React.useCallback(() => {
    cancel();
    dispatchUniverseReset("search-clear");
    setState((current) => ({
      ...current,
      query: "",
      result: null,
      busy: false,
      phase: "idle",
      summaryStreaming: false,
      error: "",
      committedSearchKey: "",
      lastQuery: "",
      topK: DEFAULT_SEARCH_TOP_K,
      hasMore: false,
      contentView: current.contentPreference,
    }));
  }, [cancel]);

  const setQuery = React.useCallback(
    (query: string) => {
      if (!query.trim()) {
        clear();
        return;
      }
      setState((current) => ({ ...current, query }));
    },
    [clear],
  );

  const ensureSources = React.useCallback(async () => {
    if (sourcesLoadedRef.current) return stateRef.current.sources;
    if (!sourcesPromiseRef.current) {
      sourcesPromiseRef.current = api
        .listSources()
        .then((sources) => {
          sourcesLoadedRef.current = true;
          setState((current) => ({ ...current, sources }));
          return sources;
        })
        .finally(() => {
          sourcesPromiseRef.current = null;
        });
    }
    return sourcesPromiseRef.current;
  }, []);

  const ensureActivity = React.useCallback(async (sourceIds?: string[]) => {
    const normalizedSourceIds = [...new Set(
      sourceIds?.map((sourceId) => sourceId.trim()).filter(Boolean) ?? [],
    )].sort();
    const key = normalizedSourceIds.join("\u0000");
    if (
      loadedActivityKeyRef.current === key
      && stateRef.current.activity !== null
    ) {
      return stateRef.current.activity;
    }
    if (activityPromiseRef.current?.key === key) {
      return activityPromiseRef.current.promise;
    }

    requestedActivityKeyRef.current = key;
    setState((current) => (
      loadedActivityKeyRef.current === key
        ? current
        : { ...current, activity: null }
    ));
    const promise = api
        .getActivity(normalizedSourceIds.length ? normalizedSourceIds : undefined)
        .then((items) => items.filter((item) => item.type === "document"))
        .catch(() => [])
        .then((activity) => {
          if (requestedActivityKeyRef.current !== key) return activity;
          loadedActivityKeyRef.current = key;
          setState((current) => ({ ...current, activity }));
          return activity;
        })
        .finally(() => {
          if (activityPromiseRef.current?.key === key) {
            activityPromiseRef.current = null;
          }
        });
    activityPromiseRef.current = { key, promise };
    return promise;
  }, []);

  const run = React.useCallback(async (options: SearchRunOptions = {}) => {
    const snapshot = stateRef.current;
    const query = (options.query ?? snapshot.query).trim();
    if (!query) {
      clear();
      return null;
    }

    const strategy = options.strategy ?? snapshot.strategy;
    const sourceIds = options.sourceIds
      ?? (snapshot.scoped.length ? snapshot.scoped.map((source) => source.id) : undefined);
    const requestedKey = createSearchKey({ query, strategy, sourceIds });
    const resolvedRun = resolveSearchRun({
      intent: options.intent,
      requestedKey,
      requestedTopK: options.topK,
      committedKey: snapshot.committedSearchKey,
      committedTopK: snapshot.topK,
      hasResult: snapshot.result !== null,
      idle: snapshot.phase === "idle",
    });
    const rollback = resolvedRun.intent === "load-more"
      ? captureSearchLifecycle(snapshot)
      : null;

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const requestId = ++requestIdRef.current;
    const resetEpoch = resolvedRun.intent === "replace"
      ? dispatchUniverseReset("search-start")
      : null;
    activeRequestRef.current = {
      id: requestId,
      rollback,
    };

    setState((current) => ({
      ...current,
      ...beginSearchLifecycle(current, { ...resolvedRun, strategy }),
      query,
      strategy,
      contentView: "results",
    }));
    let completed = false;
    const isCurrentRequest = () => (
      requestId === requestIdRef.current
      && activeRequestRef.current?.id === requestId
    );
    let pendingSummary = "";
    let summaryFrame: number | null = null;
    const discardPendingSummary = () => {
      pendingSummary = "";
      if (summaryFrame !== null) cancelAnimationFrame(summaryFrame);
      summaryFrame = null;
    };
    const flushPendingSummary = () => {
      summaryFrame = null;
      const delta = pendingSummary;
      pendingSummary = "";
      if (!delta || completed || !isCurrentRequest()) return;
      setState((current) => ({
        ...current,
        ...appendSearchSummary(current, delta),
      }));
    };
    const enqueueSummary = (delta: string) => {
      if (!delta) return;
      pendingSummary += delta;
      if (summaryFrame === null) {
        // One markdown/layout update per paint keeps long answers smooth while
        // preserving the provider's real token stream semantics.
        summaryFrame = requestAnimationFrame(flushPendingSummary);
      }
    };
    const commitResult = (result: SearchResponse) => {
      if (completed || !isCurrentRequest()) return;
      completed = true;
      discardPendingSummary();
      const activationEpoch = resolvedRun.intent === "load-more"
        ? dispatchUniverseReset("search-load-more-complete")
        : resetEpoch;
      dispatchUniverseActivation(
        activationFromSearch(result),
        activationEpoch ?? undefined,
      );
      const committedQuery = result.query || query;
      const history = rememberSearch(committedQuery, stateRef.current.history);
      setState((current) => ({
        ...current,
        ...completeSearchLifecycle(current, result, {
          key: requestedKey,
          query: committedQuery,
          strategy,
          topK: resolvedRun.topK,
          hasMore: Boolean(result.stats.has_more),
        }),
        history,
      }));
    };

    try {
      const saveExploration = options.saveExploration ?? false;
      const result = await api.streamGlobalSearch(
        {
          query,
          source_ids: sourceIds,
          top_k: resolvedRun.topK,
          strategy,
          save_exploration: saveExploration,
        },
        {
          onResult: (result) => {
            if (completed || !isCurrentRequest()) return;
            setState((current) => ({
              ...current,
              ...receiveSearchResult(current, result),
            }));
          },
          onSummaryDelta: (delta) => {
            if (completed || !isCurrentRequest()) return;
            enqueueSummary(delta);
          },
          onCompleted: commitResult,
        },
        controller.signal,
      );
      if (requestId !== requestIdRef.current) return null;
      commitResult(result);
      return result;
    } catch (error) {
      if (requestId !== requestIdRef.current) return null;
      if (error instanceof ApiError && error.code === "aborted") {
        setState((current) => ({
          ...current,
          ...failSearchLifecycle(current, "", rollback),
        }));
        return null;
      }
      const message = error instanceof ApiError ? error.message : t("failed");
      setState((current) => ({
        ...current,
        ...failSearchLifecycle(current, message, rollback),
      }));
      throw error;
    } finally {
      discardPendingSummary();
      if (requestId === requestIdRef.current && abortRef.current === controller) {
        abortRef.current = null;
      }
      if (activeRequestRef.current?.id === requestId) {
        activeRequestRef.current = null;
      }
    }
  }, [clear, t]);

  const setStrategy = React.useCallback((strategy: SearchStrategy) => {
    setState((current) => ({ ...current, strategy }));
  }, []);

  const setScope = React.useCallback((scoped: SearchScope[]) => {
    setState((current) => ({ ...current, scoped }));
  }, []);

  const toggleSource = React.useCallback((source: Source) => {
    setState((current) => ({
      ...current,
      scoped: current.scoped.some((item) => item.id === source.id)
        ? current.scoped.filter((item) => item.id !== source.id)
        : [...current.scoped, { id: source.id, name: source.name }],
    }));
  }, []);

  const removeSource = React.useCallback((sourceId: string) => {
    setState((current) => ({
      ...current,
      scoped: current.scoped.filter((item) => item.id !== sourceId),
    }));
  }, []);

  const removeHistory = React.useCallback((query: string) => {
    const history = stateRef.current.history.filter((item) => item !== query);
    writeSearchHistory(history);
    setState((current) => ({ ...current, history }));
  }, []);

  const clearHistory = React.useCallback(() => {
    writeSearchHistory([]);
    setState((current) => ({ ...current, history: [] }));
  }, []);

  const setContentView = React.useCallback((contentView: SearchContentView) => {
    if (contentView === "results") {
      setState((current) => ({ ...current, contentView }));
      return;
    }
    writeSearchContentPreference(contentView);
    setState((current) => ({
      ...current,
      contentView,
      contentPreference: contentView,
    }));
  }, []);

  const restoreContentPreference = React.useCallback(() => {
    setState((current) => ({
      ...current,
      contentView: current.contentPreference,
    }));
  }, []);

  const draftSearchKey = createSearchKey({
    query: state.query,
    strategy: state.strategy,
    sourceIds: state.scoped.map((source) => source.id),
  });
  const canLoadMore = canLoadMoreSearch({
    phase: state.phase,
    hasResult: state.result !== null,
    hasMore: state.hasMore,
    topK: state.topK,
    committedKey: state.committedSearchKey,
    draftKey: draftSearchKey,
  });

  const value = React.useMemo<SearchWorkspaceContextValue>(
    () => ({
      ...state,
      defaultStrategy,
      canLoadMore,
      setQuery,
      setStrategy,
      setScope,
      toggleSource,
      removeSource,
      ensureSources,
      ensureActivity,
      run,
      removeHistory,
      clearHistory,
      setContentView,
      restoreContentPreference,
      clear,
      cancel,
    }),
    [
      cancel,
      canLoadMore,
      clear,
      defaultStrategy,
      ensureActivity,
      ensureSources,
      clearHistory,
      removeHistory,
      removeSource,
      restoreContentPreference,
      run,
      setContentView,
      setQuery,
      setScope,
      setStrategy,
      state,
      toggleSource,
    ],
  );

  return (
    <SearchWorkspaceContext.Provider value={value}>
      {children}
    </SearchWorkspaceContext.Provider>
  );
}

export function useSearchWorkspace(): SearchWorkspaceContextValue {
  const value = React.useContext(SearchWorkspaceContext);
  if (!value) throw new Error("useSearchWorkspace must be used inside SearchProvider");
  return value;
}
