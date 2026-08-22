/** @vitest-environment jsdom */

import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";
import { api, ApiError } from "@/lib/api";
import { clearToken, getToken, setToken } from "@/lib/auth";
import {
  createStorageBootstrapPoller,
  viewForStorageBootstrap,
} from "@/lib/storage-bootstrap";
import type { StorageBootstrapStatus } from "@/lib/types";
import {
  StorageBootstrapGate,
  StorageBootstrapGateView,
} from "./storage-bootstrap-gate";

function status(
  phase: StorageBootstrapStatus["phase"],
  overrides: Partial<StorageBootstrapStatus> = {},
): StorageBootstrapStatus {
  return {
    phase,
    detected_version: "0.7",
    target_version: "0.8.2",
    choices: phase === "choice_required" ? ["migrate", "fresh"] : [],
    stage: null,
    error: null,
    recoverable: false,
    runtime_ready: phase === "ready",
    ...overrides,
  };
}

function provider(children: React.ReactNode) {
  return (
    <NextIntlClientProvider
      locale="zh-CN"
      timeZone="Asia/Shanghai"
      messages={messages}
    >
      {children}
    </NextIntlClientProvider>
  );
}

function renderView(
  bootstrapStatus: StorageBootstrapStatus,
  options: {
    authenticated?: boolean;
    selectedChoice?: "migrate" | "fresh" | null;
  } = {},
) {
  return renderToStaticMarkup(
    provider(
      <StorageBootstrapGateView
        status={bootstrapStatus}
        authenticated={options.authenticated ?? false}
        selectedChoice={options.selectedChoice ?? null}
        submitting={false}
        loginName=""
        loginLoading={false}
        errorMessage={null}
        onSelectChoice={vi.fn()}
        onCancelChoice={vi.fn()}
        onConfirmChoice={vi.fn()}
        onLoginNameChange={vi.fn()}
        onLogin={vi.fn()}
        onRetry={vi.fn()}
      >
        <div>应用已挂载</div>
      </StorageBootstrapGateView>,
    ),
  );
}

async function mountGate(options: { strict?: boolean } = {}) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const gate = provider(
    <StorageBootstrapGate>
      <div>应用已挂载</div>
    </StorageBootstrapGate>,
  );
  await act(async () => {
    root.render(options.strict ? <React.StrictMode>{gate}</React.StrictMode> : gate);
  });
  return { container, root };
}

async function unmount(root: Root, container: HTMLElement) {
  await act(async () => root.unmount());
  container.remove();
}

function button(container: HTMLElement, text: string): HTMLButtonElement {
  const match = [...container.querySelectorAll("button")].find((candidate) =>
    candidate.textContent?.includes(text),
  );
  if (!match) throw new Error(`button not found: ${text}`);
  return match;
}

async function click(element: HTMLElement) {
  await act(async () => element.click());
}

async function enterLoginName(container: HTMLElement, name: string) {
  const input = container.querySelector("input");
  if (!(input instanceof HTMLInputElement)) throw new Error("login input not found");
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )?.set;
    setter?.call(input, name);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  clearToken();
  vi.restoreAllMocks();
});

