// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";
import { api, ApiError } from "@/lib/api";
import type { FnOSNasStatus } from "@/lib/types";
import { NasImportPanel } from "./nas-import-panel";

vi.mock("@/lib/api", async (original) => {
  const actual = await original<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, registerFnOSNasLegacyFolder: vi.fn() },
  };
});

const status: FnOSNasStatus = {
    eligible: true,
    mode: "legacy_manual",
    system_version: null,
    automatic_authorization: false,
    folders: [],
    limits: {
      max_files: 5000,
      max_import_files: 500,
      max_import_bytes: 1024 * 1024,
      max_file_bytes: 1024 * 1024,
    },
    reason: "host_authorization_unavailable",
};

function renderFallback() {
  render(
    <NextIntlClientProvider locale="zh-CN" messages={messages}>
      <NasImportPanel
        sourceId="source-1"
        sourceName="公司知识"
        status={status}
        refreshStatus={vi.fn()}
        onImported={vi.fn()}
      />
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

it("shows the complete fnOS authorization path without an App Center launcher", async () => {
  const user = userEvent.setup();
  renderFallback();

  expect(screen.getByText("飞牛系统暂未向 SAG 提供 NAS 接口授权，可先使用应用设置授权目录。安装新版包或重启应用后，SAG 会自动重试快速授权。"))
    .toBeInTheDocument();
  expect(screen.getByText(/应用中心/)).toBeInTheDocument();
  expect(screen.getByText(/设置 → 访问权限/)).toBeInTheDocument();
  expect(screen.getByText(/允许访问以下文件/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /打开.*应用中心/ })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "我已完成授权，验证路径" })).toBeDisabled();

  await user.type(screen.getByLabelText("已授权文件夹的绝对路径"), "/vol1/团队资料");
  expect(screen.queryByRole("button", { name: "复制路径" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "我已完成授权，验证路径" })).toBeEnabled();
});

it("confirms a verified folder without leaving the NAS panel", async () => {
  const user = userEvent.setup();
  vi.mocked(api.registerFnOSNasLegacyFolder).mockResolvedValue({
    id: "folder-1",
    display_path: "/vol1/团队资料",
    source: "legacy_manual",
    readable: true,
  });
  renderFallback();

  await user.type(screen.getByLabelText("已授权文件夹的绝对路径"), "/vol1/团队资料");
  await user.click(screen.getByRole("button", { name: "我已完成授权，验证路径" }));

  expect(await screen.findByText("目录验证成功，可以开始扫描文档。"))
    .toBeInTheDocument();
});

it.each([
  ["nas_folder_unreadable", "请返回 fnOS 的 SAG 访问权限设置，确认已添加该目录并重启 SAG。"],
  ["nas_folder_path_invalid", "请选择 /volN/具体目录，不能授权整个存储卷。"],
])("maps %s to actionable authorization guidance", async (code, expected) => {
  const user = userEvent.setup();
  vi.mocked(api.registerFnOSNasLegacyFolder).mockRejectedValue(
    new ApiError(422, code, "server detail"),
  );
  renderFallback();

  await user.type(screen.getByLabelText("已授权文件夹的绝对路径"), "/vol1/团队资料");
  await user.click(screen.getByRole("button", { name: "我已完成授权，验证路径" }));

  expect(await screen.findByText(expected)).toBeInTheDocument();
});
