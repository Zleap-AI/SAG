import { closeSync, openSync, readdirSync, readSync, statSync } from "node:fs";
import { release as osRelease, version as osVersion } from "node:os";
import path from "node:path";

import {
  app,
  autoUpdater as nativeUpdater,
  BrowserWindow,
  ipcMain,
  Menu,
  session as electronSession,
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
import { WorkspaceProfiles, type WorkspaceProfile, type RemoteProfileInput } from "./workspace-profiles";
import { WorkspaceController, trustedFrame, type WorkspaceDestination } from "./workspace-controller";
import { RemoteWebBoundary, type RemoteWindowBinding } from "./remote-web-boundary";
import { FnOsHostContext, type HostWindow } from "./fnos-host-context";

let mainWindow: BrowserWindow | null = null;
let managerWindow: BrowserWindow | null = null;
let workspaceMessage = "";
let switching = false;
let splashWindow: BrowserWindow | null = null;
let updater: UpdaterController | null = null;
let trustedOrigin = "";
let quitting = false;
let quitAllowed = false;
let updateQuitRequested = false;
let booting: Promise<void> | undefined;
let failureDialog: Promise<void> | undefined;
let workspaceGeneration = 0;

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
    workspaceGeneration++;
    fnosHost.close();
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

const profiles = new WorkspaceProfiles(app.getPath("userData"));
const workspaces = new WorkspaceController(profiles, {
  startLocal: async () => (await runtime.start()).webUrl,
  stopLocal: () => runtime.stop(),
  inspectLocal: async () => runtime.session ? runtime.session.inspectActivity() : false,
  confirmSwitch: async active => (await dialog.showMessageBox({
    type: "warning", title: "切换工作区", message: active === null ? "无法确认本机任务状态" : "本机仍有正在处理的任务",
    detail: "切换到 NAS 会停止本机服务，未完成任务将在下次启动时恢复。",
    buttons: ["取消", "停止本机服务并切换"], defaultId: 0, cancelId: 0,
  })).response === 1,
});
const fnosHost = new FnOsHostContext({
  create: createFnOsHostWindow,
  probe: async (profile, signal) => (await checkRemoteWorkspace(profile, signal, 2_000)).ok,
});

async function checkRemoteWorkspace(profile: WorkspaceProfile, signal: AbortSignal, timeoutMs = 10_000): Promise<{ ok: boolean; message: string }> {
  try {
    const response = await electronSession.fromPartition(profile.partition).fetch(
      `${profile.webOrigin}${profile.appPrefix}/api/v1/system/capabilities`,
      { signal: AbortSignal.any([signal, AbortSignal.timeout(timeoutMs)]), redirect: "error" });
    if ([401, 403].includes(response.status)) {
      await response.body?.cancel();
      return { ok: false, message: profile.deployment === "fnos" ? "请先在此连接的 NAS 宿主窗口完成登录。"
        : "请先打开工作区，在此连接的独立窗口登录服务器。" };
    }
    if (!response.ok) {
      await response.body?.cancel();
      return { ok: false, message: `工作台 API 返回 HTTP ${response.status}。请检查应用前缀与服务状态。` };
    }
    const capabilities: unknown = await response.json();
    if (!capabilities || typeof capabilities !== "object" || !("llm_configured" in capabilities)) {
      return { ok: false, message: "此地址未提供兼容的 SAG 工作台，请确认应用前缀与服务器版本。" };
    }
    return { ok: true, message: "SAG 工作台可连接。打开工作区后确认服务器和当前登录身份。" };
  } catch { return { ok: false, message: profile.deployment === "fnos"
    ? "宿主会话尚未就绪。请检查网络与地址，并在 NAS 宿主窗口完成登录。"
    : "无法验证工作台。请检查网络、证书与地址；如需要登录，先打开工作区。" }; }
}

async function openWorkspace(id: string, destination: WorkspaceDestination = "knowledge"): Promise<boolean> {
  const generation = ++workspaceGeneration;
  const previousId = workspaces.active?.id;
  if (id !== workspaces.active?.id) fnosHost.close();
  const opened = await workspaces.open(id, destination);
  if (!opened || quitting || generation !== workspaceGeneration) return false;
  // After switching is accepted, no old renderer may refill a partition being cleared
  // while a different host is still bootstrapping.
  if (destination !== "host-login" || previousId !== opened.profile.id) {
    switching = true;
    try { mainWindow?.destroy(); mainWindow = null; } finally { switching = false; }
  }
  const fnos = opened.profile.kind === "remote-web" && opened.profile.deployment === "fnos";
  if (fnos) {
    if (destination === "host-login") {
      fnosHost.show(opened.profile);
      splashWindow?.close(); splashWindow = null;
      return true;
    }
    const ready = await fnosHost.ready(opened.profile);
    if (quitting || generation !== workspaceGeneration) return false;
    if (!ready) {
      workspaceMessage = "请先在 NAS 窗口完成登录，再打开连接。";
      showWorkspaceManager(); splashWindow?.close(); splashWindow = null;
      return false;
    }
  } else fnosHost.close();
  switching = true;
  try { mainWindow?.destroy(); mainWindow = createMainWindow(opened.url, opened.profile); }
  finally { switching = false; }
  if (fnos) fnosHost.hide(opened.profile);
  return true;
}
function showWorkspaceManager(): void {
  if (managerWindow && !managerWindow.isDestroyed()) {
    void managerWindow.reload(); managerWindow.show(); managerWindow.focus(); return;
  }
  managerWindow = new BrowserWindow({ width: 800, height: 640, minWidth: 640, minHeight: 480,
    title: "SAG · 连接设置", backgroundColor: "#ffffff",
    webPreferences: { preload: path.join(__dirname, "workspace-preload.js"),
      nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true },
  });
  managerWindow.webContents.on("will-navigate", event => event.preventDefault());
  managerWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  managerWindow.on("closed", () => { managerWindow = null; });
  void managerWindow.loadFile(path.join(app.getAppPath(), "assets", "workspaces.html"));
}
function installWorkspaceMenu(): void {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    ...(process.platform === "darwin" ? [{ role: "appMenu" as const }] : []),
    { label: "工作区", submenu: [
      { label: "NAS / 远程连接…", accelerator: "CmdOrCtrl+Shift+W", click: showWorkspaceManager },
      { label: "重试当前工作区", click: () => { const id = profiles.read().activeId; if (id) void openWorkspace(id).catch(error => { workspaceMessage = String(error); showWorkspaceManager(); }); } },
    ] },
    { role: "editMenu" }, { role: "viewMenu" }, { role: "windowMenu" },
  ]));
}
function registerWorkspaceIpc(): void {
  const trust = (event: IpcMainInvokeEvent) => {
    if (!trustedFrame(event, managerWindow?.webContents ?? null) ||
        event.senderFrame?.url !== managerWindow?.webContents.getURL()) throw new Error("Untrusted workspace manager");
  };
  ipcMain.handle("workspace:list", event => { trust(event); return { ...profiles.read(), message: workspaceMessage }; });
  ipcMain.handle("workspace:save", async (event, input: RemoteProfileInput) => {
    trust(event);
    if (input?.id && input.id === workspaces.active?.id) {
      workspaceGeneration++; fnosHost.closeProfile(input.id);
      await workspaces.close(); switching = true;
      try { mainWindow?.destroy(); mainWindow = null; } finally { switching = false; }
    }
    const old = input?.id ? profiles.read().profiles.find(profile => profile.id === input.id) : undefined;
    const saved = profiles.save(input);
    if (old && old.partition !== saved.partition) await electronSession.fromPartition(old.partition).clearStorageData();
    workspaceMessage = ""; return saved;
  });
  ipcMain.handle("workspace:open", async (event, id: string) => { trust(event); workspaceMessage = ""; return { opened: await openWorkspace(id) }; });
  ipcMain.handle("workspace:host-login", async (event, id: string) => {
    trust(event);
    const opened = await openWorkspace(id, "host-login");
    workspaceMessage = opened
      ? "已打开 NAS 登录页。完成登录后，再打开连接。"
      : "已取消工作区切换。";
    return { opened };
  });
  ipcMain.handle("workspace:remove", async (event, id: string) => {
    trust(event); fnosHost.closeProfile(id); if (workspaces.active?.id === id) {
      workspaceGeneration++;
      await workspaces.close(); switching = true;
      try { mainWindow?.destroy(); mainWindow = null; } finally { switching = false; }
    }
    const profile = profiles.remove(id); await electronSession.fromPartition(profile.partition).clearStorageData(); return { removed: true };
  });
  ipcMain.handle("workspace:logout", async (event, id: string) => {
    trust(event); const profile = profiles.read().profiles.find(item => item.id === id && item.kind === "remote-web");
    if (!profile) throw new Error("连接不存在");
    const generation = ++workspaceGeneration;
    fnosHost.closeProfile(id);
    if (profile.deployment === "fnos" && workspaces.active?.id === id) {
      switching = true;
      try { mainWindow?.destroy(); mainWindow = null; } finally { switching = false; }
    }
    await electronSession.fromPartition(profile.partition).clearStorageData();
    if (quitting || generation !== workspaceGeneration) return { cleared: true, opened: false };
    if (profile.deployment === "fnos") {
      await electronSession.fromPartition(profile.partition).clearAuthCache();
      if (quitting || generation !== workspaceGeneration) return { cleared: true, opened: false };
      const opened = await openWorkspace(id, "host-login");
      workspaceMessage = opened
        ? "已清除登录状态并打开 NAS 登录页。完成登录后，再打开连接。"
        : "已清除此连接的登录状态；工作区切换已取消。";
      return { cleared: true, opened };
    }
    if (workspaces.active?.id === id) mainWindow?.reload(); return { cleared: true };
  });
  ipcMain.handle("workspace:check", async (event, id: string) => {
    trust(event); const profile = profiles.read().profiles.find(item => item.id === id && item.kind === "remote-web");
    if (!profile) throw new Error("连接不存在");
    return checkRemoteWorkspace(profile, AbortSignal.timeout(10_000));
  });
}

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
  if (workspaces.active?.kind !== "local-managed" || !trustedFrame(event, mainWindow?.webContents ?? null)) return false;
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

