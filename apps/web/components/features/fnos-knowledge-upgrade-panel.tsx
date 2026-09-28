"use client";

import * as React from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import type { FnOSKnowledgeUpgradeStatus } from "@/lib/types";

type UpgradeContextValue = {
  enabled: boolean;
  status: FnOSKnowledgeUpgradeStatus | null;
  error: boolean;
  loading: boolean;
  refresh: () => Promise<void>;
};

const UpgradeContext = React.createContext<UpgradeContextValue | null>(null);

// Mounted once per authenticated tenant. Global and detail guidance share one poll.
export function FnOSKnowledgeUpgradeProvider({ enabled, children }: {
  enabled: boolean;
  children: React.ReactNode;
}) {
  const [status, setStatus] = React.useState<FnOSKnowledgeUpgradeStatus | null>(null);
  const [error, setError] = React.useState(false);
  const [loading, setLoading] = React.useState(false);
  const generation = React.useRef(0);
  const inFlight = React.useRef<Promise<void> | null>(null);
  const refresh = React.useCallback((): Promise<void> => {
    if (!enabled) return Promise.resolve();
    if (inFlight.current) return inFlight.current;
    const current = ++generation.current;
    setLoading(true);
    const request = (async () => {
      try {
        const next = await api.fnosKnowledgeUpgradeStatus();
        if (current === generation.current) {
          setStatus(next);
          setError(false);
        }
      } catch {
        if (current === generation.current) setError(true);
      } finally {
        if (current === generation.current) {
          setLoading(false);
          inFlight.current = null;
        }
      }
    })();
    inFlight.current = request;
    return request;
  }, [enabled]);

  React.useEffect(() => {
    if (!enabled) return;
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5_000);
    return () => {
      window.clearInterval(timer);
      generation.current += 1;
      inFlight.current = null;
    };
  }, [enabled, refresh]);

  return <UpgradeContext.Provider value={{ enabled, status, error, loading, refresh }}>
    {children}
  </UpgradeContext.Provider>;
}

function useUpgradeStatus() {
  const value = React.useContext(UpgradeContext);
  if (!value) throw new Error("Native upgrade guidance requires its provider");
  return value;
}

function UpgradeStatusError() {
  const t = useTranslations("FnOSKnowledgeUpgrade");
  const { error, loading, refresh } = useUpgradeStatus();
  if (!error) return null;
  return <div className="space-y-2">
    <p>{t("statusFailed")}</p>
    <Button variant="outline" disabled={loading} onClick={() => void refresh()}>{t("retry")}</Button>
  </div>;
}

export function FnOSKnowledgeUpgradeBanner() {
  const t = useTranslations("FnOSKnowledgeUpgrade");
  const { enabled, status, error } = useUpgradeStatus();
  if (!enabled || (!status?.required && !error)) return null;
  return <Alert className="mx-4 mt-2 w-auto shrink-0 max-h-[35svh] overflow-y-auto">
    <AlertTitle>{t(status?.required ? "title" : "statusTitle")}</AlertTitle>
    <AlertDescription className="space-y-2">
      {status?.required && <>
        <p>{t("description")}</p>
        <Link href="/knowledge" className="font-medium underline underline-offset-4">{t("openKnowledge")}</Link>
      </>}
      <UpgradeStatusError />
    </AlertDescription>
  </Alert>;
}

export function FnOSKnowledgeUpgradePanel({ sourceId, onChanged }: {
  sourceId: string;
  onChanged: () => void;
}) {
  const t = useTranslations("FnOSKnowledgeUpgrade");
  const { enabled, status, error, refresh } = useUpgradeStatus();
  const [busy, setBusy] = React.useState(false);
  if (!enabled) return null;
  if (!status) return error ? <Alert className="mb-4">
    <AlertTitle>{t("statusTitle")}</AlertTitle>
    <AlertDescription><UpgradeStatusError /></AlertDescription>
  </Alert> : null;
  if (!status.required) return status.total > 0 ? <Alert className="mb-4">
    <AlertTitle>{t("completedTitle")}</AlertTitle>
    <AlertDescription className="space-y-2">
      <p>{t("completedDescription")}</p>
      <p>{t("progress", { ready: status.states.ready, total: status.total })}</p>
      <UpgradeStatusError />
    </AlertDescription>
  </Alert> : error ? <Alert className="mb-4"><UpgradeStatusError /></Alert> : null;

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
        <UpgradeStatusError />
        <p>{t("progress", { ready: status.states.ready, total: status.total })}</p>
        <Button onClick={() => void reingest()} disabled={busy || error || !canQueue}>
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
                  disabled={busy || error}
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
