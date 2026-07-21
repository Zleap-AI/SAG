import path from "node:path";

import {
  app,
  BrowserWindow,
  ipcMain,
  shell,
  type IpcMainInvokeEvent,
} from "electron";
import log from "electron-log/main";

import { DESKTOP_CHANNELS } from "./channels";
import {
  startPackagedRuntime,
  waitForDevelopmentRuntime,
  type ManagedRuntime,
} from "./runtime";
import { createUpdaterController, type UpdaterController } from "./updater";

let mainWindow: BrowserWindow | null = null;
let splashWindow: BrowserWindow | null = null;
let runtime: ManagedRuntime | null = null;
let updater: UpdaterController | null = null;
let trustedOrigin = "";
let quitting = false;

if (!app.isPackaged) {
  app.setPath("userData", path.join(app.getPath("appData"), "SAG Development"));
}

log.initialize();

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

function registerIpc(): void {
  ipcMain.removeHandler(DESKTOP_CHANNELS.appInfo);
  ipcMain.removeHandler(DESKTOP_CHANNELS.checkForUpdates);
  ipcMain.handle(DESKTOP_CHANNELS.appInfo, (event) => {
    if (!isTrustedSender(event)) throw new Error("Untrusted IPC sender");
    return { version: app.getVersion(), platform: process.platform, arch: process.arch };
  });
  ipcMain.handle(DESKTOP_CHANNELS.checkForUpdates, async (event) => {
    if (!isTrustedSender(event)) throw new Error("Untrusted IPC sender");
    return updater?.check() ?? { supported: false };
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
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
    },
  });
  installNavigationPolicy(window);
  window.once("ready-to-show", () => {
    splashWindow?.close();
    splashWindow = null;
    window.show();
  });
  window.on("closed", () => {
    mainWindow = null;
  });
  void window.loadURL(webUrl);
  return window;
}

async function bootstrap(): Promise<void> {
  splashWindow = createSplashWindow();
  const devWebUrl =
    process.env.SAG_DESKTOP_DEV_WEB_URL || "http://127.0.0.1:3000";
  try {
    runtime = app.isPackaged
      ? await startPackagedRuntime()
      : await waitForDevelopmentRuntime(devWebUrl);
    mainWindow = createMainWindow(runtime.webUrl);
    updater = createUpdaterController(() => mainWindow);
    registerIpc();
  } catch (error) {
    showStartupError(error);
  }
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
    if (runtime) {
      mainWindow = createMainWindow(runtime.webUrl);
      return;
    }
    if (!quitting) void bootstrap();
  });

  app.on("before-quit", () => {
    quitting = true;
    updater?.dispose();
    runtime?.stop();
    runtime = null;
  });

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
}
