/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ChatPage from "@/app/(app)/chat/[[...id]]/page";
import { AppSidebar } from "@/components/features/app-sidebar";
import { ConversationProvider, useConversationRuntime } from "@/components/features/chat/conversation-provider";
import type { ConversationRuntime, ConversationTransport } from "@/lib/conversation-runtime";
import type { AgentRunOutcome } from "@/lib/sse";

const navigation = vi.hoisted(() => ({
  pathname: "/knowledge",
  params: {} as { id?: string[] },
  router: { push: vi.fn() },
}));
vi.mock("next/navigation", () => ({
  usePathname: () => navigation.pathname,
  useParams: () => navigation.params,
  useRouter: () => navigation.router,
}));
vi.mock("next-intl", () => ({
  useLocale: () => "en-US",
  useTranslations: () => (key: string) => key,
}));
vi.mock("next/link", () => ({
  default: ({ children, href, ...props }: React.PropsWithChildren<{ href: string }>) => <a href={href} {...props}>{children}</a>,
}));
vi.mock("next/image", () => ({ default: () => null }));
vi.mock("@/components/features/app-shell", () => ({
  useApp: () => ({
    agent: { id: "agent-1", name: "Agent", avatar: "", persona: {} },
    appMode: "normal", user: { name: "Local test" }, logout: vi.fn(),
    threads: [], hasMoreThreads: false, threadsExpanded: false,
    loadingMoreThreads: false, refreshThreads: vi.fn(), loadMoreThreads: vi.fn(),
    collapseThreads: vi.fn(), timezone: "UTC",
  }),
}));
vi.mock("@/components/features/pet-head-avatar", () => ({ PetHeadAvatar: () => null }));
vi.mock("@/components/features/workspace-section-icon", () => ({ WorkspaceSectionIcon: () => null }));
vi.mock("@/components/features/desktop-update-indicator", () => ({ DesktopUpdateIndicator: () => null }));
vi.mock("@/components/features/app-version-badge", () => ({ AppVersionBadge: () => null }));
vi.mock("@/components/features/chat/conversation-panel", async () => {
  const { useConversationSession, useConversationRuntime } = await import("@/components/features/chat/conversation-provider");
  return {
    ConversationPanel: ({ sessionId }: { sessionId: string }) => {
      const session = useConversationSession(sessionId);
      const runtime = useConversationRuntime();
      React.useEffect(() => {
        runtime.activate(sessionId);
        void runtime.ensureHistory(sessionId);
      }, [runtime, sessionId]);
      return <section data-testid="conversation" data-session={sessionId} data-thread={session?.threadId ?? ""}>
        {session?.messages.map((message) => <p key={message.id}>{message.content}</p>)}
      </section>;
    },
  };
});
vi.mock("@/components/ui/sidebar", () => {
  const pass = ({ children }: React.PropsWithChildren) => <>{children}</>;
  return Object.fromEntries([
    "Sidebar", "SidebarContent", "SidebarFooter", "SidebarGroup", "SidebarGroupLabel",
    "SidebarHeader", "SidebarMenu", "SidebarMenuButton", "SidebarMenuItem", "SidebarRail",
  ].map((name) => [name, pass]));
});
vi.mock("@/components/ui/tooltip", () => {
  const pass = ({ children }: React.PropsWithChildren) => <>{children}</>;
  return { Tooltip: pass, TooltipTrigger: pass, TooltipContent: pass };
});
vi.mock("@/components/ui/dropdown-menu", () => {
  const pass = ({ children }: React.PropsWithChildren) => <>{children}</>;
  return Object.fromEntries([
    "DropdownMenu", "DropdownMenuContent", "DropdownMenuItem", "DropdownMenuLabel",
    "DropdownMenuSeparator", "DropdownMenuTrigger",
  ].map((name) => [name, pass]));
});

let root: Root;
let container: HTMLDivElement;
let runtime: ConversationRuntime;
let transport: ConversationTransport;
let settleRun: ((outcome: AgentRunOutcome) => void) | null;

