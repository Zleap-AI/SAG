/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";
import messages from "@/messages/en-US.json";
import { RetrievalStatus } from "./retrieval-status";

async function render(stats: Record<string, unknown>) {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  const root = createRoot(container);
  await act(async () => root.render(<NextIntlClientProvider locale="en-US" messages={messages}><RetrievalStatus stats={stats} requestedStrategy="multi_jev" /></NextIntlClientProvider>));
  const text = container.textContent;
  await act(async () => root.unmount());
  return text;
}

describe("retrieval execution status", () => {
  it("keeps successful JEV results free of fallback warnings", async () => {
    expect(await render({ requested_strategy: "multi_jev", effective_strategy: "multi_jev", ranking: { method: "jev", fallback_reason: null } })).toBe("");
  });
  it("does not call a same-profile coarse fallback successful JEV reranking", async () => {
    const text = await render({ requested_strategy: "multi_jev", effective_strategy: "multi_jev", fallback_used: true, ranking: { method: "jev", fallback_reason: "timeout" } });
    expect(text).toContain("JEV reranking did not complete");
    expect(text).toContain("coarse ranking");
  });
  it("shows actual execution separately from the requested mode", async () => {
    const text = await render({ requested_strategy: "multi_jev", effective_strategy: "vector" });
    expect(text).toContain("Requested: JEV reranking");
    expect(text).toContain("Actual: Fast (vector)");
  });
  it("surfaces per-source fallback and partial results", async () => {
    const text = await render({ partial: true, source_stats: { first: { ranking: { method: "jev", fallback_reason: "service_error" } } } });
    expect(text).toContain("JEV reranking did not complete");
    expect(text).toContain("Partial results");
  });
});
