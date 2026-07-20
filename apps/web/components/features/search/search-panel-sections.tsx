"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import {
  ArrowUpRight,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Clock,
  FileText,
  History,
  Search as SearchIcon,
  Sparkles,
  Trash2,
} from "lucide-react";

import { sortSearchEvents, type SearchResultSort } from "@/lib/search-result-sort";
import type {
  ActivityItem,
  Citation,
  SearchEvent,
  SearchResponse,
  Section,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/features/empty-state";
import { MarkdownContent } from "@/components/features/markdown-content";
import { useDetailPanel } from "@/components/features/detail-panel";
import { DocStatusBadge } from "@/components/features/status-badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";

/** 检索扫描动画：骨架行 1→N 铺开、随机高亮、清空、再来——「翻找数据」的具象化。 */
export function SearchScanning() {
  const t = useTranslations("Search");
  const [rows, setRows] = React.useState(1);
  const [lit, setLit] = React.useState<Set<number>>(new Set());
  React.useEffect(() => {
    let n = 1;
    const grow = window.setInterval(() => {
      n = n >= 6 ? 1 : n + 1;
      setRows(n);
      const picks = new Set<number>();
      const count = n <= 2 ? 1 : 2 + (n % 2);
      while (picks.size < Math.min(count, n)) picks.add(Math.floor(Math.random() * n));
      setLit(picks);
    }, 420);
    return () => window.clearInterval(grow);
  }, []);
  return (
    <div
      className="flex flex-col gap-1.5 overflow-hidden rounded-lg border p-3"
      role="status"
      aria-live="polite"
      aria-label={t("searching")}
    >
      {Array.from({ length: 6 }).map((_, i) => (
        <div
          key={i}
          aria-hidden="true"
          className={cn(
            "h-8 rounded-md transition-[opacity,background-color,box-shadow] duration-300",
            i < rows ? "opacity-100" : "opacity-25",
            lit.has(i) ? "bg-primary/15 ring-1 ring-primary/25" : "bg-muted",
            "motion-reduce:bg-muted motion-reduce:opacity-100 motion-reduce:ring-0 motion-reduce:transition-none",
          )}
        />
      ))}
      <p className="pt-1 text-center text-xs text-muted-foreground">{t("scanning")}</p>
    </div>
  );
}

const ROW_CARD =
  "group flex w-full items-start gap-3 rounded-lg border bg-card px-4 py-3 text-left shadow-soft outline-none transition-all hover:bg-muted/35 hover:shadow-lift focus-visible:ring-2 focus-visible:ring-ring";
const ACTIVITY_PAGE_SIZE = 6;

function ActivityCard({
  item,
  compact,
  interactive,
  onOpen,
}: {
  item: ActivityItem;
  compact: boolean;
  interactive: boolean;
  onOpen?: (item: ActivityItem) => void;
}) {
  const t = useTranslations("Search");
  const { open } = useDetailPanel();
  const Icon = FileText;
  const cardClass = cn(
    "group flex w-full items-center gap-3 text-left outline-none transition-colors",
    compact ? "px-3 py-2.5" : "px-4 py-3",
    interactive && "hover:bg-muted/35 focus-visible:bg-muted/45",
  );
  const body = (
    <>
      <span className="grid size-7 shrink-0 place-items-center rounded-md bg-muted/70 text-muted-foreground">
        <Icon className="size-3.5" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="min-w-0 max-w-full truncate text-sm font-medium text-foreground">
            {item.title}
          </span>
          {item.status && <DocStatusBadge status={item.status} />}
        </span>
        <span className="mt-1 line-clamp-2 text-xs leading-relaxed text-muted-foreground">
          {item.subtitle || t("document")}
        </span>
      </span>
      {interactive && (
        compact ? (
          <ChevronRight className="size-3.5 shrink-0 text-muted-foreground transition-colors group-hover:text-foreground" />
        ) : (
          <span className="inline-flex shrink-0 items-center gap-1 text-xs text-muted-foreground transition-colors group-hover:text-foreground">
            {t("viewDetails")}
            <ArrowUpRight className="size-3" />
          </span>
        )
      )}
    </>
  );

  if (!interactive) {
    return (
      <div data-activity-item={item.id} className={cardClass}>
        {body}
      </div>
    );
  }

  return (
    <button
      type="button"
      data-activity-item={item.id}
      disabled={!item.source_id}
      onClick={() => {
        if (!item.source_id) return;
        if (onOpen) {
          onOpen(item);
          return;
        }
        open({
          kind: "document",
          sourceId: item.source_id,
          documentId: item.id,
          title: item.title,
        });
      }}
      className={cardClass}
    >
      {body}
    </button>
  );
}

export function ActivityTimeline({
  items,
  compact,
  interactive,
  onItemOpen,
}: {
  items: ActivityItem[] | null;
  compact: boolean;
  interactive: boolean;
  onItemOpen?: (item: ActivityItem) => void;
}) {
  const t = useTranslations("Search");
  const [visibleCount, setVisibleCount] = React.useState(ACTIVITY_PAGE_SIZE);
  const orderedItems = React.useMemo(() => {
    if (!items) return [];
    return items
      .map((item, index) => ({
        item,
        index,
        timestamp: Date.parse(item.at),
      }))
      .sort((left, right) => {
        const leftTime = Number.isFinite(left.timestamp) ? left.timestamp : 0;
        const rightTime = Number.isFinite(right.timestamp) ? right.timestamp : 0;
        return rightTime - leftTime || left.index - right.index;
      })
      .map(({ item }) => item);
  }, [items]);

  if (items === null) {
    return (
      <div className="flex flex-col gap-2">
        {Array.from({ length: 5 }).map((_, i) => (
          <Skeleton key={i} className="h-12 rounded-lg" />
        ))}
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <EmptyState
        icon={Clock}
        title={t("noRecentActivity")}
        description={t("recentActivityDescription")}
      />
    );
  }

  const visibleItems = orderedItems.slice(0, visibleCount);
  const remaining = Math.max(orderedItems.length - visibleItems.length, 0);

  return (
    <div
      data-activity-list="recent"
      className="overflow-hidden rounded-lg border border-border/70 bg-card/45"
    >
      {visibleItems.map((item, index) => (
        <div key={`${item.type}-${item.id}`} className={cn(index > 0 && "border-t border-border/60")}>
          <ActivityCard
            item={item}
            compact={compact}
            interactive={interactive}
            onOpen={onItemOpen}
          />
        </div>
      ))}
      {remaining > 0 && (
        <div className="border-t border-border/60 p-1.5">
          <button
            type="button"
            onClick={() =>
              setVisibleCount((current) =>
                Math.min(current + ACTIVITY_PAGE_SIZE, orderedItems.length),
              )
            }
            className="flex h-8 w-full items-center justify-center gap-1.5 rounded-md text-xs text-muted-foreground outline-none transition-colors hover:bg-muted/70 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
            aria-label={t("moreActivityAria", { count: remaining })}
          >
            {t("viewMore")}
            <ChevronDown className="size-3.5" />
          </button>
        </div>
      )}
    </div>
  );
}

export function SearchHistoryList({
  queries,
  compact,
  onSelect,
  onRemove,
  onClear,
}: {
  queries: string[];
  compact: boolean;
  onSelect: (query: string) => void;
  onRemove: (query: string) => void;
  onClear: () => void;
}) {
  const t = useTranslations("Search");
  const pageSize = 5;
  const [visibleCount, setVisibleCount] = React.useState(pageSize);
  const [clearConfirmOpen, setClearConfirmOpen] = React.useState(false);

  if (queries.length === 0) {
    return (
      <EmptyState
        icon={History}
        title={t("noHistory")}
        description={t("historyDescription")}
      />
    );
  }

  const shownQueries = queries.slice(0, visibleCount);
  const remaining = Math.max(queries.length - shownQueries.length, 0);
  const canCollapse = remaining === 0 && queries.length > pageSize;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-3 px-1">
        <span className="text-xs text-muted-foreground">
          {t("localRecords", { count: queries.length })}
        </span>
        <button
          type="button"
          onClick={() => setClearConfirmOpen(true)}
          className="rounded px-1.5 py-1 text-xs text-muted-foreground outline-none transition-colors hover:bg-destructive/10 hover:text-destructive focus-visible:ring-2 focus-visible:ring-ring"
        >
          {t("clearAll")}
        </button>
      </div>
      <div className="overflow-hidden rounded-lg border border-border/70 bg-card/45">
        {shownQueries.map((query, index) => (
          <div
            key={query}
            className={cn(
              "group flex w-full items-center transition-colors hover:bg-muted/35 focus-within:bg-muted/35",
              index > 0 && "border-t border-border/60",
            )}
          >
            <button
              type="button"
              onClick={() => onSelect(query)}
              className={cn(
                "flex min-w-0 flex-1 items-center gap-3 text-left outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
                compact ? "px-3 py-2.5" : "px-4 py-3",
              )}
            >
              <span className="grid size-7 shrink-0 place-items-center rounded-md bg-muted/70 text-muted-foreground">
                <History className="size-3.5" />
              </span>
              <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">
                {query}
              </span>
            </button>
            <button
              type="button"
              onClick={() => onRemove(query)}
              className="mr-2 grid size-7 shrink-0 place-items-center rounded-md text-muted-foreground/65 outline-none transition-colors hover:bg-destructive/10 hover:text-destructive focus-visible:ring-2 focus-visible:ring-ring"
              aria-label={t("deleteHistoryAria", { query })}
              title={t("delete")}
            >
              <Trash2 className="size-3.5" />
            </button>
          </div>
        ))}
      </div>
      {(remaining > 0 || canCollapse) && (
        <button
          type="button"
          onClick={() =>
            setVisibleCount((current) =>
              remaining > 0 ? Math.min(current + pageSize, queries.length) : pageSize,
            )
          }
          className="mx-auto inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-xs text-muted-foreground outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
          aria-expanded={remaining === 0}
        >
          {remaining > 0 ? (
            <>
              {t("expandMore")}
              <ChevronDown className="size-3.5" />
            </>
          ) : (
            <>
              {t("collapse")}
              <ChevronUp className="size-3.5" />
            </>
          )}
        </button>
      )}
      <ConfirmDialog
        open={clearConfirmOpen}
        onOpenChange={setClearConfirmOpen}
        title={t("clearHistoryTitle")}
        description={t("clearHistoryDescription", { count: queries.length })}
        confirmLabel={t("clearAll")}
        onConfirm={onClear}
      />
    </div>
  );
}

