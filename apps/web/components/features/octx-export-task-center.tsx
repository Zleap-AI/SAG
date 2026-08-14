"use client";

import Link from "next/link";
import { Download, FileJson, FileWarning, X } from "lucide-react";
import { useTranslations } from "next-intl";

import {
  exportStage,
  isTerminalExportStatus,
  type PersistedOctxExportTask,
} from "@/lib/octx-export-tasks";
import { useOctxExports } from "@/components/features/octx-export-provider";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";

function exportProgressKey(kind?: string | null) {
  switch (kind) {
    case "documents": return "documents" as const;
    case "chunks": return "chunks" as const;
    case "events": return "events" as const;
    case "entities": return "entities" as const;
    case "event_entities": return "relations" as const;
    case "chunk.heading": return "chunkHeadingVectors" as const;
    case "chunk.content": return "chunkContentVectors" as const;
    case "event.title": return "eventTitleVectors" as const;
    case "event.content": return "eventContentVectors" as const;
    case "entity.name": return "entityVectors" as const;
    case "event_entity.relation": return "relationVectors" as const;
    default: return null;
  }
}

export function OctxExportTaskList({
  tasks,
  onCancel,
  onDownload,
  onDiagnostics,
  onDismiss,
}: {
  tasks: PersistedOctxExportTask[];
  onCancel: (transferId: string) => void;
  onDownload: (transferId: string) => void;
  onDiagnostics: (transferId: string) => void;
  onDismiss: (transferId: string) => void;
}) {
  const t = useTranslations("SourceCard");
  if (!tasks.length) return null;

  return (
    <aside
      aria-live="polite"
      aria-label={t("exportTasksTitle")}
      className="fixed bottom-4 right-4 z-[70] w-[min(24rem,calc(100vw-2rem))] overflow-hidden rounded-xl border bg-background/95 shadow-lift backdrop-blur-md"
    >
      <div className="border-b px-4 py-3 text-sm font-medium">
        {t("exportTasksTitle")}
      </div>
      <div className="max-h-[min(32rem,70vh)] space-y-1 overflow-auto p-2">
        {tasks.map((task) => {
          const { transfer } = task;
          const detail = transfer.progress_detail;
          const progressKey = exportProgressKey(detail?.kind);
          const progressLabel =
            progressKey && typeof detail?.completed === "number" && typeof detail.total === "number"
              ? t(`exportProgress.${progressKey}`, {
                  completed: detail.completed,
                  total: detail.total,
                })
              : t(`exportStage.${exportStage(transfer.status)}`);
          const progress = Math.max(0, Math.min(100, Math.round(transfer.progress * 100)));
          const active = !isTerminalExportStatus(transfer.status) && transfer.status !== "decision_required";
          const recoveryDocuments =
            transfer.error?.code === "octx_source_reextract_required"
              ? transfer.error.details?.documents ?? []
              : [];
          return (
            <div key={task.transferId} className="rounded-lg border bg-card p-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{task.sourceName}</p>
                  {transfer.export_scope === "document" && (
                    <p className="mt-0.5 truncate text-[11px] text-muted-foreground">
                      {t("documentExportBadge")} · {transfer.document_name ?? task.filenameHint}
                    </p>
                  )}
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {progressLabel}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  {active && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="h-7 px-2 text-xs"
                      onClick={() => onCancel(task.transferId)}
                    >
                      <X className="size-3.5" />
                      {t("exportCancel")}
                    </Button>
                  )}
                  {transfer.status === "ready" && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-7 px-2 text-xs"
                      onClick={() => onDownload(task.transferId)}
                    >
                      <Download className="size-3.5" />
                      {t("exportDownloadAgain")}
                    </Button>
                  )}
                  {(transfer.status === "failed" || task.downloadError) && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-7 px-2 text-xs"
                      onClick={() => onDiagnostics(task.transferId)}
                    >
                      <FileJson className="size-3.5" />
                      {t("exportDiagnostics")}
                    </Button>
                  )}
                  {isTerminalExportStatus(transfer.status) && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="size-7"
                      aria-label={t("exportDismiss")}
                      title={t("exportDismiss")}
                      onClick={() => onDismiss(task.transferId)}
                    >
                      <X className="size-3.5" />
                    </Button>
                  )}
                </div>
              </div>
              {active && (
                <div className="mt-2 flex items-center gap-2">
                  <Progress value={progress} className="h-1.5" />
                  <span className="w-9 text-right text-xs tabular-nums text-muted-foreground">
                    {progress}%
                  </span>
                </div>
              )}
              {recoveryDocuments.length > 0 && (
                <div className="mt-3 rounded-md bg-destructive/5 p-2.5 text-xs">
                  <p className="flex items-center gap-1.5 font-medium text-destructive">
                    <FileWarning className="size-3.5" />
                    {t("exportReextractRequired")}
                  </p>
                  <ul className="mt-2 space-y-1 text-muted-foreground">
                    {recoveryDocuments.map((document) => (
                      <li key={document.id} className="break-all">
                        {document.filename} · {t("exportMissingEvents", { count: document.event_count })}
                      </li>
                    ))}
                  </ul>
                  <Button asChild variant="link" size="sm" className="mt-1 h-auto p-0 text-xs">
                    <Link href={`/knowledge/${task.sourceId}`}>
                      {t("exportGoToDocuments")}
                    </Link>
                  </Button>
                </div>
              )}
              {transfer.status === "failed" && recoveryDocuments.length === 0 && (
                <p className="mt-2 break-words text-xs text-destructive">
                  {transfer.error?.message ?? t("exportFailed")}
                </p>
              )}
              {task.downloadError && (
                <p className="mt-2 break-words text-xs text-destructive">
                  {t("exportDownloadFailed")}: {task.downloadError}
                </p>
              )}
            </div>
          );
        })}
      </div>
    </aside>
  );
}

export function OctxExportTaskCenter() {
  const { tasks, cancelTransfer, dismissTransfer, downloadAgain, downloadDiagnostics } = useOctxExports();
  return (
    <OctxExportTaskList
      tasks={tasks}
      onCancel={(transferId) => void cancelTransfer(transferId)}
      onDownload={(transferId) => void downloadAgain(transferId)}
      onDiagnostics={(transferId) => void downloadDiagnostics(transferId)}
      onDismiss={dismissTransfer}
    />
  );
}
