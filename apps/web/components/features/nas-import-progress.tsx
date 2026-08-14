"use client";

import { useTranslations } from "next-intl";

import type { FnOSNasImportProgress as ImportProgress } from "@/lib/types";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";

const REASON_KEYS = {
  authorization_revoked: "reasonAuthorizationRevoked",
  file_unreadable: "reasonFileUnreadable",
  file_changed: "reasonFileChanged",
  document_busy: "reasonDocumentBusy",
  unsafe_or_unsupported: "reasonUnsafeOrUnsupported",
  copy_failed: "reasonCopyFailed",
  import_failed: "reasonImportFailed",
} as const;

const OUTCOME_KEYS = {
  created: "resultCreated",
  updated: "resultUpdated",
  skipped: "resultSkipped",
  failed: "resultFailed",
} as const;

export function NasImportProgress({ progress }: { progress: ImportProgress }) {
  const t = useTranslations("FnOSNas");
  const terminal = progress.status === "succeeded" || progress.status === "failed";
  const statusText =
    progress.status === "queued"
      ? t("importQueued")
      : terminal
        ? progress.status === "failed"
          ? t("importJobFailed")
          : t("importComplete", {
              created: progress.created,
              updated: progress.updated,
              skipped: progress.skipped,
              failed: progress.failed,
            })
        : t("importRunning", {
            completed: progress.completed,
            total: progress.total,
          });

  return (
    <section className="space-y-3 rounded-lg border bg-muted/20 p-4" aria-label={statusText}>
      <div role="status" aria-live="polite" className="space-y-2">
        <div className="text-sm font-medium">{statusText}</div>
        <Progress
          value={Math.round(progress.progress * 100)}
          aria-label={statusText}
        />
      </div>
      {progress.failed > 0 && terminal ? (
        <Alert>
          <AlertDescription>{t("partialSuccess")}</AlertDescription>
        </Alert>
      ) : null}
      {terminal && progress.results.length > 0 ? (
        <ul className="max-h-44 space-y-1 overflow-auto text-xs">
          {progress.results.map((result, index) => (
            <li
              key={`${result.display_path}:${index}`}
              className="flex items-start justify-between gap-3 rounded-md bg-background px-3 py-2"
            >
              <span className="min-w-0 break-all">{result.display_path}</span>
              <span className="flex shrink-0 items-center gap-2">
                <Badge variant={result.outcome === "failed" ? "destructive" : "secondary"}>
                  {t(OUTCOME_KEYS[result.outcome])}
                </Badge>
                {result.reason ? (
                  <span className="max-w-44 text-muted-foreground">
                    {t(
                      REASON_KEYS[result.reason as keyof typeof REASON_KEYS] ??
                        "reasonImportFailed",
                    )}
                  </span>
                ) : null}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