export function sectionCitation(section: Section, index: number): Citation {
  return {
    n: index + 1,
    chunk_id: section.chunk_id,
    heading: section.heading,
    snippet: section.content,
    score: section.score,
    source_id: section.source_id,
    source_name: section.source_name,
  };
}

export function ResultList({
  result,
  onEventClick,
  onCitationClick,
  compact,
  sort,
}: {
  result: SearchResponse;
  onEventClick?: (event: SearchEvent, result: SearchResponse) => void;
  onCitationClick?: (citation: Citation, result: SearchResponse) => void;
  compact: boolean;
  sort: SearchResultSort;
}) {
  const t = useTranslations("Search");
  const { open } = useDetailPanel();
  const events = React.useMemo(
    () => sortSearchEvents(result.events, sort),
    [result.events, sort],
  );
  if (result.events.length === 0 && result.sections.length === 0) {
    return (
      <EmptyState
        icon={SearchIcon}
        title={t("noEvidence")}
        description={t("noEvidenceDescription")}
      />
    );
  }
  if (result.events.length === 0) {
    return (
      <div className="flex flex-col gap-3">
        {result.sections.map((section, index) => {
          const citation = sectionCitation(section, index);
          return (
            <button
              key={`${section.source_id}:${section.chunk_id}:${index}`}
              type="button"
              disabled={!section.chunk_id || !section.source_id}
              onClick={() => {
                if (onCitationClick) {
                  onCitationClick(citation, result);
                  return;
                }
                if (!section.chunk_id || !section.source_id) return;
                open({
                  kind: "chunk",
                  sourceId: section.source_id,
                  chunkId: section.chunk_id,
                  heading: section.heading,
                  sourceName: section.source_name ?? undefined,
                });
              }}
              className={cn(
                ROW_CARD,
                compact && "gap-2 px-3",
                "disabled:cursor-default disabled:hover:bg-card disabled:hover:shadow-soft",
              )}
            >
              <span className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-md bg-primary/10 font-mono text-xs text-primary">
                {index + 1}
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex min-w-0 items-center gap-2">
                  <span className="min-w-0 flex-1 truncate text-sm font-medium">
                    {section.heading || t("relatedMaterial")}
                  </span>
                  <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] tabular-nums text-muted-foreground">
                    {section.score.toFixed(3)}
                  </span>
                </span>
                <span className="mt-1 line-clamp-3 text-xs leading-relaxed text-muted-foreground">
                  {section.content}
                </span>
              </span>
            </button>
          );
        })}
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      {events.map((event) => (
        <button
          key={event.id}
          type="button"
          disabled={!event.chunk_id || !event.source_id}
          onClick={() => {
            if (onEventClick) {
              onEventClick(event, result);
              return;
            }
            if (!event.chunk_id || !event.source_id) return;
            open({
              kind: "chunk",
              sourceId: event.source_id,
              chunkId: event.chunk_id,
              heading: event.title,
              sourceName: event.source_name ?? undefined,
            });
          }}
          className={cn(
            ROW_CARD,
            compact && "gap-2 px-3",
            "disabled:cursor-default disabled:hover:bg-card disabled:hover:shadow-soft",
          )}
        >
          <span className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-md bg-amber-500/10 text-amber-700 dark:text-amber-300">
            <Sparkles className="size-4" />
          </span>
          <span className="min-w-0 flex-1">
            <span className="flex min-w-0 items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-sm font-medium">
                {event.title || t("unnamedEvent")}
              </span>
              {!compact && event.category && (
                <span className="shrink-0 rounded bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-700 dark:text-amber-300">
                  {event.category}
                </span>
              )}
              {!compact && (
                <span className="shrink-0 text-xs text-muted-foreground">
                  {event.source_name}
                </span>
              )}
              <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] tabular-nums text-muted-foreground">
                {event.score.toFixed(3)}
              </span>
            </span>
            <span className="mt-1 line-clamp-2 text-xs leading-relaxed text-muted-foreground">
              {event.summary || t("noEventSummary")}
            </span>
          </span>
          {!compact && event.chunk_id && event.source_id && (
            <span className="mt-1 inline-flex shrink-0 items-center gap-1 text-xs text-muted-foreground transition-colors group-hover:text-foreground">
              {t("viewOriginal")}
              <ArrowUpRight className="size-3" />
            </span>
          )}
        </button>
      ))}
    </div>
  );
}

