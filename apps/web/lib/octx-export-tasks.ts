import type { OctxTransfer, OctxTransferStatus } from "@/lib/types";

export const OCTX_EXPORT_TASKS_STORAGE_KEY = "sag:octx-export-tasks:v1";
const MAX_PERSISTED_TASKS = 10;

export type OctxExportStage =
  | "waiting"
  | "snapshot"
  | "packaging"
  | "decision"
  | "complete"
  | "failed"
  | "cancelled";

export interface PersistedOctxExportTask {
  transferId: string;
  sourceId: string;
  sourceName: string;
  filenameHint?: string;
  downloadError?: string;
  autoDownloaded: boolean;
  createdAt: string;
  transfer: OctxTransfer;
}

export type NewOctxExportTask = Omit<
  PersistedOctxExportTask,
  "autoDownloaded" | "transfer"
>;

const TERMINAL_STATUSES: OctxTransferStatus[] = [
  "ready",
  "failed",
  "cancelled",
  "expired",
];

function isTransfer(value: unknown): value is OctxTransfer {
  if (!value || typeof value !== "object") return false;
  const transfer = value as Partial<OctxTransfer>;
  return (
    typeof transfer.id === "string" &&
    transfer.direction === "export" &&
    typeof transfer.status === "string" &&
    typeof transfer.progress === "number"
  );
}

function isTask(value: unknown): value is PersistedOctxExportTask {
  if (!value || typeof value !== "object") return false;
  const task = value as Partial<PersistedOctxExportTask>;
  return (
    typeof task.transferId === "string" &&
    typeof task.sourceId === "string" &&
    typeof task.sourceName === "string" &&
    (task.filenameHint === undefined || typeof task.filenameHint === "string") &&
    (task.downloadError === undefined || typeof task.downloadError === "string") &&
    typeof task.autoDownloaded === "boolean" &&
    typeof task.createdAt === "string" &&
    isTransfer(task.transfer) &&
    task.transfer.id === task.transferId
  );
}

export function parseExportTasks(raw: string | null): PersistedOctxExportTask[] {
  if (!raw) return [];
  try {
    const payload = JSON.parse(raw) as { version?: unknown; tasks?: unknown };
    if (payload.version !== 1 || !Array.isArray(payload.tasks)) return [];
    return payload.tasks.filter(isTask).slice(0, MAX_PERSISTED_TASKS);
  } catch {
    return [];
  }
}

export function serializeExportTasks(tasks: PersistedOctxExportTask[]): string {
  return JSON.stringify({ version: 1, tasks: tasks.slice(0, MAX_PERSISTED_TASKS) });
}

export function mergeExportTransfer(
  tasks: PersistedOctxExportTask[],
  metadata: NewOctxExportTask,
  transfer: OctxTransfer,
): PersistedOctxExportTask[] {
  const existing = tasks.find((task) => task.transferId === transfer.id);
  const merged: PersistedOctxExportTask = {
    transferId: transfer.id,
    sourceId: existing?.sourceId ?? metadata.sourceId,
    sourceName: existing?.sourceName ?? metadata.sourceName,
    filenameHint: existing?.filenameHint ?? metadata.filenameHint,
    downloadError: existing?.downloadError,
    autoDownloaded: existing?.autoDownloaded ?? false,
    createdAt: existing?.createdAt ?? metadata.createdAt,
    transfer,
  };
  return [merged, ...tasks.filter((task) => task.transferId !== transfer.id)]
    .sort((left, right) => right.createdAt.localeCompare(left.createdAt))
    .slice(0, MAX_PERSISTED_TASKS);
}

export function exportStage(status: OctxTransferStatus): OctxExportStage {
  if (status === "exporting") return "snapshot";
  if (status === "packaging") return "packaging";
  if (status === "decision_required") return "decision";
  if (status === "ready") return "complete";
  if (status === "failed") return "failed";
  if (status === "cancelled" || status === "expired") return "cancelled";
  return "waiting";
}

export function isActiveExportForSource(
  tasks: PersistedOctxExportTask[],
  sourceId: string,
): boolean {
  return tasks.some(
    (task) =>
      task.sourceId === sourceId && !TERMINAL_STATUSES.includes(task.transfer.status),
  );
}

export function isTerminalExportStatus(status: OctxTransferStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}
