/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/messages/en-US.json";
import type { EvalCompareResponse } from "@/lib/api";
import type { Capabilities, Section } from "@/lib/types";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SearchPanel } from "./search-panel";
import { SearchProvider, useSearchWorkspace } from "./search-provider";

const state = vi.hoisted(() => ({ capabilities: {} as Partial<Capabilities> }));
const detail = vi.hoisted(() => ({ open: vi.fn() }));
vi.mock("@/components/features/app-shell", () => ({ useApp: () => state }));
vi.mock("@/components/features/detail-panel", () => ({ useDetailPanel: () => detail }));
vi.mock("next/dynamic", () => ({ default: () => () => null }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("next/link", () => ({
  default: React.forwardRef<HTMLAnchorElement, React.PropsWithChildren<{ href: string }>>(
    function Link({ children, href, ...props }, ref) { return <a ref={ref} href={href} {...props}>{children}</a>; },
  ),
}));

function section(chunk: string, score: number): Section {
  return { chunk_id: chunk, heading: `Heading ${chunk}`, content: `Body ${chunk}`, score, rank: 1, source_id: "src", source_name: "Manuals" };
}
const compareResponse: EvalCompareResponse = {
  query: "question",
  results: [
    { strategy: "multi", sections: [section("c1", 0.8)], stats: { latency_total_ms: 900 }, error: null },
    { strategy: "sag2", sections: [section("c1", 0.9), section("c2", 0.7)], stats: { latency_total_ms: 2100 }, error: null },
  ],
  judges: [],
  judge_enabled: true,
  judge_reason: null,
};

let root: Root;
let container: HTMLDivElement;
let compareRequests: Array<Record<string, unknown>>;
let searchRequests: Array<Record<string, unknown>>;
let workspace: ReturnType<typeof useSearchWorkspace>;

function Capture() {
  workspace = useSearchWorkspace();
  return null;
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  state.capabilities = {
    llm_configured: true, search_strategy: "vector",
    search_strategies: ["vector", "multi_es_fast", "multi", "multi_jev", "sag2"],
  };
  detail.open.mockClear();
  compareRequests = []; searchRequests = [];
  vi.stubGlobal("fetch", async (url: RequestInfo | URL, options?: RequestInit) => {
    const target = String(url);
    if (target.includes("/sources")) return Response.json([]);
    if (target.includes("/activity")) return Response.json([]);
    if (target.includes("/search/eval-compare")) {
      compareRequests.push(JSON.parse(String(options?.body)));
      return Response.json(compareResponse);
    }
    if (target.includes("/search")) {
      searchRequests.push(JSON.parse(String(options?.body)));
      return Response.json({ error: { code: "boom", message: "stopped" } }, { status: 500 });
    }
    return Response.json({});
  });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals(); });

async function render() {
  await act(async () => root.render(
    <NextIntlClientProvider locale="en-US" messages={messages}>
      <TooltipProvider>
        <SearchProvider defaultStrategy="vector">
          <Capture />
          <SearchPanel showRecentActivity={false} showGraphView={false} />
        </SearchProvider>
      </TooltipProvider>
    </NextIntlClientProvider>,
  ));
}
const byTestId = (id: string) => container.querySelector<HTMLElement>(`[data-testid="${id}"]`);
const chip = (strategy: string) => byTestId(`search-compare-chip-${strategy}`) as HTMLButtonElement | null;
const chipState = (strategy: string) => chip(strategy)?.getAttribute("data-state");
async function click(element: HTMLElement | null) {
  expect(element).not.toBeNull();
  await act(async () => element!.click());
}
async function typeQuery(value: string) {
  const input = container.querySelector<HTMLInputElement>('input[type="text"]')!;
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  await act(async () => {
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function submit() {
  await act(async () => container.querySelector("form")!.requestSubmit());
}

describe("search compare mode", () => {
  it("replaces the mode control with 2–3 chips seeded from the current mode", async () => {
    await render();
    expect(byTestId("search-strategy")).not.toBeNull();
    expect(byTestId("search-compare-config")).toBeNull();
    await click(byTestId("search-compare-toggle"));
    expect(byTestId("search-strategy")).toBeNull();
    expect(container.querySelectorAll('[data-testid^="search-compare-chip-"]')).toHaveLength(5);
    expect(chipState("vector")).toBe("on");
    expect(chipState("sag2")).toBe("on");
    expect(chipState("multi")).toBe("off");
    expect(chip("sag2")?.textContent).toContain("Paid");
    expect(chip("vector")?.textContent).not.toContain("Paid");
    expect(byTestId("search-compare-hint")?.textContent).toBe("Pick 2–3 modes to compare side by side with the same question");

    await click(chip("multi"));
    expect(chipState("multi")).toBe("on");
    expect(chip("multi_jev")?.disabled).toBe(true);
    expect(chip("multi_es_fast")?.disabled).toBe(true);
    await click(chip("vector"));
    expect(chipState("vector")).toBe("off");
    expect(chip("multi_jev")?.disabled).toBe(false);
    // Two is the floor: deselecting one of the remaining pair is ignored.
    await click(chip("sag2"));
    expect(chipState("sag2")).toBe("on");
    expect(chipState("multi")).toBe("on");
  });

  it("omits modes the deployment cannot serve", async () => {
    state.capabilities = {
      ...state.capabilities,
      search_strategies_disabled: { sag2: { reason: "jev_api_key_missing", message: "Add a key" } },
    };
    await render();
    await click(byTestId("search-compare-toggle"));
    expect(chip("sag2")).toBeNull();
    expect(chipState("vector")).toBe("on");
    expect(chipState("multi_jev")).toBe("on");
  });

  it("runs eval-compare with the query, chips, scope and judge, then adopts a mode", async () => {
    await render();
    await act(async () => workspace.setScope([{ id: "a", name: "Alpha" }]));
    await click(byTestId("search-compare-toggle"));
    await click(chip("multi"));
    await click(chip("vector"));
    // The scope chips move under the mode chips while comparing.
    expect(byTestId("search-compare-config")?.textContent).toContain("Alpha");
    expect(byTestId("search-compare-judge")?.getAttribute("data-state")).toBe("checked");
    await typeQuery("question");
    await submit();
    expect(compareRequests).toEqual([
      { query: "question", strategies: ["multi", "sag2"], source_ids: ["a"], top_k: 12, judge: true },
    ]);
    expect(searchRequests).toEqual([]);
    const results = byTestId("search-compare-results")!;
    expect(results.querySelector('[data-testid="compare-column-multi"]')).not.toBeNull();
    expect(results.querySelector('[data-testid="compare-column-sag2"]')).not.toBeNull();
    expect(results.querySelectorAll('[data-testid="compare-common-hit"]')).toHaveLength(2);

    await click(byTestId("compare-adopt-sag2"));
    expect(byTestId("search-compare-config")).toBeNull();
    expect(workspace.strategy).toBe("sag2");
    expect(workspace.strategyOrigin).toBe("pinned");
    expect(searchRequests).toEqual([
      expect.objectContaining({ query: "question", strategy: "sag2", source_ids: ["a"] }),
    ]);
    expect(byTestId("search-strategy")?.textContent).toContain("SAG2 evidence retrieval");
  });

  it("hides the judge switch without an LLM and sends judge=false", async () => {
    state.capabilities = { ...state.capabilities, llm_configured: false };
    await render();
    await click(byTestId("search-compare-toggle"));
    expect(byTestId("search-compare-judge")).toBeNull();
    expect(byTestId("search-compare-config")?.textContent).toContain("Scope: all knowledge");
    await typeQuery("question");
    await submit();
    expect(compareRequests).toEqual([
      { query: "question", strategies: ["vector", "sag2"], top_k: 12, judge: false },
    ]);
  });

  it("keeps the last normal result while compare is on and restores it when off", async () => {
    await render();
    await typeQuery("question");
    await submit();
    expect(searchRequests).toHaveLength(1);
    expect(container.textContent).toContain("Search did not complete");
    await click(byTestId("search-compare-toggle"));
    expect(container.textContent).not.toContain("Search did not complete");
    expect(container.textContent).toContain("Enter a question and press Enter to compare");
    await click(byTestId("search-compare-toggle"));
    expect(container.textContent).toContain("Search did not complete");
    expect(searchRequests).toHaveLength(1);
  });

  it("opens a compared passage through the detail panel", async () => {
    await render();
    await click(byTestId("search-compare-toggle"));
    await typeQuery("question");
    await submit();
    const passage = [...container.querySelectorAll<HTMLButtonElement>('[data-testid="compare-column-sag2"] button')]
      .find((button) => button.textContent?.includes("Heading c2"))!;
    await click(passage);
    expect(detail.open).toHaveBeenCalledWith({
      kind: "chunk", sourceId: "src", chunkId: "c2", heading: "Heading c2", sourceName: "Manuals",
    });
  });
});
