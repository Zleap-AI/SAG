/** @vitest-environment jsdom */

import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Doc, Source } from "@/lib/types";
import zh from "@/messages/zh-CN.json";
import { DocumentList } from "./document-list";
import { OctxExportProvider } from "./octx-export-provider";
import { useSourceContent } from "./use-source-content";

const paused: Doc = {
  id: "doc-1", source_id: "source-1", filename: "report.md", content_type: "text/markdown",
  size_bytes: 123, status: "paused", has_publication: true, chunk_count: 20, event_count: 4,
  progress: 52, token_usage: 100, error: null, original_file_available: true,
  processing_stage: "waiting_confirmation", processed_chunks: 8, total_chunks: 20,
  created_at: "2026-10-07T00:00:00Z", updated_at: "2026-10-07T00:00:00Z",
};
const source: Source = {
  id: "source-1", name: "Reports", description: "", source_type: "document", connector_kind: "file_upload",
  status: "active", document_count: 1, chunk_count: 20, event_count: 4,
  created_at: paused.created_at, updated_at: paused.updated_at,
};

function Controls({ variant }: { variant: "normal" | "compact" }) {
  const { documents, documentActivities, mutateDocument } = useSourceContent("source-1");
  return <DocumentList sourceId="source-1" sourceName="Reports" documents={documents ?? []}
    activities={documentActivities} onAction={mutateDocument} variant={variant} />;
}

describe("document restart confirmation", () => {
  let root: Root;
  let container: HTMLDivElement;
  let current: Doc;
  let posts: string[];
  let replacementBodies: unknown[];
  let targetChanges: boolean;

  function json(value: unknown, status = 200) {
    return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
  }

  function challenge(token: string, reason = "resume_configuration_changed") {
    return json({ error: {
      code: "document_restart_required", message: "Confirm processing again",
      restart_reason: reason, confirmation_token: token,
    } }, 409);
  }

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.useFakeTimers();
    current = { ...paused };
    posts = [];
    replacementBodies = [];
    targetChanges = false;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    vi.stubGlobal("fetch", async (url: RequestInfo | URL, options?: RequestInit) => {
      const path = new URL(String(url)).pathname;
      if (options?.method === "POST") {
        posts.push(path);
        if (path.endsWith("/resume")) return challenge("nonce-a");
        if (path.endsWith("/reprocess")) {
          replacementBodies.push(JSON.parse(String(options.body)));
          if (targetChanges && replacementBodies.length === 1) return challenge("nonce-b", "confirmation_target_changed");
          current = { ...paused, status: "pending", processing_stage: "queued", progress: 0 };
          return json({
            id: "replacement", type: "process_document", status: "queued", source_id: source.id,
            document_id: paused.id, progress: 0, attempts: 0, error: null,
            created_at: paused.created_at, started_at: null, finished_at: null,
          });
        }
      }
      if (path.endsWith("/documents")) return json([current]);
      if (path.endsWith("/documents/doc-1")) return json(current);
      if (path.endsWith("/sources/source-1")) return json(source);
      throw new Error(`Unexpected request: ${path}`);
    });
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  async function render(variant: "normal" | "compact" = "normal") {
    await act(async () => root.render(
      <NextIntlClientProvider locale="zh-CN" timeZone="UTC" messages={zh}>
        <OctxExportProvider><Controls variant={variant} /></OctxExportProvider>
      </NextIntlClientProvider>,
    ));
  }

  async function resume() {
    await act(async () => container.querySelector<HTMLButtonElement>('button[title="继续抽取"]')!.click());
  }

  async function clickDialogButton(label: string) {
    const dialog = document.querySelector('[role="alertdialog"]');
    const button = [...(dialog?.querySelectorAll("button") ?? [])].find((item) => item.textContent === label);
    expect(button).toBeDefined();
    await act(async () => button!.click());
  }

  it.each(["normal", "compact"] as const)("cancels without a replacement in the %s list", async (variant) => {
    await render(variant);
    await resume();
    expect(document.querySelector('[role="alertdialog"]')?.textContent).toContain("已发布版本");
    expect(replacementBodies).toEqual([]);
    await clickDialogButton("取消");
    expect(document.querySelector('[role="alertdialog"]')).toBeNull();
    expect(posts).toEqual(["/api/v1/sources/source-1/documents/doc-1/resume"]);
    expect(container.textContent).toContain("已暂停");
    expect(container.querySelector<HTMLButtonElement>('button[title="导出此文档为 .octx"]')?.disabled).toBe(false);
  });

  it("confirms the exact target through the existing source controller", async () => {
    await render();
    await resume();
    expect(document.querySelector('[role="alertdialog"]')).not.toBeNull();
    await clickDialogButton("重新处理整份文档");
    expect(replacementBodies).toEqual([{ confirmation_token: "nonce-a" }]);
    expect(document.querySelector('[role="alertdialog"]')).toBeNull();
    expect(container.textContent).toContain("等待处理");
  });

  it("requires another click when the confirmed target changes", async () => {
    targetChanges = true;
    await render();
    await resume();
    await clickDialogButton("重新处理整份文档");
    expect(replacementBodies).toEqual([{ confirmation_token: "nonce-a" }]);
    expect(document.querySelector('[role="alertdialog"]')?.textContent).toContain("重新确认");
    await clickDialogButton("重新处理整份文档");
    expect(replacementBodies).toEqual([{ confirmation_token: "nonce-a" }, { confirmation_token: "nonce-b" }]);
    expect(document.querySelector('[role="alertdialog"]')).toBeNull();
  });
});
