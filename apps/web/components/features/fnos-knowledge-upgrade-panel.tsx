"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import type { FnOSKnowledgeUpgradeStatus } from "@/lib/types";

export function FnOSKnowledgeUpgradePanel({
  sourceId,
  onChanged,
}: {
  sourceId: string;
  onChanged: () => void;
}) {
  const t = useTranslations("FnOSKnowledgeUpgrade");
  const [status, setStatus] = React.useState<FnOSKnowledgeUpgradeStatus | null>(null);
  const [busy, setBusy] = React.useState(false);

  const refresh = React.useCallback(async () => {
    try {
      setStatus(await api.fnosKnowledgeUpgradeStatus());
    } catch {
      setStatus(null);
    }
  }, []);

  React.useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  if (!status?.required) return null;

  async function reingest() {
    setBusy(true);
    try {
      const result = await api.fnosReingestKnowledge();
      toast.success(t("queued", { count: result.queued }));
      await refresh();
      onChanged();
    } catch {
      toast.error(t("failed"));
    } finally {
      setBusy(false);
    }
  }

  async function uploadOriginal(documentId: string, file: File) {
    setBusy(true);
    try {
      await api.fnosReplaceMissingOriginal(documentId, file);
      toast.success(t("uploaded"));
      await refresh();
      onChanged();
    } catch {
      toast.error(t("uploadFailed"));
    } finally {
      setBusy(false);
    }
  }

  const missingHere = status.missing.filter((item) => item.source_id === sourceId);
  const canQueue = status.states.pending + status.states.failed > 0;

  return (
    <Alert className="mb-4">
      <AlertTitle>{t("title")}</AlertTitle>
      <AlertDescription className="space-y-3">
        <p>{t("description")}</p>
        <p>{t("progress", { ready: status.states.ready, total: status.total })}</p>
        <Button onClick={() => void reingest()} disabled={busy || !canQueue}>
          {t("reingest")}
        </Button>
        {missingHere.length > 0 && (
          <div className="space-y-2">
            <p>{t("missing")}</p>
            {missingHere.map((item) => (
              <label key={item.document_id} className="flex items-center gap-2 text-sm">
                <span className="min-w-0 truncate">{item.filename}</span>
                <input
                  type="file"
                  aria-label={t("uploadOriginal", { filename: item.filename })}
                  disabled={busy}
                  onChange={(event) => {
                    const file = event.currentTarget.files?.[0];
                    if (file) void uploadOriginal(item.document_id, file);
                  }}
                />
              </label>
            ))}
          </div>
        )}
      </AlertDescription>
    </Alert>
  );
}
