"use client";

import * as React from "react";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { usePathname } from "next/navigation";
import {
  ArrowUpRight,
  ChevronsLeft,
  ChevronsRight,
  Code2,
  Download,
  Eye,
  FileText,
  PackageOpen,
  X,
} from "lucide-react";

import { api, ApiError } from "@/lib/api";
import { getToken } from "@/lib/auth";
import type { CitationEventRef, Doc } from "@/lib/types";
import { formatBytes, formatDate, formatTokenCount, relativeTime } from "@/lib/format";
import { cleanCitationText, stripCitationTransportTokens } from "@/lib/citation-presentation";
import { cn } from "@/lib/utils";
import { ChunkedMarkdown, ChunkedRawText } from "@/components/features/markdown-content";
import { useApp } from "@/components/features/app-shell";
import { DocStatusBadge } from "@/components/features/status-badge";
import { Button } from "@/components/ui/button";
import type { ImperativePanelHandle } from "react-resizable-panels";

import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

/** 详情面板目标：引用/搜索结果的原文分块，或知识库文档（含原始文件预览）。 */
export type DetailTarget =
  | {
      kind: "chunk";
      sourceId: string;
      chunkId: string;
      heading?: string;
      sourceName?: string;
      eventRefs?: CitationEventRef[];
    }
  | { kind: "document"; sourceId: string; documentId: string; title?: string };

interface PanelCtx {
  target: DetailTarget | null;
  maximized: boolean;
  open: (target: DetailTarget) => void;
  close: () => void;
  toggleMaximize: () => void;
  /** 详情 ResizablePanel 的命令句柄（放大/还原经官方 resize API） */
  panelRef: React.RefObject<ImperativePanelHandle | null>;
}

const Ctx = React.createContext<PanelCtx>({
  target: null,
  maximized: false,
  open: () => {},
  close: () => {},
  toggleMaximize: () => {},
  panelRef: { current: null },
});

const DEFAULT_PANEL_SIZE = 34;

function sameTarget(a: DetailTarget, b: DetailTarget): boolean {
  if (a.kind !== b.kind) return false;
  if (a.kind === "document" && b.kind === "document") {
    return a.sourceId === b.sourceId && a.documentId === b.documentId;
  }
  if (a.kind === "chunk" && b.kind === "chunk") {
    return a.sourceId === b.sourceId && a.chunkId === b.chunkId;
  }
  return false;
}

export function useDetailPanel() {
  return React.useContext(Ctx);
}

export function DetailPanelProvider({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [target, setTarget] = React.useState<DetailTarget | null>(null);
  const [maximized, setMaximized] = React.useState(false);

  const panelRef = React.useRef<ImperativePanelHandle | null>(null);
  const resetPanelSize = React.useCallback(() => {
    panelRef.current?.resize(DEFAULT_PANEL_SIZE);
  }, []);
  // 同一目标二次点击 = 关闭（toggle）。用户预期：点开某文档后再点同一个文档
  // 应该收起面板；否则会以为"点没反应"。不同目标点击 = 切换目标，就地更新。
  const open = React.useCallback((t: DetailTarget) => {
    setTarget((prev) => {
      if (prev && sameTarget(prev, t)) {
        resetPanelSize();
        setMaximized(false);
        return null;
      }
      return t;
    });
  }, [resetPanelSize]);
  const close = React.useCallback(() => {
    resetPanelSize();
    setTarget(null);
    setMaximized(false);
  }, [resetPanelSize]);
  const toggleMaximize = React.useCallback(() => {
    setMaximized((m) => {
      const next = !m;
      const panel = panelRef.current;
      if (panel) {
        if (next) {
          panel.resize(100);
        } else {
          panel.resize(DEFAULT_PANEL_SIZE);
        }
      }
      return next;
    });
  }, []);

  // 切换主导航（/chat ↔ /search ↔ /knowledge…）时收起面板
  const section = pathname.split("/")[1];
  const prevSection = React.useRef(section);
  React.useEffect(() => {
    if (prevSection.current !== section) {
      prevSection.current = section;
      close();
    }
  }, [section, close]);

  return (
    <Ctx.Provider value={{ target, maximized, open, close, toggleMaximize, panelRef }}>
      {children}
    </Ctx.Provider>
  );
}

/** 主内容区：面板放大时隐藏（只留左侧菜单 + 面板）。 */
export function DetailPanelMain({ children }: { children: React.ReactNode }) {
  return <div className="h-full min-w-0 overflow-y-auto overscroll-contain">{children}</div>;
}

// ── 内容视图 ─────────────────────────────────────────────────────────

/** 单按钮切换 Markdown 预览与原始内容，图标表示当前模式。 */
function RenderModeToggle({
  mode,
  onChange,
}: {
  mode: "md" | "raw";
  onChange: (m: "md" | "raw") => void;
}) {
  const t = useTranslations("DetailPanel");
  const isPreview = mode === "md";
  const label = isPreview ? t("renderMode.raw") : t("renderMode.preview");

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="icon"
          className="size-8 bg-background"
          aria-label={label}
          onClick={() => onChange(isPreview ? "raw" : "md")}
        >
          {isPreview ? <Eye /> : <Code2 />}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">{label}</TooltipContent>
    </Tooltip>
  );
}

