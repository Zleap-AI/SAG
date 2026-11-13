"use client";

import Link from "next/link";
import * as React from "react";
import { Download, FileText, Network, Puzzle, Trash2 } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { toast } from "sonner";

import type { Source } from "@/lib/types";
import { relativeTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EditSourceDialog } from "@/components/features/edit-source-dialog";
import { KnowledgeEnginePill } from "@/components/features/knowledge-engine-pill";
import { useOctxExports } from "@/components/features/octx-export-provider";
import { useApp } from "@/components/features/app-shell";
import { SourceIdCopy } from "@/components/features/source-id-copy";
import { Spinner } from "@/components/ui/spinner";
import { SourceDeletionStatus, useSourceDeletion } from "./source-deletion";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

export function SourceCard({ source, onChanged }: { source: Source; onChanged?: () => void }) {
  const t = useTranslations("SourceCard");
  const locale = useLocale();
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const { timezone } = useApp();
  const deletion = useSourceDeletion(source, onChanged);

  const { startExport, isSourceExporting } = useOctxExports();
  const exportRunning = isSourceExporting(source.id);
  const sag2 = source.engine_kind === "sag2";

  const handleExport = React.useCallback(
    (event: React.MouseEvent<HTMLButtonElement>) => {
      event.preventDefault();
      event.stopPropagation();
      if (exportRunning || deletion.active) return;
      toast.message(t("exporting"));
      void startExport(source.id, source.name);
    },
    [deletion.active, exportRunning, source.id, source.name, startExport, t],
  );

  return (
    <div className="group/source relative flex h-full min-w-0 flex-col rounded-lg border bg-card p-5 shadow-soft transition-all duration-150 ease-smooth hover:border-foreground/15 hover:shadow-lift">
      {!deletion.active && <Link
        href={`/knowledge/${source.id}`}
        className="absolute inset-0 z-0 rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-ring"
        aria-label={source.name}
      >
        <span className="sr-only">{source.name}</span>
      </Link>}

      <div className="pointer-events-none relative z-10 flex h-full min-w-0 flex-col">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 pr-28">
          <h3 className="min-w-0 break-words font-display text-lg font-medium leading-tight text-foreground">
            {source.name}
          </h3>
          {(
            <KnowledgeEnginePill kind={source.engine_kind} className="pointer-events-auto" />
          )}
        </div>
        <p className="mb-3 mt-1.5 line-clamp-2 min-h-[2.5rem] text-sm text-muted-foreground">
          {source.description || t("noDescription")}
        </p>
        <SourceIdCopy
          sourceId={source.id}
          className="pointer-events-auto mb-3"
        />
        <SourceDeletionStatus deletion={deletion} />
        <div className="mt-auto flex flex-wrap items-center gap-x-4 gap-y-2 border-t pt-3 text-xs tabular-nums text-muted-foreground">
          <span className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap">
            <FileText className="size-3.5 shrink-0" />
            {t("documents", { count: source.document_count })}
          </span>
          <span className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap">
            <Puzzle className="size-3.5 shrink-0" />
            {t("chunks", { count: source.chunk_count })}
          </span>
          <span className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap">
            <Network className="size-3.5 shrink-0" />
            {t("events", { count: source.event_count })}
          </span>
          <span className="ml-auto shrink-0 whitespace-nowrap">
            {relativeTime(source.updated_at, timezone, locale)}
          </span>
        </div>
      </div>

      <div className="absolute right-5 top-5 z-20 flex items-center gap-1 opacity-0 transition-opacity group-hover/source:opacity-100 group-focus-within/source:opacity-100">
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label={t("export")}
              title={sag2 ? t("sag2OctxUnsupported") : t("export")}
              onClick={handleExport}
              disabled={exportRunning || deletion.active || sag2}
              className="bg-background/95 text-muted-foreground shadow-soft backdrop-blur-sm hover:text-foreground"
            >
              {exportRunning ? <Spinner className="size-4" /> : <Download className="size-4" />}
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">{sag2 ? t("sag2OctxUnsupported") : t("export")}</TooltipContent>
        </Tooltip>
        {!deletion.active && <EditSourceDialog
          source={source}
          onUpdated={onChanged}
          tooltipSide="bottom"
          buttonClassName="bg-background/95 shadow-soft backdrop-blur-sm"
        />}
        {!deletion.active && <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label={t("delete")}
              title={t("delete")}
              onClick={() => setConfirmDelete(true)}
              className="bg-background/95 text-muted-foreground shadow-soft backdrop-blur-sm hover:text-destructive"
            >
              <Trash2 className="size-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">{t("delete")}</TooltipContent>
        </Tooltip>}
      </div>

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t("delete")}
        description={t("deleteDescription", { name: source.name })}
        confirmLabel={t("delete")}
        onConfirm={deletion.submit}
      />
    </div>
  );
}
