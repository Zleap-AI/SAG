/** @vitest-environment jsdom */

import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";

import messages from "@/messages/zh-CN.json";
import englishMessages from "@/messages/en-US.json";
import { api, ApiError } from "@/lib/api";
import type { DshIntegrationDescriptor, Source } from "@/lib/types";
import {
  DshServiceSettings,
  dshConnectionFilename,
  dshConnectionGuidance,
  dshInstallCommand,
  dshSetupCommand,
} from "./dsh-service-settings";

function integration(
  overrides: Partial<DshIntegrationDescriptor> = {},
): DshIntegrationDescriptor {
  return {
    schemaVersion: 1,
    capabilities: ["knowledge.search"],
    upload: {
      maxMb: 100,
      extensions: ["md", "pdf"],
    },
    defaultSourceId: null,
    ...overrides,
  };
}

const sources: Source[] = [
  {
    id: "source-a",
    name: "项目资料",
    description: "",
    source_type: "document",
    connector_kind: "local",
    status: "active",
    document_count: 0,
    chunk_count: 0,
    event_count: 0,
    created_at: "2026-08-25T00:00:00Z",
    updated_at: "2026-08-25T00:00:00Z",
  },
];

function provider(children: React.ReactNode) {
  return (
    <NextIntlClientProvider locale="zh-CN" messages={messages}>
      {children}
    </NextIntlClientProvider>
  );
}

async function mount() {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(provider(<DshServiceSettings />));
  });
  return { container, root };
}

async function unmount(root: Root, container: HTMLElement) {
  await act(async () => root.unmount());
  container.remove();
}

function button(container: ParentNode, text: string): HTMLButtonElement {
  const match = [...container.querySelectorAll("button")].find((candidate) =>
    candidate.textContent?.includes(text),
  );
  if (!match) throw new Error(`button not found: ${text}`);
  return match;
}

async function click(element: HTMLElement) {
  await act(async () => element.click());
}

function pointerEvent(type: string) {
  const event = new MouseEvent(type, { bubbles: true, button: 0 });
  Object.defineProperties(event, {
    pointerId: { value: 1 },
    pointerType: { value: "mouse" },
  });
  return event;
}

async function chooseSource(container: HTMLElement, sourceName: string) {
  const trigger = container.querySelector<HTMLButtonElement>(
    '[aria-label="DeepSeek Harness 的默认知识库"]',
  );
  if (!trigger) throw new Error("default knowledge selector not found");
  await act(async () => {
    trigger.dispatchEvent(pointerEvent("pointerdown"));
  });
  const option = [...document.body.querySelectorAll<HTMLElement>('[role="option"]')].find(
    (candidate) => candidate.textContent === sourceName,
  );
  if (!option) throw new Error(`source option not found: ${sourceName}`);
  await act(async () => {
    option.dispatchEvent(pointerEvent("pointerdown"));
    option.dispatchEvent(pointerEvent("pointerup"));
  });
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  Object.defineProperties(HTMLElement.prototype, {
    hasPointerCapture: { configurable: true, value: () => false },
    releasePointerCapture: { configurable: true, value: () => undefined },
    scrollIntoView: { configurable: true, value: () => undefined },
    setPointerCapture: { configurable: true, value: () => undefined },
  });
  vi.restoreAllMocks();
  vi.spyOn(api, "dshIntegration").mockResolvedValue(integration());
  vi.spyOn(api, "listSources").mockResolvedValue(sources);
});

