// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";
import { QuickModelSetupDialog } from "./quick-model-setup-dialog";

it("links first-time model configuration to the 302.cn portal", () => {
  render(
    <NextIntlClientProvider locale="zh-CN" messages={messages}>
      <QuickModelSetupDialog open onOpenChange={vi.fn()} onConfigured={vi.fn()} />
    </NextIntlClientProvider>,
  );

  expect(screen.getByRole("link", { name: "获取 API Key" })).toHaveAttribute(
    "href",
    "https://302ai.cn/",
  );
});
