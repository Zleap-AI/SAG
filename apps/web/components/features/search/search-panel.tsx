"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import {
  Clock,
  History,
  Library,
  List,
  Search as SearchIcon,
  Sparkles,
  Waypoints,
  X,
} from "lucide-react";
import { toast } from "sonner";

import {
  getSearchStrategy,
  type SearchStrategy,
} from "@/lib/retrieval-config";
import type { SearchResultSort } from "@/lib/search-result-sort";
import { cn } from "@/lib/utils";
import type {
  ActivityItem,
  Citation,
  SearchEvent,
  SearchResponse,
  Source,
} from "@/lib/types";
import { useDetailPanel } from "@/components/features/detail-panel";
import {
  useSearchWorkspace,
  type SearchContentView,
  type SearchRunIntent,
} from "@/components/features/search/search-provider";
import {
  ActivityTimeline,
  ResultList,
  SearchHistoryList,
  SearchScanning,
  SearchSummaryCard,
  sectionCitation,
} from "@/components/features/search/search-panel-sections";
import { SearchStrategyControl } from "@/components/features/search-strategy-control";
import { EmptyState } from "@/components/features/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Toggle } from "@/components/ui/toggle";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

// 图谱视图较重，按需加载。
const SearchGraph = dynamic(() => import("@/components/features/search-graph"), {
  ssr: false,
  loading: () => <Skeleton className="h-[560px] rounded-lg" />,
});

function mentionTerm(query: string): string | null {
  const at = query.lastIndexOf("@");
  if (at < 0) return null;
  const term = query.slice(at + 1);
  return /\s/.test(term) ? null : term;
}

function removeMentionTerm(query: string): string {
  const at = query.lastIndexOf("@");
  if (at < 0) return query;
  const before = query.slice(0, at).trimEnd();
  return before ? `${before} ` : "";
}

export interface SearchPanelProps {
  active?: boolean;
  initialQuery?: string;
  initialSourceId?: string | null;
  saveInitialExploration?: boolean;
  showCancel?: boolean;
  showGraphView?: boolean;
  showRecentActivity?: boolean;
  showContentSwitcher?: boolean;
  activityInteractive?: boolean;
  onCancel?: () => void;
  onSearchStart?: (query: string) => void;
  onSearchComplete?: (result: SearchResponse) => void;
  onSearchError?: (message: string) => void;
  onSearchCancel?: () => void;
  onActivityClick?: (item: ActivityItem) => void;
  onEventClick?: (event: SearchEvent, result: SearchResponse) => void;
  onCitationClick?: (citation: Citation, result: SearchResponse) => void;
  className?: string;
}

