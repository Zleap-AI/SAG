import { describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";
import { DiagnosticsStore } from "@/lib/diagnostics";
import {
  buildFolderImportPlan,
  resolveFolderImportItem,
  setFolderImportItemSelected,
  type FolderImportItem,
} from "@/lib/folder-import";
import {
  createFolderImportUploadSession,
  folderImportDispatchItems,
} from "./folder-import-upload";

function item(name: string, displayPath = `folder/${name}`): FolderImportItem {
  const file = new File([name], name);
  Object.defineProperty(file, "webkitRelativePath", { value: displayPath });
  return {
    id: `item-${name}`,
    file,
    name,
    displayPath,
    status: "eligible",
    selected: true,
    decision: "upload",
    duplicateWith: null,
  };
}

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function recordWith(store: DiagnosticsStore) {
  return store.record.bind(store);
}

describe("folder import dispatch authorization", () => {
  it("blocks dispatch until every duplicate has an explicit decision", () => {
    const plan = buildFolderImportPlan(
      [new File(["new"], "new.md"), new File(["old"], "old.md")],
      ["old.md"],
      [".md"],
      1024,
    );

    expect(folderImportDispatchItems(plan, false)).toEqual([]);
    expect(folderImportDispatchItems(plan, true)).toEqual([]);
  });

  it("sends only eligible files to upload after final confirmation when conflicts are skipped", async () => {
    const plan = buildFolderImportPlan(
      [new File(["new"], "new.md"), new File(["old"], "old.md")],
      ["old.md"],
      [".md"],
      1024,
    );
    const conflict = plan.items.find((candidate) => candidate.status === "conflict");
    const resolved = resolveFolderImportItem(plan, conflict!.id, "skip");

    const uploadNames: string[] = [];
    const session = createFolderImportUploadSession({
      batchId: "batch-confirmed",
      upload: async ({ item: uploadItem }) => {
        uploadNames.push(uploadItem.name);
        return { requestId: "request-confirmed" };
      },
      record: recordWith(new DiagnosticsStore()),
      onFinished: vi.fn(),
    });

    expect(folderImportDispatchItems(resolved, false)).toEqual([]);
    await session.runInitial(folderImportDispatchItems(resolved, true));
    expect(uploadNames).toEqual(["new.md"]);
  });

  it("does not dispatch a deselected unresolved duplicate", () => {
    const plan = buildFolderImportPlan(
      [new File(["new"], "new.md"), new File(["old"], "old.md")],
      ["old.md"],
      [".md"],
      1024,
    );
    const conflict = plan.items.find((candidate) => candidate.status === "conflict");
    const deselected = setFolderImportItemSelected(plan, conflict!.id, false);

    expect(folderImportDispatchItems(deselected, true).map((item) => item.name)).toEqual([
      "new.md",
    ]);
  });
});

describe("folder import upload session", () => {
  it("starts the next upload only after the active upload settles", async () => {
    const firstUpload = deferred();
    const starts: string[] = [];
    const finished = vi.fn();
    const session = createFolderImportUploadSession({
      batchId: "batch-sequential",
      upload: async ({ item: uploadItem }) => {
        starts.push(uploadItem.name);
        if (uploadItem.name === "first.md") await firstUpload.promise;
        return { requestId: `request-${uploadItem.name}` };
      },
      record: recordWith(new DiagnosticsStore()),
      onFinished: finished,
    });

    const pending = session.runInitial([item("first.md"), item("second.md")]);
    await Promise.resolve();
    expect(starts).toEqual(["first.md"]);

    firstUpload.resolve();
    await pending;
    expect(starts).toEqual(["first.md", "second.md"]);
  });

  it("notifies a terminal accepted queue exactly once without a Done action", async () => {
    const finished = vi.fn();
    const session = createFolderImportUploadSession({
      batchId: "batch-finished",
      upload: async () => ({ requestId: "request-finished" }),
      record: recordWith(new DiagnosticsStore()),
      onFinished: finished,
    });

    const result = await session.runInitial([item("first.md"), item("second.md")]);
    await session.retryFailures();

    expect(result.batch).toEqual({
      attempted: 2,
      succeeded: 2,
      failed: 0,
      cancelled: 0,
    });
    expect(finished).toHaveBeenCalledOnce();
    expect(finished).toHaveBeenCalledWith(result.batch);
  });

  it("stops before later items when the active session is dismissed", async () => {
    const activeUpload = deferred();
    const starts: string[] = [];
    const finished = vi.fn();
    const session = createFolderImportUploadSession({
      batchId: "batch-dismissed",
      upload: async ({ item: uploadItem }) => {
        starts.push(uploadItem.name);
        await activeUpload.promise;
        return { requestId: "request-active" };
      },
      record: recordWith(new DiagnosticsStore()),
      onFinished: finished,
    });

    const pending = session.runInitial([item("active.md"), item("never.md")]);
    await Promise.resolve();
    session.dismiss();
    activeUpload.resolve();
    const result = await pending;

    expect(starts).toEqual(["active.md"]);
    expect(result.batch.cancelled).toBe(1);
    expect(finished).toHaveBeenCalledOnce();
  });

  it("waits for retry completion when cancel remaining leaves an active failure", async () => {
    const activeUpload = deferred();
    const calls: string[] = [];
    let firstAttempt = true;
    const finished = vi.fn();
    const session = createFolderImportUploadSession({
      batchId: "batch-cancel-failure",
      upload: async ({ item: uploadItem }) => {
        calls.push(uploadItem.name);
        if (firstAttempt) {
          firstAttempt = false;
          await activeUpload.promise;
          throw new ApiError(500, "upload_failed", "failed active upload");
        }
        return { requestId: "request-retry" };
      },
      record: recordWith(new DiagnosticsStore()),
      onFinished: finished,
    });

    const pending = session.runInitial([item("active.md"), item("never.md")]);
    await Promise.resolve();
    session.cancel();
    activeUpload.resolve();
    const cancelledRun = await pending;

    expect(calls).toEqual(["active.md"]);
    expect(cancelledRun.batch.cancelled).toBe(1);
    expect(cancelledRun.failures.map(({ item: failedItem }) => failedItem.name)).toEqual([
      "active.md",
    ]);
    expect(finished).not.toHaveBeenCalled();

    const retried = await session.retryFailures();
    expect(calls).toEqual(["active.md", "active.md"]);
    expect(finished).toHaveBeenCalledOnce();
    expect(finished).toHaveBeenCalledWith(retried?.batch);
  });

  it("retries failed items only and preserves the batch ID", async () => {
    const calls: Array<{ name: string; batchId: string }> = [];
    let firstAttempt = true;
    const finished = vi.fn();
    const session = createFolderImportUploadSession({
      batchId: "batch-stable",
      upload: async ({ item: uploadItem, batchId }) => {
        calls.push({ name: uploadItem.name, batchId });
        if (uploadItem.name === "failed.md" && firstAttempt) {
          firstAttempt = false;
          throw new ApiError(500, "upload_failed", "failed", "request-failed");
        }
        return { requestId: `request-${uploadItem.name}` };
      },
      record: recordWith(new DiagnosticsStore()),
      onFinished: finished,
    });

    await session.runInitial([item("failed.md"), item("ok.md")]);
    expect(finished).not.toHaveBeenCalled();
    const retried = await session.retryFailures();

    expect(calls).toEqual([
      { name: "failed.md", batchId: "batch-stable" },
      { name: "ok.md", batchId: "batch-stable" },
      { name: "failed.md", batchId: "batch-stable" },
    ]);
    expect(retried?.failures).toEqual([]);
    expect(finished).toHaveBeenCalledOnce();
  });

  it("notifies once when a completed failed queue is dismissed instead of retried", async () => {
    const finished = vi.fn();
    const session = createFolderImportUploadSession({
      batchId: "batch-failed-dismissed",
      upload: async () => {
        throw new ApiError(500, "upload_failed", "failed");
      },
      record: recordWith(new DiagnosticsStore()),
      onFinished: finished,
    });

    const result = await session.runInitial([item("failed.md")]);
    expect(finished).not.toHaveBeenCalled();

    session.dismiss();
    session.dismiss();
    expect(finished).toHaveBeenCalledOnce();
    expect(finished).toHaveBeenCalledWith(result.batch);
  });

  it("records upload diagnostics through the safe folder-import allowlist", async () => {
    const store = new DiagnosticsStore();
    const unsafeItem = item("secret.md", "/private/customer/secret.md");
    unsafeItem.name = "/private/customer/secret.md";
    const session = createFolderImportUploadSession({
      batchId: "batch-safe",
      upload: async () => {
        throw new ApiError(
          500,
          "upload_failed",
          "contains /private/customer/secret.md",
          "request-safe",
        );
      },
      record: recordWith(store),
      onFinished: vi.fn(),
    });

    await session.runInitial([unsafeItem]);
    const uploadEntry = store
      .snapshot()
      .find(({ type }) => type === "knowledge.folder_upload");

    expect(uploadEntry?.data).toMatchObject({
      batch_id: "batch-safe",
      request_id: "request-safe",
      filename: "secret.md",
      outcome: "failed",
      error_code: "upload_failed",
      attempt: 1,
    });
    expect(uploadEntry?.data).not.toHaveProperty("displayPath");
    expect(JSON.stringify(uploadEntry?.data)).not.toContain("/private/customer");
  });
});
