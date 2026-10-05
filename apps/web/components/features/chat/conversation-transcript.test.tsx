/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import english from "@/messages/en-US.json";
import { ConversationTranscript, type ConversationTranscriptMessage } from "./conversation-transcript";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: keyof typeof english.Conversation) => english.Conversation[key],
}));
vi.mock("@/components/features/markdown-content", () => ({
  MarkdownContent: ({ content }: { content: string }) => <div>{content}</div>,
}));
vi.mock("./agent-activity-timeline", () => ({ AgentActivityTimeline: () => null }));

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe("conversation source scope", () => {
  it("shows accessible source badges alongside the question during streaming", async () => {
    const messages: ConversationTranscriptMessage[] = [
      { id: "user", role: "user", content: "Question", sourceScope: [
        { id: "source-1", name: "資料庫" }, { id: "source-2", name: "A very long source name" },
      ] },
      { id: "assistant", role: "assistant", content: "" },
    ];
    await act(async () => root.render(<ConversationTranscript
      messages={messages} live={{ messageId: "assistant", streaming: true, steps: [] }}
    />));
    const badges = container.querySelector('ul[aria-label="Selected sources for this question"]')!;
    expect([...badges.querySelectorAll("li")].map((item) => item.textContent)).toEqual([
      "@資料庫", "@A very long source name",
    ]);
    expect(badges.getAttribute("title")).toContain("Selected search scope");
    expect(badges.querySelectorAll("li")[1].getAttribute("title")).toBe("A very long source name");

    await act(async () => root.render(<ConversationTranscript messages={messages} />));
    expect(container.querySelector("li")?.textContent).toBe("@資料庫");
  });

  it("labels a recorded default scope without inventing scope for legacy messages", async () => {
    await act(async () => root.render(<ConversationTranscript messages={[
      { id: "default", role: "user", content: "Default", sourceScope: [] },
      { id: "legacy", role: "user", content: "Legacy", sourceScope: null },
      { id: "absent", role: "user", content: "Old client" },
    ]} />));
    expect(container.textContent?.match(/Default source scope/g)).toHaveLength(1);
    expect(container.querySelector("ul")).toBeNull();
  });

  it("does not label default retrieval sources or citations as explicit selection", async () => {
    await act(async () => root.render(<ConversationTranscript messages={[
      { id: "user", role: "user", content: "Question", sourceScope: [] },
      { id: "assistant", role: "assistant", content: "Answer", steps: [{
        kind: "tool", step: 1, name: "search_context", details: {
          sources: [{ id: "source-1", name: "Default retrieval source" }],
        },
      }] },
    ]} />));
    expect(container.querySelector("ul")).toBeNull();
    expect(container.textContent).not.toContain("@Default retrieval source");
  });
});
