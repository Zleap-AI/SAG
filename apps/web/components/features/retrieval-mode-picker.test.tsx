/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/messages/en-US.json";
import { api } from "@/lib/api";
import { needsJevQuickSetup, type SearchStrategy, type SearchStrategyDisabledMap } from "@/lib/retrieval-config";
import { useJevQuickSetup } from "./jev-quick-setup";
import { RetrievalModePicker } from "./retrieval-mode-picker";

vi.mock("next/link", () => ({
  default: React.forwardRef<HTMLAnchorElement, React.PropsWithChildren<{ href: string }>>(
    function Link({ children, href, ...props }, ref) { return <a ref={ref} href={href} {...props}>{children}</a>; },
  ),
}));
vi.mock("@/components/features/app-shell", () => ({ useApp: () => ({ refreshCapabilities: vi.fn() }) }));

let root: Root;
let container: HTMLDivElement;
const onValueChange = vi.fn<(value: SearchStrategy | null) => void>();

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  onValueChange.mockClear();
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

function Harness({ value, disabledMap, withJevSetup = false }: {
  value: SearchStrategy | null; disabledMap?: SearchStrategyDisabledMap; withJevSetup?: boolean;
}) {
  const jevSetup = useJevQuickSetup({ enabled: needsJevQuickSetup(value, disabledMap) });
  return (
    <RetrievalModePicker
      id="mode" value={value} onValueChange={onValueChange} globalStrategy="vector"
      disabledMap={disabledMap} jevSetup={withJevSetup ? jevSetup : undefined}
    />
  );
}

async function mount(props: React.ComponentProps<typeof Harness>) {
  await act(async () => root.render(
    <NextIntlClientProvider locale="en-US" messages={messages}><Harness {...props} /></NextIntlClientProvider>,
  ));
}
async function openOptions() {
  const trigger = container.querySelector<HTMLElement>('[data-testid="retrieval-mode-picker"]')!;
  await act(async () => trigger.dispatchEvent(pointerEvent("pointerdown")));
  return [...document.body.querySelectorAll<HTMLElement>('[role="option"]')];
}
const description = () => container.querySelector('[data-testid="retrieval-mode-description"]')!.textContent ?? "";

