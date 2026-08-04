import { describe, expect, it } from "vitest";

import type { BackgroundJob, Doc } from "./types";
import {
  beginDocumentMutation,
  deriveDocumentActivity,
  documentActivityLabelKey,
  documentActivityShowsProgress,
  failedPollingDeadline,
  isCurrentDocumentSource,
  isLatestDocumentRefresh,
  isDocumentJobTerminal,
  shouldApplyDocumentJobResponse,
  shouldKeepDocumentMutation,
  shouldPollDocument,
  runDocumentPollingCycle,
} from "./document-activity";

function document(overrides: Partial<Doc> = {}): Doc {
  return {
    id: "document-1",
    source_id: "source-1",
    filename: "guide.pdf",
    content_type: "application/pdf",
    size_bytes: 1_024,
    status: "failed",
    chunk_count: 10,
    event_count: 4,
    progress: 52,
    token_usage: 12_000,
    error: "模型输出被截断",
    created_at: "2026-07-28T09:00:00Z",
    updated_at: "2026-07-28T09:10:00Z",
    ...overrides,
  };
}

function job(overrides: Partial<BackgroundJob> = {}): BackgroundJob {
  return {
    id: "job-1",
    type: "process_document",
    status: "queued",
    source_id: "source-1",
    document_id: "document-1",
    progress: 0.52,
    attempts: 2,
    error: null,
    created_at: "2026-07-28T09:11:00Z",
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

describe("document activity", () => {
  it("keeps a failed checkpoint visible while requeueing", () => {
    const doc = document();
    const mutation = beginDocumentMutation(doc, "reprocess", 1_000);

    expect(deriveDocumentActivity(doc, mutation, 1_001)).toMatchObject({
      phase: "requeueing",
      progress: 52,
      busy: true,
      poll: true,
      error: null,
    });
  });

  it("resets a ready document to zero while reprocessing", () => {
    const doc = document({ status: "ready", progress: 100, error: null });
    const mutation = beginDocumentMutation(doc, "reprocess", 1_000);

    expect(deriveDocumentActivity(doc, mutation, 1_001)).toMatchObject({
      phase: "requeueing",
      progress: 0,
      busy: true,
    });
  });

  it("uses server progress after a ready document actually starts reprocessing", () => {
    const ready = document({ status: "ready", progress: 100, error: null });
    const mutation = {
      ...beginDocumentMutation(ready, "reprocess", 1_000),
      job: job({ status: "running", progress: 0.44 }),
    };
    const extracting = document({ status: "extracting", progress: 44, error: null });

    expect(deriveDocumentActivity(extracting, mutation, 1_500)).toMatchObject({
      phase: "extracting",
      progress: 44,
    });
  });

  it("shows a queued retry distinctly when the job reports another attempt", () => {
    const doc = document();
    const mutation = {
      ...beginDocumentMutation(doc, "reprocess", 1_000),
      job: job({ error: "第 2 次失败，将重试：限流" }),
    };

    expect(deriveDocumentActivity(doc, mutation, 1_500)).toMatchObject({
      phase: "waiting-retry",
      progress: 52,
      poll: true,
      error: "第 2 次失败，将重试：限流",
    });
  });

  it("uses the authoritative document phase once the queued job starts", () => {
    const doc = document({ status: "extracting", progress: 68, error: null });
    const mutation = {
      ...beginDocumentMutation(document(), "reprocess", 1_000),
      job: job({ status: "running", progress: 0.68 }),
    };

    expect(deriveDocumentActivity(doc, mutation, 1_500)).toMatchObject({
      phase: "extracting",
      progress: 68,
      busy: true,
      poll: true,
    });
  });

  it("does not show a stale failure after the document resumes processing", () => {
    const extracting = document({
      status: "extracting",
      progress: 68,
      error: "文档入库失败，请重试；若仍失败，请查看服务日志。",
    });

    expect(deriveDocumentActivity(extracting)).toMatchObject({
      phase: "extracting",
      progress: 68,
      error: null,
    });
  });

  it("keeps polling a freshly failed document for the automatic retry window", () => {
    const deadline = failedPollingDeadline("extracting", "failed", 10_000);
    const activity = deriveDocumentActivity(document(), undefined, 12_000, deadline);

    expect(deadline).toBe(25_000);
    expect(activity.poll).toBe(true);
    expect(activity.busy).toBe(false);
  });

  it("does not poll stable terminal or paused documents", () => {
    expect(shouldPollDocument(document({ status: "ready" }), undefined, 20_000)).toBe(false);
    expect(shouldPollDocument(document({ status: "paused" }), undefined, 20_000)).toBe(false);
    expect(shouldPollDocument(document(), undefined, 20_000, 19_999)).toBe(false);
  });

  it("treats succeeded, failed and paused jobs as terminal", () => {
    expect(isDocumentJobTerminal("succeeded")).toBe(true);
    expect(isDocumentJobTerminal("failed")).toBe(true);
    expect(isDocumentJobTerminal("paused")).toBe(true);
    expect(isDocumentJobTerminal("queued")).toBe(false);
    expect(isDocumentJobTerminal("running")).toBe(false);
  });

  it("accepts the newest terminal job response", () => {
    expect(
      shouldApplyDocumentJobResponse(
        job({ status: "running" }),
        job({ status: "succeeded" }),
        2,
        2,
      ),
    ).toBe(true);
    expect(
      shouldApplyDocumentJobResponse(
        job({ status: "running" }),
        job({ status: "paused" }),
        3,
        3,
      ),
    ).toBe(true);
  });

  it("rejects stale or terminal-regressing job responses", () => {
    expect(
      shouldApplyDocumentJobResponse(
        job({ status: "succeeded" }),
        job({ status: "running" }),
        1,
        2,
      ),
    ).toBe(false);
    expect(
      shouldApplyDocumentJobResponse(
        job({ status: "succeeded" }),
        job({ status: "running" }),
        3,
        3,
      ),
    ).toBe(false);
  });

  it("retains a terminal job overlay until the document response catches up", () => {
    const mutation = {
      ...beginDocumentMutation(document(), "reprocess", 1_000),
      job: job({ status: "succeeded" }),
    };

    expect(shouldKeepDocumentMutation(document(), mutation)).toBe(true);
    expect(
      shouldKeepDocumentMutation(
        document({ status: "ready", progress: 100, error: null }),
        mutation,
      ),
    ).toBe(false);
  });

  it("clears delete and pause overlays only after their document state is reflected", () => {
    const deleting = {
      ...beginDocumentMutation(document(), "delete", 1_000),
      job: undefined,
    };
    const pausing = {
      ...beginDocumentMutation(document({ status: "extracting" }), "pause", 1_000),
      job: job({ status: "paused" }),
    };

    expect(shouldKeepDocumentMutation(undefined, deleting)).toBe(false);
    expect(
      shouldKeepDocumentMutation(document({ status: "extracting" }), pausing),
    ).toBe(true);
    expect(shouldKeepDocumentMutation(document({ status: "paused" }), pausing)).toBe(false);
  });

  it("clears a pause overlay when the job completes or fails before pausing", () => {
    const started = beginDocumentMutation(
      document({ status: "extracting", progress: 80 }),
      "pause",
      1_000,
    );

    expect(
      shouldKeepDocumentMutation(
        document({ status: "ready", progress: 100, error: null }),
        { ...started, job: job({ status: "succeeded" }) },
      ),
    ).toBe(false);
    expect(
      shouldKeepDocumentMutation(
        document({ status: "failed", error: "抽取失败" }),
        { ...started, job: job({ status: "failed", error: "抽取失败" }) },
      ),
    ).toBe(false);
  });

  it("clears reprocess and resume overlays when another view pauses the job", () => {
    const pausedDocument = document({ status: "paused", error: null });
    const pausedJob = job({ status: "paused" });

    expect(
      shouldKeepDocumentMutation(pausedDocument, {
        ...beginDocumentMutation(document(), "reprocess", 1_000),
        job: pausedJob,
      }),
    ).toBe(false);
    expect(
      shouldKeepDocumentMutation(pausedDocument, {
        ...beginDocumentMutation(
          document({ status: "paused" }),
          "resume",
          1_000,
        ),
        job: pausedJob,
      }),
    ).toBe(false);
  });

  it("rejects an older refresh response after a newer request starts", () => {
    expect(isLatestDocumentRefresh(4, 5)).toBe(false);
    expect(isLatestDocumentRefresh(5, 5)).toBe(true);
  });

  it("rejects mutation follow-up work after navigating to another source", () => {
    expect(isCurrentDocumentSource("source-1", "source-1")).toBe(true);
    expect(isCurrentDocumentSource("source-1", "source-2")).toBe(false);
  });

  it("schedules the next poll only after the current requests settle", async () => {
    let release!: () => void;
    const pending = new Promise<void>((resolve) => {
      release = resolve;
    });
    let scheduled = false;
    const cycle = runDocumentPollingCycle(
      () => pending,
      () => {
        scheduled = true;
      },
    );

    expect(scheduled).toBe(false);
    release();
    await cycle;
    expect(scheduled).toBe(true);
  });

  it("maps transient phases to stable translation keys", () => {
    expect(documentActivityLabelKey("requeueing")).toBe("requeueing");
    expect(documentActivityLabelKey("waiting-retry")).toBe("waitingRetry");
    expect(documentActivityLabelKey("extracting")).toBe("extracting");
  });

  it("shows progress for every non-terminal processing outcome including failure", () => {
    expect(documentActivityShowsProgress("pending")).toBe(true);
    expect(documentActivityShowsProgress("extracting")).toBe(true);
    expect(documentActivityShowsProgress("failed")).toBe(true);
    expect(documentActivityShowsProgress("paused")).toBe(true);
    expect(documentActivityShowsProgress("ready")).toBe(false);
    expect(documentActivityShowsProgress("deleting")).toBe(false);
  });
});
