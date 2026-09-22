import { existsSync } from "node:fs";
import path from "node:path";

import { app, autoUpdater as nativeUpdater, type BrowserWindow, dialog, shell } from "electron";
import log from "electron-log/main";
import { autoUpdater, type ProgressInfo, type UpdateInfo } from "electron-updater";

import { DESKTOP_CHANNELS, type UpdateState } from "./channels";
import { desktopConfig } from "./config";
import { describeUpdaterError } from "./updater-error";

export interface UpdaterController {
  check(): Promise<{ supported: boolean }>;
  getState(): UpdateState;
  download(version: string): Promise<{ started: boolean }>;
  install(version: string): { started: boolean };
  dispose(): void;
}

export function createUpdaterController(
  getWindow: () => BrowserWindow | null,
): UpdaterController {
  let delayTimer: NodeJS.Timeout | null = null;
  let intervalTimer: NodeJS.Timeout | null = null;
  let currentState: UpdateState = { status: "idle" };
  let checkPending = false;
  let downloadPending = false;
  let installPending = false;
  let consentVersion: string | undefined;
  let nativeInstallListeners: ReturnType<typeof nativeUpdater.listeners> = [];
  const clearNativeInstallListeners = () => {
    for (const listener of nativeInstallListeners) {
      nativeUpdater.removeListener("update-downloaded", listener);
    }
    nativeInstallListeners = [];
  };
  const supported =
    app.isPackaged
    && existsSync(path.join(process.resourcesPath, "app-update.yml"));

  const publish = (state: UpdateState) => {
    currentState = state;
    const window = getWindow();
    if (window && !window.isDestroyed()) {
      window.webContents.send(DESKTOP_CHANNELS.updateState, state);
    }
  };

  const showUpdaterError = async (error: unknown): Promise<void> => {
    const presentation = describeUpdaterError(error);
    const window = getWindow();
    if (!window || window.isDestroyed()) return;
    const buttons = presentation.actionLabel
      ? ["知道了", presentation.actionLabel]
      : ["知道了"];
    try {
      const result = await dialog.showMessageBox(window, {
        type: "error",
        title: presentation.title,
        message: presentation.message,
        detail: presentation.detail,
        buttons,
        defaultId: presentation.actionLabel ? 1 : 0,
        cancelId: 0,
      });
      if (presentation.actionUrl && result.response === 1) {
        await shell.openExternal(presentation.actionUrl);
      }
    } catch (dialogError) {
      log.error("Failed to present updater error", dialogError);
    }
  };

  const pendingUpdate = () => downloadPending || installPending
    || currentState.status === "downloading" || currentState.status === "downloaded"
    || (currentState.status === "error" && currentState.operation !== "check");

  const fail = (error: unknown, operation: "check" | "download" | "install") => {
    const message = error instanceof Error ? error.message : String(error);
    log.error(`Update ${operation} failed`, error);
    publish({ status: "error", message, operation, version: consentVersion });
    if (operation === "install") {
      installPending = false;
      clearNativeInstallListeners();
      void showUpdaterError(error);
    }
  };

  const check = async (): Promise<{ supported: boolean }> => {
    if (!supported) return { supported: false };
    if (checkPending || pendingUpdate()) return { supported: true };
    checkPending = true;
    publish({ status: "checking" });
    try {
      await autoUpdater.checkForUpdates();
    } catch (error) {
      if (!pendingUpdate()) fail(error, "check");
    } finally {
      checkPending = false;
    }
    return { supported: true };
  };

  if (supported) {
    autoUpdater.logger = log;
    // Consent is deliberately session-local: cached packages never authorize install.
    autoUpdater.autoDownload = false;
    autoUpdater.autoInstallOnAppQuit = false;

    autoUpdater.on("checking-for-update", () => {
      if (!pendingUpdate()) publish({ status: "checking" });
    });
    autoUpdater.on("update-available", (info: UpdateInfo) => {
      if (!pendingUpdate()) publish({ status: "available", version: info.version });
    });
    autoUpdater.on("update-not-available", () => {
      if (!pendingUpdate()) publish({ status: "not-available" });
    });
    autoUpdater.on("download-progress", (progress: ProgressInfo) => {
      if (downloadPending && currentState.status === "downloading" && consentVersion) {
        publish({ status: "downloading", version: consentVersion, percent: progress.percent });
      }
    });
    autoUpdater.on("error", (error) => {
      // downloadUpdate also rejects; its catch owns that operation's failure.
      if (downloadPending) return;
      if (installPending) fail(error, "install");
      else if (!pendingUpdate()) fail(error, "check");
    });
    autoUpdater.on("update-downloaded", (info: UpdateInfo) => {
      if (currentState.status === "downloading" && info.version === consentVersion) {
        publish({ status: "downloaded", version: info.version });
      }
    });

    delayTimer = setTimeout(() => {
      void check();
      intervalTimer = setInterval(() => void check(), desktopConfig.updateCheckIntervalMs);
      intervalTimer.unref();
    }, desktopConfig.updateCheckDelayMs);
    delayTimer.unref();
  }

  return {
    check,
    getState: () => currentState,
    download: async (version) => {
      const canDownload = currentState.status === "available"
        || (currentState.status === "error" && currentState.operation === "download");
      if (!supported || checkPending || downloadPending || installPending
        || !canDownload || !("version" in currentState) || currentState.version !== version) {
        return { started: false };
      }
      consentVersion = version;
      downloadPending = true;
      publish({ status: "downloading", version, percent: 0 });
      try {
        await autoUpdater.downloadUpdate();
        return { started: true };
      } catch (error) {
        fail(error, "download");
        return { started: false };
      } finally {
        downloadPending = false;
      }
    },
    install: (version) => {
      const canInstall = currentState.status === "downloaded"
        || (currentState.status === "error" && currentState.operation === "install");
      if (!supported || downloadPending || installPending || !canInstall
        || !("version" in currentState) || version !== currentState.version || version !== consentVersion) {
        return { started: false };
      }
      installPending = true;
      const previousListeners = new Set(nativeUpdater.listeners("update-downloaded"));
      try {
        log.info("Applying explicitly requested update via quitAndInstall(false, true)");
        autoUpdater.quitAndInstall(false, true);
        return { started: installPending };
      } catch (error) {
        fail(error, "install");
        return { started: false };
      } finally {
        // MacUpdater leaves its Squirrel install callback registered after a
        // signature/network error. Remove only listeners added by this attempt
        // on failure, so an explicit retry cannot install twice.
        nativeInstallListeners = nativeUpdater.listeners("update-downloaded")
          .filter((listener) => !previousListeners.has(listener));
        if (!installPending) clearNativeInstallListeners();
      }
    },
    dispose: () => {
      if (delayTimer) clearTimeout(delayTimer);
      if (intervalTimer) clearInterval(intervalTimer);
    },
  };
}
