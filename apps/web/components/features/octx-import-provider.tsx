"use client";

import * as React from "react";
import { toast } from "sonner";
import { useTranslations } from "next-intl";

import { useKnowledgeWorkspace } from "@/components/features/knowledge-provider";
import { api, ApiError } from "@/lib/api";
import { OctxImportTaskManager } from "@/lib/octx-import-manager";
import {
  OCTX_IMPORT_TASKS_STORAGE_KEY,
  type PersistedOctxImportTask,
} from "@/lib/octx-import-tasks";
import type { OctxImportAction } from "@/lib/types";

interface ContextValue {
  tasks: PersistedOctxImportTask[];
  startImport: (file: File) => Promise<void>;
  cancelTransfer: (id: string) => Promise<void>;
  decideImport: (id: string, body: {
    action: OctxImportAction;
    decision_token: string;
    target_source_id?: string;
    discard_local_changes?: boolean;
  }) => Promise<void>;
  dismissTransfer: (id: string) => void;
}

const Context = React.createContext<ContextValue | null>(null);

export function OctxImportProvider({ children }: { children: React.ReactNode }) {
  const t = useTranslations("Knowledge");
  const { refresh } = useKnowledgeWorkspace();
  const [tasks, setTasks] = React.useState<PersistedOctxImportTask[]>([]);
  const managerRef = React.useRef<OctxImportTaskManager | null>(null);
  const completedRef = React.useRef(new Set<string>());

  React.useEffect(() => {
    const manager = new OctxImportTaskManager({
      load: () => window.localStorage.getItem(OCTX_IMPORT_TASKS_STORAGE_KEY),
      save: (value) => window.localStorage.setItem(OCTX_IMPORT_TASKS_STORAGE_KEY, value),
      startImport: api.importOctxPackage,
      getTransfer: api.getOctxTransfer,
      cancelTransfer: api.cancelOctxTransfer,
      decideImport: api.decideOctxImport,
      newTransferId: () => crypto.randomUUID().replaceAll("-", ""),
      now: () => new Date().toISOString(),
    });
    managerRef.current = manager;
    const unsubscribe = manager.subscribe(setTasks);
    manager.hydrate();
    void manager.refresh();
    const interval = window.setInterval(() => void manager.refresh(), 1500);
    const refreshActive = () => void manager.refresh();
    window.addEventListener("online", refreshActive);
    document.addEventListener("visibilitychange", refreshActive);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener("online", refreshActive);
      document.removeEventListener("visibilitychange", refreshActive);
      unsubscribe();
      managerRef.current = null;
    };
  }, []);

  React.useEffect(() => {
    for (const task of tasks) {
      if (task.transfer.status !== "ready" || task.completionNotified || completedRef.current.has(task.transferId)) continue;
      completedRef.current.add(task.transferId);
      toast.success(t("importReady"));
      void refresh();
      managerRef.current?.acknowledgeCompletion(task.transferId);
    }
  }, [refresh, t, tasks]);

  React.useEffect(() => {
    const timers = tasks.flatMap((task) => {
      const delay = task.transfer.status === "ready" ? 6000
        : task.transfer.status === "cancelled" || task.transfer.status === "expired" ? 3000
          : null;
      return delay === null ? [] : [window.setTimeout(() => managerRef.current?.dismiss(task.transferId), delay)];
    });
    return () => timers.forEach((timer) => window.clearTimeout(timer));
  }, [tasks]);

  const report = React.useCallback((error: unknown) => {
    toast.error(error instanceof ApiError ? error.message : t("importFailed"));
  }, [t]);

  const value = React.useMemo<ContextValue>(() => ({
    tasks,
    startImport: async (file) => {
      try {
        await managerRef.current?.start(file);
        toast.message(t("importStarted"));
      } catch (error) { report(error); }
    },
    cancelTransfer: async (id) => {
      try { await managerRef.current?.cancel(id); } catch (error) { report(error); }
    },
    decideImport: async (id, body) => {
      try { await managerRef.current?.decide(id, body); } catch (error) { report(error); }
    },
    dismissTransfer: (id) => managerRef.current?.dismiss(id),
  }), [report, t, tasks]);

  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useOctxImports() {
  const value = React.useContext(Context);
  if (!value) throw new Error("useOctxImports must be used inside OctxImportProvider");
  return value;
}
