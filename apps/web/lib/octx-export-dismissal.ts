import type { PersistedOctxExportTask } from "@/lib/octx-export-tasks";

export function exportDismissDelay(task: PersistedOctxExportTask): number | null {
  if (task.transfer.status === "ready") {
    return task.autoDownloaded ? 5_000 : null;
  }
  if (task.transfer.status === "cancelled" || task.transfer.status === "expired") {
    return 3_000;
  }
  return null;
}
