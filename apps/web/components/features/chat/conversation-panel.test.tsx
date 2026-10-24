/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import english from "@/messages/en-US.json";
import type { ConversationMessage, ConversationSessionSnapshot } from "@/lib/conversation-runtime";
import { AgentHttpError } from "@/lib/sse";
import type { Agent, Capabilities, MessageSourceScope, Source } from "@/lib/types";
import { ConversationPanel } from "./conversation-panel";

const state = vi.hoisted(() => ({
  snapshot: null as ConversationSessionSnapshot | null,
  send: vi.fn(), activate: vi.fn(), ensureHistory: vi.fn(), setInput: vi.fn(),
  toastError: vi.fn(),
  sources: [] as Source[],
  scoped: [{ id: "composer-source", name: "Composer selection" }],
  capabilities: { llm_configured: true } as Partial<Capabilities>,
  agent: null as Agent | null,
}));
const catalog = english as unknown as Record<string, Record<string, string>>;
vi.mock("next-intl", () => ({
  useLocale: () => "en-US",
  useTranslations: (namespace: string) => (key: string, values?: Record<string, unknown>) =>
    (catalog[namespace]?.[key] ?? key).replace(/\{(\w+)\}/g, (_, name: string) => String(values?.[name] ?? "")),
}));
vi.mock("next/link", () => ({
  default: React.forwardRef<HTMLAnchorElement, React.PropsWithChildren<{ href: string }>>(
    function Link({ children, href, ...props }, ref) { return <a ref={ref} href={href} {...props}>{children}</a>; },
  ),
}));
vi.mock("sonner", () => ({ toast: { error: state.toastError, success: vi.fn() } }));
vi.mock("@/components/features/app-shell", () => ({
  useApp: () => ({ capabilities: state.capabilities, agent: state.agent, timezone: "UTC" }),
}));
vi.mock("./conversation-provider", () => ({
  useConversationSession: () => state.snapshot,
  useConversationRuntime: () => ({
    activate: state.activate, ensureHistory: state.ensureHistory, send: state.send,
    getIndexSnapshot: () => ({ activeRunSessionId: null }),
    getSessionSnapshot: () => ({ run: {} }),
  }),
  useConversationComposer: () => ({
    input: "Unsent draft", scoped: state.scoped,
    images: [], webEnabled: false, setInput: state.setInput,
    setImages: vi.fn(), setScoped: vi.fn(), setWebEnabled: vi.fn(),
  }),
}));
vi.mock("@/lib/api", () => ({ api: { listSources: async () => state.sources } }));
vi.mock("@/components/features/detail-panel", () => ({ useDetailPanel: () => ({ open: vi.fn() }) }));
vi.mock("@/components/features/markdown-content", () => ({
  MarkdownContent: ({ content }: { content: string }) => <div>{content}</div>,
}));
vi.mock("./prompt-preview", () => ({ PromptPreview: () => null }));
vi.mock("./agent-activity-timeline", () => ({ AgentActivityTimeline: () => null }));

const ALL_STRATEGIES = ["vector", "multi_es_fast", "multi", "multi_jev", "sag2"] as const;
const emptySession: ConversationSessionSnapshot = {
  sessionId: "session-1", agentId: "agent-1", threadId: "thread-1", error: null, run: null,
  history: { status: "ready", requestId: 0, hasMore: false, nextCursor: null, error: null }, messages: [],
};
function source(id: string, defaultStrategy: Source["default_search_strategy"] = null, status = "active"): Source {
  return {
    id, name: id, description: "", source_type: "document", connector_kind: "file_upload",
    status, default_search_strategy: defaultStrategy, document_count: 0, chunk_count: 0, event_count: 0,
    created_at: "2026-10-09T00:00:00Z", updated_at: "2026-10-09T00:00:00Z",
  } as Source;
}
function agent(searchStrategy?: string): Agent {
  return {
    id: "agent-1", name: "Agent", avatar: "", created_at: "2026-10-09T00:00:00Z", updated_at: "2026-10-09T00:00:00Z",
    persona: { greeting: "Hi", ...(searchStrategy ? { search_strategy: searchStrategy as Agent["persona"]["search_strategy"] } : {}) },
  };
}
function pointerEvent(type: string) {
  const event = new MouseEvent(type, { bubbles: true, button: 0 });
  Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: "mouse" } });
  return event;
}

