/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, expect, it, vi } from "vitest";
import messages from "@/messages/en-US.json";
import { api } from "@/lib/api";
import { SearchStrategyControl } from "./search-strategy-control";

vi.mock("@/components/features/app-shell", () => ({ useApp: () => ({ refreshCapabilities: vi.fn(async () => {}) }) }));
vi.mock("next/link", () => ({ default: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => <a {...props} /> }));
let cleanup: (() => Promise<void>) | undefined;
afterEach(async () => { await cleanup?.(); vi.restoreAllMocks(); });
it("opens JEV setup for an unconfigured paid mode without changing the current mode", async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.spyOn(api, "getModelConfig").mockResolvedValue({ jev_endpoint: null, jev_model: "jev-latest" } as never);
  Object.defineProperties(HTMLElement.prototype, {
    hasPointerCapture: { configurable: true, value: () => false }, releasePointerCapture: { configurable: true, value: () => {} },
    setPointerCapture: { configurable: true, value: () => {} }, scrollIntoView: { configurable: true, value: () => {} },
  });
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container); const change = vi.fn();
  cleanup = async () => { await act(async () => root.unmount()); container.remove(); };
  await act(async () => root.render(<NextIntlClientProvider locale="en-US" messages={messages}>
    <SearchStrategyControl value="vector" defaultValue="vector" onValueChange={change}
      disabledMap={{ sag2: { reason: "jev_api_key_missing", message: "JEV key required" } }} />
  </NextIntlClientProvider>));
  function pointer(type: string) {
    const event = new MouseEvent(type, { bubbles: true, button: 0 });
    Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: "mouse" } }); return event;
  }
  await act(async () => container.querySelector("button")!.dispatchEvent(pointer("pointerdown")));
  const option = document.querySelector('[data-testid="search-strategy-sag2-paid"]')!.closest<HTMLElement>('[role="menuitem"]')!;
  expect(option.getAttribute("aria-disabled")).not.toBe("true");
  await act(async () => option.click());
  expect(document.querySelector('[role="dialog"]')).not.toBeNull();
  expect(document.querySelector('input[type="password"]')).not.toBeNull();
  expect(change).not.toHaveBeenCalled();
  const input = document.querySelector<HTMLInputElement>('input[type="password"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "synthetic-key");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  const save = [...document.querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent?.includes("Save and use"))!;
  expect(save.disabled).toBe(false);
  const request = vi.spyOn(api, "saveModelConfig").mockRejectedValueOnce(new Error("Save rejected"));
  await act(async () => save.click());
  expect(change).not.toHaveBeenCalled();
  expect(document.querySelector('[role="dialog"]')).not.toBeNull();
  request.mockResolvedValueOnce({ config: {}, capabilities: {} } as never);
  await act(async () => save.click());
  expect(change).toHaveBeenCalledWith("sag2");
  expect(document.querySelector('[role="dialog"]')).toBeNull();
});