export function SearchSummaryCard({
  summary,
  citations,
  compact,
  streaming,
  containerRef,
  onCitationClick,
}: {
  summary: string;
  citations: Citation[];
  compact: boolean;
  streaming: boolean;
  containerRef: React.RefObject<HTMLDivElement | null>;
  onCitationClick: (citation: Citation) => void;
}) {
  const t = useTranslations("Search");
  const [expanded, setExpanded] = React.useState(true);
  const [canExpand, setCanExpand] = React.useState(false);
  const [expandedScrollable, setExpandedScrollable] = React.useState(false);
  const [following, setFollowing] = React.useState(true);
  const [availableHeight, setAvailableHeight] = React.useState<number | null>(null);
  const cardRef = React.useRef<HTMLDivElement>(null);
  const viewportRef = React.useRef<HTMLDivElement>(null);
  const contentRef = React.useRef<HTMLDivElement>(null);
  const followOutputRef = React.useRef(true);
  const wasStreamingRef = React.useRef(streaming);
  const contentId = React.useId();
  const hintId = `${contentId}-hint`;
  const accessiblePreviewExcerpt = React.useMemo(() => {
    // The preview is only exposed while collapsed. Avoid repeatedly cleaning
    // the growing Markdown buffer for every streamed token while expanded.
    if (expanded) return "";
    const accessiblePreview = summary
      .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/[`*_>#~|]+/g, " ")
      .replace(/\s+/g, " ")
      .trim();
    return accessiblePreview.length > 240
      ? `${accessiblePreview.slice(0, 240).trimEnd()}…`
      : accessiblePreview;
  }, [expanded, summary]);

  const updateFollowing = React.useCallback((next: boolean) => {
    followOutputRef.current = next;
    setFollowing((current) => (current === next ? current : next));
  }, []);

  const scrollToLatest = React.useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    updateFollowing(true);
    viewport.scrollTop = viewport.scrollHeight;
  }, [updateFollowing]);

  React.useLayoutEffect(() => {
    if (streaming && !wasStreamingRef.current) {
      setExpanded(true);
      updateFollowing(true);
    }
    wasStreamingRef.current = streaming;
  }, [streaming, updateFollowing]);

  const collapsedLimit = compact ? 72 : 96;

  const measureAvailableHeight = React.useCallback(() => {
    const container = containerRef.current;
    const card = cardRef.current;
    if (!container || !card) return;
    const containerRect = container.getBoundingClientRect();
    const cardRect = card.getBoundingClientRect();
    const topInScrollContent = cardRect.top - containerRect.top + container.scrollTop;
    const bottomPadding = Number.parseFloat(window.getComputedStyle(container).paddingBottom) || 0;
    const next = Math.max(
      80,
      Math.floor(container.clientHeight - topInScrollContent - bottomPadding),
    );
    setAvailableHeight((current) => (current === next ? current : next));
  }, [containerRef]);

  React.useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    measureAvailableHeight();
    const observer = new ResizeObserver(measureAvailableHeight);
    observer.observe(container);
    window.addEventListener("resize", measureAvailableHeight);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measureAvailableHeight);
    };
  }, [containerRef, measureAvailableHeight]);

  React.useLayoutEffect(() => {
    // The query form can wrap without resizing the outer panel. Re-measure
    // when a new streamed answer is mounted so the cap still ends exactly at
    // the container boundary.
    measureAvailableHeight();
  }, [compact, measureAvailableHeight, streaming]);

  React.useLayoutEffect(() => {
    const viewport = viewportRef.current;
    const content = contentRef.current;
    if (!viewport || !content) return;

    const update = () => {
      const contentHeight = content.scrollHeight;
      const nextCanExpand = contentHeight > collapsedLimit + 1;
      const nextScrollable = expanded && viewport.scrollHeight > viewport.clientHeight + 1;
      setCanExpand((current) => (current === nextCanExpand ? current : nextCanExpand));
      setExpandedScrollable((current) => (current === nextScrollable ? current : nextScrollable));
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(viewport);
    observer.observe(content);
    return () => observer.disconnect();
  }, [collapsedLimit, compact, expanded]);

  React.useLayoutEffect(() => {
    if (!expanded || !followOutputRef.current) return;
    const frame = requestAnimationFrame(() => {
      // The user may scroll up after this frame was queued. Re-check here so
      // programmatic following never wins that race and pulls them back down.
      if (followOutputRef.current) scrollToLatest();
    });
    return () => cancelAnimationFrame(frame);
  }, [expanded, scrollToLatest, summary]);

  const collapsed = !expanded;
  const expandedScrollRegion = expanded && expandedScrollable;

  return (
    <div
      ref={cardRef}
      className={cn(
        "flex shrink-0 flex-col overflow-hidden rounded-lg border border-amber-500/15 bg-amber-500/[0.04]",
        compact ? "px-3 py-2.5" : "px-4 py-3",
      )}
      style={expanded && availableHeight !== null
        ? { maxHeight: `${availableHeight}px` }
        : undefined}
    >
      <div className="flex items-center justify-between gap-3">
        <div
          className="flex min-w-0 items-center gap-1.5 text-xs font-medium text-amber-700 dark:text-amber-300"
          role={streaming ? "status" : undefined}
          aria-live={streaming ? "polite" : undefined}
        >
          <Sparkles className="size-3.5 shrink-0" />
          <span className="truncate">
            {streaming ? t("generatingAnswer") : t("evidenceAnswer")}
          </span>
          {streaming && <Spinner className="size-3 text-amber-600/70 dark:text-amber-300/70" />}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {streaming && expanded && !following && (
            <button
              type="button"
              onClick={scrollToLatest}
              className="inline-flex h-7 items-center rounded-md px-2 text-xs text-muted-foreground outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
            >
              {t("followOutput")}
            </button>
          )}
          {summary && (canExpand || streaming || !expanded) && (
            <button
              type="button"
              onClick={() => {
                const next = !expanded;
                setExpanded(next);
                updateFollowing(next);
              }}
              aria-expanded={expanded}
              aria-controls={contentId}
              aria-describedby={collapsed ? hintId : undefined}
              className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-xs text-muted-foreground outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
            >
              {expanded ? t("collapse") : t("expandAnswer")}
              {expanded ? (
                <ChevronUp className="size-3.5" />
              ) : (
                <ChevronDown className="size-3.5" />
              )}
            </button>
          )}
        </div>
      </div>
      <div
        ref={viewportRef}
        id={contentId}
        inert={collapsed || undefined}
        aria-hidden={collapsed || undefined}
        aria-label={expandedScrollRegion ? t("fullAnswerScrollable") : undefined}
        role={expandedScrollRegion ? "region" : undefined}
        tabIndex={expandedScrollRegion ? 0 : undefined}
        onScroll={(event) => {
          if (!expanded) return;
          const viewport = event.currentTarget;
          const distance = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight;
          updateFollowing(distance <= 32);
        }}
        className={cn(
          "min-h-0 text-sm leading-6 text-foreground/80",
          summary && "mt-1.5",
          !expanded && "overflow-hidden",
          expanded && "shrink overflow-y-auto overscroll-contain pr-1 outline-none [scrollbar-gutter:stable] focus-visible:ring-2 focus-visible:ring-ring",
        )}
        style={{
          maxHeight: collapsed ? `${collapsedLimit}px` : undefined,
          WebkitMaskImage: collapsed
            ? "linear-gradient(to bottom, black 0, black calc(100% - 2rem), transparent 100%)"
            : undefined,
          maskImage: collapsed
            ? "linear-gradient(to bottom, black 0, black calc(100% - 2rem), transparent 100%)"
            : undefined,
        }}
      >
        <div ref={contentRef}>
          {summary ? (
            <MarkdownContent
              content={summary}
              citations={citations}
              onCitationClick={onCitationClick}
              streaming={streaming}
            />
          ) : null}
        </div>
      </div>
      {collapsed && (
        <span id={hintId} className="sr-only">
          {t("collapsedPreview", { preview: accessiblePreviewExcerpt })}
        </span>
      )}
    </div>
  );
}
