"use client";

import { LoaderCircle, X } from "lucide-react";
import { useTranslations } from "next-intl";

import { useOctxImports } from "@/components/features/octx-import-provider";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import {
  importDurationParts,
  importStage,
  isTerminalImportStatus,
  vectorProgressHintKey,
  vectorProgressMessageKey,
} from "@/lib/octx-import-tasks";

export function OctxImportTaskCenter() {
  const t = useTranslations("Knowledge");
  const { tasks, cancelTransfer, dismissTransfer } = useOctxImports();
  if (!tasks.length) return null;
  return (
    <aside aria-live="polite" className="fixed bottom-4 right-4 z-[71] w-[min(24rem,calc(100vw-2rem))] overflow-hidden rounded-xl border bg-background/95 shadow-lift backdrop-blur-md">
      <div className="border-b px-4 py-3 text-sm font-medium">{t("importTasksTitle")}</div>
      <div className="max-h-[min(32rem,70vh)] space-y-2 overflow-auto p-2">
        {tasks.map((task) => {
          const transfer = task.transfer;
          const terminal = isTerminalImportStatus(transfer.status);
          const progress = Math.round(Math.max(0, Math.min(1, transfer.progress)) * 100);
          const detail = transfer.progress_detail;
          const vectorKind = detail?.current_kind
            ? {
                chunks: t("importVectorKind.chunks"),
                events: t("importVectorKind.events"),
                entities: t("importVectorKind.entities"),
                event_entities: t("importVectorKind.event_entities"),
              }[detail.current_kind]
            : null;
          const duration = importDurationParts(detail?.duration_seconds ?? 0);
          const durationText = [
            duration.hours ? `${duration.hours}${t("importDurationHour")}` : "",
            duration.minutes ? `${duration.minutes}${t("importDurationMinute")}` : "",
            `${duration.seconds}${t("importDurationSecond")}`,
          ].filter(Boolean).join(" ");
          const vectorCompleted = detail?.written_records ?? detail?.completed_vectors ?? 0;
          const vectorTotal = detail?.role_total_records ?? detail?.total_vectors ?? 0;
          const reusableRoleCount = detail?.reusable_vector_roles?.length;
          const rebuiltRoleCount = reusableRoleCount === undefined ? undefined : Math.max(0, 6 - reusableRoleCount);
          return <div key={task.transferId} className="rounded-lg border bg-card p-3">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium">{transfer.asset?.name || task.filename}</p>
                <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  {!terminal && transfer.status !== "decision_required" && <LoaderCircle className="size-3 animate-spin" />}
                  {t(`importStage.${importStage(transfer)}`)}
                </p>
              </div>
              <Button type="button" variant="ghost" size="icon" className="size-7" onClick={() => terminal ? dismissTransfer(task.transferId) : void cancelTransfer(task.transferId)} aria-label={terminal ? t("importDismiss") : t("importCancel")}><X className="size-3.5" /></Button>
            </div>
            {!terminal && transfer.status !== "decision_required" && <>
              <div className="mt-2 flex items-center gap-2"><Progress value={progress} className="h-1.5" /><span className="w-9 text-right text-xs tabular-nums">{progress}%</span></div>
              {detail?.phase === "vectorizing" ? <div className="mt-2 space-y-1 text-xs text-muted-foreground">
                <p>{t(vectorProgressMessageKey(detail.vector_mode), { kind: vectorKind ?? t("importVectorKind.unknown"), completed: vectorCompleted, total: vectorTotal })}</p>
                {detail.current_batch_size ? <p>{t(detail.batch_state === "started" ? "importVectorBatchRunning" : "importVectorBatchComplete", { count: detail.current_batch_size })}</p> : null}
                {reusableRoleCount === undefined ? null : <p>{t("importVectorReuseSummary", { reused: reusableRoleCount, rebuilt: rebuiltRoleCount ?? 0 })}</p>}
                <p>{t(vectorProgressHintKey(detail.vector_mode))}</p>
              </div> : detail?.total_documents ? <p className="mt-1 truncate text-xs text-muted-foreground">{t("importDocumentProgress", { completed: detail.completed_documents || 0, total: detail.total_documents })}{detail.current_document ? ` · ${detail.current_document}` : ""}</p> : <p className="mt-1 text-xs text-muted-foreground">{t("importAtomicVisibility")}</p>}
            </>}
            {transfer.status === "failed" && <p className="mt-2 break-words text-xs text-destructive">{transfer.error?.message || t("importFailed")}</p>}
            {transfer.status === "ready" && <p className="mt-2 text-xs text-emerald-600">{detail?.duration_seconds === undefined ? t("importComplete") : t("importCompleteWithDuration", { duration: durationText })}</p>}
          </div>;
        })}
      </div>
    </aside>
  );
}
