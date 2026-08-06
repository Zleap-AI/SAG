"use client";

import * as React from "react";
import { FileText, Pause, Play, RefreshCw, Trash2 } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { toast } from "sonner";

import { ApiError } from "@/lib/api";
import { getDiagnosticsStore } from "@/lib/diagnostics";
import {
  deriveDocumentActivity,
  documentActivityShowsProgress,
  type DocumentAction,
  type DocumentActivity,
} from "@/lib/document-activity";
import type { Doc } from "@/lib/types";
import { formatBytes, formatTokenCount, relativeTime } from "@/lib/format";
import { useDetailPanel } from "@/components/features/detail-panel";
import { useApp } from "@/components/features/app-shell";
import { DocumentActivityBadge } from "@/components/features/status-badge";
import { Button } from "@/components/ui/button";

export function DocumentList({
  sourceId,
  documents,
  activities,
  onAction,
  variant = "normal",
  onOpenDocument,
}: {
  sourceId: string;
  documents: Doc[];
  activities: Record<string, DocumentActivity>;
  onAction: (document: Doc, action: DocumentAction) => Promise<boolean>;
  variant?: "normal" | "compact";
  onOpenDocument?: (document: Doc) => void;
}) {
  const t = useTranslations("DocumentList");
  const locale = useLocale();
  const { open } = useDetailPanel();
  const { timezone } = useApp();

  async function perform(document: Doc, action: DocumentAction) {
    try {
      const applied = await onAction(document, action);
      if (!applied) return;
      if (action === "reprocess") toast.success(t("requeued"));
      else if (action === "pause") toast.success(t("pausing"));
      else if (action === "resume") toast.success(t("resumed"));
      else toast.success(t("deleted"));
      getDiagnosticsStore().record("knowledge.upload", {
        action: `document.${action}`,
        source_id: sourceId,
        document_id: document.id,
        filename: document.filename,
        status: document.status,
        chunk_count: document.chunk_count,
        event_count: document.event_count,
      });
    } catch (error) {
      const fallback =
        action === "delete"
          ? t("deleteFailed")
          : action === "pause"
            ? t("pauseFailed")
            : action === "resume"
              ? t("resumeFailed")
              : t("operationFailed");
      toast.error(error instanceof ApiError ? error.message : fallback);
      getDiagnosticsStore().record("error", {
        context: `document.${action}`,
        source_id: sourceId,
        document_id: document.id,
        filename: document.filename,
        error_message: error instanceof ApiError ? error.message : String(error),
      });
    }
  }

  function openDocument(document: Doc) {
    if (onOpenDocument) onOpenDocument(document);
    else open({ kind: "document", sourceId, documentId: document.id });
  }

  function actions(document: Doc, activity: DocumentActivity, compact = false) {
    const buttonClass = compact ? "size-7" : undefined;
    return (
      <div className="flex shrink-0 items-center gap-0.5">
        {document.status === "extracting" && (
          <Button
            variant="ghost"
            size="icon"
            className={buttonClass}
            title={t("pause")}
            disabled={activity.busy}
            onClick={() => void perform(document, "pause")}
          >
            <Pause className="size-4" />
          </Button>
        )}
        {document.status === "paused" && (
          <Button
            variant="ghost"
            size="icon"
            className={buttonClass}
            title={t("resume")}
            disabled={activity.busy}
            onClick={() => void perform(document, "resume")}
          >
            <Play className="size-4" />
          </Button>
        )}
        {(document.status === "failed" || document.status === "ready") && (
          <Button
            variant="ghost"
            size="icon"
            className={buttonClass}
            title={t("reprocess")}
            disabled={activity.busy}
            onClick={() => void perform(document, "reprocess")}
          >
            <RefreshCw className={activity.busy ? "size-4 animate-spin" : "size-4"} />
          </Button>
        )}
        <Button
          variant="ghost"
          size="icon"
          className={`${buttonClass ?? ""} text-muted-foreground hover:text-destructive`}
          title={t("delete")}
          disabled={activity.busy}
          onClick={() => void perform(document, "delete")}
        >
          <Trash2 className="size-4" />
        </Button>
      </div>
    );
  }

  if (variant === "compact") {
    return (
      <div className="space-y-0.5">
        {documents.map((document) => {
          const activity = activities[document.id] ?? deriveDocumentActivity(document);
          const showProgress = documentActivityShowsProgress(activity.phase);
          return (
            <div
              key={document.id}
              className="group/document flex items-center gap-1 rounded-lg px-1 transition-colors hover:bg-muted"
            >
              <button
                type="button"
                onClick={() => openDocument(document)}
                className="flex min-w-0 flex-1 items-center gap-3 rounded-lg px-1.5 py-2.5 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring"
                title={t("viewDocument")}
              >
                <div className="grid size-9 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground transition-colors group-hover/document:text-foreground">
                  <FileText className="size-4" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium text-foreground">
                    {document.filename}
                  </div>
                  <div className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-1.5 text-[11px] text-muted-foreground">
                    <span>{formatBytes(document.size_bytes, locale)}</span>
                    <span>·</span>
                    <span>{relativeTime(document.created_at, timezone, locale)}</span>
                    <span>·</span>
                    <span>{activity.progress}%</span>
                    <span>·</span>
                    <span>{t("tokens", { count: formatTokenCount(document.token_usage, locale) })}</span>
                  </div>
                  {activity.error && (
                    <p className="mt-0.5 truncate text-[11px] text-destructive" title={activity.error}>
                      {activity.error}
                    </p>
                  )}
                  {showProgress && (
                    <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full rounded-full bg-primary/60 transition-[width] duration-300"
                        style={{ width: `${activity.progress}%` }}
                      />
                    </div>
                  )}
                </div>
                <DocumentActivityBadge phase={activity.phase} />
              </button>
              {actions(document, activity, true)}
            </div>
          );
        })}
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      {documents.map((document, index) => {
        const activity = activities[document.id] ?? deriveDocumentActivity(document);
        const showProgress = documentActivityShowsProgress(activity.phase);
        return (
          <div
            key={document.id}
            className={
              "flex items-center gap-3 px-4 py-3 transition-colors hover:bg-muted/60 "
              + (index > 0 ? "border-t" : "")
            }
          >
            <div className="grid size-9 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground">
              <FileText className="size-4" />
            </div>
            <button
              type="button"
              onClick={() => openDocument(document)}
              className="min-w-0 flex-1 rounded-md text-left outline-none focus-visible:bg-muted/60"
              title={t("viewDetails")}
            >
              <div className="truncate text-sm font-medium text-foreground">
                {document.filename}
              </div>
              <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
                <span>{formatBytes(document.size_bytes, locale)}</span>
                <span>·</span>
                <span>{relativeTime(document.created_at, timezone, locale)}</span>
                {document.status === "ready" && (
                  <>
                    <span>·</span>
                    <span>
                      {t("chunksAndEvents", {
                        chunks: document.chunk_count,
                        events: document.event_count,
                      })}
                    </span>
                  </>
                )}
                <span>·</span>
                <span>
                  {activity.progress}% ·{" "}
                  {t("tokens", { count: formatTokenCount(document.token_usage, locale) })}
                </span>
                {activity.error && (
                  <>
                    <span>·</span>
                    <span className="truncate text-destructive" title={activity.error}>
                      {activity.error}
                    </span>
                  </>
                )}
              </div>
              {showProgress && (
                <div className="mt-1.5 h-1 w-full max-w-56 overflow-hidden rounded-full bg-muted">
                  <div
                    className="h-full rounded-full bg-primary/60 transition-[width] duration-300"
                    style={{ width: `${activity.progress}%` }}
                  />
                </div>
              )}
            </button>
            <DocumentActivityBadge phase={activity.phase} />
            {actions(document, activity)}
          </div>
        );
      })}
    </div>
  );
}
