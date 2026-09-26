import { closeSync, openSync, readdirSync, readSync, statSync } from "node:fs";
import { release as osRelease, version as osVersion } from "node:os";
import path from "node:path";

import {
  app,
  autoUpdater as nativeUpdater,
  BrowserWindow,
  ipcMain,
  dialog,
  shell,
  type IpcMainInvokeEvent,
} from "electron";
import log from "electron-log/main";

import { DESKTOP_CHANNELS, type DesktopDiagnosticsInfo } from "./channels";
import {
  startPackagedRuntime,
  waitForDevelopmentRuntime,
  type ManagedRuntime,
} from "./runtime";
import { createUpdaterController, type UpdaterController } from "./updater";
import { RuntimeController } from "./runtime-controller";
import { ExitController } from "./exit-controller";

let mainWindow: BrowserWindow | null = null;
let splashWindow: BrowserWindow | null = null;
let updater: UpdaterController | null = null;
let trustedOrigin = "";
let quitting = false;
let quitAllowed = false;
let updateQuitRequested = false;
let booting: Promise<void> | undefined;
let failureDialog: Promise<void> | undefined;

const runtime = new RuntimeController<ManagedRuntime>(
  (signal) => app.isPackaged
    ? startPackagedRuntime(signal)
    : waitForDevelopmentRuntime(process.env.SAG_DESKTOP_DEV_WEB_URL || "http://127.0.0.1:3000", signal),
  (state) => {
    log.info("Desktop runtime", state);
    if (state.phase === "error" && !quitting) void presentRuntimeFailure(state.message);
  },
);
const exitController = new ExitController(
  async () => runtime.session ? runtime.session.inspectActivity() : false,
  async (reason, active) => {
    const action = reason === "update" ? "重启并安装" : "退出";
    const result = await dialog.showMessageBox({
      type: "warning", title: "SAG", message: active === null
        ? "暂时无法确认后台任务状态" : "仍有正在处理或排队的任务",
      detail: `${action}会中断未完成的处理。你可以取消并等待任务完成后再试。`,
      buttons: ["取消", `仍然${action}`], defaultId: 0, cancelId: 0,
    });
    return result.response === 1;
  },
  async () => {
    quitting = true;
    try { await runtime.stop(); } catch (error) { quitting = false; throw error; }
  },
);

if (!app.isPackaged) {
  app.setPath("userData", path.join(app.getPath("appData"), "SAG Development"));
}

log.initialize();

// Cap each log file at 5MB. On overflow electron-log rotates main.log ->
// main.old.log (keeping a single archive), so on-disk usage stays bounded at
// ~10MB total and older logs are discarded automatically — no manual cleanup
// needed. 5MB comfortably covers a recent troubleshooting window while staying
// small enough to export and share.
log.transports.file.maxSize = 5 * 1024 * 1024;

const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) app.quit();

function createSplashWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 420,
    height: 260,
    resizable: false,
    frame: false,
    show: false,
    backgroundColor: "#09090b",
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
    },
  });
  void window.loadFile(path.join(app.getAppPath(), "assets", "splash.html"));
  window.once("ready-to-show", () => window.show());
  return window;
}

function escapeHtml(value: string): string {
  return value.replace(
    /[&<>"']/g,
    (character) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#039;",
      })[character] ?? character,
  );
}

function showStartupError(error: unknown): void {
  const message = error instanceof Error ? error.message : String(error);
  log.error("Desktop startup failed", error);
  const safeMessage = escapeHtml(message);
  const html =
    `<meta charset="utf-8"><style>`
      + `body{margin:0;background:#09090b;color:#fafafa;font:14px system-ui;`
      + `display:grid;place-items:center;min-height:100vh}`
      + `main{max-width:340px;text-align:center}p{color:#a1a1aa;line-height:1.5}`
      + `</style><main><h2>SAG 启动失败</h2><p>${safeMessage}</p></main>`;
  void splashWindow?.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
}

function isTrustedSender(event: IpcMainInvokeEvent): boolean {
  try {
    return new URL(event.senderFrame?.url ?? "").origin === trustedOrigin;
  } catch {
    return false;
  }
}

const LOG_TAIL_BYTES = 5 * 1024 * 1024; // Export at most the last 5MB per log.

/**
 * Read up to the last `LOG_TAIL_BYTES` of a file without loading the whole
 * thing into memory. Returns the tail text and whether it was truncated.
 * Uses only node:fs primitives (no extra dependency).
 */
