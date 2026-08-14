"use client";

import { useEffect, useMemo, useReducer, useRef, useState } from "react";
import { FolderOpen, RefreshCw, Search, ShieldCheck, Trash2 } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";

import { api, ApiError } from "@/lib/api";
import {
  createFnOSAuthState,
  createFnOSFolderAuthorizer,
  fnOSAuthCallbackUrl,
} from "@/lib/fnos-app";
import { formatBytes } from "@/lib/format";
import {
  initialFnOSNasImportState,
  reduceFnOSNasImport,
  selectImportTotals,
  selectSelectedTokens,
} from "@/lib/fnos-nas-model";
import type { FnOSNasStatus } from "@/lib/types";
import { NasFileTable } from "@/components/features/nas-file-table";
import { NasImportProgress } from "@/components/features/nas-import-progress";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";

export function NasImportPanel({
  sourceId,
  sourceName,
  status,
  refreshStatus,
  compact = false,
  onImported,
}: {
  sourceId: string;
  sourceName: string;
  status: FnOSNasStatus;
  refreshStatus: () => void | Promise<unknown>;
  compact?: boolean;
  onImported: () => void | Promise<void>;
}) {
  const t = useTranslations("FnOSNas");
  const locale = useLocale();
  const [state, dispatch] = useReducer(
    reduceFnOSNasImport,
    initialFnOSNasImportState,
  );
  const readableFolders = useMemo(
    () => status.folders.filter((folder) => folder.readable),
    [status.folders],
  );
  const [folderId, setFolderId] = useState(readableFolders[0]?.id ?? "");
  const [recursive, setRecursive] = useState(true);
  const [legacyPath, setLegacyPath] = useState("");
  const [legacyVerified, setLegacyVerified] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const scanController = useRef<AbortController | null>(null);
  const importedJob = useRef<string | null>(null);
  const resultAnchor = useRef<HTMLDivElement | null>(null);
  const totals = selectImportTotals(state);
  const selectedTokens = selectSelectedTokens(state);

  useEffect(() => {
    dispatch({ type: "reset" });
    setFolderId(readableFolders[0]?.id ?? "");
    setLocalError(null);
  }, [sourceId, readableFolders]);

  useEffect(() => {
    setLegacyVerified(false);
  }, [sourceId]);

  useEffect(() => {
    if (!state.scan || state.scan.files.length === 0 || !resultAnchor.current) return;
    const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    resultAnchor.current.scrollIntoView({
      behavior: reducedMotion ? "auto" : "smooth",
      block: "start",
    });
  }, [state.scan]);

  useEffect(() => {
    const jobId = state.importAccepted?.job_id;
    if (!jobId) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const startedAt = Date.now();
    const poll = async () => {
      try {
        const progress = await api.getFnOSNasImport(jobId);
        if (stopped) return;
        dispatch({ type: "import.progress", progress });
        if (progress.status === "succeeded" || progress.status === "failed") {
          if (importedJob.current !== jobId) {
            importedJob.current = jobId;
            await onImported();
          }
          return;
        }
        timer = setTimeout(poll, Date.now() - startedAt >= 30_000 ? 3_000 : 1_000);
      } catch (error) {
        if (!stopped) {
          dispatch({
            type: "import.failed",
            message: error instanceof Error ? error.message : t("importJobFailed"),
          });
        }
      }
    };
    void poll();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [onImported, state.importAccepted?.job_id, t]);

  useEffect(
    () => () => {
      scanController.current?.abort();
    },
    [],
  );

  async function authorizeFolder() {
    setBusy(true);
    setLocalError(null);
    try {
      const result = await createFnOSFolderAuthorizer().authorizeDirectory({
        callbackUrl: fnOSAuthCallbackUrl(),
        state: createFnOSAuthState(),
        title: t("authorizePickerTitle"),
        confirmText: t("authorizePickerConfirm"),
      });
      if (result === "authorized") await refreshStatus();
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : t("accessUnavailable"));
    } finally {
      setBusy(false);
    }
  }

  async function rememberLegacyFolder() {
    const path = legacyPath.trim();
    if (!path.startsWith("/")) {
      setLocalError(t("legacyPath"));
      return;
    }
    setBusy(true);
    setLocalError(null);
    try {
      const folder = await api.registerFnOSNasLegacyFolder(path);
      await refreshStatus();
      setFolderId(folder.id);
      setLegacyPath("");
      setLegacyVerified(true);
    } catch (error) {
      setLocalError(
        error instanceof ApiError && error.code === "nas_folder_unreadable"
          ? t("legacyUnreadable")
          : error instanceof ApiError && error.code === "nas_folder_path_invalid"
            ? t("legacyPathInvalid")
            : error instanceof Error
              ? error.message
              : t("accessUnavailable"),
      );
    } finally {
      setBusy(false);
    }
  }

  async function removeLegacyFolder(id: string) {
    setBusy(true);
    setLocalError(null);
    try {
      await api.deleteFnOSNasLegacyFolder(id);
      await refreshStatus();
      if (folderId === id) setFolderId("");
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : t("accessUnavailable"));
    } finally {
      setBusy(false);
    }
  }

  async function scan() {
    if (!folderId) return;
    scanController.current?.abort();
    const controller = new AbortController();
    scanController.current = controller;
    dispatch({ type: "scan.started" });
    setLocalError(null);
    try {
      const result = await api.scanFnOSNas(
        { source_id: sourceId, folder_id: folderId, recursive },
        controller.signal,
      );
      dispatch({ type: "scan.loaded", result });
    } catch (error) {
      if (controller.signal.aborted) {
        dispatch({ type: "scan.cancelled" });
      } else {
        dispatch({
          type: "scan.failed",
          message:
            error instanceof ApiError && error.code === "nas_selection_expired"
              ? t("selectionExpired")
              : error instanceof Error
                ? error.message
                : t("scanFailed"),
        });
      }
    } finally {
      if (scanController.current === controller) scanController.current = null;
    }
  }

  async function startImport() {
    if (!selectedTokens.length) return;
    setBusy(true);
    setLocalError(null);
    try {
      const accepted = await api.createFnOSNasImport({
        source_id: sourceId,
        selection_tokens: selectedTokens,
      });
      dispatch({ type: "import.started", accepted });
    } catch (error) {
      dispatch({
        type: "import.failed",
        message: error instanceof Error ? error.message : t("importJobFailed"),
      });
    } finally {
      setBusy(false);
    }
  }

  if (!status.eligible || status.mode === "unavailable") {
    return (
      <Alert>
        <ShieldCheck className="size-4" />
        <AlertDescription>{t("administratorRequired")}</AlertDescription>
      </Alert>
    );
  }

  return (
    <div className={cn("space-y-4", compact && "text-sm")}>
      <header className="space-y-1">
        <h3 className="font-semibold">{t("title")}</h3>
        <p className="text-sm text-muted-foreground">{t("description")}</p>
      </header>

      {status.mode === "automatic" ? (
        <div className="rounded-lg border bg-muted/20 p-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
            <div className="min-w-0 flex-1 space-y-1.5">
              <Label htmlFor="fnos-nas-folder">{t("folder")}</Label>
              <select
                id="fnos-nas-folder"
                value={folderId}
                className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                onChange={(event) => setFolderId(event.target.value)}
              >
                <option value="">{t("noFolders")}</option>
                {readableFolders.map((folder) => (
                  <option key={folder.id} value={folder.id}>
                    {folder.display_path}
                  </option>
                ))}
              </select>
            </div>
            <Button type="button" variant="outline" disabled={busy} onClick={authorizeFolder}>
              <FolderOpen className="mr-2 size-4" />
              {t("authorizeFolder")}
            </Button>
            <Button type="button" variant="ghost" disabled={busy} onClick={() => refreshStatus()}>
              <RefreshCw className="mr-2 size-4" />
              {t("refreshAuthorization")}
            </Button>
          </div>
        </div>
      ) : (
        <div className="space-y-3 rounded-lg border bg-muted/20 p-4">
          {status.reason === "host_authorization_unavailable" ? (
            <Alert>
              <AlertDescription>{t("hostAuthorizationUnavailable")}</AlertDescription>
            </Alert>
          ) : null}
          <div>
            <h4 className="text-sm font-medium">{t("legacyTitle")}</h4>
            <ol className="mt-2 space-y-1 text-xs text-muted-foreground">
              <li>{t("legacyStep1")}</li>
              <li>{t("legacyStep2")}</li>
              <li>{t("legacyStep3")}</li>
            </ol>
          </div>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
            <div className="min-w-0 flex-1 space-y-1.5">
              <Label htmlFor="fnos-legacy-path">{t("legacyPath")}</Label>
              <Input
                id="fnos-legacy-path"
                value={legacyPath}
                placeholder={t("legacyPlaceholder")}
                onChange={(event) => {
                  setLegacyPath(event.target.value);
                  setLegacyVerified(false);
                }}
              />
            </div>
            <Button type="button" disabled={busy || !legacyPath.trim()} onClick={rememberLegacyFolder}>
              {t("rememberFolder")}
            </Button>
          </div>
          {legacyVerified ? (
            <p role="status" aria-live="polite" className="text-sm text-muted-foreground">
              {t("legacyVerified")}
            </p>
          ) : null}
          {readableFolders.length ? (
            <div className="space-y-2">
              {readableFolders.map((folder) => (
                <div key={folder.id} className="flex items-center justify-between gap-3 rounded-md bg-background px-3 py-2 text-sm">
                  <button type="button" className="min-w-0 flex-1 truncate text-left" onClick={() => setFolderId(folder.id)}>
                    {folder.display_path}
                  </button>
                  <Button type="button" size="sm" variant="ghost" aria-label={`${t("removeFolder")} ${folder.display_path}`} onClick={() => removeLegacyFolder(folder.id)}>
                    <Trash2 className="size-4" />
                  </Button>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      )}

      {localError || state.error ? (
        <Alert variant="destructive">
          <AlertTitle>{t("accessUnavailable")}</AlertTitle>
          <AlertDescription>{localError ?? state.error}</AlertDescription>
        </Alert>
      ) : null}

      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={recursive}
            onChange={(event) => setRecursive(event.target.checked)}
          />
          {t("recursive")}
        </label>
        <Button
          type="button"
          disabled={!folderId || busy || state.stage === "scanning"}
          aria-busy={state.stage === "scanning"}
          onClick={scan}
        >
          {state.stage === "scanning" ? (
            <Spinner aria-hidden="true" className="mr-2 size-4" />
          ) : (
            <Search className="mr-2 size-4" />
          )}
          {state.stage === "scanning" ? t("scanning") : t("scan")}
        </Button>
        {state.stage === "scanning" ? (
          <Button type="button" variant="outline" onClick={() => scanController.current?.abort()}>
            {t("cancelScan")}
          </Button>
        ) : null}
      </div>

      {state.stage === "scanning" && !state.scan ? (
        <div
          role="status"
          aria-label={t("scanningDocuments")}
          aria-live="polite"
          className="flex min-h-32 items-center justify-center rounded-md border border-dashed bg-muted/20 text-sm text-muted-foreground"
        >
          <Spinner aria-hidden="true" className="mr-2 size-4" />
          {t("scanningDocuments")}
        </div>
      ) : null}

      {state.scan ? (
        <div ref={resultAnchor} className="scroll-mt-4 space-y-3" aria-busy={state.stage === "scanning"}>
          <div className="rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground">
            {t("scanSummary", {
              visited: state.scan.summary.visited,
              eligible: state.scan.summary.eligible,
            })}
          </div>
          {state.scan.truncated ? (
            <Alert><AlertDescription>{t("truncated")}</AlertDescription></Alert>
          ) : null}
          {state.scan.files.length ? (
            <NasFileTable state={state} dispatch={dispatch} />
          ) : (
            <p className="py-6 text-center text-sm text-muted-foreground">{t("scanEmpty")}</p>
          )}
        </div>
      ) : null}

      {state.importAccepted && !state.importProgress ? (
        <div role="status" aria-live="polite" className="text-sm text-muted-foreground">
          {t("importStarting")}
        </div>
      ) : null}
      {state.importProgress ? <NasImportProgress progress={state.importProgress} /> : null}

      {state.scan && state.stage !== "importing" && state.stage !== "complete" ? (
        <div className={cn("flex items-center justify-between gap-3 border-t bg-background pt-3", compact && "sticky bottom-0 z-20 pb-1")}>
          <span className="text-xs text-muted-foreground">
            {formatBytes(totals.bytes, locale)}
          </span>
          <Button type="button" disabled={totals.files === 0 || busy || state.stage === "scanning"} onClick={() => setConfirmOpen(true)}>
            {t("importSelected", { count: totals.files })}
          </Button>
        </div>
      ) : null}

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("confirmTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("confirmDescription", {
                count: totals.files,
                size: formatBytes(totals.bytes, locale),
                source: sourceName,
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={startImport}>{t("confirm")}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