function TextBody({ text, mode }: { text: string; mode: "md" | "raw" }) {
  // 纵向滚动由外层 DetailPanelOutlet/DetailPanelSheet 承担，避免"面板滚动条 + TextBody
  // 滚动条"双层套嵌导致的宽度抖动。但内部宽内容（大代码块/表格/长 URL）如果没有单独的
  // 横向出口，会顶穿容器；外层 overflow-x-hidden 会把它们裁掉表现为"右半边被截断"。
  // 这里仅开启 overflow-x-auto —— 只作用于宽元素的横向溢出，与外层纵向滚动互不干扰。
  // 外壳样式两种模式共用；md / raw 只换内层渲染器（ChunkedMarkdown / ChunkedRawText）。
  return (
    <div className="w-full min-w-0 max-w-full overflow-x-auto rounded-md border bg-muted/30 p-4">
      {mode === "md" ? (
        <ChunkedMarkdown content={text} />
      ) : (
        <ChunkedRawText content={text} />
      )}
    </div>
  );
}

function ChunkView({
  target,
}: {
  target: Extract<DetailTarget, { kind: "chunk" }>;
}) {
  const t = useTranslations("DetailPanel");
  const tRef = React.useRef(t);
  tRef.current = t;
  const locale = useLocale();
  const { timezone } = useApp();
  const [content, setContent] = React.useState<string | null>(null);
  const [meta, setMeta] = React.useState<{ heading: string; sourceName: string } | null>(null);
  const [mode, setMode] = React.useState<"md" | "raw">("md");
  const [error, setError] = React.useState("");
  const citationEvent = React.useMemo(
    () =>
      (target.eventRefs ?? []).find((event) => cleanCitationText(event.title)),
    [target.eventRefs],
  );
  const eventTitle = cleanCitationText(citationEvent?.title);
  const eventBody = cleanCitationText(citationEvent?.content);
  const eventCategory = cleanCitationText(citationEvent?.category);
  const eventTime = citationEvent?.start_time
    ? formatDate(citationEvent.start_time, timezone, { dateStyle: "medium" }, locale)
    : "";

  React.useEffect(() => {
    let alive = true;
    setContent(null);
    setError("");
    api
      .getChunk(target.sourceId, target.chunkId)
      .then((c) => {
        if (!alive) return;
        setContent(c.content);
        setMeta({ heading: c.heading || target.heading || tRef.current("chunk.fallbackHeading"), sourceName: c.source_name });
      })
      .catch((e) => alive && setError(e instanceof ApiError ? e.message : tRef.current("chunk.loadFailed")));
    return () => {
      alive = false;
    };
    // 只依赖真正会变的字段，target 本身每次点击都是新对象引用；t 是每次渲染新引用（tRef 兜底）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target.sourceId, target.chunkId, target.heading]);

  if (error) {
    return <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</p>;
  }
  if (content === null) {
    return (
      <div className="flex flex-col gap-2">
        <Skeleton className="h-5 w-2/3" />
        <Skeleton className="h-32" />
      </div>
    );
  }
  if (citationEvent && eventTitle) {
    const evidenceHeading = meta?.heading || target.heading || t("chunk.fallbackHeading");
    const evidenceSource = meta?.sourceName ?? target.sourceName ?? t("chunk.source");
    return (
      <div className="flex min-w-0 flex-col gap-5">
        <section className="rounded-lg border border-amber-500/20 bg-amber-500/[0.07] p-3.5 shadow-sm dark:border-amber-300/20 dark:bg-amber-300/[0.07]">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            <Link
              href={`/knowledge/${target.sourceId}`}
              className="min-w-0 truncate hover:text-foreground"
            >
              {evidenceSource}
            </Link>
            {eventCategory && (
              <span className="rounded bg-background/70 px-1.5 py-0.5 text-[11px] text-amber-700 dark:text-amber-200">
                {eventCategory}
              </span>
            )}
            {eventTime && <span>{eventTime}</span>}
          </div>
          <h3 className="mt-2 font-display text-base font-medium leading-6">
            {eventTitle}
          </h3>
          {eventBody && (
            <div className="mt-3">
              <p className="text-[11px] font-medium tracking-wide text-muted-foreground/75">
                {t("chunk.eventDetail")}
              </p>
              <p className="mt-1 whitespace-pre-wrap break-words text-sm leading-6 text-foreground/75">
                {eventBody}
              </p>
            </div>
          )}
        </section>

        <section className="rounded-lg border bg-background/75 p-3.5 shadow-sm">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <h4 className="text-xs font-medium text-muted-foreground">
                {t("chunk.sourceEvidence")}
              </h4>
              {evidenceHeading && (
                <p className="mt-1 truncate text-sm font-medium text-foreground">
                  {evidenceHeading}
                </p>
              )}
            </div>
            <RenderModeToggle mode={mode} onChange={setMode} />
          </div>
          <div className="mt-3">
            <TextBody text={stripCitationTransportTokens(content)} mode={mode} />
          </div>
        </section>
      </div>
    );
  }
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="font-display text-base font-medium">{meta?.heading}</h3>
          <Link
            href={`/knowledge/${target.sourceId}`}
            className="mt-0.5 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            {t("chunk.from", { source: meta?.sourceName ?? target.sourceName ?? t("chunk.source") })}
            <ArrowUpRight className="size-3" />
          </Link>
        </div>
        <RenderModeToggle mode={mode} onChange={setMode} />
      </div>
      <TextBody text={content} mode={mode} />
    </div>
  );
}

function OriginalDocumentPreview({
  doc,
  onShowParsed,
}: {
  doc: Doc;
  onShowParsed: () => void;
}) {
  const locale = useLocale();
  const t = useTranslations("DetailPanel");
  const tRef = React.useRef(t);
  tRef.current = t;
  const [state, setState] = React.useState<
    | { phase: "loading" }
    | { phase: "blob"; url: string; kind: "pdf" | "image" }
    | { phase: "text"; text: string }
    | { phase: "none" }
    | { phase: "error"; message: string }
  >({ phase: "loading" });

  const [textMode, setTextMode] = React.useState<"md" | "raw">("md");
  const fileUrl = api.documentFileUrl(doc.source_id, doc.id);
  const previewUrl = api.documentPreviewUrl(doc.source_id, doc.id);

  React.useEffect(() => {
    const tr = tRef.current;
    let alive = true;
    let objectUrl: string | null = null;
    setState({ phase: "loading" });
    if (doc.original_file_available === false) {
      setState({ phase: "none" });
      return () => {
        alive = false;
      };
    }
    (async () => {
      try {
        const res = await fetch(previewUrl, {
          headers: {
            Authorization: `Bearer ${getToken() ?? ""}`,
            "Accept-Language": locale,
          },
        });
        if (!res.ok) throw new Error(tr("original.unavailable", { status: res.status }));
        const ct = (res.headers.get("content-type") || doc.content_type || "").toLowerCase();
        if (ct.includes("pdf")) {
          objectUrl = URL.createObjectURL(await res.blob());
          if (alive) setState({ phase: "blob", url: objectUrl, kind: "pdf" });
        } else if (ct.startsWith("image/")) {
          objectUrl = URL.createObjectURL(await res.blob());
          if (alive) setState({ phase: "blob", url: objectUrl, kind: "image" });
        } else if (
          ct.startsWith("text/") ||
          ct.includes("markdown") ||
          ct.includes("json") ||
          ct.includes("csv")
        ) {
          const text = await res.text();
          if (alive) setState({ phase: "text", text: text.slice(0, 200_000) });
        } else {
          if (alive) setState({ phase: "none" });
        }
      } catch (e) {
        if (alive) setState({ phase: "error", message: e instanceof Error ? e.message : tr("original.loadFailed") });
      }
    })();
    return () => {
      alive = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [doc.content_type, doc.id, doc.original_file_available, doc.source_id, locale, previewUrl]);

  if (doc.original_file_available === false) {
    return (
      <div className="grid min-h-56 flex-1 place-items-center rounded-xl border border-dashed bg-muted/20 p-5">
        <div className="flex max-w-md flex-col items-center text-center">
          <div className="relative mb-4 grid size-12 place-items-center rounded-xl border bg-background shadow-sm">
            <PackageOpen className="size-5 text-muted-foreground" />
            <FileText className="absolute -bottom-1.5 -right-1.5 size-5 rounded-md border bg-background p-0.5 text-primary" />
          </div>
          <p className="text-sm font-medium text-foreground">{t("original.octxTitle")}</p>
          <p className="mt-1.5 text-sm leading-6 text-muted-foreground">
            {t("original.octxDescription")}
          </p>
          <Button type="button" variant="outline" size="sm" className="mt-4 gap-1.5" onClick={onShowParsed}>
            <FileText className="size-3.5" />
            {t("original.viewParsed")}
          </Button>
        </div>
      </div>
    );
  }

  async function download() {
    try {
      const res = await fetch(fileUrl, {
        headers: {
          Authorization: `Bearer ${getToken() ?? ""}`,
          "Accept-Language": locale,
        },
      });
      if (!res.ok) throw new Error(t("original.downloadFailed"));
      const url = URL.createObjectURL(await res.blob());
      const a = document.createElement("a");
      a.href = url;
      a.download = doc.filename;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      /* 提示由浏览器兜底 */
    }
  }

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-muted-foreground">{t("original.title")}</span>
        <span className="flex items-center gap-1.5">
          {state.phase === "text" && <RenderModeToggle mode={textMode} onChange={setTextMode} />}
          <Button variant="ghost" size="sm" className="h-7 gap-1.5 px-2 text-xs" onClick={download}>
            <Download />
            {t("original.download")}
          </Button>
        </span>
      </div>
      {state.phase === "loading" && (
        <div className="grid flex-1 place-items-center rounded-md border">
          <Spinner />
        </div>
      )}
      {state.phase === "error" && (
        <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {state.message}
        </p>
      )}
      {state.phase === "none" && (
        <p className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">
          {t("original.unsupported")}
        </p>
      )}
      {state.phase === "text" && (
        <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2 overflow-auto">
          <TextBody text={state.text} mode={textMode} />
        </div>
      )}
      {state.phase === "blob" && state.kind === "pdf" && (
        <iframe title={doc.filename} src={state.url} className="min-h-0 flex-1 rounded-md border" />
      )}
      {state.phase === "blob" && state.kind === "image" && (
        <div className="min-h-0 flex-1 overflow-auto rounded-md border bg-muted/30 p-2">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={state.url} alt={doc.filename} className="mx-auto max-w-full" />
        </div>
      )}
    </div>
  );
}

type ParsedPreviewState =
  | { phase: "loading" }
  | { phase: "text"; text: string; truncated: boolean }
  | { phase: "none"; message: string }
  | { phase: "error"; message: string };

function ParsedDocumentPreview({ doc }: { doc: Doc }) {
  const locale = useLocale();
  const t = useTranslations("DetailPanel");
  const tRef = React.useRef(t);
  tRef.current = t;
  const [state, setState] = React.useState<ParsedPreviewState>({ phase: "loading" });
  const [textMode, setTextMode] = React.useState<"md" | "raw">("md");
  const parsedUrl = api.documentParsedUrl(doc.source_id, doc.id);

  React.useEffect(() => {
    const tr = tRef.current;
    if (doc.status !== "ready") {
      setState({
        phase: "none",
        message:
          doc.status === "failed"
            ? doc.error || tr("parsed.failed")
            : tr("parsed.processing"),
      });
      return;
    }

    let alive = true;
    const controller = new AbortController();
    setState({ phase: "loading" });
    fetch(parsedUrl, {
      headers: {
        Authorization: `Bearer ${getToken() ?? ""}`,
        "Accept-Language": locale,
      },
      signal: controller.signal,
    })
      .then(async (res) => {
        if (res.status === 404) {
          if (alive) {
            setState({ phase: "none", message: tr("parsed.notFound") });
          }
          return;
        }
        if (res.status === 409) {
          if (alive) setState({ phase: "none", message: tr("parsed.notReady") });
          return;
        }
        if (!res.ok) throw new Error(tr("parsed.unavailable", { status: res.status }));
        const text = await res.text();
        if (!alive) return;
        if (!text.trim()) {
          setState({ phase: "none", message: tr("parsed.empty") });
          return;
        }
        const limit = 500_000;
        setState({ phase: "text", text: text.slice(0, limit), truncated: text.length > limit });
      })
      .catch((error) => {
        if (!alive || controller.signal.aborted) return;
        setState({
          phase: "error",
          message: error instanceof Error ? error.message : tr("parsed.loadFailed"),
        });
      });
    return () => {
      alive = false;
      controller.abort();
    };
  }, [doc.error, doc.status, locale, parsedUrl]);

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-muted-foreground">{t("parsed.title")}</span>
        {state.phase === "text" && <RenderModeToggle mode={textMode} onChange={setTextMode} />}
      </div>
      {state.phase === "loading" && (
        <div className="grid flex-1 place-items-center rounded-md border">
          <Spinner />
        </div>
      )}
      {state.phase === "error" && (
        <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {state.message}
        </p>
      )}
      {state.phase === "none" && (
        <p className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">
          {state.message}
        </p>
      )}
      {state.phase === "text" && (
        <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
          {state.truncated && (
            <p className="text-xs text-muted-foreground">{t("parsed.truncated")}</p>
          )}
          <TextBody text={state.text} mode={textMode} />
        </div>
      )}
    </div>
  );
}

export function DocumentPreview({ doc }: { doc: Doc }) {
  const t = useTranslations("DetailPanel");
  const [previewMode, setPreviewMode] = React.useState<"parsed" | "original">(
    doc.status === "ready" ? "parsed" : "original",
  );

  return (
    <Tabs
      value={previewMode}
      onValueChange={(value) => setPreviewMode(value as "parsed" | "original")}
      className="flex min-h-0 min-w-0 flex-1 flex-col"
    >
      <TabsList className="grid w-full shrink-0 grid-cols-2">
        <TabsTrigger value="parsed">{t("tabs.parsed")}</TabsTrigger>
        <TabsTrigger value="original">{t("tabs.original")}</TabsTrigger>
      </TabsList>
      {/* forceMount 让两个 tab 内容都保持挂载，切换只是 CSS 显隐；避免解析 tab 每次
          切换都重新 fetch/重新分块渲染导致的闪屏抖动。TabsContent 在非激活时会带
          `hidden` 属性（等同 display:none），不占布局空间。
          min-w-0：flex 子项默认 min-width:auto 会被内容撑破容器，导致侧栏「右半边被截断」。 */}
      <TabsContent
        value="parsed"
        forceMount
        className="mt-2 min-h-0 min-w-0 flex-1 data-[state=active]:flex data-[state=active]:flex-col data-[state=inactive]:hidden"
      >
        <ParsedDocumentPreview doc={doc} />
      </TabsContent>
      <TabsContent
        value="original"
        forceMount
        className="mt-2 min-h-0 min-w-0 flex-1 data-[state=active]:flex data-[state=active]:flex-col data-[state=inactive]:hidden"
      >
        <OriginalDocumentPreview doc={doc} onShowParsed={() => setPreviewMode("parsed")} />
      </TabsContent>
    </Tabs>
  );
}

export function DocumentDetailContent({
  sourceId,
  documentId,
  compact = false,
}: {
  sourceId: string;
  documentId: string;
  compact?: boolean;
}) {
  const locale = useLocale();
  const t = useTranslations("DetailPanel");
  const tRef = React.useRef(t);
  tRef.current = t;
  const [doc, setDoc] = React.useState<Doc | null>(null);
  const [error, setError] = React.useState("");
  const { timezone } = useApp();
  // 切换选中文档时不清空旧 doc，避免整块塌陷到 Skeleton 再撑回来造成的侧栏抖动。
  // 只有首次加载（无历史 doc）才展示 Skeleton；切换视为"刷新"，旧内容原地保留，
  // 直到新数据到位再整体替换。DocumentPreview 用 key={doc.id} 强制子树重置。
  // dep 只依赖 id —— t 是 next-intl 每次渲染的新引用，若加入 dep 会让同一文档被
  // 反复重新 fetch，触发下游 remount 表现为"每次点击都闪一下"。
  React.useEffect(() => {
    let alive = true;
    setError("");
    api
      .getDocument(sourceId, documentId)
      .then((d) => alive && setDoc(d))
      .catch((e) => alive && setError(e instanceof ApiError ? e.message : tRef.current("document.loadFailed")));
    return () => {
      alive = false;
    };
  }, [documentId, sourceId]);

  if (error) {
    return <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</p>;
  }
  if (!doc) {
    return (
      <div className="flex flex-col gap-2">
        <Skeleton className="h-5 w-2/3" />
        <Skeleton className="h-64" />
      </div>
    );
  }
  return (
    <TooltipProvider delayDuration={300}>
      <div className={cn("flex min-h-0 min-w-0 flex-1 flex-col", compact ? "gap-3" : "gap-4")}>
        <div className="flex flex-col gap-2">
          <h3
            className={cn(
              "break-all font-display font-medium",
              compact ? "text-sm" : "text-base",
            )}
          >
            {doc.filename}
          </h3>
          <div
            className={cn(
              "flex flex-wrap items-center gap-2 text-muted-foreground",
              compact ? "text-[11px]" : "text-xs",
            )}
          >
            <DocStatusBadge status={doc.status} />
            <span>
              {Math.min(100, Math.max(0, Math.round(doc.progress)))}% ·{" "}
              {t("document.tokens", { count: formatTokenCount(doc.token_usage, locale) })}
            </span>
            <span>·</span>
            <span>{formatBytes(doc.size_bytes, locale)}</span>
            <span>·</span>
            <span>{t("document.chunks", { count: doc.chunk_count })}</span>
            <span>·</span>
            <span>{t("document.events", { count: doc.event_count })}</span>
            <span>·</span>
            <span>{relativeTime(doc.created_at, timezone, locale)}</span>
          </div>
          {doc.error && (
            <p className="rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {doc.error}
            </p>
          )}
        </div>
        <DocumentPreview key={doc.id} doc={doc} />
      </div>
    </TooltipProvider>
  );
}

// ── 面板外壳 ─────────────────────────────────────────────────────────

function PanelBody({ target }: { target: DetailTarget }) {
  return target.kind === "chunk" ? (
    <ChunkView target={target} />
  ) : (
    <DocumentDetailContent sourceId={target.sourceId} documentId={target.documentId} />
  );
}

/** lg 断点（详情栏 内嵌/Sheet 的分界）。
 *  阈值 900 刻意小于桌面窗口 minWidth（main.ts 960），保证桌面端在任何窗口尺寸都停留
 *  在内嵌 Resizable 分栏；避免用户拖窗口时在断点两侧反复触发 Sheet ↔ 分栏切换、造成
 *  侧栏抖动与布局错乱。Web 端窄浏览器（<900px）仍进入 Sheet 覆盖层。 */
export function useIsLgUp(): boolean {
  const [isLg, setIsLg] = React.useState(true);
  React.useEffect(() => {
    const mq = window.matchMedia("(min-width: 900px)");
    const update = () => setIsLg(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);
  return isLg;
}

/** 小屏详情：Sheet 覆盖层。 */
export function DetailPanelSheet() {
  const t = useTranslations("DetailPanel");
  const { target, close } = useDetailPanel();
  if (!target) return null;
  return (
    <Sheet open onOpenChange={(o) => !o && close()}>
      <SheetContent side="right" className="flex w-full flex-col gap-4 sm:max-w-lg">
        <SheetTitle className="text-sm font-medium">
          {target.kind === "chunk" ? t("panel.chunkTitle") : t("panel.documentTitle")}
        </SheetTitle>
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto [scrollbar-gutter:stable]">
          <PanelBody target={target} />
        </div>
      </SheetContent>
    </Sheet>
  );
}

/** 桌面详情：Resizable 面板内的内容（宽度由外层官方组件管理）。 */
export function DetailPanelOutlet() {
  const t = useTranslations("DetailPanel");
  const { target, maximized, close, toggleMaximize } = useDetailPanel();
  if (!target) return null;

  return (
    <aside className="flex min-h-0 min-w-0 flex-1 flex-col bg-background">
      <div className="flex h-12 shrink-0 items-center gap-1 border-b px-3">
        <span className="min-w-0 flex-1 truncate text-sm font-medium">
          {target.kind === "chunk" ? t("panel.chunkTitle") : t("panel.documentTitle")}
        </span>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="size-7"
              onClick={toggleMaximize}
              aria-label={maximized ? t("panel.restore") : t("panel.maximize")}
            >
              {maximized ? <ChevronsRight /> : <ChevronsLeft />}
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">
            {maximized ? t("panel.restore") : t("panel.expandReading")}
          </TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" size="icon" className="size-7" onClick={close} aria-label={t("panel.close")}>
              <X />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">{t("panel.close")}</TooltipContent>
        </Tooltip>
      </div>
      <div
        className={cn(
          // scrollbar-gutter: stable 预留滚动条位置，避免内容变长/变短时滚动条闪现
          // 导致侧栏可用宽度瞬间抖动、连带 tab 与 markdown 换行错位。
          // min-w-0 保证 flex 子项不会被内部宽内容撑破父面板宽度；宽内容由 TextBody 的
          // overflow-x-auto 处理横向出口，此处**不**用 overflow-x-hidden，
          // 否则内部 grid（如 TabsList）在某些计算路径下可能被裁掉，表现为 tabs 消失。
          "flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto p-4 [scrollbar-gutter:stable]",
          maximized && "mx-auto w-full max-w-4xl",
        )}
      >
        <PanelBody target={target} />
      </div>
    </aside>
  );
}
