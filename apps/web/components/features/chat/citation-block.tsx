"use client";

import * as React from "react";
import { ArrowUpRight, BookOpenText, ChevronDown, Globe2, Library } from "lucide-react";
import { useTranslations } from "next-intl";

import type { Citation } from "@/lib/types";
import { citationCopy } from "@/lib/citation-presentation";
import { cn } from "@/lib/utils";
import { useDetailPanel } from "@/components/features/detail-panel";

function safeExternalUrl(citation: Citation): string | null {
  const raw = citation.url;
  if (typeof raw !== "string" || !raw || raw !== raw.trim() || /\s/.test(raw)) return null;
  try {
    const parsed = new URL(raw);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    if (!parsed.hostname || parsed.username || parsed.password) return null;
    return raw;
  } catch {
    return null;
  }
}

function CitationNumber({ children }: { children: React.ReactNode }) {
  return (
    <span className="grid size-5 shrink-0 place-items-center rounded-full bg-muted font-mono text-[10px] font-semibold text-muted-foreground">
      {children}
    </span>
  );
}

function CitationBody({ body, title }: { body: string; title: string }) {
  const t = useTranslations("Citations");
  const [expanded, setExpanded] = React.useState(false);
  const [overflowing, setOverflowing] = React.useState(false);
  const bodyRef = React.useRef<HTMLParagraphElement>(null);
  const bodyId = React.useId();

  const measureOverflow = React.useCallback(() => {
    const element = bodyRef.current;
    if (!element || expanded) return;
    setOverflowing(element.scrollHeight > element.clientHeight + 1);
  }, [expanded]);

  React.useLayoutEffect(() => {
    measureOverflow();
    const element = bodyRef.current;
    if (!element || expanded) return;

    const observer = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(measureOverflow);
    observer?.observe(element);
    window.addEventListener("resize", measureOverflow);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measureOverflow);
    };
  }, [body, expanded, measureOverflow]);

  return (
    <>
      <p
        ref={bodyRef}
        id={bodyId}
        className={cn(
          "mt-1 whitespace-pre-wrap break-words text-[11px] leading-[1.65] text-muted-foreground",
          !expanded && "line-clamp-3",
        )}
      >
        {body}
      </p>
      {overflowing && (
        <div className="mt-1.5 flex justify-end">
          <button
            type="button"
            onClick={() => setExpanded((value) => !value)}
            aria-expanded={expanded}
            aria-controls={bodyId}
            aria-label={expanded
              ? t("collapseBody", { title })
              : t("expandBody", { title })}
            title={expanded ? t("collapse") : t("expand")}
            className="inline-flex h-5 shrink-0 items-center gap-0.5 whitespace-nowrap rounded px-1 text-[10px] font-normal text-muted-foreground/55 outline-none transition-colors hover:bg-muted/40 hover:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring"
          >
            {expanded ? t("collapse") : t("expand")}
            <ChevronDown
              className={cn("size-3 transition-transform", expanded && "rotate-180")}
              aria-hidden="true"
            />
          </button>
        </div>
      )}
    </>
  );
}

function CitationCard({
  citation,
  fallbackIndex,
  onOpen,
  url,
}: {
  citation: Citation;
  fallbackIndex: number;
  onOpen?: () => void;
  url?: string;
}) {
  const t = useTranslations("Citations");
  const copy = citationCopy(citation, fallbackIndex);
  const sourceActionClass =
    "inline-flex h-6 shrink-0 items-center gap-1 whitespace-nowrap rounded-md bg-muted/55 px-1.5 text-[10px] font-medium text-muted-foreground/85 outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-default disabled:opacity-40";

  return (
    <article className="px-2.5 py-2.5 transition-colors hover:bg-background/55">
      <div className="flex items-start gap-2.5">
        <CitationNumber>{citation.n || fallbackIndex}</CitationNumber>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-start gap-2">
            <h4 className="min-w-0 flex-1 line-clamp-2 text-xs font-semibold leading-5 text-foreground">
              {copy.title}
            </h4>
            {url ? (
              <a
                href={url}
                target="_blank"
                rel="noopener noreferrer"
                aria-label={t("visitExternal", { title: copy.title })}
                title={t("openOriginal")}
                className={sourceActionClass}
              >
                <ArrowUpRight className="size-3" aria-hidden="true" />
                {t("original")}
              </a>
            ) : (
              <button
                type="button"
                onClick={onOpen}
                disabled={!onOpen}
                aria-label={t("viewInternal", { title: copy.title })}
                title={onOpen ? t("viewOriginal") : t("unavailable")}
                className={sourceActionClass}
              >
                <ArrowUpRight className="size-3" aria-hidden="true" />
                {t("original")}
              </button>
            )}
          </div>

          {copy.body && (
            <CitationBody key={copy.body} body={copy.body} title={copy.title} />
          )}
        </div>
      </div>
    </article>
  );
}

