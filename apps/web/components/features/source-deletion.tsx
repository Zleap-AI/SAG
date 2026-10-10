"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import type { BackgroundJob, Source } from "@/lib/types";
import { isSourceDeleting } from "@/lib/source-activity";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";

export function useSourceDeletion(source: Source, onChanged?: () => void) {
  const t = useTranslations("SourceCard");
  const [job, setJob] = React.useState<BackgroundJob | null>(source.deletion_job ?? null);
  const [busy, setBusy] = React.useState(false);
  const completed = React.useRef<string | null>(null);

  React.useEffect(() => setJob(source.deletion_job ?? null), [source.id, source.deletion_job]);

  React.useEffect(() => {
    if (!job) return;
    if (job.status === "succeeded") {
      if (completed.current !== job.id) {
        completed.current = job.id;
        toast.success(t("deleted"));
        onChanged?.();
      }
      return;
    }
    if (job.status !== "queued" && job.status !== "running") return;
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      try {
        const next = await api.getJob(job.id);
        if (!cancelled) {
          setJob(next);
          if (next.status !== job.status) onChanged?.();
        }
      } catch (error) {
        if (!cancelled) {
          toast.error(error instanceof ApiError ? error.message : t("deleteFailed"));
          onChanged?.();
        }
      }
    }, 1000);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [job, onChanged, t]);

  async function submit() {
    if (busy) return false;
    setBusy(true);
    try {
      setJob(await api.deleteSource(source.id));
      toast.message(t("deleteSubmitted"));
      onChanged?.();
      return true;
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : t("deleteFailed"));
      return false;
    } finally {
      setBusy(false);
    }
  }

  const active = isSourceDeleting(source) || job !== null;
  const paused = job?.status === "paused";
  const failed = job ? job.status === "failed" : source.status === "delete_failed";
  return { active, busy, submit, job, canContinue: paused || failed || (active && !job),
    label: job?.status === "succeeded" ? t("deleted")
      : paused ? t("deletePaused") : failed ? t("deleteFailed") : t("deleting") };
}

export function SourceDeletionStatus({ deletion }: { deletion: ReturnType<typeof useSourceDeletion> }) {
  const t = useTranslations("SourceCard");
  if (!deletion.active) return null;
  return (
    <div role="status" className="pointer-events-auto relative z-20 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      <span title={deletion.job?.error ?? undefined}>{deletion.label}</span>
      {deletion.canContinue && (
        <Button variant="outline" size="sm" disabled={deletion.busy} onClick={() => void deletion.submit()}>
          {deletion.busy && <Spinner />}{t("continueDelete")}
        </Button>
      )}
    </div>
  );
}