function readLogTail(filePath: string, sizeBytes: number): {
  content: string;
  truncated: boolean;
} {
  const truncated = sizeBytes > LOG_TAIL_BYTES;
  const start = truncated ? sizeBytes - LOG_TAIL_BYTES : 0;
  const length = sizeBytes - start;
  if (length <= 0) return { content: "", truncated: false };
  const fd = openSync(filePath, "r");
  try {
    const buffer = Buffer.allocUnsafe(length);
    let offset = 0;
    while (offset < length) {
      const read = readSync(fd, buffer, offset, length - offset, start + offset);
      if (read <= 0) break;
      offset += read;
    }
    return { content: buffer.subarray(0, offset).toString("utf8"), truncated };
  } finally {
    closeSync(fd);
  }
}

function registerIpc(): void {
  ipcMain.removeHandler(DESKTOP_CHANNELS.appInfo);
  ipcMain.removeHandler(DESKTOP_CHANNELS.checkForUpdates);
  ipcMain.removeHandler(DESKTOP_CHANNELS.getUpdateState);
  ipcMain.removeHandler(DESKTOP_CHANNELS.downloadUpdate);
  ipcMain.removeHandler(DESKTOP_CHANNELS.installUpdate);
  ipcMain.removeHandler(DESKTOP_CHANNELS.diagnosticsInfo);
  ipcMain.handle(DESKTOP_CHANNELS.appInfo, (event) => {
    if (!isTrustedSender(event)) throw new Error("Untrusted IPC sender");
    return { version: app.getVersion(), platform: process.platform, arch: process.arch };
  });
  ipcMain.handle(DESKTOP_CHANNELS.checkForUpdates, async (event) => {
    if (!isTrustedSender(event)) throw new Error("Untrusted IPC sender");
    return updater?.check() ?? { supported: false };
  });
  ipcMain.handle(DESKTOP_CHANNELS.getUpdateState, (event) => {
    if (!isTrustedSender(event)) throw new Error("Untrusted IPC sender");
    return updater?.getState() ?? { status: "idle" };
  });
  ipcMain.handle(DESKTOP_CHANNELS.downloadUpdate, (event, version: unknown) => {
    if (!isTrustedSender(event)) throw new Error("Untrusted IPC sender");
    if (typeof version !== "string" || !version) return { started: false };
    return updater?.download(version) ?? { started: false };
  });
  ipcMain.handle(DESKTOP_CHANNELS.installUpdate, (event, version: unknown) => {
    if (!isTrustedSender(event)) throw new Error("Untrusted IPC sender");
    if (typeof version !== "string" || !version) return { started: false };
    return updater?.install(version) ?? { started: false };
  });
  ipcMain.handle(DESKTOP_CHANNELS.diagnosticsInfo, (event) => {
    if (!isTrustedSender(event)) throw new Error("Untrusted IPC sender");
    const logFiles: DesktopDiagnosticsInfo["logFiles"] = [];
    try {
      const logPath = log.transports.file.getFile().path;
      const dir = path.dirname(logPath);
      const entries = readdirSync(dir);
      for (const name of entries) {
        if (name.endsWith(".log")) {
          const filePath = path.join(dir, name);
          const sizeBytes = statSync(filePath).size;
          let content = "";
          let truncated = false;
          try {
            const tail = readLogTail(filePath, sizeBytes);
            content = tail.content;
            truncated = tail.truncated;
          } catch {
            // best-effort: an unreadable file still lists its metadata
          }
          logFiles.push({ name, path: filePath, sizeBytes, content, truncated });
        }
      }
    } catch {
      // best-effort: log file discovery is not critical
    }
    return {
      version: app.getVersion(),
      platform: process.platform,
      arch: process.arch,
      osRelease: osRelease(),
      osVersion: osVersion(),
      packaged: app.isPackaged,
      electron: process.versions.electron ?? "unknown",
      chrome: process.versions.chrome ?? "unknown",
      node: process.versions.node ?? "unknown",
      logFiles,
    };
  });
}

function installNavigationPolicy(window: BrowserWindow): void {
  window.webContents.on("will-navigate", (event, url) => {
    try {
      const parsed = new URL(url);
      if (parsed.origin === trustedOrigin) return;
      if (parsed.protocol === "https:" || parsed.protocol === "http:") {
        void shell.openExternal(parsed.toString());
      }
    } catch {
      // Fall through and block malformed URLs.
    }
    event.preventDefault();
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    try {
      const parsed = new URL(url);
      if (parsed.origin === trustedOrigin) {
        void window.loadURL(parsed.toString());
        return { action: "deny" };
      }
      if (parsed.protocol === "https:" || parsed.protocol === "http:") {
        void shell.openExternal(parsed.toString());
      }
    } catch {
      // Invalid URLs are denied below.
    }
    return { action: "deny" };
  });
}