/**
 * A single source center for knowledge-base and web references. Legacy
 * citations without `kind` remain internal so persisted conversations keep
 * their existing traceability.
 */
export const CitationBlock = React.memo(function CitationBlock({
  citations,
  onCitationClick,
}: {
  citations: Citation[];
  onCitationClick?: (citation: Citation) => void;
}) {
  const t = useTranslations("Citations");
  const panel = useDetailPanel();
  const [expanded, setExpanded] = React.useState(false);
  const contentId = React.useId();
  const { internal, external } = React.useMemo(() => {
    const internalItems: Citation[] = [];
    const externalItems: Array<{ citation: Citation; url: string }> = [];
    const seenInternal = new Set<string>();
    const seenExternal = new Set<string>();

    for (const citation of citations ?? []) {
      if (citation.kind === "external") {
        const url = safeExternalUrl(citation);
        if (!url || seenExternal.has(url)) continue;
        seenExternal.add(url);
        externalItems.push({ citation, url });
        continue;
      }
      const key = `${citation.n}:${citation.source_id ?? ""}:${citation.chunk_id ?? ""}`;
      if (seenInternal.has(key)) continue;
      seenInternal.add(key);
      internalItems.push(citation);
    }
    return { internal: internalItems, external: externalItems };
  }, [citations]);

  const referenceCount = internal.length + external.length;
  if (!referenceCount) return null;

  const openInternal = (citation: Citation) => {
    if (!citation.chunk_id || !citation.source_id) return;
    if (onCitationClick) {
      onCitationClick(citation);
      return;
    }
    panel.open({
      kind: "chunk",
      sourceId: citation.source_id,
      chunkId: citation.chunk_id,
      heading: citation.heading || undefined,
      sourceName: citation.source_name ?? undefined,
    });
  };

  return (
    <>
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
        aria-controls={contentId}
        aria-label={t("toggleAria", {
          action: expanded ? t("collapse") : t("expand"),
          total: referenceCount,
          internal: internal.length,
          external: external.length,
        })}
        title={expanded ? t("collapseReferences") : t("expandReferences")}
        className={cn(
          "inline-flex h-6 shrink-0 items-center gap-1 rounded-md px-2 text-[11px] font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring",
          expanded
            ? "bg-muted text-foreground"
            : "bg-muted/70 text-muted-foreground hover:bg-muted hover:text-foreground",
        )}
      >
        <BookOpenText className="size-3" aria-hidden="true" />
        <span>{t("referenceCount", { count: referenceCount })}</span>
        <ChevronDown
          className={cn("size-3 opacity-60 transition-transform", expanded && "rotate-180")}
          aria-hidden="true"
        />
      </button>

      {expanded && (
        <section
          id={contentId}
          aria-label={t("sources")}
          className="order-last mt-1.5 w-full basis-full overflow-hidden rounded-xl border border-border/70 bg-muted/20 animate-in fade-in slide-in-from-top-1 duration-150"
        >
          <div className="flex items-center justify-between gap-3 px-3 py-2.5">
            <span className="text-xs font-semibold text-foreground">{t("sources")}</span>
            <span className="shrink-0 rounded-full bg-background px-1.5 py-0.5 font-mono text-[10px] font-medium tabular-nums text-muted-foreground">
              {referenceCount}
            </span>
          </div>

          {internal.length > 0 && (
            <div className="border-t border-border/60 px-1.5 py-1.5">
              <div className="flex items-center justify-between px-1.5 py-1.5">
                <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-foreground">
                  <Library className="size-3.5" aria-hidden="true" />
                  {t("internal")}
                </span>
                <span className="font-mono text-[10px] font-normal tabular-nums text-muted-foreground">
                  {internal.length}
                </span>
              </div>
              <div className="divide-y divide-border/50">
                {internal.map((citation, index) => {
                  const traceable = Boolean(citation.chunk_id && citation.source_id);
                  return (
                    <CitationCard
                      key={`internal:${citation.n}:${citation.source_id ?? ""}:${citation.chunk_id ?? ""}`}
                      citation={citation}
                      fallbackIndex={index + 1}
                      onOpen={traceable ? () => openInternal(citation) : undefined}
                    />
                  );
                })}
              </div>
            </div>
          )}

          {external.length > 0 && (
            <div className="border-t border-border/60 px-1.5 py-1.5">
              <div className="flex items-center justify-between px-1.5 py-1.5">
                <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-foreground">
                  <Globe2 className="size-3.5" aria-hidden="true" />
                  {t("external")}
                </span>
                <span className="font-mono text-[10px] font-normal tabular-nums text-muted-foreground">
                  {external.length}
                </span>
              </div>
              <div className="divide-y divide-border/50">
                {external.map(({ citation, url }, index) => (
                  <CitationCard
                    key={`external:${url}`}
                    citation={citation}
                    fallbackIndex={index + 1}
                    url={url}
                  />
                ))}
              </div>
            </div>
          )}
        </section>
      )}
    </>
  );
});
