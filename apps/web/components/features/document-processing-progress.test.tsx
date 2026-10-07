/** @vitest-environment jsdom */

import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { beginDocumentMutation, deriveDocumentActivity } from "@/lib/document-activity";
import type { Doc, Source } from "@/lib/types";
import zh from "@/messages/zh-CN.json";
import en from "@/messages/en-US.json";
import { DocumentList } from "./document-list";
import { DocumentDetailContent } from "./detail-panel";
import { OctxExportProvider } from "./octx-export-provider";
import { useSourceContent } from "./use-source-content";

const base: Doc = {
  id: "doc-1", source_id: "source-1", filename: "report.pdf",
  content_type: "application/pdf", size_bytes: 123, status: "extracting",
  chunk_count: 20, event_count: 0, progress: 52, token_usage: 0, error: null,
  original_file_available: false,
  processing_stage: "extracting", processed_chunks: 8, total_chunks: 20,
  created_at: "2026-10-07T00:00:00Z", updated_at: "2026-10-07T00:00:00Z",
};

function provider(children: React.ReactNode, locale: "zh-CN" | "en-US" = "zh-CN") {
  return <NextIntlClientProvider locale={locale} timeZone="UTC" messages={locale === "zh-CN" ? zh : en}>
    {children}
  </NextIntlClientProvider>;
}

function list(doc: Doc, variant: "normal" | "compact" = "normal", locale: "zh-CN" | "en-US" = "zh-CN", resume = false) {
  const container = document.createElement("div");
  const activity = deriveDocumentActivity(doc, resume ? beginDocumentMutation(doc, "resume") : undefined);
  container.innerHTML = renderToStaticMarkup(provider(
    <OctxExportProvider>
      <DocumentList sourceId="source-1" sourceName="Reports" documents={[doc]}
        activities={{ [doc.id]: activity }} onAction={async () => true} variant={variant} />
    </OctxExportProvider>, locale,
  ));
  return container;
}

describe("document processing in existing list layouts", () => {
  it.each(["normal", "compact"] as const)("shows real chunk counts without an overall percentage in %s rows", (variant) => {
    const view = list(base, variant);
    expect(view.textContent).toContain("已处理 8 / 20 个分块");
    expect(view.textContent).not.toContain("%");
    const bar = view.querySelector('[role="progressbar"]');
    expect(bar?.getAttribute("aria-valuenow")).toBe("8");
    expect(bar?.getAttribute("aria-valuemax")).toBe("20");
  });

  it.each([
    ["queued", "pending", "等待处理"],
    ["parsing", "loading", "解析文档"],
    ["chunking", "loading", "划分分块"],
    ["indexing", "loading", "生成分块向量"],
    ["waiting_extraction", "extracting", "等待抽取"],
    ["finalizing", "extracting", "整理并保存结果"],
    ["waiting_retry", "pending", "等待后台重试"],
  ] as const)("shows %s without borrowing counts from the previous stage", (stage, status, label) => {
    const view = list({ ...base, status, processing_stage: stage, processed_chunks: 20 });
    expect(view.textContent).toContain(label);
    expect(view.textContent).not.toContain("已处理");
    expect(view.querySelector('[role="progressbar"]')).toBeNull();
  });

  it("treats all chunks as awaiting persistence, never as a finished document", () => {
    const view = list({ ...base, processed_chunks: 20 });
    expect(view.textContent).toContain("整理并保存结果");
    expect(view.textContent).not.toContain("就绪");
    expect(view.querySelector('[role="progressbar"]')).toBeNull();
  });

  it.each(["paused", "failed", "ready"] as const)("keeps %s authoritative over a stale processing stage", (status) => {
    const view = list({ ...base, status, processing_stage: "finalizing", processed_chunks: 20 });
    expect(view.textContent).toContain({ paused: "已暂停", failed: "失败", ready: "就绪" }[status]);
    expect(view.textContent).not.toContain("整理并保存结果");
    expect(view.querySelector('[role="progressbar"]')).toBeNull();
  });

  it("keeps local resume feedback ahead of stale saved progress", () => {
    const view = list({ ...base, status: "paused", processed_chunks: 20 }, "normal", "zh-CN", true);
    expect(view.textContent).toContain("正在继续");
    expect(view.textContent).not.toContain("整理并保存结果");
    expect(view.querySelector('[role="progressbar"]')).toBeNull();
  });

  it.each(["paused", "failed"] as const)("retains the final reported count when saving ends as %s", (status) => {
    const view = list({ ...base, status, processing_stage: "finalizing", processed_chunks: 20 });
    expect(view.textContent).toContain("已处理 20 / 20 个分块");
    expect(view.textContent).toContain(status === "paused" ? "已暂停" : "失败");
    expect(view.querySelector('[role="progressbar"]')).toBeNull();
  });

  it.each([
    { processing_stage: undefined, processed_chunks: undefined, total_chunks: undefined },
    { processing_stage: "future_stage", processed_chunks: 8, total_chunks: 20 },
    { processed_chunks: null, total_chunks: 20 },
    { processed_chunks: 0, total_chunks: 0 },
    { processed_chunks: -1, total_chunks: 20 },
    { processed_chunks: 21, total_chunks: 20 },
  ])("does not invent chunk counts or use legacy percentages for incomplete data %j", (patch) => {
    const view = list({ ...base, ...patch });
    expect(view.textContent).toContain("抽取中");
    expect(view.textContent).not.toContain("%");
    expect(view.textContent).not.toContain("已处理");
    expect(view.querySelector('[role="progressbar"]')).toBeNull();
  });

  it("renders equivalent English stage and count information", () => {
    expect(list(base, "normal", "en-US").textContent).toContain("Processed 8 / 20 chunks");
    expect(list({ ...base, processing_stage: "finalizing" }, "compact", "en-US").textContent)
      .toContain("Organizing and saving results");
  });
});

