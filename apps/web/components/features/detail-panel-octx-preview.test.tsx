import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";

import messages from "@/messages/zh-CN.json";
import type { Doc } from "@/lib/types";
import { DocumentPreview } from "./detail-panel";

const importedPdf: Doc = {
  id: "doc-1",
  source_id: "source-1",
  filename: "技术报告.pdf",
  content_type: "application/pdf",
  size_bytes: 123,
  status: "ready",
  chunk_count: 3,
  event_count: 2,
  progress: 100,
  token_usage: 0,
  error: null,
  original_file_available: false,
  created_at: "2026-08-12T00:00:00Z",
  updated_at: "2026-08-12T00:00:00Z",
};

describe("OCTX document preview", () => {
  it("explains why the original PDF is unavailable and offers parsed content", () => {
    const html = renderToStaticMarkup(
      <NextIntlClientProvider locale="zh-CN" timeZone="Asia/Shanghai" messages={messages}>
        <DocumentPreview doc={importedPdf} />
      </NextIntlClientProvider>,
    );

    expect(html).toContain("此文档来自 OCTX 数据包");
    expect(html).toContain("未包含原始 PDF 文件");
    expect(html).toContain("查看解析内容");
    expect(html).not.toContain(">下载</button>");
  });
});
