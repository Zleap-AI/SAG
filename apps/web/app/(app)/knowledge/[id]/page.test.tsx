import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";
import { TooltipProvider } from "@/components/ui/tooltip";
import { OctxExportProvider } from "@/components/features/octx-export-provider";
import { dismissFolderImportDialog } from "@/components/features/folder-import-dialog";
import SourceDetailPage from "./page";

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "3fe9533639544615bc732d8d7a8f648e" }),
  useRouter: () => ({ replace: vi.fn() }),
}));

vi.mock("@/components/ui/dialog", () => ({
  Dialog: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  DialogContent: ({ children }: { children: React.ReactNode }) => <section>{children}</section>,
  DialogDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
  DialogHeader: ({ children }: { children: React.ReactNode }) => <header>{children}</header>,
  DialogTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
}));

vi.mock("@/components/features/use-source-content", () => ({
  useSourceContent: () => ({
    source: {
      id: "3fe9533639544615bc732d8d7a8f648e",
      name: "AI",
      description: "测试信源",
      source_type: "document",
      connector_kind: "file_upload",
      status: "active",
      document_count: 0,
      chunk_count: 0,
      event_count: 0,
      created_at: "2026-07-27T12:00:00Z",
      updated_at: "2026-07-27T12:00:00Z",
    },
    documents: [],
    refresh: vi.fn(),
    notFound: false,
  }),
}));

describe("source detail page", () => {
  it("cancels an active folder import before dismissing the parent dialog", () => {
    const actions: string[] = [];

    dismissFolderImportDialog(
      { dismiss: () => actions.push("cancel") },
      (open) => actions.push(`open:${open}`),
    );

    expect(actions).toEqual(["cancel", "open:false"]);
  });

  it("shows the loaded source id with a copy action", () => {
    const html = renderToStaticMarkup(
      <NextIntlClientProvider
        locale="zh-CN"
        timeZone="Asia/Shanghai"
        messages={messages}
      >
        <TooltipProvider>
          <OctxExportProvider>
            <SourceDetailPage />
          </OctxExportProvider>
        </TooltipProvider>
      </NextIntlClientProvider>,
    );

    expect(html).toContain("信源 ID");
    expect(html).toContain(
      'title="3fe9533639544615bc732d8d7a8f648e"',
    );
    expect(html).toContain('aria-label="复制信源 ID"');
  });

  it("keeps single-file upload and adds the guided folder workflow", () => {
    const html = renderToStaticMarkup(
      <NextIntlClientProvider
        locale="zh-CN"
        timeZone="Asia/Shanghai"
        messages={messages}
      >
        <TooltipProvider>
          <OctxExportProvider>
            <SourceDetailPage />
          </OctxExportProvider>
        </TooltipProvider>
      </NextIntlClientProvider>,
    );

    expect(html).toContain("拖拽文件到此处，或点击选择");
    expect(html).toContain("选择文件夹");
    expect(html).toContain("扫描结果");
    expect(html).toContain("检查冲突");
    expect(html).toContain("最终确认");
  });
});
