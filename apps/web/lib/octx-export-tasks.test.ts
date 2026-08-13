import { describe, expect, it } from "vitest";

import {
  exportStage,
  isActiveExportForSource,
  mergeExportTransfer,
  parseExportTasks,
  serializeExportTasks,
  type PersistedOctxExportTask,
} from "@/lib/octx-export-tasks";
import type { OctxTransfer } from "@/lib/types";

function transfer(id: string, status: OctxTransfer["status"], progress = 0): OctxTransfer {
  return {
    id,
    direction: "export",
    status,
    progress,
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
  };
}

describe("OCTX export task persistence", () => {
  it("rejects malformed or obsolete persisted payloads", () => {
    expect(parseExportTasks(null)).toEqual([]);
    expect(parseExportTasks("not-json")).toEqual([]);
    expect(parseExportTasks('{"version":2,"tasks":[]}')).toEqual([]);
    expect(parseExportTasks('{"version":1,"tasks":[{"transferId":4}]}')).toEqual([]);
  });

  it("keeps task metadata and download marker while merging transfer progress", () => {
    const existing: PersistedOctxExportTask = {
      transferId: "transfer-1",
      sourceId: "source-1",
      sourceName: "产品手册",
      filenameHint: "manual",
      autoDownloaded: true,
      createdAt: "2026-08-11T01:00:00Z",
      transfer: transfer("transfer-1", "queued"),
    };

    const tasks = mergeExportTransfer(
      [existing],
      {
        transferId: "transfer-1",
        sourceId: "source-1",
        sourceName: "ignored replacement",
        createdAt: "2026-08-11T02:00:00Z",
      },
      transfer("transfer-1", "packaging", 0.6),
    );

    expect(tasks).toHaveLength(1);
    expect(tasks[0]).toMatchObject({
      sourceName: "产品手册",
      filenameHint: "manual",
      autoDownloaded: true,
      transfer: { status: "packaging", progress: 0.6 },
    });
    expect(parseExportTasks(serializeExportTasks(tasks))).toEqual(tasks);
  });

  it("deduplicates by transfer id and retains only the newest ten tasks", () => {
    let tasks: PersistedOctxExportTask[] = [];
    for (let index = 0; index < 12; index += 1) {
      const id = `transfer-${index}`;
      tasks = mergeExportTransfer(
        tasks,
        {
          transferId: id,
          sourceId: `source-${index}`,
          sourceName: `Source ${index}`,
          createdAt: `2026-08-11T${String(index).padStart(2, "0")}:00:00Z`,
        },
        transfer(id, "ready", 1),
      );
    }
    expect(tasks).toHaveLength(10);
    expect(tasks.map((task) => task.transferId)).toEqual([
      "transfer-11",
      "transfer-10",
      "transfer-9",
      "transfer-8",
      "transfer-7",
      "transfer-6",
      "transfer-5",
      "transfer-4",
      "transfer-3",
      "transfer-2",
    ]);
  });

  it("derives workflow stages and per-source active state from server status", () => {
    expect(exportStage("queued")).toBe("waiting");
    expect(exportStage("exporting")).toBe("snapshot");
    expect(exportStage("packaging")).toBe("packaging");
    expect(exportStage("decision_required")).toBe("decision");
    expect(exportStage("ready")).toBe("complete");
    expect(exportStage("failed")).toBe("failed");

    const tasks = mergeExportTransfer(
      [],
      {
        transferId: "transfer-1",
        sourceId: "source-1",
        sourceName: "Source",
        createdAt: "2026-08-11T01:00:00Z",
      },
      transfer("transfer-1", "packaging", 0.6),
    );
    expect(isActiveExportForSource(tasks, "source-1")).toBe(true);
    expect(isActiveExportForSource(tasks, "source-2")).toBe(false);
    expect(
      isActiveExportForSource(
        [{ ...tasks[0], transfer: transfer("transfer-1", "ready", 1) }],
        "source-1",
      ),
    ).toBe(false);
  });
});
