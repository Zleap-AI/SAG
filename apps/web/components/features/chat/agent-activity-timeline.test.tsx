/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";
import english from "@/messages/en-US.json";
import { AgentActivityTimeline, type AgentActivityStep } from "./agent-activity-timeline";

async function render(step: AgentActivityStep) {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  const root = createRoot(container);
  await act(async () => root.render(<NextIntlClientProvider locale="en-US" messages={english}><AgentActivityTimeline steps={[step]} /></NextIntlClientProvider>));
  const text = container.textContent;
  await act(async () => root.unmount());
  return text;
}
const retrieval_stats = { requested_strategy: "multi_jev", effective_strategy: "multi_jev", fallback_used: true, ranking: { method: "jev", fallback_reason: "synthetic_provider_failure" } };

describe("chat retrieval execution status", () => {
  it("shows coarse JEV fallback even when the activity trace is collapsed", async () => {
    const text = await render({ kind: "tool", step: 1, name: "search_context", status: "done", details: { retrieval_stats } });
    expect(text).toContain("JEV reranking did not complete");
    expect(text).toContain("Actual: Coarse ranking");
    expect(text).not.toContain("synthetic_provider_failure");
  });
  it("leaves successful JEV traces free of fallback notices", async () => {
    const text = await render({ kind: "tool", step: 1, name: "search_context", status: "done", details: { retrieval_stats: { requested_strategy: "multi_jev", effective_strategy: "multi_jev", ranking: { method: "jev", fallback_reason: null } } } });
    expect(text).not.toContain("JEV reranking did not complete");
  });
});
