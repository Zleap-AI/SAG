import { contextBridge, ipcRenderer } from "electron";

import {
  DESKTOP_CHANNELS,
  type DesktopDiagnosticsInfo,
  type UpdateState,
} from "./channels";

export interface SagDesktopBridge {
  readonly isDesktop: true;
  readonly platform: NodeJS.Platform;
  appInfo(): Promise<{ version: string; platform: NodeJS.Platform; arch: string }>;
  checkForUpdates(): Promise<{ supported: boolean }>;
  getDiagnosticsInfo(): Promise<DesktopDiagnosticsInfo>;
  onUpdateState(listener: (state: UpdateState) => void): () => void;
}

const bridge: SagDesktopBridge = Object.freeze({
  isDesktop: true,
  platform: process.platform,
  appInfo: () => ipcRenderer.invoke(DESKTOP_CHANNELS.appInfo),
  checkForUpdates: () => ipcRenderer.invoke(DESKTOP_CHANNELS.checkForUpdates),
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
