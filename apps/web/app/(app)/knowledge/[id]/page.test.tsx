import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";
import { TooltipProvider } from "@/components/ui/tooltip";
import SourceDetailPage from "./page";

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "3fe9533639544615bc732d8d7a8f648e" }),
  useRouter: () => ({ replace: vi.fn() }),
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
  it("shows the loaded source id with a copy action", () => {
    const html = renderToStaticMarkup(
      <NextIntlClientProvider
        locale="zh-CN"
        timeZone="Asia/Shanghai"
        messages={messages}
      >
        <TooltipProvider>
          <SourceDetailPage />
        </TooltipProvider>
      </NextIntlClientProvider>,
    );

    expect(html).toContain("信源 ID");
    expect(html).toContain(
      'title="3fe9533639544615bc732d8d7a8f648e"',
    );
    expect(html).toContain('aria-label="复制信源 ID"');
  });
});