function createMainWindow(webUrl: string): BrowserWindow {
  trustedOrigin = new URL(webUrl).origin;
  const window = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 960,
    minHeight: 640,
    show: false,
    backgroundColor: "#09090b",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
    },
  });
  if (app.isPackaged) window.setMenu(null);
  installNavigationPolicy(window);
  window.once("ready-to-show", () => {
    splashWindow?.close();
    splashWindow = null;
    window.show();
  });
  window.on("close", (event) => {
    if (process.platform !== "darwin" && !quitAllowed) {
      event.preventDefault();
      app.quit();
    }
  });
  window.on("closed", () => {
    mainWindow = null;
  });
  void window.loadURL(webUrl);
  return window;
}

function bootstrap(): Promise<void> {
  if (booting) return booting;
  if (quitting) return Promise.resolve();
  if (!splashWindow || splashWindow.isDestroyed()) splashWindow = createSplashWindow();
  booting = (async () => {
    try {
      const session = await runtime.start();
      if (quitting || runtime.state.phase !== "ready") return;
      if (mainWindow && !mainWindow.isDestroyed()) {
        trustedOrigin = new URL(session.webUrl).origin;
        await mainWindow.loadURL(session.webUrl);
        splashWindow?.close(); splashWindow = null;
        mainWindow.show();
      } else mainWindow = createMainWindow(session.webUrl);
      if (!updater) updater = createUpdaterController(() => mainWindow, {
        beforeInstall: async () => {
          const prepared = await exitController.prepare("update");
          if (prepared) quitAllowed = true;
          return prepared;
        },
        onInstallError: () => {
          if (!quitAllowed && !quitting) return;
          quitAllowed = false; quitting = false;
          void bootstrap();
        },
      });
      registerIpc();
    } catch (error) {
      if (!quitting) {
        showStartupError(error);
        void presentRuntimeFailure(error instanceof Error ? error.message : String(error));
      }
    }
  })().finally(() => { booting = undefined; });
  return booting;
}

function presentRuntimeFailure(message: string): Promise<void> {
  if (failureDialog) return failureDialog;
  failureDialog = (async () => {
    await booting;
    if (quitting) { failureDialog = undefined; return; }
    const result = await dialog.showMessageBox({
      type: "error", title: "SAG 本地服务不可用", message: "本地服务启动失败或意外停止",
      detail: `${message}\n请检查日志；重试会重新启动本地服务，未完成的任务需在恢复后确认。`,
      buttons: ["退出", "重试"], defaultId: 1, cancelId: 0,
    });
    failureDialog = undefined;
    if (quitting) return;
    if (result.response === 1) void bootstrap();
    else app.quit();
  })().catch((error) => { failureDialog = undefined; log.error("Failed to present runtime failure", error); });
  return failureDialog;
}

if (gotSingleInstanceLock) {
  app.on("second-instance", () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });

  app.whenReady().then(bootstrap).catch(showStartupError);

  app.on("activate", () => {
    if (mainWindow) {
      mainWindow.show();
      return;
    }
    if (runtime.session && runtime.state.phase === "ready") {
      mainWindow = createMainWindow(runtime.session.webUrl);
      return;
    }
    if (!quitting && !failureDialog) void bootstrap();
  });

  nativeUpdater.on("before-quit-for-update", () => { updateQuitRequested = true; });
  app.on("before-quit", (event) => {
    if (updateQuitRequested) {
      updateQuitRequested = false;
      // NSIS may already have queued this quit before its launch error arrived.
      if (!quitAllowed) { event.preventDefault(); return; }
    }
    if (quitAllowed) { updater?.dispose(); return; }
    event.preventDefault();
    void exitController.prepare("quit").then((prepared) => {
      if (!prepared) return;
      quitAllowed = true;
      updater?.dispose();
      app.quit();
    }).catch((error) => {
      log.error("Desktop shutdown failed", error);
      dialog.showErrorBox("SAG 退出失败", "本地服务未能完成清理。请关闭 SAG 后重试；详情见日志。");
    });
  });

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
}