function Capture() {
  runtime = useConversationRuntime();
  return null;
}
function Harness({ chat }: { chat: boolean }) {
  return <ConversationProvider agentId="agent-1" transport={transport}>
    <Capture />
    <AppSidebar />
    {chat && <ChatPage />}
  </ConversationProvider>;
}
async function navigate(path: string, chat: boolean) {
  navigation.pathname = path;
  navigation.params = path.startsWith("/chat/") ? { id: [path.split("/")[2]] } : {};
  window.history.pushState({}, "", path);
  await act(async () => root.render(<Harness chat={chat} />));
}
async function clickNew() {
  const button = container.querySelector<HTMLButtonElement>('button[aria-label="newChat"]');
  expect(button).not.toBeNull();
  await act(async () => button!.click());
}

beforeEach(async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  navigation.pathname = "/knowledge";
  navigation.params = {};
  navigation.router.push.mockReset();
  settleRun = null;
  transport = {
    createThread: vi.fn(async () => ({ id: "created-thread" })),
    listMessages: vi.fn(async ({ threadId }) => ({
      items: [{
        id: `saved-${threadId}`, thread_id: threadId, role: "assistant" as const,
        content: "Saved answer", citations: [], attachments: [], steps: [],
        created_at: "2026-07-12T00:00:00Z",
      }],
      has_more: false, next_cursor: null,
    })),
    stream: vi.fn(() => new Promise<AgentRunOutcome>((resolve) => { settleRun = resolve; })),
    cancelRun: vi.fn(async () => ({})),
    approveTool: vi.fn(async () => ({})),
    rejectTool: vi.fn(async () => ({})),
    deleteMessage: vi.fn(async () => ({})),
  };
  window.history.replaceState({}, "", "/knowledge");
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<Harness chat={false} />));
  await act(async () => { runtime.forThread("last-chat", { activate: true }); });
});
afterEach(async () => {
  await act(async () => root.unmount());
  runtime.dispose();
  container.remove();
  vi.restoreAllMocks();
});

