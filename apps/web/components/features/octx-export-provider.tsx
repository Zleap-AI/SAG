"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import { OctxExportTaskManager } from "@/lib/octx-export-manager";
import { exportDismissDelay } from "@/lib/octx-export-dismissal";
import {
  isActiveExportForSource,
  OCTX_EXPORT_TASKS_STORAGE_KEY,
  type PersistedOctxExportTask,
} from "@/lib/octx-export-tasks";

const POLL_INTERVAL_MS = 1500;

interface OctxExportsContextValue {
  tasks: PersistedOctxExportTask[];
  startExport: (sourceId: string, sourceName: string) => Promise<void>;
  confirmReadyOnly: (transferId: string, decisionToken: string) => Promise<void>;
  cancelDecision: (transferId: string, decisionToken: string) => Promise<void>;
  cancelTransfer: (transferId: string) => Promise<void>;
  downloadAgain: (transferId: string) => Promise<void>;
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
      confirmReadyOnly,
      cancelDecision,
      cancelTransfer,
      downloadAgain,
      dismissTransfer,
      isSourceExporting: (sourceId) => isActiveExportForSource(tasks, sourceId),
    }),
    [
      cancelDecision,
      cancelTransfer,
      confirmReadyOnly,
      dismissTransfer,
      downloadAgain,
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
