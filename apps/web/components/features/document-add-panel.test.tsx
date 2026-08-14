// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";
import type { FnOSNasStatus } from "@/lib/types";
import { DocumentAddPanel } from "./document-add-panel";

const eligibleStatus: FnOSNasStatus = {
  eligible: true,
  mode: "automatic",
  system_version: "1.2.0500",
  automatic_authorization: true,
  folders: [],
  limits: {
    max_files: 5000,
    max_import_files: 500,
    max_import_bytes: 1024,
    max_file_bytes: 1024,
  },
  reason: null,
};

let nasState: {
  enabled: boolean;
  status: FnOSNasStatus | null;
  loading: boolean;
  error: Error | null;
  refresh: ReturnType<typeof vi.fn>;
};

vi.mock("@/components/features/app-shell", () => ({
  useApp: () => ({ capabilities: { max_upload_mb: 25 } }),
}));
vi.mock("@/components/features/use-fnos-nas-status", () => ({
  useFnOSNasStatus: () => nasState,
}));
vi.mock("@/components/features/upload-zone", () => ({
  UploadZone: () => <div>LOCAL UPLOAD</div>,
}));
vi.mock("@/components/features/nas-import-panel", () => ({
  NasImportPanel: () => <div>NAS IMPORT</div>,
}));

function renderPanel() {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messages}>
      <DocumentAddPanel
        sourceId="source-1"
        sourceName="公司知识"
        onCompleted={vi.fn()}
      />
    </NextIntlClientProvider>,
  );
}

describe("document add panel", () => {
  beforeEach(() => {
    nasState = {
      enabled: false,
      status: null,
      loading: false,
      error: null,
      refresh: vi.fn(),
    };
  });

  it("keeps local upload as the only entry outside eligible fnOS", () => {
    renderPanel();
    expect(screen.getByText("LOCAL UPLOAD")).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "NAS 文档" })).not.toBeInTheDocument();
  });

  it("adds an administrator NAS tab without unmounting local upload", async () => {
    const user = userEvent.setup();
    nasState = { ...nasState, enabled: true, status: eligibleStatus };
    renderPanel();
    expect(screen.getByText("LOCAL UPLOAD")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "NAS 文档" }));
    expect(screen.getByText("NAS IMPORT")).toBeVisible();
    expect(screen.getByText("LOCAL UPLOAD")).toBeInTheDocument();
  });

  it("keeps the NAS tab mounted while an existing NAS status refreshes", async () => {
    const user = userEvent.setup();
    nasState = { ...nasState, enabled: true, status: eligibleStatus };
    const view = renderPanel();
    await user.click(screen.getByRole("tab", { name: "NAS 文档" }));

    nasState = { ...nasState, loading: true };
    view.rerender(
      <NextIntlClientProvider locale="zh-CN" messages={messages}>
        <DocumentAddPanel sourceId="source-1" sourceName="公司知识" onCompleted={vi.fn()} />
      </NextIntlClientProvider>,
    );

    expect(screen.getByRole("tab", { name: "NAS 文档" })).toHaveAttribute("data-state", "active");
    expect(screen.getByText("NAS IMPORT")).toBeVisible();
  });

  it("shows a non-blocking retry when NAS status fails", async () => {
    const user = userEvent.setup();
    nasState = { ...nasState, enabled: true, error: new Error("offline") };
    renderPanel();
    expect(screen.getByText("LOCAL UPLOAD")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(nasState.refresh).toHaveBeenCalledOnce();
  });
});
