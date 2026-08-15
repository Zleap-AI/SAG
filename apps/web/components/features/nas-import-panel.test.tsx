// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";
import { api } from "@/lib/api";
import type { FnOSNasStatus } from "@/lib/types";
import { NasImportPanel } from "./nas-import-panel";

vi.mock("@/lib/api", async (original) => {
  const actual = await original<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api } };
});

const status: FnOSNasStatus = {
  eligible: true,
  mode: "automatic",
  system_version: "1.2.0500",
  automatic_authorization: true,
  folders: [
    { id: "folder-1", display_path: "团队资料", source: "host_api", readable: true },
  ],
  limits: {
    max_files: 5000,
    max_import_files: 500,
    max_import_bytes: 1024 * 1024,
    max_file_bytes: 1024 * 1024,
  },
  reason: null,
};

function renderPanel() {
  return render(
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

describe("NAS import panel", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("scans, defaults eligible files, and confirms a one-time import", async () => {
    const user = userEvent.setup();
    const scrollIntoView = vi.fn();
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: false }));
    vi.spyOn(api, "scanFnOSNas").mockResolvedValue({
      scan_id: "scan-1",
      folder: "团队资料",
      truncated: false,
      truncated_reason: null,
      selection_expires_at: "2026-08-13T12:00:00Z",
      summary: {
        visited: 2,
        eligible: 1,
        new: 1,
        changed: 0,
        imported: 1,
        unsupported: 0,
        too_large: 0,
        unreadable: 0,
      },
      files: [
        {
          selection_token: "token-1",
          name: "handbook.pdf",
          display_path: "制度/handbook.pdf",
          extension: ".pdf",
          size_bytes: 1024,
          modified_at: "2026-08-13T10:00:00Z",
          state: "new",
          selected_by_default: true,
          document_id: null,
        },
        {
          selection_token: null,
          name: "old.pdf",
          display_path: "制度/old.pdf",
          extension: ".pdf",
          size_bytes: 1024,
          modified_at: "2026-08-13T09:00:00Z",
          state: "imported",
          selected_by_default: false,
          document_id: "doc-old",
        },
      ],
    });
    vi.spyOn(api, "createFnOSNasImport").mockResolvedValue({ job_id: "job-1", accepted: 1 });
    vi.spyOn(api, "getFnOSNasImport").mockResolvedValue({
      id: "job-1",
      status: "succeeded",
      progress: 1,
      total: 1,
      completed: 1,
      created: 1,
      updated: 0,
      skipped: 0,
      failed: 0,
      results: [],
    });
    renderPanel();

    await user.click(screen.getByRole("button", { name: "扫描文档" }));
    expect(await screen.findByText("制度/handbook.pdf")).toBeInTheDocument();
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "start" });
    expect(screen.getByRole("checkbox", { name: /handbook.pdf/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /old.pdf/ })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: /导入 1 个文档/ }));
    expect(screen.getByText(/一次性复制到“公司知识”/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "确认导入" }));
    expect(await screen.findByRole("status")).toHaveTextContent("导入完成");
  });

  it("keeps the previous result and actions stable during a rescan", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "scanFnOSNas")
      .mockResolvedValueOnce({
        scan_id: "scan-old",
        folder: "团队资料",
        truncated: false,
        truncated_reason: null,
        selection_expires_at: "2026-08-13T12:00:00Z",
        summary: {
          visited: 1,
          eligible: 1,
          new: 1,
          changed: 0,
          imported: 0,
          unsupported: 0,
          too_large: 0,
          unreadable: 0,
        },
        files: [{
          selection_token: "old-token",
          name: "old.pdf",
          display_path: "旧结果/old.pdf",
          extension: ".pdf",
          size_bytes: 1024,
          modified_at: "2026-08-13T10:00:00Z",
          state: "new",
          selected_by_default: true,
          document_id: null,
        }],
      })
      .mockImplementationOnce(() => new Promise(() => undefined));
    renderPanel();

    await user.click(screen.getByRole("button", { name: "扫描文档" }));
    expect(await screen.findByText("旧结果/old.pdf")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "扫描文档" }));

    expect(screen.getByText("旧结果/old.pdf")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "正在扫描…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /导入 1 个文档/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "取消扫描" })).toBeEnabled();
  });

  it("uses a stable first-scan placeholder before results arrive", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "scanFnOSNas").mockImplementationOnce(() => new Promise(() => undefined));
    renderPanel();

    await user.click(screen.getByRole("button", { name: "扫描文档" }));

    expect(screen.getByRole("status", { name: "正在扫描文档" })).toHaveClass("min-h-32");
    expect(screen.getByRole("button", { name: "正在扫描…" })).toBeDisabled();
  });
});
