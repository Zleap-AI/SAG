import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";
import { SidebarProvider } from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  connectDesktopUpdater,
  DesktopUpdateIndicatorView,
  type DesktopUpdateBridge,
  type DesktopUpdateState,
} from "./desktop-update-indicator";

function render(state: DesktopUpdateState) {
  return renderToStaticMarkup(
    <NextIntlClientProvider
      locale="zh-CN"
      timeZone="Asia/Shanghai"
      messages={messages}
    >
      <TooltipProvider>
        <SidebarProvider>
          <DesktopUpdateIndicatorView state={state} onInstall={vi.fn()} />
        </SidebarProvider>
      </TooltipProvider>
    </NextIntlClientProvider>,
  );
}

describe("desktop update indicator", () => {
  it("restores the main-process snapshot without missing newer live events", async () => {
    let listener: ((state: DesktopUpdateState) => void) | undefined;
    let resolveSnapshot: ((state: DesktopUpdateState) => void) | undefined;
    const bridge: DesktopUpdateBridge = {
      getUpdateState: () =>
        new Promise((resolve) => {
          resolveSnapshot = resolve;
        }),
      installUpdate: vi.fn(),
      onUpdateState: (next) => {
        listener = next;
        return vi.fn();
      },
    };
    const states: DesktopUpdateState[] = [];

    const disconnect = connectDesktopUpdater(bridge, (state) => states.push(state));
    listener?.({ status: "downloading", percent: 61 });
    resolveSnapshot?.({ status: "available", version: "1.7.0" });
    await Promise.resolve();

    expect(states).toEqual([{ status: "downloading", percent: 61 }]);
    disconnect();
  });

  it("renders persistent progress while an update downloads", () => {
    const html = render({ status: "downloading", percent: 42.4 });

    expect(html).toContain("正在下载更新 42%");
  });

  it("offers restart installation after the update is downloaded", () => {
    const html = render({ status: "downloaded", version: "1.7.0" });

    expect(html).toContain("重启以更新 1.7.0");
    expect(html).toContain("button");
  });

  it("stays hidden when no actionable update exists", () => {
    expect(render({ status: "idle" })).not.toContain("<button");
    expect(render({ status: "not-available" })).not.toContain("<button");
  });
});