describe("retrieval mode picker", () => {
  it("lists follow-global first with the current default, then every mode with paid badges", async () => {
    await mount({ value: null });
    expect(container.querySelector('[data-testid="retrieval-mode-picker"]')?.textContent).toContain("Follow the global default (currently: Fast (vector))");
    const options = await openOptions();
    expect(options.map(option => option.getAttribute("data-testid"))).toEqual([
      "retrieval-mode-global", "retrieval-mode-vector", "retrieval-mode-multi_es_fast",
      "retrieval-mode-multi", "retrieval-mode-multi_jev", "retrieval-mode-sag2",
    ]);
    expect(document.body.querySelector('[data-testid="retrieval-mode-sag2-paid"]')?.textContent).toBe("Paid");
    expect(document.body.querySelector('[data-testid="retrieval-mode-multi_jev-paid"]')?.textContent).toBe("Paid");
    expect(document.body.querySelector('[data-testid="retrieval-mode-vector-paid"]')).toBeNull();
    const sag2 = options.find(option => option.getAttribute("data-testid") === "retrieval-mode-sag2")!;
    await act(async () => { sag2.dispatchEvent(pointerEvent("pointerdown")); sag2.dispatchEvent(pointerEvent("pointerup")); });
    expect(onValueChange).toHaveBeenCalledWith("sag2");
  });
  it("reports follow-global as null and describes the global mode", async () => {
    await mount({ value: "multi" });
    const global = (await openOptions()).find(option => option.getAttribute("data-testid") === "retrieval-mode-global")!;
    await act(async () => { global.dispatchEvent(pointerEvent("pointerdown")); global.dispatchEvent(pointerEvent("pointerup")); });
    expect(onValueChange).toHaveBeenCalledWith(null);
    await mount({ value: null });
    expect(description()).toContain("uses whatever default is chosen in Settings");
    expect(description()).toContain("Current global default: Fast (vector)");
    expect(description()).toContain("Finds the passages closest in meaning");
  });
  it("shows a plain-language summary, when to use it and the paid note for the selected mode", async () => {
    await mount({ value: "sag2" });
    expect(description()).toContain("searches the original text over several rounds");
    expect(description()).toContain("Higher usage per search, billed by volume");
    expect(container.querySelector('[data-testid="retrieval-mode-paid-note"]')?.textContent).toBe("Paid service · needs a JEV API key");
    await mount({ value: "vector" });
    expect(description()).toContain("Fastest and cheapest");
    expect(container.querySelector('[data-testid="retrieval-mode-paid-note"]')).toBeNull();
  });
  it("disables modes the deployment cannot serve and shows the reason, but keeps key-only blockers selectable", async () => {
    await mount({
      value: null,
      disabledMap: {
        multi_jev: { reason: "vector_provider_lacks_lexical", message: "Needs lexical search" },
        sag2: { reason: "jev_api_key_missing", message: "Add a JEV API key" },
      },
    });
    const options = await openOptions();
    const multiJev = options.find(option => option.getAttribute("data-testid") === "retrieval-mode-multi_jev")!;
    const sag2 = options.find(option => option.getAttribute("data-testid") === "retrieval-mode-sag2")!;
    expect(multiJev.getAttribute("aria-disabled")).toBe("true");
    expect(multiJev.textContent).toContain("Unavailable");
    expect(multiJev.textContent).toContain("Needs lexical search");
    expect(sag2.getAttribute("aria-disabled")).not.toBe("true");
    expect(sag2.textContent).toContain("Key needed");
    expect(sag2.textContent).not.toContain("Add a JEV API key");
  });
  it.each(["jev_not_configured", "jev_api_key_missing"])("renders the inline JEV setup for %s", async reason => {
    await mount({ value: "sag2", withJevSetup: true, disabledMap: { sag2: { reason, message: "Add a JEV API key" } } });
    const setup = container.querySelector('[data-testid="jev-quick-setup"]')!;
    expect(setup).not.toBeNull();
    expect(setup.textContent).toContain("Set up the JEV service first");
    expect(setup.textContent).toContain("Settings → Knowledge → JEV reranking service");
    expect(container.querySelector<HTMLInputElement>("#mode-jev-endpoint")!.value).toBe("https://api.302.ai/typesafeai/v1/systemone");
    expect(container.querySelector<HTMLInputElement>("#mode-jev-endpoint")!.readOnly).toBe(true);
    expect(container.querySelector<HTMLInputElement>("#mode-jev-api-key")!.type).toBe("password");
    expect(container.querySelector<HTMLAnchorElement>('[data-testid="jev-quick-setup-settings"]')!.getAttribute("href")).toBe("/settings?tab=knowledge&section=jev");
    expect(container.querySelector<HTMLButtonElement>('[data-testid="jev-quick-setup-test"]')!.disabled).toBe(true);
  });
  it.each([
    ["sag2" as const, "sag2_relational_unsupported"],
    ["multi_jev" as const, "vector_provider_lacks_lexical"],
    ["sag2" as const, "unsupported_backend"],
  ])("hides the inline setup for %s blocked by %s", async (value, reason) => {
    await mount({ value, withJevSetup: true, disabledMap: { [value]: { reason, message: "Not here" } } });
    expect(container.querySelector('[data-testid="jev-quick-setup"]')).toBeNull();
    expect(description()).toContain("Not here");
  });
  it("hides the inline setup for non-JEV modes and when the service is already configured", async () => {
    await mount({ value: "multi", withJevSetup: true, disabledMap: { sag2: { reason: "jev_api_key_missing", message: "Add a key" } } });
    expect(container.querySelector('[data-testid="jev-quick-setup"]')).toBeNull();
    await mount({ value: "sag2", withJevSetup: true });
    expect(container.querySelector('[data-testid="jev-quick-setup"]')).toBeNull();
  });
});
