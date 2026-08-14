"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import type { SagDesktopDiagnosticsInfo } from "@/lib/desktop-bridge";
import { OctxExportTaskManager } from "@/lib/octx-export-manager";
import { exportDismissDelay } from "@/lib/octx-export-dismissal";
import { getDiagnosticsStore, sanitize } from "@/lib/diagnostics";
import {
  isActiveExportForSource,
  OCTX_EXPORT_TASKS_STORAGE_KEY,
  type PersistedOctxExportTask,
} from "@/lib/octx-export-tasks";

const POLL_INTERVAL_MS = 1500;

interface OctxExportsContextValue {
  tasks: PersistedOctxExportTask[];
  startExport: (sourceId: string, sourceName: string) => Promise<void>;
  startDocumentExport: (
    sourceId: string,
    sourceName: string,
    documentId: string,
    documentName: string,
  ) => Promise<void>;
  confirmReadyOnly: (transferId: string, decisionToken: string) => Promise<void>;
  cancelDecision: (transferId: string, decisionToken: string) => Promise<void>;
  cancelTransfer: (transferId: string) => Promise<void>;
  downloadAgain: (transferId: string) => Promise<void>;
  downloadDiagnostics: (transferId: string) => Promise<void>;
  dismissTransfer: (transferId: string) => void;
  isSourceExporting: (sourceId: string) => boolean;
}

const OctxExportsContext = React.createContext<OctxExportsContextValue | null>(null);

function triggerDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function artifactFilename(task: PersistedOctxExportTask): string {
  const version = task.transfer.release?.version ?? "release";
  const stem = (task.filenameHint ?? task.transfer.asset?.name ?? task.sourceName ?? "source")
    .replace(/[^\w.\-]+/g, "_")
    .slice(0, 60);
  return `${stem}-${version}.octx`;
}

function downloadJson(value: unknown, filename: string) {
  const blob = new Blob([JSON.stringify(value, null, 2)], { type: "application/json" });
  triggerDownload(blob, filename);
}

export function projectDesktopDiagnostics(
  info: SagDesktopDiagnosticsInfo,
): Record<string, string | number | boolean> {
  return {
    version: info.version,
    platform: info.platform,
    arch: info.arch,
    os_release: info.osRelease,
    os_version: info.osVersion,
    packaged: info.packaged,
    electron: info.electron,
    chrome: info.chrome,
    node: info.node,
    log_file_count: info.logFiles.length,
    has_truncated_logs: info.logFiles.some((file) => file.truncated),
  };
}

