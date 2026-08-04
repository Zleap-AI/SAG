import type {
  BackgroundJob,
  Doc,
  DocumentStatus,
} from "./types";

export type DocumentAction = "reprocess" | "pause" | "resume" | "delete";

export type DocumentActivityPhase =
  | DocumentStatus
  | "requeueing"
  | "pausing"
  | "resuming"
  | "deleting"
  | "waiting-retry";

export interface DocumentMutationState {
  action: DocumentAction;
  previousStatus: DocumentStatus;
  previousProgress: number;
  startedAt: number;
  job?: BackgroundJob;
}

export interface DocumentActivity {
  phase: DocumentActivityPhase;
  progress: number;
  busy: boolean;
  poll: boolean;
  error: string | null;
}

export type DocumentActivityLabelKey =
  | DocumentStatus
  | "requeueing"
  | "pausing"
  | "resuming"
  | "deleting"
  | "waitingRetry";

const PROCESSING_STATES = new Set<DocumentStatus>([
  "pending",
  "loading",
  "extracting",
]);
const FAILED_POLLING_WINDOW_MS = 15_000;

function clampProgress(value: number) {
  return Math.min(100, Math.max(0, Math.round(value || 0)));
}

export function documentActivityLabelKey(
  phase: DocumentActivityPhase,
): DocumentActivityLabelKey {
  return phase === "waiting-retry" ? "waitingRetry" : phase;
}

export function documentActivityShowsProgress(phase: DocumentActivityPhase) {
  return phase !== "ready" && phase !== "deleting";
}

export function beginDocumentMutation(
  document: Doc,
  action: DocumentAction,
  startedAt = Date.now(),
): DocumentMutationState {
  return {
    action,
    previousStatus: document.status,
    previousProgress: clampProgress(document.progress),
    startedAt,
  };
}

export function isDocumentJobTerminal(status: BackgroundJob["status"]) {
  return status === "succeeded" || status === "failed" || status === "paused";
}

export function shouldApplyDocumentJobResponse(
  current: BackgroundJob,
  incoming: BackgroundJob,
  requestId: number,
  latestRequestId: number | undefined,
) {
  if (
    requestId !== latestRequestId
    || current.id !== incoming.id
  ) return false;
  return !isDocumentJobTerminal(current.status) || isDocumentJobTerminal(incoming.status);
}

export function isLatestDocumentRefresh(requestId: number, latestRequestId: number) {
  return requestId === latestRequestId;
}

export function isCurrentDocumentSource(expected: string, current: string) {
  return expected === current;
}

export async function runDocumentPollingCycle(
  poll: () => Promise<void>,
  scheduleNext: () => void,
) {
  await poll();
  scheduleNext();
}

export function shouldKeepDocumentMutation(
  document: Doc | undefined,
  mutation: DocumentMutationState,
) {
  if (mutation.action === "delete") return Boolean(document);
  if (!mutation.job || !isDocumentJobTerminal(mutation.job.status)) return true;
  if (!document) return false;
  if (mutation.job.status === "paused" && document.status === "paused") return false;
  if (mutation.action === "pause") {
    if (document.status === "paused") return false;
    if (mutation.job.status === "succeeded") return document.status !== "ready";
    if (mutation.job.status === "failed") return document.status !== "failed";
    return true;
  }
  if (mutation.job.status === "failed") return document.status !== "failed";
  if (mutation.action === "reprocess") return document.status !== "ready";
  if (mutation.action === "resume") {
    return document.status !== "ready" && document.status !== "failed";
  }
  return false;
}

export function failedPollingDeadline(
  previous: DocumentStatus | undefined,
  next: DocumentStatus,
  now = Date.now(),
) {
  return previous && PROCESSING_STATES.has(previous) && next === "failed"
    ? now + FAILED_POLLING_WINDOW_MS
    : undefined;
}

export function shouldPollDocument(
  document: Doc,
  mutation: DocumentMutationState | undefined,
  now = Date.now(),
  failedUntil?: number,
) {
  if (mutation && shouldKeepDocumentMutation(document, mutation)) return true;
  if (PROCESSING_STATES.has(document.status)) return true;
  return document.status === "failed" && typeof failedUntil === "number" && failedUntil > now;
}

export function deriveDocumentActivity(
  document: Doc,
  mutation?: DocumentMutationState,
  now = Date.now(),
  failedUntil?: number,
): DocumentActivity {
  const progress = clampProgress(
    mutation?.action === "reprocess"
      && mutation.previousStatus === "ready"
      && document.status === "ready"
      ? 0
      : document.progress,
  );
  const poll = shouldPollDocument(document, mutation, now, failedUntil);
  const mutationActive = Boolean(
    mutation && shouldKeepDocumentMutation(document, mutation),
  );

  if (mutation && mutationActive) {
    if (mutation.action === "delete") {
      return { phase: "deleting", progress, busy: true, poll, error: null };
    }
    if (mutation.action === "pause" && document.status !== "paused") {
      return { phase: "pausing", progress, busy: true, poll, error: null };
    }
    if (mutation.action === "resume" && (!mutation.job || mutation.job.status === "queued")) {
      return { phase: "resuming", progress, busy: true, poll, error: null };
    }
    if (mutation.action === "reprocess") {
      if (!mutation.job) {
        return { phase: "requeueing", progress, busy: true, poll, error: null };
      }
      if (mutation.job.status === "queued") {
        const waitingRetry = Boolean(mutation.job.error);
        return {
          phase: waitingRetry ? "waiting-retry" : "pending",
          progress,
          busy: true,
          poll,
          error: mutation.job.error,
        };
      }
    }
  }

  return {
    phase: document.status,
    progress,
    busy: Boolean(mutation && poll),
    poll,
    error: document.status === "failed" ? document.error : null,
  };
}