afterEach(() => {
  document.body.replaceChildren();
  clearToken();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("storage bootstrap state", () => {
  it("maps every backend phase to the gate view", () => {
    expect(viewForStorageBootstrap(status("ready")).kind).toBe("ready");
    expect(viewForStorageBootstrap(status("choice_required")).kind).toBe("choice");
    expect(viewForStorageBootstrap(status("processing")).kind).toBe("processing");
    expect(viewForStorageBootstrap(status("failed")).kind).toBe("failed");
  });

  it("polls once per second during processing and stops at a terminal phase", async () => {
    vi.useFakeTimers();
    const load = vi
      .fn<() => Promise<StorageBootstrapStatus>>()
      .mockResolvedValueOnce(status("processing", { stage: "copying" }))
      .mockResolvedValueOnce(status("ready"));
    const onStatus = vi.fn();

    const stop = createStorageBootstrapPoller(load, onStatus);
    await vi.advanceTimersByTimeAsync(999);
    expect(load).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(load).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(load).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(3_000);
    expect(load).toHaveBeenCalledTimes(2);
    expect(onStatus).toHaveBeenLastCalledWith(expect.objectContaining({ phase: "ready" }));

    stop();
  });
});

describe("StorageBootstrapGateView", () => {
  it("renders application children only when storage is ready", () => {
    expect(renderView(status("ready"))).toContain("应用已挂载");
    expect(renderView(status("processing"))).not.toContain("应用已挂载");
  });

  it("shows existing-account login without registration when a choice is required", () => {
    const html = renderView(status("choice_required"));
    expect(html).toContain("使用现有账户登录");
    expect(html).not.toContain("注册");
    expect(html).not.toContain("/Users/owner/legacy-engine");
  });

  it("requires a second confirmation and explains the fresh-workspace effects", () => {
    const html = renderView(
      status("choice_required", { preserved_path: "/Users/owner/legacy-engine" }),
      { authenticated: true, selectedChoice: "fresh" },
    );
    expect(html).toContain("再次确认");
    expect(html).toContain("保留账号和模型配置");
    expect(html).toContain("旧知识库不会删除");
    expect(html).toContain("不会出现在新的 SAG 中");
    expect(html).toContain("需要重新上传文档");
    expect(html).toContain("不支持自动合并");
    expect(html).toContain("/Users/owner/legacy-engine");
  });

  it("shows migration warning, processing stage, and recoverable failure action", () => {
    const migration = renderView(status("choice_required"), {
      authenticated: true,
      selectedChoice: "migrate",
    });
    const processing = renderView(status("processing", { stage: "copying" }));
    const failed = renderView(
      status("failed", { error: "磁盘空间不足", recoverable: true }),
      { authenticated: true },
    );
    expect(migration).toContain("需要额外磁盘空间");
    expect(migration).toContain("可能需要较长时间");
    expect(processing).toContain("正在复制旧知识库");
    expect(failed).toContain("磁盘空间不足");
    expect(failed).toContain("重试");
  });
});

describe("StorageBootstrapGate interactions", () => {
  it("hydrates an existing token and deduplicates Strict Mode startup", async () => {
    setToken("existing-token");
    const load = vi.spyOn(api, "storageBootstrap").mockResolvedValue(status("choice_required"));
    const mounted = await mountGate({ strict: true });

    expect(getToken()).toBe("existing-token");
    expect(load).toHaveBeenCalledTimes(1);
    expect(mounted.container.textContent).toContain("迁移旧知识库");
    expect(mounted.container.textContent).not.toContain("使用现有账户登录");
    await unmount(mounted.root, mounted.container);
  });

  it("recovers an initial status 401 inside the existing-account gate", async () => {
    setToken("expired-token");
    const load = vi
      .spyOn(api, "storageBootstrap")
      .mockRejectedValueOnce(new ApiError(401, "auth_error", "登录已过期"))
      .mockResolvedValueOnce(status("choice_required"));

    const mounted = await mountGate();

    expect(load).toHaveBeenCalledTimes(2);
    expect(getToken()).toBeNull();
    expect(mounted.container.textContent).toContain("使用现有账户登录");
    expect(window.location.pathname).not.toBe("/login");
    await unmount(mounted.root, mounted.container);
  });

  it("keeps wrong-name login inside the gate and accepts an exact existing name", async () => {
    vi.spyOn(api, "storageBootstrap").mockResolvedValue(status("choice_required"));
    const login = vi
      .spyOn(api, "login")
      .mockRejectedValueOnce(new ApiError(401, "auth_error", "账户不存在"))
      .mockResolvedValueOnce({
        access_token: "owner-token",
        token_type: "bearer",
        user: { id: "owner", email: "owner@example.test", name: "Owner", created_at: "2026-08-16" },
      });
    const mounted = await mountGate();

    await enterLoginName(mounted.container, "Missing");
    await click(button(mounted.container, "登录"));
    expect(login).toHaveBeenLastCalledWith({ name: "Missing" });
    expect(mounted.container.textContent).toContain("账户不存在");
    expect(mounted.container.textContent).toContain("使用现有账户登录");

    await enterLoginName(mounted.container, "Owner");
    await click(button(mounted.container, "登录"));
    expect(login).toHaveBeenLastCalledWith({ name: "Owner" });
    expect(getToken()).toBe("owner-token");
    expect(mounted.container.textContent).toContain("迁移旧知识库");
    await unmount(mounted.root, mounted.container);
  });

  it.each(["migrate", "fresh"] as const)(
    "requires confirmation and locks duplicate %s submissions",
    async (choice) => {
      setToken("owner-token");
      vi.spyOn(api, "storageBootstrap").mockResolvedValue(status("choice_required"));
      let resolveChoice!: (value: StorageBootstrapStatus) => void;
      const choose = vi.spyOn(api, "chooseStorageBootstrap").mockImplementation(
        () => new Promise((resolve) => { resolveChoice = resolve; }),
      );
      const mounted = await mountGate();

      await click(button(mounted.container, choice === "migrate" ? "迁移旧知识库" : "创建全新知识库"));
      expect(choose).not.toHaveBeenCalled();
      const confirm = button(mounted.container, "确认并开始");
      await act(async () => {
        confirm.click();
        confirm.click();
      });
      expect(choose).toHaveBeenCalledTimes(1);
      expect(choose).toHaveBeenCalledWith(choice);
      await act(async () => resolveChoice(status("processing", { stage: "queued" })));
      expect(mounted.container.textContent).toContain("正在准备新的知识库存储");
      await unmount(mounted.root, mounted.container);
    },
  );

  it("owns choice 401, clears the token, and returns to existing-account login", async () => {
    setToken("expired-token");
    vi.spyOn(api, "storageBootstrap").mockResolvedValue(status("choice_required"));
    vi.spyOn(api, "chooseStorageBootstrap").mockRejectedValue(
      new ApiError(401, "auth_error", "登录已过期"),
    );
    const mounted = await mountGate();

    await click(button(mounted.container, "迁移旧知识库"));
    await click(button(mounted.container, "确认并开始"));
    expect(getToken()).toBeNull();
    expect(mounted.container.textContent).toContain("使用现有账户登录");
    await unmount(mounted.root, mounted.container);
  });

  it("reposts the authenticated accepted choice when retrying a recoverable failure", async () => {
    setToken("owner-token");
    vi.spyOn(api, "storageBootstrap").mockResolvedValue(
      status("failed", { recoverable: true, accepted_choice: "migrate" }),
    );
    const choose = vi.spyOn(api, "chooseStorageBootstrap").mockResolvedValue(
      status("processing", { stage: "queued", accepted_choice: "migrate" }),
    );
    const mounted = await mountGate();

    await click(button(mounted.container, "重试"));
    expect(choose).toHaveBeenCalledTimes(1);
    expect(choose).toHaveBeenCalledWith("migrate");
    expect(mounted.container.textContent).toContain("正在准备新的知识库存储");
    await unmount(mounted.root, mounted.container);
  });

  it("handles a retry 401 without leaving the maintenance gate", async () => {
    setToken("expired-token");
    vi.spyOn(api, "storageBootstrap").mockResolvedValue(
      status("failed", { recoverable: true, accepted_choice: "fresh" }),
    );
    const choose = vi.spyOn(api, "chooseStorageBootstrap").mockRejectedValue(
      new ApiError(401, "auth_error", "登录已过期"),
    );
    const mounted = await mountGate();

    await click(button(mounted.container, "重试"));
    expect(choose).toHaveBeenCalledWith("fresh");
    expect(getToken()).toBeNull();
    expect(mounted.container.textContent).toContain("使用现有账户登录");
    await unmount(mounted.root, mounted.container);
  });

  it("recovers a polling 401 without navigation and shows existing-account login", async () => {
    vi.useFakeTimers();
    setToken("owner-token");
    const load = vi
      .spyOn(api, "storageBootstrap")
      .mockResolvedValueOnce(status("processing", { stage: "queued" }))
      .mockRejectedValueOnce(new ApiError(401, "auth_error", "登录已过期"))
      .mockResolvedValueOnce(
        status("failed", { error: "升级暂停", recoverable: true }),
      );
    const mounted = await mountGate();

    await act(async () => vi.advanceTimersByTimeAsync(1_000));

    expect(load).toHaveBeenCalledTimes(3);
    expect(getToken()).toBeNull();
    expect(mounted.container.textContent).toContain("使用现有账户登录");
    expect(window.location.pathname).not.toBe("/login");
    await unmount(mounted.root, mounted.container);
  });

  it("polls after one second, clears a recovered poll error, and stops on terminal status", async () => {
    vi.useFakeTimers();
    const load = vi
      .spyOn(api, "storageBootstrap")
      .mockResolvedValueOnce(status("processing", { stage: "queued" }))
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(status("processing", { stage: "copying" }))
      .mockResolvedValueOnce(status("ready"));
    const mounted = await mountGate();

    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(mounted.container.textContent).toContain("暂时无法获取升级进度");
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(mounted.container.textContent).toContain("正在复制旧知识库");
    expect(mounted.container.textContent).not.toContain("暂时无法获取升级进度");
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(mounted.container.textContent).toContain("应用已挂载");
    await act(async () => vi.advanceTimersByTimeAsync(3_000));
    expect(load).toHaveBeenCalledTimes(4);
    await unmount(mounted.root, mounted.container);
  });

  it("cleans up polling on unmount", async () => {
    vi.useFakeTimers();
    const load = vi.spyOn(api, "storageBootstrap").mockResolvedValue(
      status("processing", { stage: "queued" }),
    );
    const mounted = await mountGate();
    await unmount(mounted.root, mounted.container);
    await vi.advanceTimersByTimeAsync(3_000);
    expect(load).toHaveBeenCalledTimes(1);
  });
});
