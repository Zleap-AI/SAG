import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";
import type { PersistedOctxExportTask } from "@/lib/octx-export-tasks";
import type { OctxTransfer } from "@/lib/types";
import { OctxExportTaskList } from "@/components/features/octx-export-task-center";

function task(
  id: string,
  status: OctxTransfer["status"],
  progress: number,
  error: OctxTransfer["error"] = null,
  progressDetail?: OctxTransfer["progress_detail"],
): PersistedOctxExportTask {
  return {
    transferId: id,
    sourceId: "source-1",
    sourceName: "产品手册",
    filenameHint: "manual",
    autoDownloaded: status === "ready",
    createdAt: "2026-08-11T01:00:00Z",
    transfer: {
      id,
      direction: "export",
      status,
      progress,
      asset: null,
      release: status === "ready" ? { id: "r", version: "1.0.0", package_digest: "d" } : null,
      target_source_id: "source-1",
      installation_id: null,
      allowed_actions: [],
      decision_token: null,
      conflicts: [],
      excluded_documents: [],
      record_counts: {},
      capabilities: {},
      progress_detail: progressDetail,
      validation_report: null,
      warnings: [],
      error,
      cancellation_requested: false,
      created_at: "2026-08-11T01:00:00Z",
      updated_at: "2026-08-11T01:00:00Z",
    },
  };
}

describe("OCTX export task center", () => {
  it("shows workflow progress, cancellation, and repeat download actions", () => {
    const html = renderToStaticMarkup(
      <NextIntlClientProvider locale="zh-CN" timeZone="Asia/Shanghai" messages={messages}>
        <OctxExportTaskList
          tasks={[
            task("queued", "queued", 0),
            task("packaging", "packaging", 0.6),
            task("ready", "ready", 1),
          ]}
          onCancel={vi.fn()}
          onDownload={vi.fn()}
          onDismiss={vi.fn()}
        />
      </NextIntlClientProvider>,
    );

    expect(html).toContain("后台导出任务");
    expect(html).toContain("等待执行");
    expect(html).toContain("正在校验并打包");
    expect(html).toContain("60%");
    expect(html).toContain("取消");
    expect(html).toContain("重新下载");
  });

  it("shows the real export record progress instead of a static snapshot label", () => {
    const html = renderToStaticMarkup(
      <NextIntlClientProvider locale="zh-CN" timeZone="Asia/Shanghai" messages={messages}>
        <OctxExportTaskList
          tasks={[
            task("vectors", "exporting", 0.52, null, {
              phase: "vectors",
              kind: "entity.name",
              completed: 8000,
              total: 11985,
            }),
          ]}
          onCancel={vi.fn()}
          onDownload={vi.fn()}
          onDismiss={vi.fn()}
        />
      </NextIntlClientProvider>,
    );

    expect(html).toContain("正在导出实体向量");
    expect(html).toContain("8,000 / 11,985");
  });

  it("identifies documents that need re-extraction and links to their source", () => {
    const html = renderToStaticMarkup(
      <NextIntlClientProvider locale="zh-CN" timeZone="Asia/Shanghai" messages={messages}>
        <OctxExportTaskList
          tasks={[
            task("failed", "failed", 0.1, {
              code: "octx_source_reextract_required",
              message: "legacy graph",
              retryable: false,
              details: {
                recovery_action: "reprocess_documents",
                event_count: 2,
                documents: [
                  { id: "doc-1", filename: "legacy.md", event_count: 2 },
                ],
              },
            }),
          ]}
          onCancel={vi.fn()}
          onDownload={vi.fn()}
          onDismiss={vi.fn()}
        />
      </NextIntlClientProvider>,
    );

    expect(html).toContain("请重新提取以下文档后再导出");
    expect(html).toContain("legacy.md");
    expect(html).toContain("2 个事项");
    expect(html).toContain('href="/knowledge/source-1"');
    expect(html).toContain("前往文档列表");
  });

  it("offers dismissal for terminal tasks without replacing active cancellation", () => {
    const html = renderToStaticMarkup(
      <NextIntlClientProvider locale="zh-CN" timeZone="Asia/Shanghai" messages={messages}>
        <OctxExportTaskList
          tasks={[
            task("active", "packaging", 0.6),
            task("ready", "ready", 1),
            task("failed", "failed", 0.4),
            task("cancelled", "cancelled", 1),
          ]}
          onCancel={vi.fn()}
          onDownload={vi.fn()}
          onDismiss={vi.fn()}
        />
      </NextIntlClientProvider>,
    );

    expect(html.match(/aria-label="关闭"/g)).toHaveLength(3);
    expect(html).toContain("取消");
    expect(html).toContain("重新下载");
  });
});
