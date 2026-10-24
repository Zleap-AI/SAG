/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import messages from "@/messages/en-US.json";
import type { SearchStrategy, SearchStrategyDisabledMap } from "@/lib/retrieval-config";
import { SearchStrategyControl } from "./search-strategy-control";

vi.mock("next/link", () => ({
  default: React.forwardRef<HTMLAnchorElement, React.PropsWithChildren<{ href: string }>>(
    function Link({ children, href, ...props }, ref) { return <a ref={ref} href={href} {...props}>{children}</a>; },
  ),
}));

let root: Root;
let container: HTMLDivElement;
const onValueChange = vi.fn<(value: SearchStrategy) => void>();

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  onValueChange.mockClear();
  Object.defineProperties(HTMLElement.prototype, {
    hasPointerCapture: { configurable: true, value: () => false }, releasePointerCapture: { configurable: true, value: () => undefined },
    setPointerCapture: { configurable: true, value: () => undefined }, scrollIntoView: { configurable: true, value: () => undefined },
  });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals(); });

function pointerEvent(type: string) {
  const event = new MouseEvent(type, { bubbles: true, button: 0 });
  Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: "mouse" } });
  return event;
}
async function open(disabledMap?: SearchStrategyDisabledMap, value: SearchStrategy = "vector", hint?: string) {
  await act(async () => root.render(
    <NextIntlClientProvider locale="en-US" messages={messages}>
      <SearchStrategyControl value={value} defaultValue="vector" onValueChange={onValueChange} disabledMap={disabledMap} hint={hint} />
    </NextIntlClientProvider>,
  ));
  const trigger = container.querySelector<HTMLElement>('[data-testid="search-strategy"]')!;
  await act(async () => trigger.dispatchEvent(pointerEvent("pointerdown")));
  return [...document.body.querySelectorAll<HTMLElement>('[role="menuitem"]')];
}
function item(items: HTMLElement[], label: string) {
  return items.find(candidate => candidate.textContent?.includes(label))!;
}

describe("search strategy dropdown", () => {
  it("lists SAG2 after JEV reranking and marks both as paid", async () => {
    const items = await open();
    const labels = items.map(candidate => candidate.textContent ?? "");
    expect(labels.findIndex(text => text.includes("JEV reranking"))).toBeLessThan(labels.findIndex(text => text.includes("SAG2 evidence retrieval")));
    expect(document.body.querySelector('[data-testid="search-strategy-sag2-paid"]')?.textContent).toBe("Paid");
    expect(document.body.querySelector('[data-testid="search-strategy-multi_jev-paid"]')?.textContent).toBe("Paid");
    expect(item(items, "Fast (vector)").textContent).not.toContain("Paid");
    expect(item(items, "SAG2 evidence retrieval").textContent).toContain("BM25 recall scored over several JEV rounds");
    expect(document.body.querySelector('[data-testid$="-settings"]')).toBeNull();
  });
  it("shows where the current mode came from when a hint is given", async () => {
    await open(undefined, "sag2", "Source default");
    const hint = container.querySelector('[data-testid="search-strategy-hint"]');
    expect(hint?.textContent).toContain("Source default");
    expect(document.body.querySelector('[role="menu"]')?.textContent).toContain("Source default");
    await open(undefined, "sag2");
    expect(container.querySelector('[data-testid="search-strategy-hint"]')).toBeNull();
  });
  it("selects SAG2 when it is enabled", async () => {
    const items = await open();
    const sag2 = item(items, "SAG2 evidence retrieval");
    expect(sag2.getAttribute("aria-disabled")).not.toBe("true");
    await act(async () => { sag2.dispatchEvent(pointerEvent("pointermove")); sag2.dispatchEvent(pointerEvent("pointerup")); sag2.click(); });
    expect(onValueChange).toHaveBeenCalledWith("sag2");
  });
  it.each(["jev_not_configured", "jev_api_key_missing"])("offers the settings page when a JEV mode is disabled for %s", async reason => {
    const items = await open({ sag2: { reason, message: "Add a JEV API key" }, multi_jev: { reason, message: "Add a JEV API key" } });
    const sag2 = item(items, "SAG2 evidence retrieval");
    expect(sag2.getAttribute("aria-disabled")).toBe("true");
    expect(sag2.textContent).toContain("Add a JEV API key");
    const link = document.body.querySelector<HTMLAnchorElement>('[data-testid="search-strategy-sag2-settings"]')!;
    expect(link.getAttribute("href")).toBe("/settings?tab=knowledge&section=jev");
    expect(link.textContent).toContain("Open settings");
    expect(document.body.querySelector('[data-testid="search-strategy-multi_jev-settings"]')).not.toBeNull();
    expect(onValueChange).not.toHaveBeenCalled();
  });
  it.each(["unsupported_backend", "sag2_relational_unsupported", "vector_provider_lacks_lexical"])("does not link to settings for %s", async reason => {
    const items = await open({ sag2: { reason, message: "Not available here" }, multi_jev: { reason, message: "Not available here" } });
    expect(item(items, "SAG2 evidence retrieval").getAttribute("aria-disabled")).toBe("true");
    expect(document.body.querySelector('[data-testid$="-settings"]')).toBeNull();
  });
});