afterEach(() => {
  document.body.replaceChildren();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("dsh service settings", () => {
  it("keeps the dsh quick-start, recovery, export, and translation keys stable", () => {
    expect(dshInstallCommand()).toBe(
      "dsh plugin --profile web add @zleap-ai/dsh-sag",
    );
    expect(dshSetupCommand()).toBe("dsh-sag setup sag-dsh.json");
    expect(dshConnectionFilename()).toBe("sag-dsh.json");
    expect(dshConnectionGuidance("ready")).toBe("connectionReady");
    expect(dshConnectionGuidance("download")).toBe("connectionDownload");
  });

  it("loads the server descriptor and available knowledge bases", async () => {
    const mounted = await mount();

    expect(api.dshIntegration).toHaveBeenCalledOnce();
    expect(api.listSources).toHaveBeenCalledOnce();
    expect(integration().upload).toEqual({
      maxMb: 100,
      extensions: ["md", "pdf"],
    });
    expect(mounted.container.querySelector(
      '[aria-label="DeepSeek Harness 的默认知识库"]',
    )).not.toBeNull();
    expect(mounted.container.textContent).toContain("自动发现本机知识库连接");
    expect(mounted.container.textContent).toContain("dsh-sag setup sag-dsh.json");
    expect(button(mounted.container, "复制")).not.toBeNull();
    expect(mounted.container.textContent).toContain("未设置（搜索全部；写入时选择）");
    expect(messages.DshService.noDefaultSource).toBe("未设置（搜索全部；写入时选择）");
    expect(englishMessages.DshService.noDefaultSource).toBe(
      "Not set (searches all; choose when writing)",
    );
    await unmount(mounted.root, mounted.container);
  });

  it("saves a selected source and clears the default for all knowledge bases", async () => {
    const update = vi.spyOn(api, "updateDshIntegration")
      .mockResolvedValue(integration({ defaultSourceId: "source-a" }));
    const mounted = await mount();

    await chooseSource(mounted.container, "项目资料");
    expect(update).toHaveBeenLastCalledWith("source-a");
    await chooseSource(mounted.container, "未设置（搜索全部；写入时选择）");
    expect(update).toHaveBeenLastCalledWith(null);
    await unmount(mounted.root, mounted.container);
  });

  it("downloads the connection file and releases its temporary URL", async () => {
    vi.useFakeTimers();
    vi.spyOn(api, "downloadDshConnection").mockResolvedValue(new Blob(["{}"]));
    const createObjectURL = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:connection");
    const revokeObjectURL = vi.spyOn(URL, "revokeObjectURL");
    const mounted = await mount();
    const appendChild = vi.spyOn(document.body, "appendChild");
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const anchorRemove = vi.spyOn(HTMLAnchorElement.prototype, "remove");

    await click(button(mounted.container, "下载配置"));

    expect(api.downloadDshConnection).toHaveBeenCalledOnce();
    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(appendChild).toHaveBeenCalledWith(expect.objectContaining({
      download: "sag-dsh.json",
      href: "blob:connection",
    }));
    expect(anchorClick).toHaveBeenCalledOnce();
    expect(anchorRemove).toHaveBeenCalledOnce();
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:connection");
    await unmount(mounted.root, mounted.container);
  });

  it("reports a failed connection download", async () => {
    vi.spyOn(api, "downloadDshConnection").mockRejectedValue(
      new ApiError(500, "download_failed", "连接配置下载失败"),
    );
    const reportError = vi.spyOn(toast, "error");
    const mounted = await mount();

    await click(button(mounted.container, "下载配置"));

    expect(reportError).toHaveBeenCalledWith("连接配置下载失败");
    await unmount(mounted.root, mounted.container);
  });

  it("does not rebuild before confirmation and rebuilds after confirmation", async () => {
    const regenerate = vi.spyOn(api, "regenerateDshToken").mockResolvedValue(integration());
    const mounted = await mount();

    await click(button(mounted.container, "重建连接"));
    expect(regenerate).not.toHaveBeenCalled();
    const initial = button(mounted.container, "重建连接");
    const confirm = [...document.body.querySelectorAll("button")].find((candidate) =>
      candidate.textContent?.includes("重建连接") && candidate !== initial,
    );
    if (!confirm) throw new Error("rebuild confirmation button not found");
    await click(confirm);
    expect(regenerate).toHaveBeenCalledOnce();
    await unmount(mounted.root, mounted.container);
  });
});
