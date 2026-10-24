/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/messages/en-US.json";
import { api, type EvalCompareResponse } from "@/lib/api";
import type { Capabilities, Section } from "@/lib/types";
import { RetrievalTestDialog } from "./retrieval-test-dialog";

const state = vi.hoisted(() => ({ capabilities: {} as Partial<Capabilities> }));
vi.mock("@/components/features/app-shell", () => ({ useApp: () => state }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

function section(chunk: string, score: number): Section {
  return { chunk_id: chunk, heading: `Heading ${chunk}`, content: `Body ${chunk}`, score, rank: 1, source_id: "s1", source_name: "Manuals" };
}
const compareResponse: EvalCompareResponse = {
  query: "question",
  results: [
    { strategy: "vector", sections: [section("c1", 0.8)], stats: { latency_total_ms: 300, candidates: 20, relevant: 5 }, error: null },
    { strategy: "multi_es_fast", sections: [], stats: {}, error: "lexical search unavailable" },
  ],
  judges: [{ a_strategy: "vector", b_strategy: "multi_es_fast", winner: "A", reason: "Only A retrieved anything" }],
  judge_enabled: true,
  judge_reason: null,
};

let root: Root;
let container: HTMLDivElement;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  state.capabilities = { search_strategy: "vector", search_strategies: ["vector", "multi_es_fast", "multi", "multi_jev", "sag2"] };
  Object.defineProperties(HTMLElement.prototype, {
    hasPointerCapture: { configurable: true, value: () => false }, releasePointerCapture: { configurable: true, value: () => undefined },
    setPointerCapture: { configurable: true, value: () => undefined }, scrollIntoView: { configurable: true, value: () => undefined },
  });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function openDialog() {
  await act(async () => root.render(
    <NextIntlClientProvider locale="en-US" messages={messages}>
      <RetrievalTestDialog sourceId="s1" sourceName="Manuals" open onOpenChange={() => {}} />
    </NextIntlClientProvider>,
  ));
  const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')!;
  expect(dialog).not.toBeNull();
  return dialog;
}
async function typeQuery(dialog: HTMLElement, value: string) {
  const input = dialog.querySelector<HTMLInputElement>("#rt-q")!;
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  await act(async () => {
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("retrieval test dialog", () => {
  it("still renders the single-mode form", async () => {
    const dialog = await openDialog();
    expect(dialog.textContent).toContain("Test retrieval · Manuals");
    expect(dialog.querySelector("#rt-q")).not.toBeNull();
    expect(dialog.querySelector("#rt-a-select")).toBeNull();
  });
  it("compares two strategies with the shared columns and verdict list", async () => {
    const evalCompare = vi.spyOn(api, "evalCompare").mockResolvedValue(compareResponse);
    const dialog = await openDialog();
    await act(async () => dialog.querySelector<HTMLButtonElement>("#rt-compare")!.click());
    expect(dialog.querySelector("#rt-a-select")).not.toBeNull();
    expect(dialog.querySelector("#rt-b-select")).not.toBeNull();
    await typeQuery(dialog, "question");
    await act(async () => dialog.querySelector("form")!.requestSubmit());
    expect(evalCompare).toHaveBeenCalledWith({
      query: "question", source_ids: ["s1"], top_k: 8, strategies: ["vector", "multi_es_fast"], judge: true,
    });
    expect(dialog.textContent).toContain("Fast (vector)");
    expect(dialog.textContent).toContain("Heading c1");
    expect(dialog.textContent).toContain("300ms · cand=20 · rel=5");
    expect(dialog.textContent).toContain("lexical search unavailable");
    expect(dialog.querySelector('[data-testid="compare-judges"]')?.textContent).toContain("Only A retrieved anything");
  });
});
