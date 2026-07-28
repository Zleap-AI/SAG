import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";

import messages from "@/messages/zh-CN.json";
import type { Source } from "@/lib/types";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SourceCard } from "./source-card";

const source: Source = {
  id: "3fe9533639544615bc732d8d7a8f648e",
  name: "AI",
  description: "测试信源",
  source_type: "document",
  connector_kind: "file_upload",
  status: "active",
  document_count: 2,
  chunk_count: 2,
  event_count: 4,
  created_at: "2026-07-27T12:00:00Z",
  updated_at: "2026-07-27T12:00:00Z",
};

describe("source card", () => {
  it("exposes an always-visible source id control outside its navigation link", () => {
    const html = renderToStaticMarkup(
      <NextIntlClientProvider
        locale="zh-CN"
        timeZone="Asia/Shanghai"
        messages={messages}
      >
        <TooltipProvider>
          <SourceCard source={source} />
        </TooltipProvider>
      </NextIntlClientProvider>,
    );

    const navigationEnd = html.indexOf("</a>");
    const copyButton = html.indexOf('aria-label="复制信源 ID"');

    expect(html).toContain("信源 ID");
    expect(html).toContain(
      'title="3fe9533639544615bc732d8d7a8f648e"',
    );
    expect(navigationEnd).toBeGreaterThanOrEqual(0);
    expect(copyButton).toBeGreaterThan(navigationEnd);
  });
});
