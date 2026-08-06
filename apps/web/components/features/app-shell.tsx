"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { usePathname, useRouter } from "next/navigation";
import { Grip, Settings2 } from "lucide-react";
import { motion } from "motion/react";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import { clearToken, getToken } from "@/lib/auth";
import {
  APP_INITIALIZATION_DEFAULTS,
  dismissQuickModelSetup,
  persistAppMode,
  readInitialAppState,
  rememberThemeBeforeExplore,
  resolveThemePreference,
  restoreThemeAfterExplore,
  shouldShowQuickModelSetup,
  type AppMode,
  type ThemePreference,
} from "@/lib/app-initialization";
import { DEFAULT_AGENT_AVATAR, DEFAULT_AGENT_NAME } from "@/lib/branding";
import type { ConversationTransport } from "@/lib/conversation-runtime";
import {
  getDiagnosticsStore,
  type DiagEntry,
  type DiagExport,
  type DiagLogFile,
} from "@/lib/diagnostics";
import { DEFAULT_TIME_ZONE } from "@/lib/format";
import { PetAgent } from "@/lib/pet-agent";
import {
  DEFAULT_SEARCH_STRATEGY,
  isSearchStrategy,
} from "@/lib/retrieval-config";
import {
  SIDEBAR_THREADS_PAGE_SIZE,
  settingsTabHref,
  type SettingsTab,
} from "@/lib/settings-config";
import { streamAgentAsk } from "@/lib/sse";
import type { Agent, Capabilities, Thread, User } from "@/lib/types";
import {
  type WorkspaceSection,
  workspaceSectionFromPathname,
} from "@/lib/workspace";
import {
  UNIVERSE_ASK_EVENT,
  UNIVERSE_DETAIL_EVENT,
  dispatchUniverseActivation,
  dispatchUniverseReset,
} from "@/lib/universe-events";
import { cn } from "@/lib/utils";
import {
  DEFAULT_WINDOW_MODE,
  DEFAULT_WINDOW_SIZE,
  clampWindowSize,
  persistWindowMode,
  persistWindowSize,
  readWindowMode,
  readWindowSize,
  resolveWindowScalingEnabled,
  type WindowMode,
  type WindowSize,
} from "@/lib/window-layout";
import { useDiagnostics } from "@/hooks/use-diagnostics";
import { AppSidebar } from "@/components/features/app-sidebar";
import { ConversationProvider } from "@/components/features/chat/conversation-provider";
import {
  DetailPanelMain,
  DetailPanelOutlet,
  DetailPanelProvider,
  DetailPanelSheet,
  useDetailPanel,
  useIsLgUp,
} from "@/components/features/detail-panel";
import { KnowledgeProvider } from "@/components/features/knowledge-provider";
import { PetWithPreference } from "@/components/features/pet";
import { PetHeadAvatar } from "@/components/features/pet-head-avatar";
import { QuickModelSetupDialog } from "@/components/features/quick-model-setup-dialog";
import { SearchProvider } from "@/components/features/search/search-provider";
import { SpaceBackdrop } from "@/components/features/space-backdrop";
import { SiteHeader } from "@/components/features/site-header";
import { ThemeToggle } from "@/components/features/theme-toggle";
import { UniverseViewSettingsDrawer } from "@/components/features/universe-view-settings-drawer";
import { Button } from "@/components/ui/button";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";

const KnowledgeUniverse = dynamic(
  () =>
    import("@/components/features/knowledge-universe").then(
      (module) => module.KnowledgeUniverse,
    ),
  { ssr: false },
);

export type { AppMode } from "@/lib/app-initialization";

const WINDOW_SCALING_ENABLED = resolveWindowScalingEnabled(
  process.env.NEXT_PUBLIC_ENABLE_WINDOW_SCALING,
);

function currentViewportSize(): WindowSize {
  return { width: window.innerWidth, height: window.innerHeight };
}

