import { describe, expect, it, vi } from "vitest";

import { OctxExportTaskManager } from "@/lib/octx-export-manager";
import {
  mergeExportTransfer,
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
    release: status === "ready" ? { id: "release-1", version: "1.0.0", package_digest: "digest" } : null,
    target_source_id: "source-1",
    installation_id: null,
    allowed_actions: status === "decision_required" ? ["export_ready_only", "cancel"] : [],
    decision_token: status === "decision_required" ? "signed" : null,
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

function task(serverTransfer: OctxTransfer, autoDownloaded = false): PersistedOctxExportTask {
  return mergeExportTransfer(
    [],
    {
      transferId: serverTransfer.id,
      sourceId: "source-1",
      sourceName: "产品手册",
      filenameHint: "manual",
      createdAt: "2026-08-11T01:00:00Z",
    },
    serverTransfer,
  ).map((item) => ({ ...item, autoDownloaded }))[0];
}

describe("OCTX export task manager", () => {
  it("dismisses a task from memory and persisted storage", async () => {
    let stored = serializeExportTasks([
      task(transfer("transfer-1", "failed", 0.4)),
      task(transfer("transfer-2", "ready", 1), true),
    ]);
    const manager = new OctxExportTaskManager({
      load: () => stored,
      save: (value) => { stored = value; },
      getTransfer: vi.fn(),
      startExport: vi.fn(),
      decideExport: vi.fn(),
      cancelDecision: vi.fn(),
      cancelTransfer: vi.fn(),
      download: vi.fn(),
      now: () => "2026-08-11T02:00:00Z",
    });

    await manager.hydrate();
    manager.dismiss("transfer-1");

    expect(manager.snapshot().map((item) => item.transferId)).toEqual(["transfer-2"]);
    expect(JSON.parse(stored)).toMatchObject({
      version: 1,
      tasks: [{ transferId: "transfer-2" }],
    });
  });

  it("hydrates an active task, resumes polling, and auto-downloads ready exactly once", async () => {
    let stored = serializeExportTasks([task(transfer("transfer-1", "exporting", 0.1))]);
    const download = vi.fn().mockResolvedValue(undefined);
    const getTransfer = vi.fn().mockResolvedValue(transfer("transfer-1", "ready", 1));
    const manager = new OctxExportTaskManager({
      load: () => stored,
      save: (value) => { stored = value; },
      getTransfer,
      startExport: vi.fn(),
      decideExport: vi.fn(),
      cancelDecision: vi.fn(),
      cancelTransfer: vi.fn(),
      download,
      now: () => "2026-08-11T02:00:00Z",
    });

    await manager.hydrate();
    await manager.refresh();

    expect(getTransfer).toHaveBeenCalledWith("transfer-1");
    expect(download).toHaveBeenCalledTimes(1);
    expect(manager.snapshot()[0]).toMatchObject({
      autoDownloaded: true,
      transfer: { status: "ready", progress: 1 },
    });
    await manager.refresh();
    expect(download).toHaveBeenCalledTimes(1);
  });

  it("does not download a completed task again after a remount", async () => {
    const download = vi.fn().mockResolvedValue(undefined);
    const manager = new OctxExportTaskManager({
      load: () => serializeExportTasks([task(transfer("transfer-1", "ready", 1), true)]),
      save: vi.fn(),
      getTransfer: vi.fn(),
      startExport: vi.fn(),
      decideExport: vi.fn(),
      cancelDecision: vi.fn(),
      cancelTransfer: vi.fn(),
      download,
      now: () => "2026-08-11T02:00:00Z",
    });

    await manager.hydrate();

    expect(download).not.toHaveBeenCalled();
  });

  it("keeps the last server state when a refresh request temporarily fails", async () => {
    const previous = task(transfer("transfer-1", "packaging", 0.6));
    const manager = new OctxExportTaskManager({
      load: () => serializeExportTasks([previous]),
      save: vi.fn(),
      getTransfer: vi.fn().mockRejectedValue(new Error("offline")),
      startExport: vi.fn(),
      decideExport: vi.fn(),
      cancelDecision: vi.fn(),
      cancelTransfer: vi.fn(),
      download: vi.fn(),
      now: () => "2026-08-11T02:00:00Z",
    });

    await manager.hydrate();
    await manager.refresh();

    expect(manager.snapshot()[0].transfer).toEqual(previous.transfer);
  });

  it("creates, decides, and cancels through the API while preserving task identity", async () => {
    const startExport = vi.fn().mockResolvedValue(transfer("transfer-1", "decision_required"));
    const decideExport = vi.fn().mockResolvedValue(transfer("transfer-1", "queued"));
    const cancelDecision = vi.fn().mockResolvedValue(transfer("transfer-1", "cancelled", 1));
    const cancelTransfer = vi.fn().mockResolvedValue(transfer("transfer-1", "cancelled", 1));
    const manager = new OctxExportTaskManager({
      load: () => null,
      save: vi.fn(),
      getTransfer: vi.fn(),
      startExport,
      decideExport,
      cancelDecision,
      cancelTransfer,
      download: vi.fn(),
      now: () => "2026-08-11T02:00:00Z",
    });

    await manager.hydrate();
    await manager.start("source-1", "产品手册");
    await manager.confirmReadyOnly("transfer-1", "signed");
    await manager.cancel("transfer-1");

    await manager.start("source-1", "产品手册");
    await manager.cancelDecision("transfer-1", "signed");

    expect(startExport).toHaveBeenCalledWith("source-1");
    expect(decideExport).toHaveBeenCalledWith("transfer-1", "signed");
    expect(cancelTransfer).toHaveBeenCalledWith("transfer-1");
    expect(cancelDecision).toHaveBeenCalledWith("transfer-1", "signed");
    expect(manager.snapshot()[0]).toMatchObject({
      transferId: "transfer-1",
      sourceId: "source-1",
      transfer: { status: "cancelled" },
    });
  });
});