describe("live document details", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.useFakeTimers();
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ status: 404, ok: false }));
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  async function render(documentId = "doc-1") {
    await act(async () => root.render(provider(<DocumentDetailContent sourceId="source-1" documentId={documentId} />)));
  }

  it("updates counts, then explains persistence and converges to ready", async () => {
    vi.spyOn(api, "getDocument")
      .mockResolvedValueOnce(base)
      .mockResolvedValueOnce({ ...base, processed_chunks: 15 })
      .mockResolvedValueOnce({ ...base, processed_chunks: 20, processing_stage: "finalizing" })
      .mockResolvedValue({ ...base, status: "ready", processed_chunks: 20, processing_stage: "ready", event_count: 4 });
    await render();
    expect(container.textContent).toContain("已处理 8 / 20 个分块");
    await act(async () => vi.advanceTimersByTimeAsync(4_000));
    expect(container.textContent).toContain("已处理 15 / 20 个分块");
    await act(async () => vi.advanceTimersByTimeAsync(4_000));
    expect(container.textContent).toContain("正在检查处理结果并保存，完成后即可检索");
    expect(container.querySelector('[role="progressbar"]')).toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(4_000));
    expect(container.textContent).toContain("就绪");
    expect(container.textContent).not.toContain("正在检查处理结果并保存");
    expect(container.textContent).not.toContain("%");
  });

  it("never applies a previous document's delayed response after switching selection", async () => {
    let release!: (doc: Doc) => void;
    vi.spyOn(api, "getDocument").mockImplementation((_source, id) => id === "doc-1"
      ? new Promise((resolve) => { release = resolve; })
      : Promise.resolve({ ...base, id, filename: "second.pdf", processed_chunks: 3 }));
    await render();
    await render("doc-2");
    expect(container.textContent).toContain("second.pdf");
    await act(async () => release(base));
    expect(container.textContent).toContain("second.pdf");
    expect(container.textContent).not.toContain("report.pdf");
    expect(container.textContent).toContain("已处理 3 / 20 个分块");
  });

  it("refreshes a paused document on focus and resumes live updates", async () => {
    vi.spyOn(api, "getDocument")
      .mockResolvedValueOnce({ ...base, status: "paused" })
      .mockResolvedValueOnce({ ...base, processed_chunks: 1 })
      .mockResolvedValue({ ...base, processed_chunks: 2 });
    await render();
    expect(container.textContent).toContain("已暂停");
    await act(async () => window.dispatchEvent(new Event("focus")));
    expect(container.textContent).toContain("已处理 1 / 20 个分块");
    await act(async () => vi.advanceTimersByTimeAsync(4_000));
    expect(container.textContent).toContain("已处理 2 / 20 个分块");
  });

  it("wakes paused details after resume in the source controls without waiting for focus", async () => {
    let current: Doc = { ...base, status: "paused" };
    const source: Source = {
      id: "source-1", name: "Reports", description: "", source_type: "document",
      connector_kind: "file_upload", status: "active", document_count: 1,
      chunk_count: 20, event_count: 0, created_at: base.created_at, updated_at: base.updated_at,
    };
    vi.spyOn(api, "getSource").mockResolvedValue(source);
    vi.spyOn(api, "listDocuments").mockImplementation(async () => [current]);
    vi.spyOn(api, "getDocument").mockImplementation(async () => current);
    vi.spyOn(api, "resumeDocument").mockImplementation(async () => {
      current = { ...base, processing_stage: "waiting_extraction", processed_chunks: 0 };
      return {
        id: "job-1", type: "process_document", status: "running", source_id: "source-1",
        document_id: "doc-1", progress: 0, attempts: 1, error: null,
        created_at: base.created_at, started_at: base.created_at, finished_at: null,
      };
    });
    function SourceControls() {
      const { documents, mutateDocument } = useSourceContent("source-1");
      return <>
        <button onClick={() => documents?.[0] && void mutateDocument(documents[0], "resume")}>Resume</button>
        <DocumentDetailContent sourceId="source-1" documentId="doc-1" />
      </>;
    }
    await act(async () => root.render(provider(<SourceControls />)));
    expect(container.textContent).toContain("已暂停");
    await act(async () => container.querySelector("button")!.click());
    expect(container.textContent).toContain("等待抽取");
    expect(container.textContent).not.toContain("已暂停");
  });

  it("keeps the last snapshot on a transient poll failure and recovers on the next poll", async () => {
    vi.spyOn(api, "getDocument")
      .mockResolvedValueOnce(base)
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue({ ...base, processed_chunks: 16 });
    await render();
    await act(async () => vi.advanceTimersByTimeAsync(4_000));
    expect(container.textContent).toContain("已处理 8 / 20 个分块");
    await act(async () => vi.advanceTimersByTimeAsync(4_000));
    expect(container.textContent).toContain("已处理 16 / 20 个分块");
  });

  it("does not overlap polls while the document request is still pending", async () => {
    let release!: (doc: Doc) => void;
    const request = vi.spyOn(api, "getDocument").mockResolvedValueOnce(base)
      .mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }))
      .mockResolvedValue({ ...base, processed_chunks: 18 });
    await render();
    await act(async () => vi.advanceTimersByTimeAsync(4_000));
    await act(async () => vi.advanceTimersByTimeAsync(12_000));
    expect(request).toHaveBeenCalledTimes(2);
    expect(container.textContent).toContain("已处理 8 / 20 个分块");
    await act(async () => release({ ...base, processed_chunks: 12 }));
    expect(container.textContent).toContain("已处理 12 / 20 个分块");
    await act(async () => vi.advanceTimersByTimeAsync(4_000));
    expect(container.textContent).toContain("已处理 18 / 20 个分块");
  });

  it("suspends requests in a hidden tab and refreshes when visible again", async () => {
    let hidden = false;
    vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
    const request = vi.spyOn(api, "getDocument").mockResolvedValueOnce(base)
      .mockResolvedValue({ ...base, processed_chunks: 17 });
    await render();
    hidden = true;
    await act(async () => vi.advanceTimersByTimeAsync(8_000));
    expect(request).toHaveBeenCalledTimes(1);
    hidden = false;
    await act(async () => document.dispatchEvent(new Event("visibilitychange")));
    expect(container.textContent).toContain("已处理 17 / 20 个分块");
  });
});