/** Complete adaptive search surface shared by the main workspace and mini entry. */
export function SearchPanel({
  active = true,
  initialQuery = "",
  initialSourceId,
  saveInitialExploration = false,
  showCancel = false,
  showGraphView = true,
  showRecentActivity = true,
  showContentSwitcher = true,
  activityInteractive = true,
  onCancel,
  onSearchStart,
  onSearchComplete,
  onSearchError,
  onSearchCancel,
  onActivityClick,
  onEventClick,
  onCitationClick,
  className,
}: SearchPanelProps) {
  const t = useTranslations("Search");
  const searchStrategies = useTranslations("SearchStrategies");
  const search = useSearchWorkspace();
  const ensureSources = search.ensureSources;
  const { open } = useDetailPanel();
  const [mentionOpen, setMentionOpen] = React.useState(false);
  const [mentionIndex, setMentionIndex] = React.useState(0);
  const [view, setView] = React.useState<"list" | "graph">("graph");
  const [resultSort, setResultSort] = React.useState<SearchResultSort>("relevance");
  const [compact, setCompact] = React.useState(false);
  const rootRef = React.useRef<HTMLDivElement>(null);
  const inputRef = React.useRef<HTMLInputElement>(null);
  const hydrationKeyRef = React.useRef("");
  const contentView = search.contentView;
  const changeContentView = search.setContentView;

  const executeSearch = React.useCallback(
    async (options: {
      query?: string;
      topK?: number;
      strategy?: SearchStrategy;
      sourceIds?: string[];
      saveExploration?: boolean;
      intent?: SearchRunIntent;
    } = {}) => {
      const query = (options.query ?? search.query).trim();
      if (!query) {
        search.clear();
        return null;
      }
      setMentionOpen(false);
      changeContentView("results");
      onSearchStart?.(query);
      try {
        const result = await search.run(options);
        if (result) onSearchComplete?.(result);
        return result;
      } catch (error) {
        const message = error instanceof Error ? error.message : t("failed");
        if (onSearchError) onSearchError(message);
        else if (options.intent === "load-more") toast.error(message);
        return null;
      }
    },
    [changeContentView, onSearchComplete, onSearchError, onSearchStart, search, t],
  );

  const activitySourceIds = React.useMemo(
    () => search.scoped.map((source) => source.id).sort(),
    [search.scoped],
  );
  const activityScopeKey = activitySourceIds.join("\u0000");
  const scopedActivity = React.useMemo(() => {
    if (!search.activity || activitySourceIds.length === 0) return search.activity;
    const allowed = new Set(activitySourceIds);
    return search.activity.filter((item) => (
      typeof item.source_id === "string" && allowed.has(item.source_id)
    ));
  }, [activitySourceIds, search.activity]);

  React.useEffect(() => {
    if (showRecentActivity) void search.ensureActivity(activitySourceIds);
    const seedQuery = initialQuery.trim();
    const hydrationKey = `${seedQuery}\u0000${initialSourceId ?? ""}`;
    if (hydrationKeyRef.current === hydrationKey) {
      if (active) inputRef.current?.focus();
      return;
    }
    let cancelled = false;
    void ensureSources().then((sources) => {
      if (cancelled) return;
      if (hydrationKeyRef.current === hydrationKey) return;
      hydrationKeyRef.current = hydrationKey;
      const source = initialSourceId
        ? sources.find((item) => item.id === initialSourceId)
        : undefined;
      const sourceIds = source ? [source.id] : undefined;
      search.setScope(source ? [{ id: source.id, name: source.name }] : []);
      if (!seedQuery) {
        if (active) inputRef.current?.focus();
        return;
      }
      search.setQuery(seedQuery);
      void executeSearch({
        query: seedQuery,
        sourceIds,
        saveExploration: saveInitialExploration,
      });
    });
    return () => {
      cancelled = true;
    };
    // The URL seed is consumed once; live search state is owned by SearchProvider.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    active,
    activityScopeKey,
    initialQuery,
    initialSourceId,
    saveInitialExploration,
    showRecentActivity,
  ]);

  React.useEffect(() => {
    void ensureSources();
    if (active) inputRef.current?.focus();
  }, [active, ensureSources]);

  React.useLayoutEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const update = () => {
      const next = root.getBoundingClientRect().width < 520;
      setCompact((current) => (current === next ? current : next));
    };
    update();
    // Use the border box: compact mode changes this same node's padding, so
    // observing contentRect would make the 520px breakpoint feed back into
    // itself and oscillate forever when a list scrollbar appears.
    const observer = new ResizeObserver(update);
    observer.observe(root);
    return () => observer.disconnect();
  }, []);

  const term = mentionTerm(search.query);
  const filteredSources = React.useMemo(() => {
    const needle = (term ?? "").trim().toLowerCase();
    return search.sources.filter((source) =>
      !needle || source.name.toLowerCase().includes(needle),
    );
  }, [search.sources, term]);
  React.useEffect(() => {
    if (!mentionOpen) return;
    if (term === null) {
      setMentionOpen(false);
      return;
    }
    setMentionIndex(0);
  }, [mentionOpen, term]);

  const chooseSource = React.useCallback((source: Source | undefined) => {
    if (!source) return;
    search.toggleSource(source);
    search.setQuery(removeMentionTerm(search.query));
    setMentionOpen(false);
    setMentionIndex(0);
    requestAnimationFrame(() => inputRef.current?.focus());
  }, [search]);

  function changeStrategy(value: SearchStrategy) {
    if (value === search.strategy) return;
    search.setStrategy(value);
    const activeQuery = search.query.trim() || search.lastQuery;
    if ((search.result !== null || search.busy) && activeQuery) {
      void executeSearch({
        query: activeQuery,
        strategy: value,
        saveExploration: false,
      });
    }
  }

  async function run(e?: React.FormEvent) {
    e?.preventDefault();
    await executeSearch({
      saveExploration: false,
    });
  }

  async function loadMore() {
    if (!search.canLoadMore) return;
    await executeSearch({
      intent: "load-more",
      topK: search.topK + 12,
      saveExploration: false,
    });
  }

  const activeView = showGraphView ? view : "list";
  const graphViewActive = activeView === "graph" && Boolean(search.result?.events.length);
  const citations = React.useMemo(
    () => search.result?.sections.map(sectionCitation) ?? [],
    [search.result],
  );
  const resultStatus =
    search.phase === "loading-more"
      ? t("loadingMoreEvidence")
      : search.busy && search.result === null
        ? t("retrievingEvidence")
        : search.result
          ? t("selectedEvidence", { count: search.result.sections.length })
          : "";
  const currentSortLabel =
    resultSort === "relevance" ? t("sortRelevance") : t("sortTime");
  const nextSortLabel =
    resultSort === "relevance" ? t("sortTime") : t("sortRelevance");
  const sortToggleHint = t("sortToggleHint", {
    current: currentSortLabel,
    next: nextSortLabel,
  });
  const currentViewLabel = activeView === "list" ? t("listView") : t("graphView");
  const nextViewLabel = activeView === "list" ? t("graphView") : t("listView");
  const viewToggleHint = t("viewToggleHint", {
    current: currentViewLabel,
    next: nextViewLabel,
  });

  return (
    <div
      ref={rootRef}
      data-compact={compact || undefined}
      className={cn(
        "flex h-full min-h-0 w-full flex-col overflow-y-auto [scrollbar-gutter:stable]",
        compact ? "gap-3 p-3" : "gap-6 p-6",
        graphViewActive && "h-full min-h-0 overflow-hidden",
        className,
      )}
    >
      <form onSubmit={run} className="mx-auto flex w-full max-w-3xl shrink-0 flex-col gap-2">
        <div className="flex w-full items-center gap-2">
          <div className="relative flex-1">
            <div
              className={cn(
                "flex min-h-11 items-center gap-1 rounded-lg border border-input bg-card py-1.5 pl-10 pr-2 shadow-soft transition-[border-color,box-shadow]",
                "focus-within:border-ring focus-within:ring-2 focus-within:ring-ring/25",
              )}
            >
              <button
                type="submit"
                aria-label={t("search")}
                disabled={!search.query.trim() || search.busy}
                className="absolute left-2 top-1/2 grid size-6 -translate-y-1/2 place-items-center rounded text-muted-foreground outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none"
              >
                <SearchIcon className="size-4" />
              </button>
              <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1">
                {search.scoped.map((sc) => (
                  <span
                    key={sc.id}
                    className="inline-flex max-w-[min(100%,12rem)] items-center gap-1 rounded-md bg-primary/10 px-1.5 py-0.5 text-xs font-medium text-primary"
                  >
                    <Library className="size-3 shrink-0" />
                    <span className="truncate">{sc.name}</span>
                    <button
                      type="button"
                      aria-label={t("removeSource", { name: sc.name })}
                      onClick={() => search.removeSource(sc.id)}
                      className="rounded-sm text-primary/70 hover:bg-primary/15 hover:text-primary"
                    >
                      <X className="size-2.5" />
                    </button>
                  </span>
                ))}
                <input
                  ref={inputRef}
                  type="text"
                  autoFocus={active}
                  value={search.query}
                  onChange={(e) => {
                    const v = e.target.value;
                    const nextMentionOpen = mentionTerm(v) !== null;
                    const cancelledSearch = search.busy && !v.trim();
                    search.setQuery(v);
                    if (cancelledSearch) onSearchCancel?.();
                    setMentionOpen(nextMentionOpen);
                  }}
                  onKeyDown={(e) => {
                    if (mentionOpen) {
                      if (e.key === "ArrowDown") {
                        e.preventDefault();
                        setMentionIndex((i) => (filteredSources.length ? (i + 1) % filteredSources.length : 0));
                        return;
                      }
                      if (e.key === "ArrowUp") {
                        e.preventDefault();
                        setMentionIndex((i) =>
                          filteredSources.length ? (i - 1 + filteredSources.length) % filteredSources.length : 0,
                        );
                        return;
                      }
                      if (e.key === "Enter" || e.key === "Tab") {
                        e.preventDefault();
                        chooseSource(filteredSources[mentionIndex]);
                        return;
                      }
                      if (e.key === "Escape") {
                        e.preventDefault();
                        setMentionOpen(false);
                        return;
                      }
                    }
                    if (e.key === "@") {
                      setMentionOpen(true);
                      setMentionIndex(0);
                    }
                  }}
                  placeholder={search.scoped.length ? t("continueKeywords") : t("placeholder")}
                  className="min-w-[8ch] flex-1 bg-transparent py-0.5 text-sm outline-none placeholder:text-muted-foreground"
                />
              </div>
              {search.query && (
                <button
                  type="button"
                  onClick={() => {
                    const cancelledSearch = search.busy;
                    search.clear();
                    if (cancelledSearch) onSearchCancel?.();
                    setMentionOpen(false);
                    inputRef.current?.focus();
                  }}
                  aria-label={t("clear")}
                  className="grid size-5 shrink-0 place-items-center rounded-full text-muted-foreground outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <X className="size-3.5" />
                </button>
              )}
              <SearchStrategyControl
                value={search.strategy}
                defaultValue={search.defaultStrategy}
                onValueChange={changeStrategy}
              />
            </div>
            {mentionOpen && (
              <div className="absolute left-0 top-full z-20 mt-2 w-full">
                <div className="overflow-hidden rounded-lg border bg-card shadow-lift">
                  <div className="border-b px-3 py-2 text-xs font-medium text-muted-foreground">
                    {t("knowledgeScope")}
                  </div>
                  <div className="max-h-64 overflow-y-auto p-1" role="listbox">
                    {filteredSources.length === 0 ? (
                      <p className="px-3 py-5 text-center text-sm text-muted-foreground">{t("noMatchingSources")}</p>
                    ) : (
                      filteredSources.map((s, index) => {
                        const on = search.scoped.some((x) => x.id === s.id);
                        const active = index === mentionIndex;
                        return (
                          <button
                            key={s.id}
                            type="button"
                            role="option"
                            aria-selected={active}
                            onMouseEnter={() => setMentionIndex(index)}
                            onMouseDown={(event) => event.preventDefault()}
                            onClick={() => chooseSource(s)}
                            className={cn(
                              "flex h-9 w-full items-center gap-2 rounded-md px-2 text-left text-sm outline-none transition-colors",
                              active && "bg-muted text-foreground",
                            )}
                          >
                            <Library className="size-3.5 shrink-0 text-muted-foreground" />
                            <span className="min-w-0 flex-1 truncate">{s.name}</span>
                            {on && <span className="text-xs text-muted-foreground">{t("selected")}</span>}
                          </button>
                        );
                      })
                    )}
                  </div>
                </div>
              </div>
            )}
          </div>
          {showCancel && (
            <button
              type="button"
              onClick={() => {
                search.cancel();
                onSearchCancel?.();
                onCancel?.();
              }}
              className="shrink-0 rounded-md px-2 py-1.5 text-sm text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
            >
              {t("cancel")}
            </button>
          )}
        </div>
      </form>

      {showRecentActivity && showContentSwitcher && (
        <div className="mx-auto flex w-full max-w-3xl items-center px-1">
          <ToggleGroup
            type="single"
            variant="outline"
            size="sm"
            value={contentView}
            onValueChange={(value) =>
              value && changeContentView(value as SearchContentView)
            }
            aria-label={t("contentPanelAria")}
          >
            <ToggleGroupItem
              value="history"
              className="gap-1.5 px-3"
              disabled={search.busy}
            >
              <History className="size-3.5" />
              {t("history")}
            </ToggleGroupItem>
            <ToggleGroupItem
              value="activity"
              className="gap-1.5 px-3"
              disabled={search.busy}
            >
              <List className="size-3.5" />
              {t("recentActivity")}
            </ToggleGroupItem>
          </ToggleGroup>
        </div>
      )}

      {contentView === "results" && search.busy && search.result === null ? (
        <div className="mx-auto w-full max-w-3xl">
          <SearchScanning />
        </div>
      ) : contentView === "results" && search.result !== null ? (
        <div
          className={cn(
            "flex w-full flex-col gap-2",
            activeView === "graph" && search.result.events.length > 0
              ? "mx-auto min-h-0 max-w-[1200px] flex-1"
              : "mx-auto max-w-3xl",
          )}
        >
          <div className="flex items-center justify-between gap-3 px-1">
            <h2 className="flex items-center gap-2 text-sm font-medium">
              {t("results")}
              <span
                className="inline-flex items-center rounded-md bg-muted px-1.5 py-0.5 text-[11px] font-normal text-muted-foreground"
              >
                {searchStrategies(
                  getSearchStrategy(search.busy ? search.strategy : search.lastStrategy).labelKey,
                )}
              </span>
            </h2>
            <div className="flex items-center gap-3">
              {!compact && <span className="text-xs text-muted-foreground">
                {resultStatus}
              </span>}
              <div className="flex items-center gap-1">
                {activeView === "list" && search.result.events.length > 0 && (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Toggle
                        variant="outline"
                        size="sm"
                        pressed={resultSort === "time"}
                        onPressedChange={(pressed) =>
                          setResultSort(pressed ? "time" : "relevance")
                        }
                        aria-label={sortToggleHint}
                      >
                        {resultSort === "time" ? <Clock /> : <Sparkles />}
                      </Toggle>
                    </TooltipTrigger>
                    <TooltipContent side="bottom">{sortToggleHint}</TooltipContent>
                  </Tooltip>
                )}
                {showGraphView && (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Toggle
                        variant="outline"
                        size="sm"
                        pressed={activeView === "graph"}
                        onPressedChange={(pressed) =>
                          setView(pressed ? "graph" : "list")
                        }
                        aria-label={viewToggleHint}
                      >
                        {activeView === "graph" ? <Waypoints /> : <List />}
                      </Toggle>
                    </TooltipTrigger>
                    <TooltipContent side="bottom">{viewToggleHint}</TooltipContent>
                  </Tooltip>
                )}
              </div>
            </div>
          </div>
          {(search.summaryStreaming || search.result.summary) && (
            <SearchSummaryCard
              summary={search.result.summary}
              citations={citations}
              compact={compact}
              streaming={search.summaryStreaming}
              containerRef={rootRef}
              onCitationClick={(citation) => {
                if (onCitationClick) {
                  onCitationClick(citation, search.result!);
                  return;
                }
                if (!citation.chunk_id || !citation.source_id) return;
                open({
                  kind: "chunk",
                  sourceId: citation.source_id,
                  chunkId: citation.chunk_id,
                  heading: citation.heading,
                  sourceName: citation.source_name ?? undefined,
                });
              }}
            />
          )}
          {search.error && (
            <div
              role="alert"
              className="flex items-center justify-between gap-3 rounded-lg border border-destructive/25 bg-destructive/[0.04] px-3 py-2 text-xs text-destructive"
            >
              <span className="min-w-0">{t("loadMoreFailed", { error: search.error })}</span>
              {search.canLoadMore && (
                <button
                  type="button"
                  onClick={() => void loadMore()}
                  className="shrink-0 rounded-md border border-destructive/25 px-2 py-1 font-medium outline-none hover:bg-destructive/10 focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {t("retry")}
                </button>
              )}
            </div>
          )}
          {activeView === "graph" && search.result.events.length > 0 ? (
            <div className="min-h-0 flex-1">
              <SearchGraph
                events={search.result.events}
                entities={search.result.entities}
                relations={search.result.relations}
                onOpenEvent={
                  onEventClick
                    ? (event) => onEventClick(event, search.result!)
                    : undefined
                }
              />
            </div>
          ) : (
            <>
              <ResultList
                result={search.result}
                onEventClick={onEventClick}
                onCitationClick={onCitationClick}
                compact={compact}
                sort={resultSort}
              />
              {((search.canLoadMore && !search.error) || (search.busy && search.hasMore)) && (
                <div className="flex justify-center pt-1">
                  <button
                    type="button"
                    onClick={() => void loadMore()}
                    disabled={!search.canLoadMore}
                    className="rounded-full border bg-card px-4 py-1.5 text-xs text-muted-foreground shadow-soft outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
                  >
                    {search.busy ? t("loading") : t("showMoreResults")}
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      ) : contentView === "results" && search.error ? (
        <div className="mx-auto w-full max-w-3xl">
          <EmptyState
            icon={SearchIcon}
            title={t("incomplete")}
            description={search.error}
            action={(
              <button
                type="button"
                onClick={() => void run()}
                className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground outline-none hover:bg-primary/90 focus-visible:ring-2 focus-visible:ring-ring"
              >
                {t("searchAgain")}
              </button>
            )}
          />
        </div>
      ) : contentView === "history" ? (
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-3">
          <SearchHistoryList
            queries={search.history}
            compact={compact}
            onSelect={(query) => {
              search.setQuery(query);
              void executeSearch({ query, saveExploration: false });
            }}
            onRemove={search.removeHistory}
            onClear={search.clearHistory}
          />
        </div>
      ) : contentView === "activity" && showRecentActivity ? (
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-3">
          <ActivityTimeline
            items={scopedActivity}
            compact={compact}
            interactive={activityInteractive}
            onItemOpen={onActivityClick}
          />
        </div>
      ) : (
        <div className="mx-auto flex w-full max-w-3xl flex-1 items-center justify-center px-6 text-center">
          <div>
            <SearchIcon className="mx-auto size-6 text-muted-foreground/55" />
            <p className="mt-2 text-sm font-medium">{t("startTitle")}</p>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              {t("startDescription")}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