const CONVERSATION_TRANSPORT: ConversationTransport = {
  createThread: ({ agentId, title, signal }) => api.createThread(agentId, title, signal),
  listMessages: ({ agentId, threadId, cursor, signal }) =>
    api.listMessages(agentId, threadId, { limit: 40, cursor, signal }),
  stream: ({
    agentId,
    threadId,
    query,
    attachmentIds,
    sourceIds,
    knowledgeOnly,
    webEnabled,
    onEvent,
    signal,
  }) => {
    const store = getDiagnosticsStore();
    const startedAt = Date.now();
    store.record("qa.ask", {
      agent_id: agentId,
      thread_id: threadId,
      query: query.length > 500 ? query.slice(0, 500) + "..." : query,
      attachment_count: attachmentIds?.length ?? 0,
      source_ids: sourceIds,
      web_enabled: webEnabled,
    });
    const promise = streamAgentAsk(
      agentId,
      threadId,
      {
        query,
        attachments: attachmentIds,
        source_ids: sourceIds,
        knowledge_only: knowledgeOnly === true || !webEnabled,
        web_enabled: webEnabled,
      },
      (event) => {
        // 只记录关键事件，跳过高频 message.delta
        const keyEvents = new Set([
          "run.started",
          "run.completed",
          "run.failed",
          "run.cancelled",
          "tool.started",
          "tool.completed",
          "tool.failed",
          "tool.approval_required",
        ]);
        if (keyEvents.has(event.type)) {
          store.record("qa.event", {
            event_type: event.type,
            run_id: event.run_id,
            sequence: event.sequence,
            turn: event.turn,
            payload_keys: Object.keys(event.payload ?? {}),
          });
        }
        onEvent(event);
      },
      signal,
    );
    void promise.then((outcome) => {
      store.record(outcome.status === "completed" ? "qa.complete" : "qa.error", {
        run_id: outcome.runId,
        status: outcome.status,
        message_id: outcome.messageId,
        duration_ms: Date.now() - startedAt,
        error_code: outcome.error?.code,
        error_message: outcome.error?.message
          ? outcome.error.message.slice(0, 500)
          : undefined,
      });
    }).catch((reason) => {
      store.record("qa.error", {
        run_id: "unknown",
        status: "failed",
        duration_ms: Date.now() - startedAt,
        error_message: reason instanceof Error
          ? reason.message.slice(0, 500)
          : String(reason).slice(0, 500),
      });
    });
    return promise;
  },
  cancelRun: ({ agentId, threadId, runId }) =>
    api.cancelAgentRun(agentId, threadId, runId),
  approveTool: ({ agentId, threadId, runId, toolCallId }) =>
    api.approveAgentTool(agentId, threadId, runId, toolCallId),
  rejectTool: ({ agentId, threadId, runId, toolCallId, reason }) =>
    api.rejectAgentTool(agentId, threadId, runId, toolCallId, reason),
  deleteMessage: ({ agentId, threadId, messageId }) =>
    api.deleteMessage(agentId, threadId, messageId),
};

interface AppCtx {
  user: User | null;
  capabilities: Capabilities | null;
  /** 默认 agent（客户端主对话入口） */
  agent: Agent | null;
  /** 可编排的桌面角色，与业务 Agent 分离。 */
  petAgent: PetAgent | null;
  replaceAgent: (agent: Agent) => void;
  threads: Thread[];
  hasMoreThreads: boolean;
  threadsExpanded: boolean;
  loadingMoreThreads: boolean;
  refreshThreads: () => Promise<void>;
  loadMoreThreads: () => Promise<void>;
  collapseThreads: () => void;
  windowScalingEnabled: boolean;
  windowMode: WindowMode;
  toggleWindowMode: () => void;
  appMode: AppMode;
  workspaceSection: WorkspaceSection;
  enterExploreMode: (section?: WorkspaceSection) => void;
  exitExploreMode: () => void;
  openSettings: (tab?: SettingsTab, section?: string) => void;
  logout: () => void;
  refreshCapabilities: () => Promise<void>;
  timezone: string;
  updateTimezone: (timezone: string) => Promise<void>;
  diagnostics: {
    entries: DiagEntry[];
    record: (type: DiagEntry["type"], data?: Record<string, unknown>) => void;
    exportLogs: () => DiagExport;
    downloadLogs: () => Promise<void>;
  };
}

