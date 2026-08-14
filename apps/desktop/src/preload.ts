import { contextBridge, ipcRenderer } from "electron";

import type { DesktopDiagnosticsInfo, UpdateState } from "./channels";

// A sandboxed Electron preload only supports a limited require() surface and
// cannot load local CommonJS modules. Keep these stable IPC names self-contained
// so the bridge is available in packaged builds.
const DESKTOP_CHANNELS = {
  appInfo: "desktop:app-info",
  checkForUpdates: "desktop:check-for-updates",
  getUpdateState: "desktop:get-update-state",
  installUpdate: "desktop:install-update",
  diagnosticsInfo: "desktop:diagnostics-info",
  updateState: "desktop:update-state",
} as const;

export interface SagDesktopBridge {
  readonly isDesktop: true;
  readonly platform: NodeJS.Platform;
  appInfo(): Promise<{ version: string; platform: NodeJS.Platform; arch: string }>;
  checkForUpdates(): Promise<{ supported: boolean }>;
  getUpdateState(): Promise<UpdateState>;
  installUpdate(): Promise<{ started: boolean }>;
  getDiagnosticsInfo(): Promise<DesktopDiagnosticsInfo>;
  onUpdateState(listener: (state: UpdateState) => void): () => void;
}

const bridge: SagDesktopBridge = Object.freeze({
  isDesktop: true,
  platform: process.platform,
  appInfo: () => ipcRenderer.invoke(DESKTOP_CHANNELS.appInfo),
  checkForUpdates: () => ipcRenderer.invoke(DESKTOP_CHANNELS.checkForUpdates),
  getUpdateState: () => ipcRenderer.invoke(DESKTOP_CHANNELS.getUpdateState),
  installUpdate: () => ipcRenderer.invoke(DESKTOP_CHANNELS.installUpdate),
  getDiagnosticsInfo: () =>
    ipcRenderer.invoke(DESKTOP_CHANNELS.diagnosticsInfo),
  onUpdateState: (listener: (state: UpdateState) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, state: UpdateState) => {
      listener(state);
    };
    ipcRenderer.on(DESKTOP_CHANNELS.updateState, handler);
    return () => ipcRenderer.removeListener(DESKTOP_CHANNELS.updateState, handler);
  },
});

contextBridge.exposeInMainWorld("sagDesktop", bridge);
