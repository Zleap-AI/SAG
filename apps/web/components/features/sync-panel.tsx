"use client";

import * as React from "react";
import { Globe, RefreshCw } from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import { getDiagnosticsStore } from "@/lib/diagnostics";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

export function SyncPanel({ sourceId, onSynced }: { sourceId: string; onSynced: () => void }) {
  const t = useTranslations("SyncPanel");
  const [busy, setBusy] = React.useState(false);

  async function sync() {
    setBusy(true);
    try {
      const result = await api.syncSource(sourceId);
      toast.success(t("started"));
      getDiagnosticsStore().record("knowledge.upload", {
        action: "source.sync",
        source_id: sourceId,
        job_type: result.type,
        job_id: result.id,
      });
      onSynced();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : t("failed"));
      getDiagnosticsStore().record("error", {
        context: "source.sync",
        source_id: sourceId,
        error_message: err instanceof ApiError ? err.message : String(err),
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-wrap items-center justify-between gap-4 rounded-lg border bg-card/40 p-5">
      <div className="flex items-start gap-3">
        <div className="grid size-10 place-items-center rounded-full bg-muted text-foreground">
          <Globe className="size-5" />
        </div>
        <div>
          <div className="text-sm font-medium text-foreground">{t("title")}</div>
          <p className="mt-0.5 text-xs text-muted-foreground">{t("description")}</p>
        </div>
      </div>
      <Button onClick={sync} disabled={busy}>
        <RefreshCw className={cn("size-4", busy && "animate-spin")} />
        {busy ? t("syncing") : t("sync")}
      </Button>
    </div>
  );
}