export function OctxExportProvider({ children }: { children: React.ReactNode }) {
  const t = useTranslations("SourceCard");
  const [tasks, setTasks] = React.useState<PersistedOctxExportTask[]>([]);
  const managerRef = React.useRef<OctxExportTaskManager | null>(null);

  React.useEffect(() => {
    const manager = new OctxExportTaskManager({
      load: () => window.localStorage.getItem(OCTX_EXPORT_TASKS_STORAGE_KEY),
      save: (value) => window.localStorage.setItem(OCTX_EXPORT_TASKS_STORAGE_KEY, value),
      getTransfer: api.getOctxTransfer,
      startExport: api.startOctxExport,
      startDocumentExport: api.startOctxDocumentExport,
      decideExport: (transferId, decisionToken) =>
        api.decideOctxExport(transferId, {
          action: "export_ready_only",
          decision_token: decisionToken,
        }),
      cancelDecision: (transferId, decisionToken) =>
        api.decideOctxExport(transferId, {
          action: "cancel",
          decision_token: decisionToken,
        }),
      cancelTransfer: api.cancelOctxTransfer,
      download: async (task) => {
        const blob = await api.downloadOctxArtifact(task.transferId);
        triggerDownload(blob, artifactFilename(task));
        toast.success(t("exportReady"));
      },
      recordEvent: (type, data) => getDiagnosticsStore().record(type, data),
      now: () => new Date().toISOString(),
    });
    managerRef.current = manager;
    const unsubscribe = manager.subscribe(setTasks);
    void manager.hydrate();
    const interval = window.setInterval(() => void manager.refresh(), POLL_INTERVAL_MS);
    const refresh = () => void manager.refresh();
    window.addEventListener("online", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener("online", refresh);
      document.removeEventListener("visibilitychange", refresh);
      unsubscribe();
      managerRef.current = null;
    };
  }, [t]);

  const reportError = React.useCallback(
    (error: unknown) => {
      toast.error(error instanceof ApiError ? error.message : t("exportFailed"));
    },
    [t],
  );

  const startExport = React.useCallback(
    async (sourceId: string, sourceName: string) => {
      if (isActiveExportForSource(tasks, sourceId)) return;
      try {
        await managerRef.current?.start(sourceId, sourceName, sourceName);
        toast.message(t("exportStarted"));
      } catch (error) {
        reportError(error);
      }
    },
    [reportError, t, tasks],
  );

  const confirmReadyOnly = React.useCallback(
    async (transferId: string, decisionToken: string) => {
      try {
        await managerRef.current?.confirmReadyOnly(transferId, decisionToken);
      } catch (error) {
        reportError(error);
      }
    },
    [reportError],
  );

  const startDocumentExport = React.useCallback(
    async (
      sourceId: string,
      sourceName: string,
      documentId: string,
      documentName: string,
    ) => {
      if (isActiveExportForSource(tasks, sourceId)) return;
      try {
        await managerRef.current?.startDocument(
          sourceId,
          sourceName,
          documentId,
          documentName,
        );
        toast.message(t("exportStarted"));
      } catch (error) {
        reportError(error);
      }
    },
    [reportError, t, tasks],
  );

  const cancelTransfer = React.useCallback(
    async (transferId: string) => {
      try {
        await managerRef.current?.cancel(transferId);
      } catch (error) {
        reportError(error);
      }
    },
    [reportError],
  );

  const cancelDecision = React.useCallback(
    async (transferId: string, decisionToken: string) => {
      try {
        await managerRef.current?.cancelDecision(transferId, decisionToken);
      } catch (error) {
        reportError(error);
      }
    },
    [reportError],
  );

  const downloadAgain = React.useCallback(
    async (transferId: string) => {
      try {
        await managerRef.current?.downloadAgain(transferId);
      } catch (error) {
        reportError(error);
      }
    },
    [reportError],
  );

  const dismissTransfer = React.useCallback((transferId: string) => {
    managerRef.current?.dismiss(transferId);
  }, []);

  const downloadDiagnostics = React.useCallback(
    async (transferId: string) => {
      try {
        const server = await api.getOctxTransferDiagnostics(transferId);
        let desktop: Record<string, unknown> | null = null;
        if (window.sagDesktop?.getDiagnosticsInfo) {
          try {
            const info = await window.sagDesktop.getDiagnosticsInfo();
            desktop = projectDesktopDiagnostics(info);
          } catch {
            desktop = {
              collection_error: "desktop_diagnostics_unavailable",
            };
          }
        }
        const frontend = getDiagnosticsStore().export({
          app: window.sagDesktop?.isDesktop ? "desktop" : "web",
          desktop_version: typeof desktop?.version === "string" ? desktop.version : undefined,
          user_agent: navigator.userAgent,
          language: navigator.language,
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        });
        downloadJson(
          sanitize({
            schema_version: 1,
            exported_at: new Date().toISOString(),
            transfer_id: transferId,
            server,
            frontend,
            desktop,
          }),
          `sag-octx-diagnostics-${transferId}.json`,
        );
        toast.success(t("exportDiagnosticsReady"));
      } catch (error) {
        reportError(error);
      }
    },
    [reportError, t],
  );

  React.useEffect(() => {
    const timers = tasks.flatMap((task) => {
      const delay = exportDismissDelay(task);
      if (delay === null) return [];
      return [window.setTimeout(() => managerRef.current?.dismiss(task.transferId), delay)];
    });
    return () => timers.forEach((timer) => window.clearTimeout(timer));
  }, [tasks]);

  const value = React.useMemo<OctxExportsContextValue>(
    () => ({
      tasks,
      startExport,
      startDocumentExport,
      confirmReadyOnly,
      cancelDecision,
      cancelTransfer,
      downloadAgain,
      downloadDiagnostics,
      dismissTransfer,
      isSourceExporting: (sourceId) => isActiveExportForSource(tasks, sourceId),
    }),
    [
      cancelDecision,
      cancelTransfer,
      confirmReadyOnly,
      dismissTransfer,
      downloadAgain,
      downloadDiagnostics,
      startDocumentExport,
      startExport,
      tasks,
    ],
  );

  return <OctxExportsContext.Provider value={value}>{children}</OctxExportsContext.Provider>;
}

export function useOctxExports(): OctxExportsContextValue {
  const value = React.useContext(OctxExportsContext);
  if (!value) throw new Error("useOctxExports must be used inside OctxExportProvider");
  return value;
}
