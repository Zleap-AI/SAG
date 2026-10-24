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
function pointerEvent(type: string) {
  const event = new MouseEvent(type, { bubbles: true, button: 0 });
  Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: "mouse" } });
  return event;
}
async function type(input: HTMLInputElement | HTMLTextAreaElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function pickMode(testId: string) {
  const trigger = container.querySelector<HTMLElement>('[data-testid="retrieval-mode-picker"]')!;
  await act(async () => trigger.dispatchEvent(pointerEvent("pointerdown")));
  const option = document.body.querySelector<HTMLElement>(`[data-testid="${testId}"]`)!;
  expect(option, `option ${testId}`).not.toBeNull();
  await act(async () => { option.dispatchEvent(pointerEvent("pointerdown")); option.dispatchEvent(pointerEvent("pointerup")); });
}
const nameInput = () => container.querySelector<HTMLInputElement>('input[maxlength="200"]')!;
const submitButton = () => container.querySelector<HTMLButtonElement>('button[type="submit"]')!;
async function submit() {
  await act(async () => container.querySelector("form")!.requestSubmit());
}

describe("source create form", () => {
  it("creates a source that follows the global default without sending a strategy", async () => {
    await mount();
    await type(nameInput(), "Manuals");
    expect(container.querySelector('[data-testid="jev-quick-setup"]')).toBeNull();
    await submit();
    expect(api.createSource).toHaveBeenCalledWith({ name: "Manuals", description: "" });
    expect(api.saveModelConfig).not.toHaveBeenCalled();
    expect(onCreated).toHaveBeenCalledWith(expect.objectContaining({ id: "source-1" }));
  });
  it("sends the chosen default retrieval mode", async () => {
    await mount();
    await type(nameInput(), "Manuals");
    await pickMode("retrieval-mode-multi");
    await submit();
    expect(api.createSource).toHaveBeenCalledWith({ name: "Manuals", description: "", default_search_strategy: "multi" });
    expect(api.saveModelConfig).not.toHaveBeenCalled();
  });
  it("blocks creation until a JEV key is entered, then saves the service before creating", async () => {
    await mount();
    await type(nameInput(), "Manuals");
    await pickMode("retrieval-mode-sag2");
    expect(container.querySelector('[data-testid="jev-quick-setup"]')).not.toBeNull();
    expect(submitButton().disabled).toBe(true);
    expect(container.querySelector('[data-testid="source-form-blocked"]')?.textContent).toBe("Enter a JEV API key to save this default mode.");

    await type(container.querySelector<HTMLInputElement>('input[type="password"]')!, "jev-secret");
    expect(submitButton().disabled).toBe(false);
    expect(container.querySelector('[data-testid="source-form-blocked"]')).toBeNull();

    await submit();
    expect(api.saveModelConfig).toHaveBeenCalledWith({
      jev_endpoint: "https://api.302.ai/typesafeai/v1/systemone", jev_model: "jev-latest", jev_api_key: "jev-secret",
    });
    expect(state.refreshCapabilities).toHaveBeenCalledTimes(1);
    expect(api.createSource).toHaveBeenCalledWith({ name: "Manuals", description: "", default_search_strategy: "sag2" });
    expect(vi.mocked(api.saveModelConfig).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(api.createSource).mock.invocationCallOrder[0]);
  });
  it("keeps a saved custom endpoint instead of replacing it with the preset", async () => {
    vi.mocked(api.getModelConfig).mockResolvedValue({ jev_endpoint: "https://jev.internal/score", jev_model: "jev-pinned" } as never);
    await mount();
    await type(nameInput(), "Manuals");
    await pickMode("retrieval-mode-multi_jev");
    const endpoint = container.querySelector<HTMLInputElement>('input[type="url"]')!;
    expect(endpoint.value).toBe("https://jev.internal/score");
    expect(endpoint.readOnly).toBe(false);
    await type(container.querySelector<HTMLInputElement>('input[type="password"]')!, "k");
    await submit();
    expect(api.saveModelConfig).toHaveBeenCalledWith({ jev_endpoint: "https://jev.internal/score", jev_model: "jev-pinned", jev_api_key: "k" });
  });
  it("shows a JEV save failure inline and does not create the source", async () => {
    vi.mocked(api.saveModelConfig).mockRejectedValue(new ApiError(400, "jev_invalid", "JEV rejected the key"));
    await mount();
    await type(nameInput(), "Manuals");
    await pickMode("retrieval-mode-sag2");
    await type(container.querySelector<HTMLInputElement>('input[type="password"]')!, "bad");
    await submit();
    expect(container.querySelector('[data-testid="jev-quick-setup-error"]')?.textContent).toBe("JEV rejected the key");
    expect(api.createSource).not.toHaveBeenCalled();
    expect(onCreated).not.toHaveBeenCalled();
    expect(submitButton().disabled).toBe(false);
  });
  it("shows a backend rejection of the default mode inline", async () => {
    vi.mocked(api.createSource).mockRejectedValue(new ApiError(400, "vector_provider_lacks_lexical", "Lexical search is required"));
    await mount();
    await type(nameInput(), "Manuals");
    await pickMode("retrieval-mode-multi");
    await submit();
    expect(container.querySelector('[data-testid="source-form-error"]')?.textContent).toBe("Lexical search is required");
    expect(onCreated).not.toHaveBeenCalled();
  });
});
