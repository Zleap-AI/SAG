import { describe, expect, it, vi } from "vitest";

import { OctxImportTaskManager } from "@/lib/octx-import-manager";
import type { OctxTransfer } from "@/lib/types";

function transfer(status: OctxTransfer["status"], progress = 0): OctxTransfer {
  return {
    id: "import-1", direction: "import", status, progress,
    asset: { id: "asset-1", name: "产品手册" }, release: null,
    target_source_id: null, installation_id: null, allowed_actions: [],
    decision_token: null, conflicts: [], excluded_documents: [], record_counts: {},
    capabilities: {}, progress_detail: { phase: "rebuilding_documents", completed_documents: 2, total_documents: 4 },
    validation_report: null, warnings: [], error: null, cancellation_requested: false,
    created_at: "2026-08-11T01:00:00Z", updated_at: "2026-08-11T01:00:00Z",
  };
}

describe("OCTX import task manager", () => {
  it("persists a started import and resumes polling after hydration", async () => {
    let stored: string | null = null;
    const getTransfer = vi.fn().mockResolvedValue(transfer("ready", 1));
    const dependencies = {
      load: () => stored,
      save: (value: string) => { stored = value; },
      startImport: vi.fn().mockResolvedValue(transfer("importing", 0.5)),
      getTransfer,
      cancelTransfer: vi.fn(), decideImport: vi.fn(),
      newTransferId: () => "import-1",
      now: () => "2026-08-11T02:00:00Z",
    };
    const manager = new OctxImportTaskManager(dependencies);
    manager.hydrate();
    await manager.start(new File(["package"], "manual.octx"));

    const remounted = new OctxImportTaskManager(dependencies);
    remounted.hydrate();
    await remounted.refresh();

    expect(getTransfer).toHaveBeenCalledWith("import-1");
    expect(remounted.snapshot()[0]).toMatchObject({
      filename: "manual.octx",
      transfer: { status: "ready", progress: 1 },
    });
  });

  it("submits the explicit conflict decision", async () => {
    let stored: string | null = null;
    const decideImport = vi.fn().mockResolvedValue(transfer("queued", 0.1));
    const manager = new OctxImportTaskManager({
      load: () => stored, save: (value) => { stored = value; },
      startImport: vi.fn().mockResolvedValue({ ...transfer("decision_required"), decision_token: "signed" }),
      getTransfer: vi.fn(), cancelTransfer: vi.fn(), decideImport,
      newTransferId: () => "import-1",
      now: () => "2026-08-11T02:00:00Z",
    });
    manager.hydrate();
    await manager.start(new File(["package"], "manual.octx"));
    await manager.decide("import-1", { action: "update", decision_token: "signed", target_source_id: "source-1", discard_local_changes: true });
    expect(decideImport).toHaveBeenCalledWith("import-1", expect.objectContaining({ action: "update", discard_local_changes: true }));
  });

  it("persists the client transfer id before the upload response arrives", async () => {
    let stored: string | null = null;
    let resolveUpload!: (value: OctxTransfer) => void;
    const startImport = vi.fn().mockImplementation(() => new Promise<OctxTransfer>((resolve) => {
      resolveUpload = resolve;
    }));
    const manager = new OctxImportTaskManager({
      load: () => stored,
      save: (value) => { stored = value; },
      startImport,
      getTransfer: vi.fn(), cancelTransfer: vi.fn(), decideImport: vi.fn(),
      newTransferId: () => "client-transfer-id",
      now: () => "2026-08-11T02:00:00Z",
    });
    manager.hydrate();

    const pending = manager.start(new File(["package"], "manual.octx"));

    expect(manager.snapshot()[0]).toMatchObject({
      transferId: "client-transfer-id",
      transfer: { id: "client-transfer-id", status: "uploaded" },
    });
    expect(startImport).toHaveBeenCalledWith(expect.any(File), "client-transfer-id");

    resolveUpload({ ...transfer("importing", 0.5), id: "client-transfer-id" });
    await pending;
  });
});
