import { existsSync } from "node:fs";
import path from "node:path";

import { app, type BrowserWindow, dialog } from "electron";
import log from "electron-log/main";
import { autoUpdater, type ProgressInfo, type UpdateInfo } from "electron-updater";

import { DESKTOP_CHANNELS, type UpdateState } from "./channels";
import { desktopConfig } from "./config";

export interface UpdaterController {
  check(): Promise<{ supported: boolean }>;
  dispose(): void;
}

export function createUpdaterController(
  getWindow: () => BrowserWindow | null,
): UpdaterController {
  let delayTimer: NodeJS.Timeout | null = null;
  let intervalTimer: NodeJS.Timeout | null = null;
  const supported =
    app.isPackaged
    && existsSync(path.join(process.resourcesPath, "app-update.yml"));

  const publish = (state: UpdateState) => {
    const window = getWindow();
    if (window && !window.isDestroyed()) {
      window.webContents.send(DESKTOP_CHANNELS.updateState, state);
    }
  };

  const check = async (): Promise<{ supported: boolean }> => {
    if (!supported) return { supported: false };
    publish({ status: "checking" });
    try {
      await autoUpdater.checkForUpdates();
      return { supported: true };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      log.error("Update check failed", error);
      publish({ status: "error", message });
      return { supported: true };
    }
  };

  if (supported) {
    autoUpdater.logger = log;
    autoUpdater.autoDownload = true;
    autoUpdater.autoInstallOnAppQuit = true;

    autoUpdater.on("checking-for-update", () => publish({ status: "checking" }));
    autoUpdater.on("update-available", (info: UpdateInfo) => {
      publish({ status: "available", version: info.version });
    });
    autoUpdater.on("update-not-available", () => {
      publish({ status: "not-available" });
    });
    autoUpdater.on("download-progress", (progress: ProgressInfo) => {
      publish({ status: "downloading", percent: progress.percent });
    });
    autoUpdater.on("error", (error) => {
      log.error("Updater error", error);
      publish({ status: "error", message: error.message });
    });
    autoUpdater.on("update-downloaded", async (info: UpdateInfo) => {
      publish({ status: "downloaded", version: info.version });
      const window = getWindow();
      if (!window || window.isDestroyed()) return;
      const result = await dialog.showMessageBox(window, {
        type: "info",
        title: "SAG 更新已就绪",
        message: `SAG ${info.version} 已下载完成`,
        detail: "可以立即重启安装，也可以在退出应用时自动安装。",
        buttons: ["稍后", "立即重启"],
        defaultId: 1,
        cancelId: 0,
      });
      if (result.response === 1) {
        autoUpdater.quitAndInstall(false, true);
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
    dispose: () => {
      if (delayTimer) clearTimeout(delayTimer);
      if (intervalTimer) clearInterval(intervalTimer);
    },
  };
}
