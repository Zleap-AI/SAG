/**
 * Desktop bridge typed declaration.
 * The actual bridge is injected by Electron's preload script (apps/desktop/src/preload.ts).
 * When running in a browser, window.sagDesktop is undefined.
 */

export interface SagDesktopDiagnosticsInfo {
  version: string;
  platform: string;
  arch: string;
  osRelease: string;
  osVersion: string;
  packaged: boolean;
  electron: string;
  chrome: string;
  node: string;
  logFiles: Array<{
    name: string;
    path: string;
    sizeBytes: number;
    content: string;
    truncated: boolean;
  }>;
}

export type SagDesktopUpdateState =
  | { status: "idle" }
  | { status: "checking" }
  | { status: "available"; version: string }
  | { status: "not-available" }
  | { status: "downloading"; percent: number }
  | { status: "downloaded"; version: string }
  | { status: "error"; message: string };

export interface SagDesktopBridge {
  readonly isDesktop: true;
  readonly platform: string;
  appInfo(): Promise<{ version: string; platform: string; arch: string }>;
  checkForUpdates(): Promise<{ supported: boolean }>;
  getUpdateState(): Promise<SagDesktopUpdateState>;
  installUpdate(): Promise<{ started: boolean }>;
  getDiagnosticsInfo(): Promise<SagDesktopDiagnosticsInfo>;
  onUpdateState(
    listener: (state: SagDesktopUpdateState) => void,
  ): () => void;
}

declare global {
  interface Window {
    sagDesktop?: SagDesktopBridge;
  }
}

export {};
