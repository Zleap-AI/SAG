/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";
import messages from "@/messages/en-US.json";
import type { SearchStrategy } from "@/lib/retrieval-config";
import { RetrievalStatus } from "./retrieval-status";

async function render(stats: Record<string, unknown>, requestedStrategy: SearchStrategy = "multi_jev") {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  const root = createRoot(container);
  await act(async () => root.render(<NextIntlClientProvider locale="en-US" messages={messages}><RetrievalStatus stats={stats} requestedStrategy={requestedStrategy} /></NextIntlClientProvider>));
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

describe("SAG2 execution status", () => {
  const success = {
    requested_strategy: "sag2", effective_strategy: "sag2", fallback_used: false,
    ranking: { method: "jev", fallback_reason: null },
    sag2: { local_ms: 12, jev_ms: 480, snapshot_revision: 3, scope_chunks: 40, hops: [] },
  };
  it("renders JEV usage for a successful run without an execution warning", async () => {
    const text = await render({ ...success, jev_usage: { requests: 3, judgments: 1200, input_tokens: 45678, output_tokens: 90 } }, "sag2");
    expect(text).toBe("JEV scoring this run: 3 requests · 1,200 judgments · 45,678 input tokens");
  });
  it("omits token counts the service did not report", async () => {
    const text = await render({ ...success, jev_usage: { requests: 1, judgments: 8, input_tokens: null, output_tokens: null } }, "sag2");
    expect(text).toBe("JEV scoring this run: 1 requests · 8 judgments");
  });
  it("stays silent without usage data", async () => {
    expect(await render(success, "sag2")).toBe("");
  });
  it("treats a SAG2 coarse fallback like any JEV fallback", async () => {
    const text = await render({ ...success, fallback_used: true, ranking: { method: "jev", fallback_reason: "timeout" } }, "sag2");
    expect(text).toContain("Requested: SAG2 evidence retrieval");
    expect(text).toContain("Actual: Coarse ranking");
    expect(text).toContain("JEV reranking did not complete");
  });
  it("shows the actual mode when SAG2 was executed as another strategy", async () => {
    const text = await render({ requested_strategy: "sag2", effective_strategy: "vector", jev_usage: { requests: 0, judgments: 0, input_tokens: null, output_tokens: null } }, "sag2");
    expect(text).toContain("Requested: SAG2 evidence retrieval");
    expect(text).toContain("Actual: Fast (vector)");
    expect(text).toContain("0 requests · 0 judgments");
  });
});
