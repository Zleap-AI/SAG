/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/messages/en-US.json";
import { api } from "@/lib/api";
import type { Capabilities, Source } from "@/lib/types";
import { TooltipProvider } from "@/components/ui/tooltip";
import { EditSourceDialog } from "./edit-source-dialog";

const state = vi.hoisted(() => ({
  capabilities: {} as Partial<Capabilities>,
  refreshCapabilities: vi.fn(async () => {}),
}));
vi.mock("@/components/features/app-shell", () => ({ useApp: () => state }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("next/link", () => ({
  default: React.forwardRef<HTMLAnchorElement, React.PropsWithChildren<{ href: string }>>(
    function Link({ children, href, ...props }, ref) { return <a ref={ref} href={href} {...props}>{children}</a>; },
  ),
}));

const source: Source = {
  id: "source-1", name: "Manuals", description: "Specs", source_type: "document", connector_kind: "file_upload",
  status: "active", default_search_strategy: "multi", document_count: 0, chunk_count: 0, event_count: 0,
  created_at: "2026-10-24T00:00:00Z", updated_at: "2026-10-24T00:00:00Z",
};

let root: Root;
let container: HTMLDivElement;
let onUpdated: ReturnType<typeof vi.fn<(source: Source) => void>>;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  state.capabilities = { search_strategy: "vector", search_strategies: ["vector", "multi_es_fast", "multi", "multi_jev", "sag2"] };
  onUpdated = vi.fn();
  vi.spyOn(api, "updateSource").mockImplementation(async (_id, body) => ({ ...source, ...body }) as Source);
  vi.spyOn(api, "getModelConfig").mockResolvedValue({ jev_endpoint: null, jev_model: "jev-latest" } as never);
  Object.defineProperties(HTMLElement.prototype, {
    hasPointerCapture: { configurable: true, value: () => false }, releasePointerCapture: { configurable: true, value: () => undefined },
    setPointerCapture: { configurable: true, value: () => undefined }, scrollIntoView: { configurable: true, value: () => undefined },
  });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function openDialog(currentSource: Source = source) {
  await act(async () => root.render(
    <NextIntlClientProvider locale="en-US" messages={messages}>
      <TooltipProvider><EditSourceDialog source={currentSource} onUpdated={onUpdated} /></TooltipProvider>
    </NextIntlClientProvider>,
  ));
  await act(async () => container.querySelector("button")!.click());
  const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')!;
  expect(dialog).not.toBeNull();
  return dialog;
}
describe("edit source dialog", () => {
  it.each(["sag1", "sag2"] as const)("shows %s as read-only and only updates metadata", async kind => {
    const current = { ...source, engine_kind: kind };
    const dialog = await openDialog(current);
    expect(dialog.textContent).toContain("cannot be changed");
    expect(dialog.querySelector('select, [role="combobox"], input[type="radio"]')).toBeNull();
    await act(async () => dialog.querySelector("form")!.requestSubmit());
    expect(api.updateSource).toHaveBeenCalledWith("source-1", { name: "Manuals", description: "Specs" });
  });
  it("allows editing metadata when the JEV connection is missing", async () => {
    state.capabilities = { search_strategy: "vector", search_strategies_disabled: {
      sag2: { reason: "jev_api_key_missing", message: "Configure JEV" },
    }};
    const dialog = await openDialog({ ...source, engine_kind: "sag2" });
    expect(dialog.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(false);
    await act(async () => dialog.querySelector("form")!.requestSubmit());
    expect(api.updateSource).toHaveBeenCalledTimes(1);
  });
});
