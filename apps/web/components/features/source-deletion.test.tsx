/** @vitest-environment jsdom */

import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";

import type { BackgroundJob, Source } from "@/lib/types";
import messages from "@/messages/zh-CN.json";
import { TooltipProvider } from "@/components/ui/tooltip";
import { KnowledgeProvider } from "./knowledge-provider";
import { OctxImportProvider } from "./octx-import-provider";
import { OctxExportProvider } from "./octx-export-provider";
import { SourceCard } from "./source-card";
import { KnowledgeWorkspace, SourceRow } from "./knowledge-workspace";

const source: Source = {
  id: "source-1", name: "Reports", description: "", source_type: "document", connector_kind: "file_upload",
  status: "active", default_search_strategy: null, document_count: 2, chunk_count: 4, event_count: 5,
  created_at: "2026-10-09T00:00:00Z", updated_at: "2026-10-09T00:00:00Z",
};
const deletion: BackgroundJob = {
  id: "delete-1", type: "delete_source", status: "queued", source_id: source.id, document_id: null,
  progress: 0, attempts: 0, error: null, created_at: source.created_at, started_at: null, finished_at: null,
};

describe("source deletion management", () => {
  let root: Root;
  let container: HTMLDivElement;
  let current: Source;
  let job: BackgroundJob;
  let requests: string[];
  let notices: string[];
  let changes: number;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.useFakeTimers();
    current = { ...source };
    job = { ...deletion };
    requests = [];
    notices = [];
    changes = 0;
    vi.spyOn(toast, "success").mockImplementation((message) => { notices.push(String(message)); return "notice"; });
    vi.spyOn(toast, "message").mockImplementation((message) => { notices.push(String(message)); return "notice"; });
    vi.stubGlobal("fetch", async (url: RequestInfo | URL, options?: RequestInit) => {
      const path = new URL(String(url)).pathname;
      requests.push(`${options?.method ?? "GET"} ${path}`);
      if (options?.method === "DELETE") {
        job = { ...deletion };
        current = { ...source, status: "deleting", deletion_job: job };
        return Response.json(job, { status: 202 });
      }
      if (path === "/api/v1/jobs/delete-1") return Response.json(job);
      if (path === "/api/v1/sources") return Response.json(job.status === "succeeded" ? [] : [current]);
      throw new Error(`Unexpected request: ${path}`);
    });
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  async function render(layout: "card" | "row" | "compact" = "card") {
    const changed = () => { changes += 1; };
    await act(async () => root.render(
      <NextIntlClientProvider locale="zh-CN" timeZone="UTC" messages={messages}>
        <TooltipProvider><KnowledgeProvider><OctxImportProvider><OctxExportProvider>
          {layout === "card" ? <SourceCard source={current} onChanged={changed} />
            : layout === "row" ? <SourceRow source={current} first onChanged={changed} />
              : <KnowledgeWorkspace variant="compact" />}
        </OctxExportProvider></OctxImportProvider></KnowledgeProvider></TooltipProvider>
      </NextIntlClientProvider>,
    ));
  }

  async function click(label: string, parent: ParentNode = container) {
    const button = [...parent.querySelectorAll<HTMLButtonElement>("button")]
      .find((item) => item.getAttribute("aria-label") === label || item.textContent === label);
    expect(button, `button ${label}`).toBeDefined();
    await act(async () => button!.click());
  }

  it("keeps accepted deletion pending until the public job succeeds", async () => {
    await render();
    await click("删除信源");
    await click("删除信源", document.querySelector('[role="alertdialog"]')!);
    expect(notices).not.toContain("信源已删除");
    expect(container.textContent).toContain("正在删除");
    expect(container.querySelector('a[href="/knowledge/source-1"]')).toBeNull();
    expect(container.querySelector<HTMLButtonElement>('button[aria-label="导出为 .octx"]')?.disabled ?? true).toBe(true);
    job = { ...deletion, status: "succeeded", progress: 1, finished_at: source.updated_at };
    await act(async () => vi.advanceTimersByTimeAsync(1100));
    expect(notices).toContain("信源已删除");
    expect(changes).toBeGreaterThan(0);
  });

  it.each(["card", "row", "compact"] as const)("retains paused deletion with manual continuation in %s", async (layout) => {
    job = { ...deletion, status: "paused", error: "Wait for manual recovery" };
    current = { ...source, status: "deleting", deletion_job: job };
    await render(layout);
    expect(container.textContent).toContain("等待手动继续");
    expect(container.querySelector('a[href="/knowledge/source-1"]')).toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(2100));
    expect(requests.filter((item) => item.startsWith("DELETE") || item.includes("/jobs/"))).toEqual([]);
    await click("继续删除");
    expect(requests.filter((item) => item.startsWith("DELETE"))).toEqual(["DELETE /api/v1/sources/source-1"]);
    expect(container.textContent).toContain("正在删除");
    expect(notices).not.toContain("信源已删除");
  });

  it("keeps failed deletion hidden and resumable after reloading the row", async () => {
    job = { ...deletion, status: "failed", error: "Storage unavailable" };
    current = { ...source, status: "delete_failed", deletion_job: job };
    await render("row");
    expect(container.textContent).toContain("删除失败");
    expect(container.querySelector('a[href="/knowledge/source-1"]')).toBeNull();
    await click("继续删除");
    expect(container.textContent).toContain("正在删除");
    expect(requests.filter((item) => item.startsWith("DELETE"))).toEqual(["DELETE /api/v1/sources/source-1"]);
  });
});
