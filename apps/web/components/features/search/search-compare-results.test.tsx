/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/messages/en-US.json";
import type { EvalCompareResponse, EvalStrategyResult } from "@/lib/api";
import type { Section } from "@/lib/types";
import {
  countChunkColumns,
  SearchCompareResults,
  type SearchCompareResultsProps,
} from "./search-compare-results";

function section(chunk: string, score: number): Section {
  return {
    chunk_id: chunk, heading: `Heading ${chunk}`, content: `Body ${chunk}`, score, rank: 1,
    source_id: "src", source_name: "Manuals",
  };
}

const vector: EvalStrategyResult = {
  strategy: "vector",
  sections: [section("c1", 0.9), section("c2", 0.8)],
  stats: { latency_total_ms: 1234, requested_strategy: "vector", effective_strategy: "vector" },
  error: null,
};
const sag2: EvalStrategyResult = {
  strategy: "sag2",
  sections: [section("c1", 0.95), section("c3", 0.7)],
  stats: {
    latency_total_ms: 2500, requested_strategy: "sag2", effective_strategy: "sag2",
    jev_usage: { requests: 3, judgments: 12, input_tokens: 4096, output_tokens: null },
    sag2: { jev_ms: 1800, local_ms: 700, scope_chunks: 40, hops: 2 },
  },
  error: null,
};
const failed: EvalStrategyResult = { strategy: "multi", sections: [], stats: {}, error: "boom" };

const data: EvalCompareResponse = {
  query: "question",
  results: [vector, sag2, failed],
  judges: [{ a_strategy: "vector", b_strategy: "sag2", winner: "B", reason: "More complete" }],
  judge_enabled: true,
  judge_reason: null,
};

let root: Root;
let container: HTMLDivElement;
const onAdopt = vi.fn();
const onOpenSection = vi.fn();
const onRetry = vi.fn();

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  onAdopt.mockClear(); onOpenSection.mockClear(); onRetry.mockClear();
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });

async function render(props: Partial<SearchCompareResultsProps> = {}) {
  await act(async () => root.render(
    <NextIntlClientProvider locale="en-US" messages={messages}>
      <SearchCompareResults
        data={data} busy={false} pendingStrategies={[]} error={null} compact={false}
        onAdopt={onAdopt} onOpenSection={onOpenSection} onRetry={onRetry}
        {...props}
      />
    </NextIntlClientProvider>,
  ));
  return container;
}
const column = (strategy: string) => container.querySelector<HTMLElement>(`[data-testid="compare-column-${strategy}"]`)!;
const metrics = (strategy: string) => column(strategy).querySelector('[data-testid="compare-metrics"]')?.textContent ?? "";

describe("search compare results", () => {
  it("renders one column per mode with a readable metrics strip", async () => {
    await render();
    expect(metrics("vector")).toBe("1.2 s·2 hits");
    expect(metrics("sag2")).toBe("2.5 s·2 hits·JEV 3 requests · 12 judgments · 4,096 input tokens");
    expect(column("sag2").textContent).toContain("SAG2 evidence retrieval");
    expect(column("sag2").textContent).toContain("Paid");
    expect(column("vector").textContent).not.toContain("Paid");
  });
  it("shows a failed mode as a muted alert and does not offer to adopt it", async () => {
    await render();
    expect(column("multi").querySelector('[role="alert"]')?.textContent).toBe("boom");
    expect(column("multi").querySelector<HTMLButtonElement>('[data-testid="compare-adopt-multi"]')?.disabled).toBe(true);
    expect(metrics("multi")).toBe("0 hits");
  });
  it("marks passages that two or more modes retrieved", async () => {
    await render();
    const markers = [...container.querySelectorAll<HTMLElement>('[data-testid="compare-common-hit"]')];
    expect(markers).toHaveLength(2);
    expect(column("vector").querySelectorAll('[data-testid="compare-common-hit"]')).toHaveLength(1);
    expect(column("sag2").querySelectorAll('[data-testid="compare-common-hit"]')).toHaveLength(1);
    expect(countChunkColumns(data.results).get("c1")).toBe(2);
    expect(countChunkColumns(data.results).get("c2")).toBe(1);
  });
  it("adopts a mode and opens a passage through the callbacks", async () => {
    await render();
    await act(async () => column("vector").querySelector<HTMLButtonElement>('[data-testid="compare-adopt-vector"]')!.click());
    expect(onAdopt).toHaveBeenCalledWith("vector");
    const rows = column("sag2").querySelectorAll<HTMLButtonElement>("button");
    const passage = [...rows].find((button) => button.textContent?.includes("Heading c3"))!;
    await act(async () => passage.click());
    expect(onOpenSection).toHaveBeenCalledWith(sag2.sections[1], 1, sag2);
  });
  it("lists the pairwise verdicts, or the reason the judge did not run", async () => {
    await render();
    const judges = container.querySelector('[data-testid="compare-judges"]')!;
    expect(judges.textContent).toContain("SAG2 evidence retrieval");
    expect(judges.textContent).toContain("More complete");
    await render({ data: { ...data, judges: [], judge_enabled: false, judge_reason: "LLM not configured" } });
    expect(container.querySelector('[data-testid="compare-judges"]')?.textContent).toBe("LLM not configured");
    await render({ data: { ...data, judges: [], judge_reason: null } });
    expect(container.querySelector('[data-testid="compare-judges"]')?.textContent).toBe("No judge verdict was produced");
  });
  it("explains a changed execution under the column header", async () => {
    const fallback: EvalStrategyResult = {
      ...sag2,
      stats: { ...sag2.stats, effective_strategy: "vector", fallback_used: true },
    };
    await render({ data: { ...data, results: [vector, fallback] } });
    expect(column("sag2").textContent).toContain("Requested: SAG2 evidence retrieval");
    expect(column("sag2").textContent).toContain("Actual: Coarse ranking");
    expect(column("vector").querySelector('[role="status"]')).toBeNull();
  });
  it("draws a busy column per pending mode and a retry when the whole request failed", async () => {
    await render({ data: null, busy: true, pendingStrategies: ["vector", "sag2"] });
    expect(container.querySelectorAll('[aria-busy="true"]')).toHaveLength(2);
    expect(column("sag2").textContent).toContain("SAG2 evidence retrieval");
    await render({ data: null, error: "service down" });
    expect(container.textContent).toContain("Comparison failed");
    expect(container.textContent).toContain("service down");
    const retry = [...container.querySelectorAll("button")].find((button) => button.textContent === "Retry")!;
    await act(async () => retry.click());
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
