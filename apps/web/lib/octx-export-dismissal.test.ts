import { describe, expect, it } from "vitest";

import { exportDismissDelay } from "@/lib/octx-export-dismissal";
import type { PersistedOctxExportTask } from "@/lib/octx-export-tasks";
import type { OctxTransferStatus } from "@/lib/types";

function task(
  status: OctxTransferStatus,
  autoDownloaded = false,
): PersistedOctxExportTask {
  return {
    transferId: `transfer-${status}`,
    sourceId: "source-1",
    sourceName: "产品手册",
    autoDownloaded,
    createdAt: "2026-08-11T01:00:00Z",
    transfer: {
      id: `transfer-${status}`,
      direction: "export",
      status,
      progress: 1,
      asset: null,
      release: null,
      target_source_id: "source-1",
      installation_id: null,
      allowed_actions: [],
      decision_token: null,
      conflicts: [],
      excluded_documents: [],
      record_counts: {},
      capabilities: {},
      validation_report: null,
      warnings: [],
      error: null,
      cancellation_requested: false,
      created_at: "2026-08-11T01:00:00Z",
      updated_at: "2026-08-11T01:00:00Z",
    },
  };
}

describe("OCTX export task dismissal", () => {
  it("auto-dismisses downloaded success and cancelled terminal tasks", () => {
    expect(exportDismissDelay(task("ready", true))).toBe(5_000);
    expect(exportDismissDelay(task("cancelled"))).toBe(3_000);
    expect(exportDismissDelay(task("expired"))).toBe(3_000);
  });

  it("keeps actionable and unfinished tasks visible", () => {
    expect(exportDismissDelay(task("ready", false))).toBeNull();
    expect(exportDismissDelay(task("failed"))).toBeNull();
    expect(exportDismissDelay(task("packaging"))).toBeNull();
    expect(exportDismissDelay(task("decision_required"))).toBeNull();
  });
});
