/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { expect, it, vi } from "vitest";
import messages from "@/messages/zh-CN.json";
import type { SearchResponse } from "@/lib/types";
import { ResultList } from "./search-panel-sections";
vi.mock("@/components/features/detail-panel", () => ({ useDetailPanel: () => ({ open: vi.fn() }) }));
it("keeps native evidence visible when SAG1 graph events are also present", async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const result: SearchResponse = {
    query: "q", entities: [], relations: [], source_hits: [], summary: "", exploration_id: null, stats: {},
    events: [{ id: "event", title: "SAG1 event", summary: "legacy", score: 0.8, source_id: "legacy", document_id: null, category: "", rank: 0, parent_id: null, chunk_id: null, start_time: null }],
    sections: [{ source_id: "native", chunk_id: "c", heading: "Native evidence", content: "NATIVE-253", score: 0.2, rank: 0,
      metadata: { engine_kind: "sag2" } }],
  };
  const container = document.createElement("div"); document.body.append(container); const root = createRoot(container);
  try {
    await act(async () => root.render(<NextIntlClientProvider locale="zh-CN" messages={messages}>
      <ResultList result={result} compact={false} sort="relevance" />
    </NextIntlClientProvider>));
    expect(container.textContent).toContain("NATIVE-253");
  } finally { await act(async () => root.unmount()); container.remove(); }
});
