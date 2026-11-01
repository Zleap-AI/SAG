/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, expect, it, vi } from "vitest";
import messages from "@/messages/zh-CN.json";
import { SourceCreateForm } from "./source-create-form";

vi.mock("@/components/features/app-shell", () => ({ useApp: () => ({ capabilities: {} }) }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
let cleanup: (() => Promise<void>) | undefined;
afterEach(async () => { await cleanup?.(); });
it("offers only two immutable ingestion algorithms and defaults to SAG1", async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  cleanup = async () => { await act(async () => root.unmount()); container.remove(); };
  await act(async () => root.render(<NextIntlClientProvider locale="zh-CN" messages={messages}>
    <SourceCreateForm onCreated={vi.fn()} />
  </NextIntlClientProvider>));
  expect(container.textContent).toContain("事件检索（SAG1）");
  expect(container.textContent).toContain("证据检索（SAG2）");
  const radios = [...container.querySelectorAll<HTMLInputElement>('input[type="radio"]')];
  expect(radios.map(r => r.value)).toEqual(["sag1", "sag2"]);
  expect(radios[0].checked).toBe(true);
  expect(container.textContent).not.toContain("默认检索模式");
});
