import type { OctxTransfer, OctxTransferStatus } from "@/lib/types";

export const OCTX_IMPORT_TASKS_STORAGE_KEY = "sag:octx-import-tasks:v1";

export interface PersistedOctxImportTask {
  transferId: string;
  filename: string;
  createdAt: string;
  completionNotified?: boolean;
  transfer: OctxTransfer;
}

const TERMINAL = new Set<OctxTransferStatus>([
  "ready",
  "failed",
  "cancelled",
  "expired",
]);

export function isTerminalImportStatus(status: OctxTransferStatus) {
  return TERMINAL.has(status);
}

export function parseImportTasks(raw: string | null): PersistedOctxImportTask[] {
  if (!raw) return [];
  try {
    const value: unknown = JSON.parse(raw);
    if (!Array.isArray(value)) return [];
    return value.filter((item): item is PersistedOctxImportTask => {
      if (!item || typeof item !== "object") return false;
      const task = item as Partial<PersistedOctxImportTask>;
      return Boolean(
        typeof task.transferId === "string" &&
          typeof task.filename === "string" &&
          task.transfer &&
          task.transfer.id === task.transferId,
      );
    });
  } catch {
    return [];
  }
}

export function serializeImportTasks(tasks: PersistedOctxImportTask[]) {
  return JSON.stringify(tasks);
}

export type OctxImportStage =
  | "uploaded" | "validating" | "queued" | "importing"
  | "building_shadow" | "rebuilding_documents" | "indexing" | "vectorizing"
  | "validating_shadow" | "switching" | "complete"
  | "decision_required" | "ready" | "failed" | "cancelled" | "expired";

export function importStage(transfer: OctxTransfer): OctxImportStage {
  const phase = transfer.progress_detail?.phase;
  const known: OctxImportStage[] = [
    "building_shadow", "rebuilding_documents", "indexing", "vectorizing",
    "validating_shadow", "switching", "complete",
  ];
  if (phase && known.includes(phase as OctxImportStage)) return phase as OctxImportStage;
  if (transfer.status === "exporting" || transfer.status === "packaging") return "importing";
  return transfer.status;
}

export function importDurationParts(value: number) {
  const total = Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0;
  return {
    hours: Math.floor(total / 3600),
    minutes: Math.floor((total % 3600) / 60),
    seconds: total % 60,
  };
}

export type OctxVectorMode = "reuse" | "generate" | "mixed";

export function vectorProgressMessageKey(mode?: OctxVectorMode) {
  if (mode === "reuse") return "importVectorProgressReuse" as const;
  if (mode === "generate") return "importVectorProgressGenerate" as const;
  if (mode === "mixed") return "importVectorProgressMixed" as const;
  return "importVectorProgressProcess" as const;
}

export function vectorProgressHintKey(mode?: OctxVectorMode) {
  if (mode === "reuse") return "importVectorReuseHint" as const;
  if (mode === "generate") return "importVectorGenerateHint" as const;
  if (mode === "mixed") return "importVectorMixedHint" as const;
  return "importVectorProcessHint" as const;
}