let container: HTMLDivElement;
let root: Root;
const originalScrollIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
beforeEach(() => {
  vi.clearAllMocks();
  state.sources = [];
  state.scoped = [{ id: "composer-source", name: "Composer selection" }];
  state.capabilities = { llm_configured: true };
  state.agent = null;
  state.snapshot = emptySession;
  state.send.mockResolvedValue({});
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  Object.defineProperties(HTMLElement.prototype, {
    scrollIntoView: { configurable: true, value: vi.fn() },
    hasPointerCapture: { configurable: true, value: () => false },
    releasePointerCapture: { configurable: true, value: () => undefined },
    setPointerCapture: { configurable: true, value: () => undefined },
  });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  if (originalScrollIntoView) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", originalScrollIntoView);
  else delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
});

async function renderPanel() {
  await act(async () => root.render(<ConversationPanel
    sessionId="session-1" avatarNode={null} heroNode={null} emptyTitle="Chat" emptyHint="Ask"
  />));
}
const trigger = () => container.querySelector<HTMLButtonElement>('[data-testid="search-strategy"]')!;
const hint = () => container.querySelector('[data-testid="search-strategy-hint"]')?.textContent ?? null;
const sendButton = () => container.querySelector<HTMLButtonElement>(`button[title="${english.Conversation.send}"]`)!;
async function pick(label: string) {
  await act(async () => trigger().dispatchEvent(pointerEvent("pointerdown")));
  const item = [...document.body.querySelectorAll<HTMLElement>('[role="menuitem"]')]
    .find((candidate) => candidate.textContent?.includes(label))!;
  await act(async () => { item.dispatchEvent(pointerEvent("pointermove")); item.dispatchEvent(pointerEvent("pointerup")); item.click(); });
}

describe("retry source scope", () => {
  it("excludes deleting sources from mentions while keeping the composer selection", async () => {
    state.sources = ["active", "paused", "error", "deleting", "delete_failed"].map((status) => source(status, null, status));
    await renderPanel();
    const textarea = container.querySelector<HTMLTextAreaElement>("textarea")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(textarea, "@");
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const mentions = [...container.querySelectorAll('[data-idx]')].map((item) => item.textContent?.trim());
    expect(mentions).toEqual(["active", "paused", "error"]);
    expect(container.textContent).toContain("Composer selection");
  });

  it.each([
    { label: "scoped", saved: [{ id: "historical-source", name: "Historical source" }], expected: [{ id: "historical-source", name: "Historical source" }] },
    { label: "default", saved: [], expected: [] },
    { label: "legacy", saved: null, expected: [{ id: "composer-source", name: "Composer selection" }] },
  ])("retries a $label question with its saved selection", async ({ saved, expected }) => {
    const base: ConversationMessage = {
      id: "question", threadId: "thread-1", role: "user", content: "Original question",
      citations: [], attachments: [], steps: [], createdAt: "2026-07-12T00:00:00Z", delivery: "persisted",
      sourceScope: saved as MessageSourceScope[] | null,
    };
    state.snapshot = {
      ...emptySession,
      messages: [base, { ...base, id: "answer", role: "assistant", content: "Original answer", sourceScope: null }],
    };
    await renderPanel();
    const retry = container.querySelector<HTMLButtonElement>('button[aria-label="Generate answer again"]')!;
    expect(retry).toBeTruthy();
    await act(async () => retry.click());
    expect(state.send).toHaveBeenCalledWith("session-1", expect.objectContaining({
      query: "Original question", sourceScope: expected,
    }));
  });
});

