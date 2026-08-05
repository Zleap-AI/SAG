/**
 * Desktop bridge typed declaration.
 * The actual bridge is injected by Electron's preload script (apps/desktop/src/preload.ts).
 * When running in a browser, window.sagDesktop is undefined.
 */

export interface SagDesktopDiagnosticsInfo {
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
    content: string;
    truncated: boolean;
  }>;
}

export interface SagDesktopBridge {
  readonly isDesktop: true;
  readonly platform: string;
  appInfo(): Promise<{ version: string; platform: string; arch: string }>;
  checkForUpdates(): Promise<{ supported: boolean }>;
  getDiagnosticsInfo(): Promise<SagDesktopDiagnosticsInfo>;
  onUpdateState(
    listener: (state: {
      status: string;
      version?: string;
      percent?: number;
      message?: string;
    }) => void,
  ): () => void;
}

declare global {
  interface Window {
    sagDesktop?: SagDesktopBridge;
  }
}

export {};
