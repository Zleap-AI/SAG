export const DESKTOP_CHANNELS = {
  appInfo: "desktop:app-info",
  checkForUpdates: "desktop:check-for-updates",
  updateState: "desktop:update-state",
  diagnosticsInfo: "desktop:diagnostics-info",
} as const;

export type UpdateState =
  | { status: "idle" }
  | { status: "checking" }
  | { status: "available"; version: string }
  | { status: "not-available" }
  | { status: "downloading"; percent: number }
  | { status: "downloaded"; version: string }
  | { status: "error"; message: string };

export interface DesktopDiagnosticsInfo {
  version: string;
  platform: string;
  arch: string;
  electron: string;
  chrome: string;
  node: string;
  logFiles: Array<{
    name: string;
    path: string;
    sizeBytes: number;
    /** Tail of the file content (up to 5MB). Empty string if unreadable. */
    content: string;
    /** True when only the tail was captured because the file exceeded the cap. */
    truncated: boolean;
  }>;
}
