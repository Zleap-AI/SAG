/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/messages/en-US.json";
import { api, ApiError } from "@/lib/api";
import type { Capabilities, Source } from "@/lib/types";
import { SourceCreateForm } from "./source-create-form";

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

const created: Source = {
  id: "source-1", name: "Manuals", description: "", source_type: "document", connector_kind: "file_upload",
  status: "active", default_search_strategy: null, document_count: 0, chunk_count: 0, event_count: 0,
  created_at: "2026-10-24T00:00:00Z", updated_at: "2026-10-24T00:00:00Z",
};
const keyMissing = { reason: "jev_api_key_missing", message: "Add a JEV API key" };

let root: Root;
let container: HTMLDivElement;
let onCreated: ReturnType<typeof vi.fn<(source: Source) => void>>;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  state.capabilities = {
    search_strategy: "vector", search_strategies: ["vector", "multi_es_fast", "multi", "multi_jev", "sag2"],
    search_strategies_disabled: { multi_jev: keyMissing, sag2: keyMissing },
  };
  state.refreshCapabilities.mockClear();
  onCreated = vi.fn();
  vi.spyOn(api, "createSource").mockImplementation(async body => ({ ...created, ...body }) as Source);
  vi.spyOn(api, "saveModelConfig").mockResolvedValue({} as never);
  vi.spyOn(api, "getModelConfig").mockResolvedValue({ jev_endpoint: null, jev_model: "jev-latest" } as never);
  Object.defineProperties(HTMLElement.prototype, {
    hasPointerCapture: { configurable: true, value: () => false }, releasePointerCapture: { configurable: true, value: () => undefined },
    setPointerCapture: { configurable: true, value: () => undefined }, scrollIntoView: { configurable: true, value: () => undefined },
  });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function mount() {
  await act(async () => root.render(
    <NextIntlClientProvider locale="en-US" messages={messages}><SourceCreateForm onCreated={onCreated} /></NextIntlClientProvider>,
  ));
}
async function type(input: HTMLInputElement | HTMLTextAreaElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
const nameInput = () => container.querySelector<HTMLInputElement>('input[maxlength="200"]')!;
const submitButton = () => container.querySelector<HTMLButtonElement>('button[type="submit"]')!;
async function submit() {
  await act(async () => container.querySelector("form")!.requestSubmit());
}

describe("source create form", () => {
  it("creates a SAG1 source by default without changing model settings", async () => {
    await mount(); await type(nameInput(), "Manuals"); await submit();
    expect(api.createSource).toHaveBeenCalledWith({ name: "Manuals", description: "", engine_kind: "sag1" });
    expect(api.saveModelConfig).not.toHaveBeenCalled();
    expect(onCreated).toHaveBeenCalledWith(expect.objectContaining({ id: "source-1" }));
  });
  it("can create a SAG2 source without a JEV key because indexing does not use JEV", async () => {
    await mount(); await type(nameInput(), "Evidence");
    await act(async () => container.querySelector<HTMLInputElement>('input[value="sag2"]')!.click());
    expect(submitButton().disabled).toBe(false);
    await submit();
    expect(api.createSource).toHaveBeenCalledWith({ name: "Evidence", description: "", engine_kind: "sag2" });
    expect(api.saveModelConfig).not.toHaveBeenCalled();
    expect(container.querySelector('[data-testid="jev-quick-setup"]')).toBeNull();
  });
  it("does not report creation success after an API failure", async () => {
    vi.mocked(api.createSource).mockRejectedValue(new ApiError(400, "invalid_source", "Source rejected"));
    await mount(); await type(nameInput(), "Manuals"); await submit();
    expect(onCreated).not.toHaveBeenCalled();
    expect(submitButton().disabled).toBe(false);
  });
});
