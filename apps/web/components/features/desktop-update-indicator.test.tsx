/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";
import { SidebarProvider } from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  connectDesktopUpdater,
  DesktopUpdateIndicatorView,
  DesktopUpdateIndicator,
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
          <DesktopUpdateIndicatorView state={state} onInstall={vi.fn()} onDownload={vi.fn()} />
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
      downloadUpdate: vi.fn(),
      installUpdate: vi.fn(),
      onUpdateState: (next) => {
        listener = next;
        return vi.fn();
      },
    };
    const states: DesktopUpdateState[] = [];

    const disconnect = connectDesktopUpdater(bridge, (state) => states.push(state));
    listener?.({ status: "downloading", version: "1.7.0", percent: 61 });
    resolveSnapshot?.({ status: "available", version: "1.7.0" });
    await Promise.resolve();

    expect(states).toEqual([{ status: "downloading", version: "1.7.0", percent: 61 }]);
    disconnect();
  });

  it("renders persistent progress while an update downloads", () => {
    const html = render({ status: "downloading", version: "1.7.0", percent: 42.4 });

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


describe("manual update actions", () => {
  it("offers a download action before any installation", () => {
    const html = render({ status: "available", version: "2.0.0" });
    expect(html).toContain("下载更新 2.0.0");
    expect(html).not.toContain('aria-disabled="true"');
  });
  it("keeps failed downloads actionable for retry", () => {
    const html = render({ status: "error", operation: "download", version: "2.0.0", message: "offline" });
    expect(html).toContain("重试下载 2.0.0");
  });
  it("keeps failed installation actionable for explicit retry", () => {
    const html = render({ status: "error", operation: "install", version: "2.0.0", message: "failed" });
    expect(html).toContain("重试安装 2.0.0");
  });
});


it("only calls the version-bound download and installation actions on separate clicks", async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  window.matchMedia = vi.fn().mockReturnValue({ matches: false, addEventListener() {}, removeEventListener() {} });
  let listener: ((state: DesktopUpdateState) => void) | undefined;
  const download = vi.fn().mockResolvedValue({ started: true });
  const install = vi.fn().mockResolvedValue({ started: true });
  window.sagDesktop = {
    isDesktop: true, platform: "darwin", appInfo: vi.fn(), checkForUpdates: vi.fn(), getDiagnosticsInfo: vi.fn(),
    getUpdateState: async () => ({ status: "available", version: "2.0.0" }),
    downloadUpdate: download, installUpdate: install,
    onUpdateState: (fn) => { listener = fn; return () => {}; },
  };
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(
      <NextIntlClientProvider locale="zh-CN" timeZone="Asia/Shanghai" messages={messages}>
        <TooltipProvider><SidebarProvider><DesktopUpdateIndicator /></SidebarProvider></TooltipProvider>
      </NextIntlClientProvider>,
    ));
    expect(download).not.toHaveBeenCalled(); expect(install).not.toHaveBeenCalled();
    await act(async () => container.querySelector("button")!.click());
    expect(download).toHaveBeenCalledWith("2.0.0"); expect(install).not.toHaveBeenCalled();
    await act(async () => listener?.({ status: "downloaded", version: "2.0.0" }));
    expect(install).not.toHaveBeenCalled();
    await act(async () => container.querySelector("button")!.click());
    expect(install).toHaveBeenCalledWith("2.0.0");
  } finally { await act(async () => root.unmount()); container.remove(); delete window.sagDesktop; }
});