const AppContext = React.createContext<AppCtx>({
  user: null,
  capabilities: null,
  agent: null,
  petAgent: null,
  replaceAgent: () => {},
  threads: [],
  hasMoreThreads: false,
  threadsExpanded: false,
  loadingMoreThreads: false,
  refreshThreads: async () => {},
  loadMoreThreads: async () => {},
  collapseThreads: () => {},
  windowScalingEnabled: WINDOW_SCALING_ENABLED,
  windowMode: WINDOW_SCALING_ENABLED ? DEFAULT_WINDOW_MODE : "full",
  toggleWindowMode: () => {},
  appMode: APP_INITIALIZATION_DEFAULTS.appMode,
  workspaceSection: APP_INITIALIZATION_DEFAULTS.workspaceSection,
  enterExploreMode: () => {},
  exitExploreMode: () => {},
  openSettings: () => {},
  logout: () => {},
  refreshCapabilities: async () => {},
  timezone: DEFAULT_TIME_ZONE,
  updateTimezone: async () => {},
  diagnostics: {
    entries: [],
    record: () => {},
    exportLogs: () => ({ version: 1 as const, exported_at: "", environment: { app: "web" as const, user_agent: "", language: "", timezone: "" }, model_config: null, capabilities: null, entries: [] }),
    downloadLogs: async () => {},
  },
});

export function useApp() {
  return React.useContext(AppContext);
}

export function usePetAgent() {
  const petAgent = useApp().petAgent;
  if (!petAgent) throw new Error("usePetAgent must be used inside AppShell");
  return petAgent;
}

