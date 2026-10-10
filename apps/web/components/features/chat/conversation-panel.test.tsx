/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import english from "@/messages/en-US.json";
import type { ConversationMessage, ConversationSessionSnapshot } from "@/lib/conversation-runtime";
import type { MessageSourceScope, Source } from "@/lib/types";
import { ConversationPanel } from "./conversation-panel";

const state = vi.hoisted(() => ({
  snapshot: null as ConversationSessionSnapshot | null,
  send: vi.fn(), activate: vi.fn(), ensureHistory: vi.fn(), setInput: vi.fn(),
  sources: [] as Source[],
}));
vi.mock("next-intl", () => ({
  useLocale: () => "en-US",
  useTranslations: () => (key: keyof typeof english.Conversation) => english.Conversation[key] ?? key,
}));
vi.mock("@/components/features/app-shell", () => ({
  useApp: () => ({ capabilities: { llm_configured: true }, timezone: "UTC" }),
}));
vi.mock("./conversation-provider", () => ({
  useConversationSession: () => state.snapshot,
  useConversationRuntime: () => ({
    activate: state.activate, ensureHistory: state.ensureHistory, send: state.send,
    getIndexSnapshot: () => ({ activeRunSessionId: null }),
    getSessionSnapshot: () => ({ run: {} }),
  }),
  useConversationComposer: () => ({
    input: "Unsent draft", scoped: [{ id: "composer-source", name: "Composer selection" }],
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

let container: HTMLDivElement;
let root: Root;
const originalScrollIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
beforeEach(() => {
  vi.clearAllMocks();
  state.sources = [];
  state.send.mockResolvedValue({});
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
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

describe("retry source scope", () => {
  it("excludes deleting sources from mentions while keeping the composer selection", async () => {
    state.sources = ["active", "paused", "error", "deleting", "delete_failed"].map((status) => ({
      id: status, name: status, description: "", source_type: "document", connector_kind: "file_upload",
      status, document_count: 0, chunk_count: 0, event_count: 0,
      created_at: "2026-10-09T00:00:00Z", updated_at: "2026-10-09T00:00:00Z",
    } as Source));
    state.snapshot = {
      sessionId: "session-1", agentId: "agent-1", threadId: "thread-1", error: null, run: null,
      history: { status: "ready", requestId: 0, hasMore: false, nextCursor: null, error: null }, messages: [],
    };
    await act(async () => root.render(<ConversationPanel
      sessionId="session-1" avatarNode={null} heroNode={null} emptyTitle="Chat" emptyHint="Ask"
    />));
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
      sessionId: "session-1", agentId: "agent-1", threadId: "thread-1", error: null, run: null,
      history: { status: "ready", requestId: 0, hasMore: false, nextCursor: null, error: null },
      messages: [base, { ...base, id: "answer", role: "assistant", content: "Original answer", sourceScope: null }],
    };
    await act(async () => root.render(<ConversationPanel
      sessionId="session-1" avatarNode={null} heroNode={null} emptyTitle="Chat" emptyHint="Ask"
    />));
    const retry = container.querySelector<HTMLButtonElement>('button[aria-label="Generate answer again"]')!;
    expect(retry).toBeTruthy();
    await act(async () => retry.click());
    expect(state.send).toHaveBeenCalledWith("session-1", expect.objectContaining({
      query: "Original question", sourceScope: expected,
    }));
  });
});
