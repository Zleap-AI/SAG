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

function pointerEvent(type: string) {
  const event = new MouseEvent(type, { bubbles: true, button: 0 });
  Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: "mouse" } });
  return event;
}
async function openDialog() {
  await act(async () => root.render(
    <NextIntlClientProvider locale="en-US" messages={messages}>
      <TooltipProvider><EditSourceDialog source={source} onUpdated={onUpdated} /></TooltipProvider>
    </NextIntlClientProvider>,
  ));
  await act(async () => container.querySelector("button")!.click());
  const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')!;
  expect(dialog).not.toBeNull();
  return dialog;
}
async function pickMode(testId: string) {
  const trigger = document.body.querySelector<HTMLElement>('[data-testid="retrieval-mode-picker"]')!;
  await act(async () => trigger.dispatchEvent(pointerEvent("pointerdown")));
  const option = document.body.querySelector<HTMLElement>(`[data-testid="${testId}"]`)!;
  expect(option, `option ${testId}`).not.toBeNull();
  await act(async () => { option.dispatchEvent(pointerEvent("pointerdown")); option.dispatchEvent(pointerEvent("pointerup")); });
}

describe("edit source dialog", () => {
  it("preselects the saved default mode and sends it unchanged", async () => {
    const dialog = await openDialog();
    expect(dialog.textContent).toContain("name, description and default retrieval mode");
    expect(dialog.querySelector('[data-testid="retrieval-mode-picker"]')?.textContent).toContain("Precise");
    await act(async () => dialog.querySelector("form")!.requestSubmit());
    expect(api.updateSource).toHaveBeenCalledWith("source-1", { name: "Manuals", description: "Specs", default_search_strategy: "multi" });
    expect(onUpdated).toHaveBeenCalledWith(expect.objectContaining({ default_search_strategy: "multi" }));
  });
  it("sends null when switching back to the global default", async () => {
    const dialog = await openDialog();
    await pickMode("retrieval-mode-global");
    await act(async () => dialog.querySelector("form")!.requestSubmit());
    expect(api.updateSource).toHaveBeenCalledWith("source-1", { name: "Manuals", description: "Specs", default_search_strategy: null });
  });
  it("blocks saving a JEV mode until the key is entered", async () => {
    state.capabilities = {
      ...state.capabilities,
      search_strategies_disabled: { sag2: { reason: "jev_not_configured", message: "Configure JEV" } },
    };
    const dialog = await openDialog();
    await pickMode("retrieval-mode-sag2");
    expect(dialog.querySelector('[data-testid="jev-quick-setup"]')).not.toBeNull();
    expect(dialog.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(true);
    expect(dialog.querySelector('[data-testid="source-form-blocked"]')).not.toBeNull();
  });
});