function FullLoader() {
  const t = useTranslations("AppShell");
  return (
    <div className="bg-space-field grid h-screen place-items-center">
      <SpaceBackdrop />
      <div
        className="relative z-10 flex flex-col items-center gap-2.5"
        role="status"
        aria-live="polite"
      >
        <PetHeadAvatar
          face={DEFAULT_AGENT_AVATAR}
          size="lg"
          className="sag-full-loader__avatar"
        />
        <span className="text-sm text-muted-foreground">{t("loading")}</span>
      </div>
    </div>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const t = useTranslations("AppShell");
  const router = useRouter();
  const pathname = usePathname();
  const { theme, resolvedTheme, setTheme } = useTheme();
  const petAgent = React.useMemo(
    () =>
      new PetAgent({
        name: DEFAULT_AGENT_NAME,
        avatar: DEFAULT_AGENT_AVATAR,
        serialNumber: 1,
        size: 1,
      }),
    [],
  );
  const [user, setUser] = React.useState<User | null>(null);
  const [capabilities, setCapabilities] = React.useState<Capabilities | null>(null);
  const [agent, setAgent] = React.useState<Agent | null>(null);
  const [threads, setThreads] = React.useState<Thread[]>([]);
  const [hasMoreThreads, setHasMoreThreads] = React.useState(false);
  const [threadsExpanded, setThreadsExpanded] = React.useState(false);
  const [loadingMoreThreads, setLoadingMoreThreads] = React.useState(false);
  const [appMode, setAppMode] = React.useState<AppMode>(
    APP_INITIALIZATION_DEFAULTS.appMode,
  );
  const [windowMode, setWindowMode] = React.useState<WindowMode>(
    WINDOW_SCALING_ENABLED ? DEFAULT_WINDOW_MODE : "full",
  );
  const [windowSize, setWindowSize] = React.useState<WindowSize>(
    DEFAULT_WINDOW_SIZE,
  );
  const [isDesktop, setIsDesktop] = React.useState(true);
  const [workspaceSection, setWorkspaceSection] = React.useState<WorkspaceSection>(
    APP_INITIALIZATION_DEFAULTS.workspaceSection,
  );
  const [sidebarOpen, setSidebarOpen] = React.useState(true);
  const [loading, setLoading] = React.useState(true);
  const [quickSetupOpen, setQuickSetupOpen] = React.useState(false);
  const [timezone, setTimezone] = React.useState(DEFAULT_TIME_ZONE);
  const diag = useDiagnostics();
  // `diag.record` is referentially stable (bound to the singleton store), unlike
  // `diag` itself which changes whenever a new entry is recorded. Depend on the
  // stable function so the bootstrap effect below doesn't re-run on every log.
  const recordDiag = diag.record;
  const sidebarOpenRef = React.useRef(true);
  const restoreSidebarOpenRef = React.useRef<boolean | null>(null);
  const threadLimitRef = React.useRef(SIDEBAR_THREADS_PAGE_SIZE);
  const threadRequestIdRef = React.useRef(0);
  const loadingMoreThreadsRef = React.useRef(false);
  const previousThemeModeRef = React.useRef<AppMode | null>(null);
  const themeBeforeExploreRef = React.useRef<ThemePreference | null>(null);
  const currentThemeRef = React.useRef<ThemePreference>(
    resolveThemePreference(theme, resolvedTheme),
  );
  currentThemeRef.current = resolveThemePreference(theme, resolvedTheme);

  // 首屏恢复模式、工作区，以及 Web 构建允许时的模拟窗口偏好。
  React.useEffect(() => {
    const initial = readInitialAppState(window.localStorage);
    setAppMode(initial.mode);
    setWorkspaceSection(initial.section);
    if (WINDOW_SCALING_ENABLED) {
      setWindowMode(readWindowMode(window.localStorage));
      setWindowSize(readWindowSize(window.localStorage, currentViewportSize()));
    }
  }, []);

  React.useEffect(() => {
    if (!WINDOW_SCALING_ENABLED) {
      setIsDesktop(false);
      return;
    }
    const query = window.matchMedia("(min-width: 768px)");
    const update = () => setIsDesktop(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  React.useEffect(() => {
    if (loading) return;
    const previousMode = previousThemeModeRef.current;
    previousThemeModeRef.current = appMode;

    if (appMode === "explore") {
      if (previousMode === "explore") return;
      const beforeExplore = rememberThemeBeforeExplore(
        window.localStorage,
        currentThemeRef.current,
      );
      themeBeforeExploreRef.current = beforeExplore;
      currentThemeRef.current = "dark";
      setTheme("dark");
      return;
    }

    if (previousMode !== "explore") return;
    const beforeExplore = restoreThemeAfterExplore(window.localStorage)
      ?? themeBeforeExploreRef.current;
    themeBeforeExploreRef.current = null;
    if (!beforeExplore) return;
    currentThemeRef.current = beforeExplore;
    setTheme(beforeExplore);
  }, [appMode, loading, setTheme]);

  const enterExploreMode = React.useCallback((section?: WorkspaceSection) => {
    const nextSection = section
      ?? workspaceSectionFromPathname(pathname)
      ?? workspaceSection;
    if (appMode !== "explore") {
      // Entering from search/answer is a fresh visit to the universe home.
      // Context results remain in their panels; the graph starts at overview
      // and only enters a source after the user deliberately selects one.
      dispatchUniverseReset("explore-home");
    }
    setWorkspaceSection(nextSection);
    setAppMode("explore");
    persistAppMode(window.localStorage, "explore", nextSection);
    if (appMode !== "explore") {
      toast(t("exploreEntered"), {
        id: "explore-mode-entered",
        description: t("exploreEnteredDescription"),
        duration: 5000,
      });
    }
  }, [appMode, pathname, t, workspaceSection]);

  const exitExploreMode = React.useCallback(() => {
    toast.dismiss("explore-mode-entered");
    setAppMode("normal");
    persistAppMode(window.localStorage, "normal");
  }, []);

  const toggleWindowMode = React.useCallback(() => {
    if (!WINDOW_SCALING_ENABLED) return;
    setWindowMode((current) => {
      const next: WindowMode = current === "full" ? "window" : "full";
      persistWindowMode(window.localStorage, next);
      return next;
    });
  }, []);

  const openSettings = React.useCallback((tab: SettingsTab = "account", section?: string) => {
    setAppMode("normal");
    persistAppMode(window.localStorage, "normal");
    router.push(settingsTabHref(tab, section));
  }, [router]);

  React.useEffect(() => {
    // A node click opens a detail preview inside the current exploration; it
    // must not silently switch the workspace to search/cumulative mode.
    const revealDetail = () => enterExploreMode();
    const revealAsk = () => enterExploreMode("answer");
    window.addEventListener(UNIVERSE_DETAIL_EVENT, revealDetail);
    window.addEventListener(UNIVERSE_ASK_EVENT, revealAsk);
    return () => {
      window.removeEventListener(UNIVERSE_DETAIL_EVENT, revealDetail);
      window.removeEventListener(UNIVERSE_ASK_EVENT, revealAsk);
    };
  }, [enterExploreMode]);

  React.useEffect(() => {
    sidebarOpenRef.current = sidebarOpen;
  }, [sidebarOpen]);

  const windowed = WINDOW_SCALING_ENABLED
    && isDesktop
    && windowMode === "window";

  React.useEffect(() => {
    if (!windowed) return;
    const onResize = () => {
      setWindowSize((current) => clampWindowSize(current, currentViewportSize()));
    };
    window.addEventListener("resize", onResize);
    onResize();
    return () => window.removeEventListener("resize", onResize);
  }, [windowed]);

  const startWindowResize = React.useCallback(
    (event: React.PointerEvent<HTMLButtonElement>) => {
      event.preventDefault();
      const startX = event.clientX;
      const startY = event.clientY;
      const startSize = windowSize;
      let nextSize = startSize;
      const previousCursor = document.documentElement.style.cursor;
      const previousSelect = document.documentElement.style.userSelect;

      document.documentElement.style.cursor = "nwse-resize";
      document.documentElement.style.userSelect = "none";

      const onMove = (moveEvent: PointerEvent) => {
        nextSize = clampWindowSize(
          {
            width: startSize.width + moveEvent.clientX - startX,
            height: startSize.height + moveEvent.clientY - startY,
          },
          currentViewportSize(),
        );
        setWindowSize(nextSize);
      };

      const onUp = () => {
        document.documentElement.style.cursor = previousCursor;
        document.documentElement.style.userSelect = previousSelect;
        persistWindowSize(window.localStorage, nextSize);
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };

      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp, { once: true });
    },
    [windowSize],
  );

  React.useEffect(() => {
    const onDetailMaximized = (event: Event) => {
      const maximized = Boolean((event as CustomEvent<boolean>).detail);
      if (maximized) {
        if (restoreSidebarOpenRef.current === null) {
          restoreSidebarOpenRef.current = sidebarOpenRef.current;
        }
        setSidebarOpen(false);
        return;
      }
      if (restoreSidebarOpenRef.current !== null) {
        setSidebarOpen(restoreSidebarOpenRef.current);
        restoreSidebarOpenRef.current = null;
      }
    };
    window.addEventListener("sag:detail-maximized", onDetailMaximized);
    return () => window.removeEventListener("sag:detail-maximized", onDetailMaximized);
  }, []);

  const refreshCapabilities = React.useCallback(async () => {
    try {
      const next = await api.capabilities();
      setCapabilities(next);
      setTimezone(next.timezone || DEFAULT_TIME_ZONE);
    } catch {
      /* ignore */
    }
  }, []);

  const updateTimezone = React.useCallback(async (nextTimezone: string) => {
    const preferences = await api.saveSystemPreferences({ timezone: nextTimezone });
    setTimezone(preferences.timezone);
    setCapabilities((current) =>
      current ? { ...current, timezone: preferences.timezone } : current,
    );
  }, []);

  const loadThreadLimit = React.useCallback(async (targetAgent: Agent, limit: number) => {
    const requestId = ++threadRequestIdRef.current;
    const page = await api.listThreads(targetAgent.id, { limit: limit + 1 });
    if (requestId !== threadRequestIdRef.current) return;

    const visible = page.slice(0, limit);
    const expanded = limit > SIDEBAR_THREADS_PAGE_SIZE && visible.length > SIDEBAR_THREADS_PAGE_SIZE;

    setThreads(visible);
    setHasMoreThreads(page.length > limit);
    setThreadsExpanded(expanded);
    if (!expanded) threadLimitRef.current = SIDEBAR_THREADS_PAGE_SIZE;
  }, []);

  const refreshThreads = React.useCallback(async () => {
    try {
      const targetAgent = agent ?? (await api.getDefaultAgent());
      await loadThreadLimit(targetAgent, threadLimitRef.current);
    } catch {
      /* keep the current sidebar list when refresh fails */
    }
  }, [agent, loadThreadLimit]);

  const loadMoreThreads = React.useCallback(async () => {
    if (!agent || loadingMoreThreadsRef.current) return;
    const previousLimit = threadLimitRef.current;
    const nextLimit = previousLimit + SIDEBAR_THREADS_PAGE_SIZE;
    threadLimitRef.current = nextLimit;
    loadingMoreThreadsRef.current = true;
    setLoadingMoreThreads(true);
    try {
      await loadThreadLimit(agent, nextLimit);
    } catch (error) {
      if (threadLimitRef.current === nextLimit) threadLimitRef.current = previousLimit;
      throw error;
    } finally {
      loadingMoreThreadsRef.current = false;
      setLoadingMoreThreads(false);
    }
  }, [agent, loadThreadLimit]);

  const collapseThreads = React.useCallback(() => {
    threadRequestIdRef.current += 1;
    threadLimitRef.current = SIDEBAR_THREADS_PAGE_SIZE;
    setThreads((current) => current.slice(0, SIDEBAR_THREADS_PAGE_SIZE));
    setThreadsExpanded(false);
    setHasMoreThreads(true);
  }, []);

  React.useEffect(() => {
    let alive = true;
    (async () => {
      if (!getToken()) {
        router.replace("/login");
        return;
      }
      try {
        const [u, c, a, setup] = await Promise.all([
          api.me(),
          api.capabilities(),
          api.getDefaultAgent(),
          api.modelSetupStatus().catch(() => null),
        ]);
        if (!alive) return;
        setUser(u);
        setCapabilities(c);
        setTimezone(c.timezone || DEFAULT_TIME_ZONE);
        setAgent(a);
        setQuickSetupOpen(
          shouldShowQuickModelSetup(Boolean(setup?.required), window.localStorage),
        );
        recordDiag("app.init", {
          user_id: u.id,
          agent_id: a.id,
          language: c.language ?? "zh",
          timezone: c.timezone || DEFAULT_TIME_ZONE,
          platform: typeof window !== "undefined" && window.sagDesktop?.isDesktop ? "desktop" : "web",
          capabilities: {
            llm_configured: c.llm_configured,
            llm_provider: c.llm_provider,
            llm_model: c.llm_model,
            embedding_model: c.embedding_model,
            document_parser: c.document_parser,
            effective_document_parser: c.effective_document_parser,
            search_strategy: c.search_strategy,
            max_upload_mb: c.max_upload_mb,
          },
        });
        threadLimitRef.current = SIDEBAR_THREADS_PAGE_SIZE;
        loadThreadLimit(a, SIDEBAR_THREADS_PAGE_SIZE).catch(() => {});
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          clearToken();
          router.replace("/login");
          return;
        }
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
      threadRequestIdRef.current += 1;
    };
  }, [loadThreadLimit, router, recordDiag]);

  // 快捷键直接进入探索模式，并打开对应的紧凑工作区。
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        enterExploreMode("search");
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "j") {
        e.preventDefault();
        enterExploreMode("answer");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [enterExploreMode]);

  const logout = React.useCallback(() => {
    clearToken();
    router.replace("/login");
  }, [router]);

  const diagnosticsExportLogs = React.useCallback(() => {
    return diag.exportLogs(
      {
        app: typeof window !== "undefined" && window.sagDesktop?.isDesktop ? "desktop" : "web",
        user_agent: typeof navigator !== "undefined" ? navigator.userAgent : "",
        language: capabilities?.language ?? "zh",
        timezone: timezone,
      },
      capabilities
        ? {
            llm_provider: capabilities.llm_provider,
            llm_model: capabilities.llm_model,
            embedding_model: capabilities.embedding_model,
            document_parser: capabilities.document_parser,
            effective_document_parser: capabilities.effective_document_parser,
            search_strategy: capabilities.search_strategy,
            max_upload_mb: capabilities.max_upload_mb,
            language: capabilities.language,
            timezone: capabilities.timezone,
          }
        : null,
      {
        llm_configured: capabilities?.llm_configured,
        llm_provider: capabilities?.llm_provider,
        llm_model: capabilities?.llm_model,
        embedding_model: capabilities?.embedding_model,
        document_parser: capabilities?.document_parser,
        search_strategy: capabilities?.search_strategy,
        max_upload_mb: capabilities?.max_upload_mb,
      },
    );
  }, [diag, capabilities, timezone]);

  const diagnosticsDownloadLogs = React.useCallback(async () => {
    // On desktop, pull the real runtime log files (frontend + backend Python
    // logs are both piped into electron-log's main.log) so the exported file
    // carries what R&D actually needs to diagnose a locally-deployed instance.
    let desktopLogFiles: DiagLogFile[] | undefined;
    if (typeof window !== "undefined" && window.sagDesktop?.getDiagnosticsInfo) {
      try {
        const info = await window.sagDesktop.getDiagnosticsInfo();
        desktopLogFiles = info.logFiles.map((file) => ({
          name: file.name,
          path: file.path,
          size_bytes: file.sizeBytes,
          content: file.content,
          truncated: file.truncated,
        }));
      } catch {
        // best-effort: fall back to an export without the on-disk logs
      }
    }
    diag.downloadLogs(
      {
        app: typeof window !== "undefined" && window.sagDesktop?.isDesktop ? "desktop" : "web",
        user_agent: typeof navigator !== "undefined" ? navigator.userAgent : "",
        language: capabilities?.language ?? "zh",
        timezone: timezone,
      },
      capabilities
        ? {
            llm_provider: capabilities.llm_provider,
            llm_model: capabilities.llm_model,
            embedding_model: capabilities.embedding_model,
            document_parser: capabilities.document_parser,
            effective_document_parser: capabilities.effective_document_parser,
            search_strategy: capabilities.search_strategy,
            max_upload_mb: capabilities.max_upload_mb,
            language: capabilities.language,
            timezone: capabilities.timezone,
          }
        : null,
      {
        llm_configured: capabilities?.llm_configured,
        llm_provider: capabilities?.llm_provider,
        llm_model: capabilities?.llm_model,
        embedding_model: capabilities?.embedding_model,
        document_parser: capabilities?.document_parser,
        search_strategy: capabilities?.search_strategy,
        max_upload_mb: capabilities?.max_upload_mb,
      },
      desktopLogFiles,
    );
  }, [diag, capabilities, timezone]);

  if (loading) return <FullLoader />;
  if (!user || !agent) return null;

  return (
    <AppContext.Provider
      value={{
        user,
        capabilities,
        agent,
        petAgent,
        replaceAgent: setAgent,
        threads,
        hasMoreThreads,
        threadsExpanded,
        loadingMoreThreads,
        refreshThreads,
        loadMoreThreads,
        collapseThreads,
        windowScalingEnabled: WINDOW_SCALING_ENABLED,
        windowMode,
        toggleWindowMode,
        appMode,
        workspaceSection,
        enterExploreMode,
        exitExploreMode,
        openSettings,
        logout,
        refreshCapabilities,
        timezone,
        updateTimezone,
        diagnostics: {
          entries: diag.entries,
          record: diag.record,
          exportLogs: diagnosticsExportLogs,
          downloadLogs: diagnosticsDownloadLogs,
        },
      }}
    >
      <SearchProvider
        defaultStrategy={
          isSearchStrategy(capabilities?.search_strategy)
            ? capabilities.search_strategy
            : DEFAULT_SEARCH_STRATEGY
        }
      >
        <KnowledgeProvider>
          <ConversationProvider
            agentId={agent.id}
            transport={CONVERSATION_TRANSPORT}
            onActivity={refreshThreads}
            onUniverseActivation={dispatchUniverseActivation}
          >
            <QuickModelSetupDialog
              open={quickSetupOpen}
              onOpenChange={(nextOpen) => {
                setQuickSetupOpen(nextOpen);
                if (!nextOpen) dismissQuickModelSetup(window.localStorage);
              }}
              onConfigured={(nextCapabilities) => {
                setCapabilities(nextCapabilities);
                setTimezone(nextCapabilities.timezone || DEFAULT_TIME_ZONE);
                setQuickSetupOpen(false);
              }}
            />
            <DetailPanelProvider>
              <div
                className={cn(
                  // grid-cols-[minmax(0,1fr)]：非-windowed 分支下 grid 项默认 min-width:auto
                  // 会读子内容的 min-content 撑破 track——PDF 解析出的宽 markdown 就会让
                  // motion.div 撑到 2198px，右侧内容漂出 SAG 窗口（devtools 实测）。
                  // minmax(0, 1fr) 显式把 track 最小值钳到 0，等价于给 grid 项加 min-w-0。
                  "bg-space-field relative grid h-svh min-h-0 grid-cols-[minmax(0,1fr)] overflow-hidden",
                  windowed && "place-items-center p-4",
                )}
              >
                <SpaceBackdrop variant={appMode === "explore" ? "universe" : "shell"} />
                <KnowledgeUniverse interactive={appMode === "explore"} />
                {appMode === "explore" && (
                  <motion.div
                    initial={{ opacity: 0, scale: 0.92, y: -4 }}
                    animate={{ opacity: 1, scale: 1, y: 0 }}
                    transition={{ duration: 0.18 }}
                    className="fixed right-4 top-3 z-[45] flex items-center gap-2"
                    data-explore-controls="true"
                  >
                    <ThemeToggle
                      className="size-8 border border-border/60 bg-background/80 shadow-soft backdrop-blur-md hover:border-amber-300/40 hover:bg-amber-300/10 hover:text-amber-200"
                    />
                    <UniverseViewSettingsDrawer
                      trigger={(
                        <Button
                          type="button"
                          variant="outline"
                          size="icon"
                          className="size-8 border-border/60 bg-background/80 shadow-soft backdrop-blur-md hover:border-[#7ea6ff]/35 hover:bg-[#4f86ff]/10 hover:text-[#c8d9ff]"
                          aria-label={t("graphSettings")}
                          title={t("graphSettings")}
                          data-universe-settings-trigger="true"
                        >
                          <Settings2 className="size-3.5" />
                        </Button>
                      )}
                    />
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-8 border-border/60 bg-background/80 px-3 text-xs font-medium shadow-soft backdrop-blur-md hover:border-destructive/30 hover:bg-destructive/10 hover:text-destructive"
                      onClick={exitExploreMode}
                      aria-label={t("exitExplore")}
                      title={t("exitExplore")}
                    >
                      {t("exitExploreShort")}
                    </Button>
                  </motion.div>
                )}
                <motion.div
                  initial={false}
                  animate={
                    appMode === "normal"
                      ? { opacity: 1, scale: 1, y: 0 }
                      : { opacity: 0, scale: 0.9, y: 24 }
                  }
                  transition={{ type: "spring", stiffness: 340, damping: 30 }}
                  style={
                    windowed
                      ? { width: windowSize.width, height: windowSize.height }
                      : { width: "100%", height: "100svh" }
                  }
                  aria-hidden={appMode !== "normal"}
                  className={cn(
                    "relative z-10",
                    appMode !== "normal" && "invisible pointer-events-none",
                    windowed
                      ? "max-h-[calc(100svh-2rem)] max-w-[calc(100vw-2rem)] transform-gpu overflow-hidden rounded-xl border bg-background shadow-lift"
                      : "min-h-svh",
                  )}
                >
                  <SidebarProvider
                    open={sidebarOpen}
                    onOpenChange={setSidebarOpen}
                    className={cn(windowed ? "h-full min-h-full" : "h-svh min-h-svh")}
                  >
                    <AppSidebar contained={windowed} />
                    <SidebarInset className="min-w-0 overflow-hidden">
                      <SiteHeader />
                      <ContentArea>{children}</ContentArea>
                    </SidebarInset>
                  </SidebarProvider>
                  {windowed && (
                    <button
                      type="button"
                      aria-label={t("resizeWindow")}
                      title={t("resizeWindow")}
                      onPointerDown={startWindowResize}
                      className="absolute bottom-1.5 right-1.5 z-20 hidden size-7 cursor-nwse-resize items-center justify-center rounded-md text-muted-foreground/55 transition-colors hover:bg-muted hover:text-foreground md:flex"
                    >
                      <Grip className="size-3.5 rotate-45" />
                    </button>
                  )}
                </motion.div>
              </div>
              <TooltipProvider delayDuration={300}>
                <PetWithPreference character={petAgent} syncIdentity />
              </TooltipProvider>
            </DetailPanelProvider>
          </ConversationProvider>
        </KnowledgeProvider>
      </SearchProvider>
    </AppContext.Provider>
  );
}

/** 内容区：官方 Resizable 组合——主区 + 可拖宽详情栏（宽度经 autoSaveId 持久化）。 */
function ContentArea({ children }: { children: React.ReactNode }) {
  const { target, maximized, panelRef } = useDetailPanel();
  const lg = useIsLgUp();
  React.useEffect(() => {
    window.dispatchEvent(new CustomEvent("sag:detail-maximized", { detail: maximized }));
  }, [maximized]);

  return (
    <>
      <ResizablePanelGroup
        direction="horizontal"
        // min-w-0 + overflow-hidden：flex 子项默认 min-width:auto 会让 Panel 内容宽度
        // 决定 group 宽度，导致解析内容超长时把整个右侧面板撑到窗口外面（图 5 现象）。
        className="min-h-0 min-w-0 flex-1 overflow-hidden"
        autoSaveId="sag:detail"
      >
        <ResizablePanel defaultSize={66} minSize={0} className="min-w-0">
          <DetailPanelMain>{children}</DetailPanelMain>
        </ResizablePanel>
        {target && lg && (
          <>
            <ResizableHandle withHandle />
            <ResizablePanel ref={panelRef} defaultSize={34} minSize={24} maxSize={100} className="flex min-h-0 min-w-0 border-l">
              <DetailPanelOutlet />
            </ResizablePanel>
          </>
        )}
      </ResizablePanelGroup>
      {target && !lg && <DetailPanelSheet />}
    </>
  );
}