describe("New conversation navigation", () => {
  it.each(["/knowledge", "/settings", "/search"])(
    "opens a blank draft after clicking New conversation from %s",
    async (path) => {
      await navigate(path, false);
      const before = runtime.getIndexSnapshot().sessions.length;
      await clickNew();
      expect(navigation.router.push).toHaveBeenCalledWith("/chat");
      expect(runtime.getIndexSnapshot().sessions).toHaveLength(before + 1);
      const draftId = runtime.getIndexSnapshot().activeSessionId!;
      expect(runtime.getSessionSnapshot(draftId).threadId).toBeNull();
      expect(transport.createThread).not.toHaveBeenCalled();
      await navigate("/chat", true);
      expect(container.querySelector('[data-testid="conversation"]')?.getAttribute("data-session")).toBe(draftId);
      expect(container.querySelector('[data-testid="conversation"]')?.getAttribute("data-thread")).toBe("");
      expect(window.location.pathname).toBe("/chat");
      expect(navigation.router.push).toHaveBeenCalledTimes(1);
    },
  );

  it("opens a blank draft when the same button is clicked while ChatPage is mounted", async () => {
    await navigate("/chat/last-chat", true);
    await clickNew();
    const draftId = runtime.getIndexSnapshot().activeSessionId!;
    expect(runtime.getSessionSnapshot(draftId).threadId).toBeNull();
    await navigate("/chat", true);
    expect(container.querySelector('[data-testid="conversation"]')?.getAttribute("data-thread")).toBe("");
    expect(window.location.pathname).toBe("/chat");
  });

  it("keeps a new draft selected across remounts and an older run completing", async () => {
    const previous = runtime.getIndexSnapshot().activeSessionId!;
    let running!: Promise<unknown>;
    await act(async () => { running = runtime.send(previous, { query: "Still generating" }); });
    await navigate("/chat/last-chat", true);
    await clickNew();
    const draftId = runtime.getIndexSnapshot().activeSessionId!;
    expect(runtime.getSessionSnapshot(draftId).threadId).toBeNull();
    await navigate("/knowledge", false);
    await navigate("/chat", true);
    expect(container.querySelector('[data-testid="conversation"]')?.getAttribute("data-session")).toBe(draftId);
    expect(container.querySelector('[data-testid="conversation"]')?.getAttribute("data-thread")).toBe("");
    expect(window.location.pathname).toBe("/chat");
    expect(runtime.getIndexSnapshot().activeRunSessionId).toBe(previous);
    await act(async () => {
      settleRun!({ status: "completed", runId: "test-run", messageId: "saved-last-chat" });
      await running;
    });
    expect(runtime.getSessionSnapshot(previous).run).toBeNull();
    expect(runtime.getIndexSnapshot().activeSessionId).toBe(draftId);
    expect(container.querySelector('[data-testid="conversation"]')?.getAttribute("data-session")).toBe(draftId);
    expect(window.location.pathname).toBe("/chat");
    expect(transport.cancelRun).not.toHaveBeenCalled();
  });

  it("switches drafts while already at /chat without requiring a route change", async () => {
    await navigate("/chat", true);
    await clickNew();
    await navigate("/chat", true);
    const first = runtime.getIndexSnapshot().activeSessionId;
    await clickNew();
    const second = runtime.getIndexSnapshot().activeSessionId;
    expect(second).not.toBe(first);
    expect(container.querySelector('[data-testid="conversation"]')?.getAttribute("data-session")).toBe(second);
    expect(window.location.pathname).toBe("/chat");
    expect(transport.createThread).not.toHaveBeenCalled();
  });

  it("leaves existing history intact and binds the new URL after the first question", async () => {
    await navigate("/chat/last-chat", true);
    const previous = runtime.getIndexSnapshot().activeSessionId!;
    const history = runtime.getSessionSnapshot(previous).messages;
    expect(container.textContent).toContain("Saved answer");
    await clickNew();
    await navigate("/chat", true);
    const draftId = runtime.getIndexSnapshot().activeSessionId!;
    expect(container.textContent).not.toContain("Saved answer");
    let sending!: Promise<unknown>;
    await act(async () => { sending = runtime.send(draftId, { query: "First question" }); });
    expect(transport.createThread).toHaveBeenCalledTimes(1);
    expect(window.location.pathname).toBe("/chat/created-thread");
    expect(runtime.getSessionSnapshot(previous).messages).toEqual(history);
    expect(transport.deleteMessage).not.toHaveBeenCalled();
    await act(async () => {
      settleRun!({ status: "completed", runId: "new-run", messageId: "saved-created-thread" });
      await sending;
    });
    await navigate("/chat/last-chat", true);
    expect(container.textContent).toContain("Saved answer");
    expect(container.querySelector('[data-testid="conversation"]')?.getAttribute("data-session")).toBe(previous);
  });

  it("ignores a late history response from the previous conversation", async () => {
    let resolveHistory!: (page: Awaited<ReturnType<ConversationTransport["listMessages"]>>) => void;
    vi.mocked(transport.listMessages).mockImplementationOnce(() => new Promise((resolve) => { resolveHistory = resolve; }));
    await navigate("/chat/last-chat", true);
    await clickNew();
    await navigate("/chat", true);
    const draftId = runtime.getIndexSnapshot().activeSessionId;
    await act(async () => {
      resolveHistory({ items: [{
        id: "late-answer", thread_id: "last-chat", role: "assistant", content: "Late history",
        citations: [], created_at: "2026-07-12T00:00:00Z",
      }], has_more: false, next_cursor: null });
    });
    expect(runtime.getIndexSnapshot().activeSessionId).toBe(draftId);
    expect(container.textContent).not.toContain("Late history");
    expect(window.location.pathname).toBe("/chat");
  });

  it("creates an initial draft on a fresh /chat visit", async () => {
    await act(async () => root.render(null));
    await act(async () => root.render(<Harness chat={false} />));
    expect(runtime.getIndexSnapshot().activeSessionId).toBeNull();
    await navigate("/chat", true);
    const draftId = runtime.getIndexSnapshot().activeSessionId!;
    expect(runtime.getSessionSnapshot(draftId).threadId).toBeNull();
    expect(container.querySelector('[data-testid="conversation"]')?.getAttribute("data-session")).toBe(draftId);
    expect(transport.createThread).not.toHaveBeenCalled();
  });
});
