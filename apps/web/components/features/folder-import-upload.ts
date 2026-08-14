import { ApiError } from "@/lib/api";
import {
  folderImportDiagnosticData,
  type DiagEventType,
} from "@/lib/diagnostics";
import {
  hasUnresolvedFolderImportConflicts,
  uploadableFolderImportItems,
  type FolderImportItem,
  type FolderImportPlan,
} from "@/lib/folder-import";

export interface CompletedFolderImportBatch {
  attempted: number;
  succeeded: number;
  failed: number;
  cancelled: number;
}

export interface FolderImportUploadFailure {
  item: FolderImportItem;
  message: string;
}

export interface FolderImportUploadProgress {
  item: FolderImportItem;
  current: number;
  total: number;
  percent: number;
}

export interface FolderImportUploadRunResult {
  batch: CompletedFolderImportBatch;
  failures: FolderImportUploadFailure[];
  attempted: number;
  succeeded: number;
  cancelled: number;
}

interface FolderImportUploadSessionOptions {
  batchId: string;
  upload: (input: {
    item: FolderImportItem;
    batchId: string;
    onProgress: (percent: number) => void;
  }) => Promise<{ requestId?: string }>;
  record: (type: DiagEventType, data: Record<string, unknown>) => void;
  onFinished: (result: CompletedFolderImportBatch) => void;
  onProgress?: (progress: FolderImportUploadProgress) => void;
  fallbackErrorMessage?: string;
}

export interface FolderImportUploadSession {
  cancel: () => void;
  dismiss: () => void;
  runInitial: (
    items: FolderImportItem[],
  ) => Promise<FolderImportUploadRunResult>;
  retryFailures: () => Promise<FolderImportUploadRunResult | null>;
}

export function folderImportDispatchItems(
  plan: FolderImportPlan,
  finallyConfirmed: boolean,
): FolderImportItem[] {
  if (!finallyConfirmed || hasUnresolvedFolderImportConflicts(plan)) return [];
  return uploadableFolderImportItems(plan);
}

export function createFolderImportUploadSession({
  batchId,
  upload,
  record,
  onFinished,
  onProgress,
  fallbackErrorMessage = "Upload failed",
}: FolderImportUploadSessionOptions): FolderImportUploadSession {
  let cancelled = false;
  let terminalRequested = false;
  let running = false;
  let initialStarted = false;
  let completionNotified = false;
  let attempt = 0;
  let failures: FolderImportUploadFailure[] = [];
  let batch: CompletedFolderImportBatch = {
    attempted: 0,
    succeeded: 0,
    failed: 0,
    cancelled: 0,
  };

  function notifyCompletionOnce() {
    if (completionNotified || batch.attempted === 0) return;
    completionNotified = true;
    onFinished({ ...batch });
  }

  async function run(
    items: FolderImportItem[],
  ): Promise<FolderImportUploadRunResult> {
    if (running) throw new Error("Folder import upload session is already running");
    running = true;
    cancelled = false;
    attempt += 1;
    const nextFailures: FolderImportUploadFailure[] = [];
    let attempted = 0;
    let succeeded = 0;

    for (const [index, item] of items.entries()) {
      if (cancelled) break;
      attempted += 1;
      onProgress?.({ item, current: index + 1, total: items.length, percent: 0 });
      const startedAt = Date.now();

      try {
        const result = await upload({
          item,
          batchId,
          onProgress: (percent) =>
            onProgress?.({
              item,
              current: index + 1,
              total: items.length,
              percent,
            }),
        });
        succeeded += 1;
        record(
          "knowledge.folder_upload",
          folderImportDiagnosticData({
            batch_id: batchId,
            filename: item.name,
            size_bytes: item.file.size,
            outcome: "succeeded",
            duration_ms: Date.now() - startedAt,
            request_id: result.requestId,
            attempt,
          }),
        );
      } catch (error) {
        const apiError = error instanceof ApiError ? error : null;
        nextFailures.push({
          item,
          message: apiError?.message ?? fallbackErrorMessage,
        });
        record(
          "knowledge.folder_upload",
          folderImportDiagnosticData({
            batch_id: batchId,
            filename: item.name,
            size_bytes: item.file.size,
            outcome: "failed",
            duration_ms: Date.now() - startedAt,
            request_id: apiError?.requestId,
            error_code: apiError?.code ?? "unknown_error",
            attempt,
          }),
        );
      }
    }

    const cancelledCount = items.length - attempted;
    failures = nextFailures;
    batch = {
      attempted: batch.attempted + attempted,
      succeeded: batch.succeeded + succeeded,
      failed: failures.length,
      cancelled: batch.cancelled + cancelledCount,
    };
    const result = {
      batch: { ...batch },
      failures: [...failures],
      attempted,
      succeeded,
      cancelled: cancelledCount,
    };
    running = false;

    if (failures.length === 0 || terminalRequested) notifyCompletionOnce();
    return result;
  }

  return {
    cancel() {
      cancelled = true;
    },
    dismiss() {
      cancelled = true;
      terminalRequested = true;
      if (!running) notifyCompletionOnce();
    },
    runInitial(items) {
      if (initialStarted) {
        throw new Error("Folder import initial queue has already started");
      }
      initialStarted = true;
      return run(items);
    },
    retryFailures() {
      if (failures.length === 0) return Promise.resolve(null);
      return run(failures.map(({ item }) => item));
    },
  };
}