function installNavigationPolicy(window: BrowserWindow, origin = trustedOrigin, profile?: WorkspaceProfile,
  binding?: () => RemoteWindowBinding): void {
  const remote = profile?.kind === "remote-web";
  const boundary = remote ? new RemoteWebBoundary(origin, { window, profileId: profile.id,
    partition: profile.partition, session: window.webContents.session }) : undefined;
  const currentBinding = binding ?? (() => ({ window: mainWindow, profileId: workspaces.active?.id,
    partition: workspaces.active?.partition,
    session: mainWindow && !mainWindow.isDestroyed() ? mainWindow.webContents.session : undefined }));
  window.webContents.on("will-navigate", (event, url) => {
    try {
      const parsed = new URL(url);
      if (boundary && !boundary.isCurrent(currentBinding())) { event.preventDefault(); return; }
      if (boundary ? boundary.navigationAllowed(url, currentBinding()) : parsed.origin === origin) return;
      if (parsed.protocol === "https:" || parsed.protocol === "http:") {
        void shell.openExternal(parsed.toString());
      }
    } catch {
      // Fall through and block malformed URLs.
    }
    event.preventDefault();
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (boundary) {
      if (boundary.popupDisposition(currentBinding()) === "unsupported") {
        workspaceMessage = "此工作台的授权弹窗路径尚不支持。请在主窗口登录；依赖弹窗或授权回跳的 NAS 登录需要完成宿主协议验证。";
        showWorkspaceManager();
      }
      return { action: "deny" };
    }
    try {
      const parsed = new URL(url);
      if (parsed.origin === origin) {
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

function createFnOsHostWindow(profile: WorkspaceProfile): HostWindow {
  const window = new BrowserWindow({ width: 1200, height: 840, show: false,
    title: `SAG · ${profile.name} · NAS 宿主`, backgroundColor: "#09090b",
    webPreferences: { partition: profile.partition, nodeIntegration: false,
      contextIsolation: true, sandbox: true, webSecurity: true },
  });
  installNavigationPolicy(window, profile.webOrigin, profile, () => ({
    window: fnosHost.isCurrent(window, profile) ? window : null,
    profileId: workspaces.active?.id, partition: workspaces.active?.partition,
    session: window.isDestroyed() ? undefined : window.webContents.session,
  }));
  window.on("closed", () => { fnosHost.closed(window); });
  window.webContents.on("did-fail-load", (_event, code, _description, _url, mainFrame) => {
    if (!mainFrame || code === -3 || quitting || !fnosHost.isCurrent(window, profile)) return;
    workspaceMessage = "NAS 宿主页面无法连接。请检查网络、证书和连接地址。";
    showWorkspaceManager();
  });
  return {
    window, isDestroyed: () => window.isDestroyed(), show: () => window.show(), hide: () => window.hide(),
    destroy: () => window.destroy(),
    load: () => { void window.loadURL(`${profile.webOrigin}/`).catch(() => {
      if (quitting || !fnosHost.isCurrent(window, profile)) return;
      workspaceMessage = "NAS 宿主页面无法连接。请检查网络、证书和连接地址。";
      showWorkspaceManager();
    }); },
  };
}

function createMainWindow(webUrl: string, profile?: WorkspaceProfile): BrowserWindow {
  const remote = profile?.kind === "remote-web";
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
      preload: remote ? undefined : path.join(__dirname, "preload.js"),
      additionalArguments: !remote && runtime.session
        ? [`--sag-api-port=${new URL(runtime.session.apiUrl).port}`] : [],
      partition: remote ? profile.partition : undefined,
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
    },
  });
  installNavigationPolicy(window, new URL(webUrl).origin, profile);
  if (remote) {
    window.setTitle(`SAG · ${profile.name} · ${profile.webOrigin}`);
    window.webContents.on("did-fail-load", (_event, code, _description, _url, mainFrame) => {
      if (!mainFrame || code === -3 || switching || quitting) return;
      workspaceMessage = code === -202 || code === -200
        ? "服务器证书无效。请修复服务器证书后重试。"
        : code === -105 || code === -106 || code === -102
          ? "工作台连接不可用。请检查网络、NAS 服务和工作台地址。"
          : `工作台加载失败（${code}）。请检查地址，或重新登录。`;
      showWorkspaceManager();
    });
  }
  window.once("ready-to-show", () => {
    splashWindow?.close();
    splashWindow = null;
    window.show();
  });
  window.on("close", (event) => {
    if (process.platform !== "darwin" && !quitAllowed && !switching) {
      event.preventDefault();
      app.quit();
    }
  });
  window.on("closed", () => {
    if (mainWindow === window) mainWindow = null;
  });
  void window.loadURL(webUrl).catch(() => {});
  return window;
}

function bootstrap(): Promise<void> {
  if (booting) return booting;
  if (quitting) return Promise.resolve();
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
  if (!splashWindow || splashWindow.isDestroyed()) splashWindow = createSplashWindow();
  booting = (async () => {
    try {
      const activeId = profiles.read().activeId ?? "local";
      await openWorkspace(activeId);
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
    if (!mainWindow) { showWorkspaceManager(); return; }
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });

  app.whenReady().then(() => { registerWorkspaceIpc(); installWorkspaceMenu(); return bootstrap(); }).catch(showStartupError);

  app.on("activate", () => {
    if (mainWindow) {
      mainWindow.show();
      return;
    }
    if (managerWindow) { managerWindow.show(); return; }
    if (runtime.session && runtime.state.phase === "ready") {
      mainWindow = createMainWindow(runtime.session.webUrl);
      return;
    }
    if (!quitting && !failureDialog) void bootstrap();
  });

  nativeUpdater.on("before-quit-for-update", () => { updateQuitRequested = true; });
  app.on("before-quit", (event) => {
    log.info("Desktop quit request", { authorized: quitAllowed, windows: BrowserWindow.getAllWindows().length,
      runtime: runtime.state.phase });
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
      log.info("Desktop quit authorized");
      updater?.dispose();
      // Electron assigns its native quitting flag after before-quit observers return.
      // Re-entering from this promise's microtask can be overwritten by the first
      // prevented event, leaving a windowless macOS process. Finish that turn first.
      setImmediate(() => app.quit());
    }).catch((error) => {
      log.error("Desktop shutdown failed", error);
      dialog.showErrorBox("SAG 退出失败", "本地服务未能完成清理。请关闭 SAG 后重试；详情见日志。");
    });
  });
  app.on("will-quit", () => { log.info("Desktop will quit"); });

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin" && !switching) app.quit();
  });
}
