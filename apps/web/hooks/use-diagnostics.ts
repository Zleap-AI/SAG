"use client";

import * as React from "react";

import {
  getDiagnosticsStore,
  downloadDiagnostics,
  type DiagEntry,
  type DiagEnvironment,
  type DiagEventType,
  type DiagExport,
  type DiagLogFile,
} from "@/lib/diagnostics";

export type { DiagEntry, DiagEnvironment, DiagEventType, DiagExport, DiagLogFile };

function emptySnapshot(): DiagEntry[] {
  return [];
}

/**
 * React hook wrapping the global DiagnosticsStore singleton.
 * Provides reactive access to log entries and export helpers.
 */
export function useDiagnostics() {
  const store = getDiagnosticsStore();

  const subscribe = React.useCallback(
    (listener: () => void) => store.subscribe(listener),
    [store],
  );

  const getSnapshot = React.useCallback(() => store.snapshot(), [store]);

  const entries = React.useSyncExternalStore(subscribe, getSnapshot, emptySnapshot);

  const record = React.useCallback(
    (type: DiagEventType, data: Record<string, unknown> = {}) => {
      store.record(type, data);
    },
    [store],
  );

  const exportLogs = React.useCallback(
    (
      environment: DiagEnvironment,
      modelConfig?: Record<string, unknown> | null,
      capabilities?: Record<string, unknown> | null,
      desktopLogFiles?: DiagLogFile[],
    ): DiagExport => {
      return store.export(environment, modelConfig, capabilities, desktopLogFiles);
    },
    [store],
  );

  const downloadLogs = React.useCallback(
    (
      environment: DiagEnvironment,
      modelConfig?: Record<string, unknown> | null,
      capabilities?: Record<string, unknown> | null,
      desktopLogFiles?: DiagLogFile[],
    ) => {
      const export_ = store.export(environment, modelConfig, capabilities, desktopLogFiles);
      downloadDiagnostics(export_);
    },
    [store],
  );

  return React.useMemo(
    () => ({
      entries,
      count: entries.length,
      record,
      exportLogs,
      downloadLogs,
    }),
    [entries, record, exportLogs, downloadLogs],
  );
}
