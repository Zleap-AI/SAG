/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { BackgroundJob, Doc, Source } from "@/lib/types";
import messages from "@/messages/zh-CN.json";
import { DocumentList } from "./document-list";
import { useSourceContent } from "./use-source-content";
import { OctxExportProvider } from "./octx-export-provider";

const source: Source = {
  id: "source-1", name: "Reports", description: "", source_type: "document", connector_kind: "file_upload",
  status: "active", document_count: 0, chunk_count: 0, event_count: 0,
  created_at: "2026-10-09T00:00:00Z", updated_at: "2026-10-09T00:00:00Z",
};
const cleanup: BackgroundJob = {
  id: "cleanup-1", type: "delete_document", status: "paused", source_id: source.id, document_id: "document-1",
  progress: 0, attempts: 0, error: "Manual recovery", created_at: source.created_at, started_at: null, finished_at: null,
};
const documentRecord: Doc = {
  id: "document-1", source_id: source.id, filename: "report.pdf", content_type: "application/pdf", size_bytes: 123,
  status: "deleting", has_publication: false, deletion_job: cleanup, chunk_count: 0, event_count: 0, progress: 0,
  token_usage: 0, error: null, created_at: source.created_at, updated_at: source.updated_at,
};

let root: Root;
let container: HTMLDivElement;
let current: Doc;
let requests: string[];
let reads: number;
let finished: boolean;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.useFakeTimers();
  current = { ...documentRecord };
  requests = [];
  reads = 0;
  finished = false;
  vi.stubGlobal("fetch", async (url: RequestInfo | URL, options?: RequestInit) => {
    const path = new URL(String(url)).pathname;
    if (options?.method === "DELETE") {
      requests.push(path);
      current = { ...current, status: "deleting", deletion_job: { ...cleanup, status: "queued", error: null } };
      return Response.json(current.deletion_job, { status: 202 });
    }
    if (path.endsWith("/documents")) { reads += 1; return Response.json(finished ? [] : [current]); }
    if (path.endsWith("/jobs/cleanup-1")) return Response.json(current.deletion_job);
    if (path.endsWith("/sources/source-1")) return Response.json(source);
    throw new Error(`Unexpected request: ${path}`);
  });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function Controls({ variant }: { variant: "normal" | "compact" }) {
  const state = useSourceContent(source.id);
  return <DocumentList sourceId={source.id} sourceName={source.name} documents={state.documents ?? []}
    activities={state.documentActivities} onAction={state.mutateDocument} variant={variant} />;
}

it.each(["normal", "compact"] as const)("discovers paused cleanup and continues manually in the %s list", async (variant) => {
  await act(async () => root.render(<NextIntlClientProvider locale="zh-CN" timeZone="UTC" messages={messages}>
    <OctxExportProvider><Controls variant={variant} /></OctxExportProvider>
  </NextIntlClientProvider>));
  expect(container.textContent).toContain("等待手动继续删除");
  await act(async () => vi.advanceTimersByTimeAsync(5000));
  expect(reads).toBe(1);
  expect(requests).toEqual([]);
  const open = container.querySelector<HTMLButtonElement>('button[title="查看详情与原文预览"], button[title="查看文档"]');
  expect(open?.disabled).toBe(true);
  const resume = container.querySelector<HTMLButtonElement>('button[title="继续删除"]');
  expect(resume?.disabled).toBe(false);
  await act(async () => resume!.click());
  expect(requests).toEqual(["/api/v1/sources/source-1/documents/document-1"]);
  expect(container.textContent).toContain("正在删除");
  expect(container.querySelector<HTMLButtonElement>('button[title="继续删除"]')?.disabled).toBe(true);
  finished = true;
  current = { ...current, deletion_job: { ...cleanup, status: "succeeded", progress: 1 } };
  await act(async () => vi.advanceTimersByTimeAsync(4100));
  expect(container.textContent).not.toContain("report.pdf");
});