describe("composer retrieval mode", () => {
  it("shows the global default without a hint when nothing else applies", async () => {
    state.capabilities = { llm_configured: true, search_strategy: "multi_es_fast" };
    await renderPanel();
    expect(trigger().textContent).toContain("Fast (multi_es-fast)");
    expect(hint()).toBeNull();
    expect(trigger().disabled).toBe(false);
  });

  it("prefers the agent persona mode and says so", async () => {
    state.capabilities = { llm_configured: true, search_strategy: "vector", search_strategies: [...ALL_STRATEGIES] };
    state.agent = agent("sag2");
    state.sources = [source("composer-source", "multi")];
    await renderPanel();
    expect(trigger().textContent).toContain("SAG2 evidence retrieval");
    expect(hint()).toContain("Persona default");
  });

  it("falls back to the shared source default when the persona mode cannot be served", async () => {
    // Without an advertised search_strategies list the JEV modes are unsupported.
    state.capabilities = { llm_configured: true, search_strategy: "vector" };
    state.agent = agent("sag2");
    state.sources = [source("composer-source", "multi")];
    await renderPanel();
    expect(trigger().textContent).toContain("Precise");
    expect(hint()).toContain("Source default");
  });

  it("ignores source defaults that the scoped sources do not share", async () => {
    state.scoped = [{ id: "a", name: "a" }, { id: "b", name: "b" }];
    state.sources = [source("a", "multi"), source("b", "multi_es_fast")];
    await renderPanel();
    expect(trigger().textContent).toContain("Fast (vector)");
    expect(hint()).toBeNull();
  });

  it("omits the mode from the request when nothing is pinned", async () => {
    state.agent = agent("multi");
    await renderPanel();
    await act(async () => sendButton().click());
    expect(state.send).toHaveBeenCalledTimes(1);
    expect(state.send.mock.calls[0][1]).not.toHaveProperty("strategy");
  });

  it("sends the pinned mode and drops the origin hint", async () => {
    state.agent = agent("multi");
    await renderPanel();
    expect(hint()).toContain("Persona default");
    await pick("Fast (multi_es-fast)");
    expect(trigger().textContent).toContain("Fast (multi_es-fast)");
    expect(hint()).toBeNull();
    await act(async () => sendButton().click());
    expect(state.send).toHaveBeenCalledWith("session-1", expect.objectContaining({ strategy: "multi_es_fast" }));
  });

  it("locks the control while a run is in flight", async () => {
    state.snapshot = {
      ...emptySession,
      run: { requestId: "r", runId: "run-1", lifecycle: "running", startedAt: 0, assistantMessageId: "a", steps: [], pendingApproval: null },
    };
    await renderPanel();
    expect(trigger().disabled).toBe(true);
  });

  it("keeps a pre-stream mode rejection above the composer with a settings link", async () => {
    state.send.mockRejectedValue(new AgentHttpError("Add a JEV API key first", "jev_api_key_missing", 400));
    await renderPanel();
    await act(async () => sendButton().click());
    const notice = container.querySelector('[data-testid="strategy-notice"]')!;
    expect(notice.textContent).toContain("Add a JEV API key first");
    expect(notice.getAttribute("data-code")).toBe("jev_api_key_missing");
    expect(container.querySelector('[data-testid="strategy-notice-settings"]')?.getAttribute("href"))
      .toBe("/settings?tab=knowledge&section=jev");
    expect(state.toastError).not.toHaveBeenCalled();
    await pick("Precise");
    expect(container.querySelector('[data-testid="strategy-notice"]')).toBeNull();
  });

  it("still toasts errors that are not about the retrieval mode", async () => {
    state.send.mockRejectedValue(new AgentHttpError("Server exploded", "internal_error", 500));
    await renderPanel();
    await act(async () => sendButton().click());
    expect(container.querySelector('[data-testid="strategy-notice"]')).toBeNull();
    expect(state.toastError).toHaveBeenCalledWith("Server exploded");
  });
});
