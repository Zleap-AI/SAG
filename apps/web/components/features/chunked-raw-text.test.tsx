/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";
import { DetailPanelOutlet, DetailPanelProvider, useDetailPanel } from "./detail-panel";
import { TooltipProvider } from "@/components/ui/tooltip";

const mocks = vi.hoisted(() => ({ getChunk: vi.fn() }));
vi.mock("@/lib/api", () => ({ ApiError: class extends Error {}, api: mocks }));
vi.mock("next/navigation", () => ({ usePathname: () => "/knowledge" }));
vi.mock("@/components/features/app-shell", () => ({ useApp: () => ({ timezone: "UTC" }) }));

let root: Root;
let container: HTMLDivElement;
function OpenChunk() {
  const panel = useDetailPanel();
  return <button onClick={() => panel.open({ kind: "chunk", sourceId: "source-1", chunkId: "chunk-1" })}>Open fixture</button>;
}
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});
async function click(label: string) {
  const button = container.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)!;
  expect(button).not.toBeNull();
  await act(async () => button.click());
}
async function open(content: string) {
  mocks.getChunk.mockResolvedValue({ id: "chunk-1", source_id: "source-1", source_name: "Fixture source", heading: "Fixture", content });
  await act(async () => root.render(
    <NextIntlClientProvider locale="zh-CN" timeZone="UTC" messages={messages}>
      <TooltipProvider>
        <DetailPanelProvider><OpenChunk /><DetailPanelOutlet /></DetailPanelProvider>
      </TooltipProvider>
    </NextIntlClientProvider>,
  ));
  await act(async () => container.querySelector<HTMLButtonElement>("button")!.click());
}
function rawText() {
  return [...container.querySelectorAll("pre")].map(pre => pre.textContent).join("");
}

describe("document detail raw text rendering", () => {
  it.each(["", "短文\n\n末尾空行\n", "\n\nliteral text\n\n"])("preserves short or empty source exactly: %j", async content => {
    await open(content);
    await click(messages.DetailPanel.renderMode.raw);
    expect(container.querySelectorAll("pre")).toHaveLength(1);
    expect(rawText()).toBe(content);
  });

  it("splits a long source while preserving every character across code, formulas and blank lines", async () => {
    const content = `\n\n${"长文段落 ".repeat(1300)}\n\n# Raw heading\n\n`
      + "```html\n<strong>literal code</strong>\n\n$notMath$\n```\n\n"
      + "$$\n\\frac{1}{n + 1}\n\n+ z\n$$\n\n"
      + "\\[\na + b\n\\]\n\n"
      + `${"末尾段落 ".repeat(1300)}\n\n`;
    await open(content);
    await click(messages.DetailPanel.renderMode.raw);
    expect(container.querySelectorAll("pre").length).toBeGreaterThan(1);
    expect(rawText()).toBe(content);
    expect(container.querySelector("pre strong, pre .katex")).toBeNull();
  });

  it("keeps Markdown and HTML literal in raw mode and retains the rendered preview when switched back", async () => {
    const content = "# Heading\n\n**bold** [link](https://example.test)\n\n$A^2$\n\n<img src=x onerror=alert(1)>\n\n<script>alert(1)</script>\n";
    await open(content);
    expect(container.querySelector("h1")?.textContent).toBe("Heading");
    expect(container.querySelector(".katex")).not.toBeNull();
    await click(messages.DetailPanel.renderMode.raw);
    expect(rawText()).toBe(content);
    expect(container.querySelector("pre h1, pre strong, pre a, pre img, pre script, pre .katex")).toBeNull();
    await click(messages.DetailPanel.renderMode.preview);
    expect(container.querySelector("h1")?.textContent).toBe("Heading");
    expect(container.querySelector(".katex")).not.toBeNull();
  });
});
