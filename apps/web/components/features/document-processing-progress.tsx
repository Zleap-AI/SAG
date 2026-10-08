"use client";

import { useTranslations } from "next-intl";

import type { DocumentActivity } from "@/lib/document-activity";
import { documentProcessingState } from "@/lib/document-processing";
import type { Doc } from "@/lib/types";
import { cn } from "@/lib/utils";
import { DocumentActivityBadge } from "./status-badge";

type Props = { document: Doc; activity?: DocumentActivity };

export function DocumentProcessingBadge({ document, activity }: Props) {
  const t = useTranslations("DocumentProcessing");
  const state = documentProcessingState(document, activity);
  return <DocumentActivityBadge phase={state.phase} label={state.stage ? t(state.stage) : undefined} />;
}

export function DocumentProcessingProgress({ document, activity, detail = false, compact = false }: Props & {
  detail?: boolean;
  compact?: boolean;
}) {
  const t = useTranslations("DocumentProcessing");
  const { stage, counts, showBar } = documentProcessingState(document, activity);
  if (detail && stage === "finalizing") {
    return <p className="text-xs text-muted-foreground" role="status">{t("finalizingDescription")}</p>;
  }
  if (!counts) return null;
  const label = t("chunks", counts);
  return (
    <div className={cn("mt-1 space-y-1.5 text-muted-foreground", compact ? "text-[11px]" : "text-xs")}>
      <span className="tabular-nums">{label}</span>
      {showBar && (
        <div
          role="progressbar"
          aria-label={t("chunkProgress")}
          aria-valuemin={0}
          aria-valuemax={counts.total}
          aria-valuenow={counts.completed}
          aria-valuetext={label}
          className={cn("h-1 w-full overflow-hidden rounded-full bg-muted", !compact && "max-w-56")}
        >
          <div className="h-full rounded-full bg-primary/60 transition-[width] duration-300"
            style={{ width: `${100 * counts.completed / counts.total}%` }} />
        </div>
      )}
    </div>
  );
}
