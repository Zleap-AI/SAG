import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { createRequire } from "node:module";
import * as fs from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import test from "node:test";

// Execute the compiled real main/updater startup; replace Electron and network boundaries only.
for (const scenario of ["fresh", "unset", "nas"] as const) test(`${scenario} startup restores its workspace without opening connection settings`, async t => {
  const directory = fs.mkdtempSync(path.join(tmpdir(), "sag-main-startup-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const output = path.join(directory, "compiled");
  execFileSync(process.execPath, [fileURLToPath(new URL("../node_modules/typescript/bin/tsc", import.meta.url)),
    "--project", fileURLToPath(new URL("../tsconfig.json", import.meta.url)), "--outDir", output]);
  const loadCompiled = createRequire(path.join(output, "main.js"));
  const { WorkspaceProfiles } = await import("../src/workspace-profiles.ts");
  const profiles = new WorkspaceProfiles(directory);
  if (scenario !== "fresh") {
    const nas = profiles.save({ name: "Saved NAS", webUrl: "https://nas.example/app/sag", deployment: "fnos" });
    profiles.activate(nas.id);
    if (scenario === "unset") {
      const file = path.join(directory, "workspace-profiles.json");
      const state = JSON.parse(fs.readFileSync(file, "utf8")); state.activeId = null;
      fs.writeFileSync(file, JSON.stringify(state));
    }
  }
  const handlers = new Map<string, unknown>(); const scheduled: Array<() => void> = [];
  const windows: Array<{ file: string; url: string }> = []; let checks = 0; let localStarts = 0;
  let menu: Array<{ label?: string; submenu?: Array<{ label?: string; click?: () => void }> }> = [];
  class Window extends EventEmitter {
    file = "";
    url = "";
    webContents = { on() {}, setWindowOpenHandler() {}, send() {} };
    constructor() { super(); windows.push(this); }
    async loadFile(file: string) { this.file = file; }
    async loadURL(url: string) { this.url = url; }
    isDestroyed() { return false; }
    close() {}
    show() {}
    setTitle() {}
  }
  const app = Object.assign(new EventEmitter(), {
    isPackaged: true, requestSingleInstanceLock: () => true, whenReady: async () => {},
    getPath: () => directory, getAppPath: () => directory, getVersion: () => "1.9.1", quit() {},
  });
  const nativeUpdater = new EventEmitter();
  const autoUpdater = Object.assign(new EventEmitter(), { checkForUpdates: async () => { checks++; } });
  const logger = { initialize() {}, info() {}, error() {}, transports: { file: { maxSize: 0 } } };
  const electron = { app, BrowserWindow: Window, autoUpdater: nativeUpdater,
    ipcMain: { handle: (name: string, handler: unknown) => handlers.set(name, handler), removeHandler: (name: string) => handlers.delete(name) },
    Menu: { setApplicationMenu() {}, buildFromTemplate: (items: typeof menu) => { menu = items; return []; } }, dialog: {}, shell: {}, session: {} };
  const timer = (callback: () => void) => { scheduled.push(callback); return { unref() {} }; };
  const updaterExports: Record<string, unknown> = {};
  runInNewContext(fs.readFileSync(path.join(output, "updater.js"), "utf8"), {
    exports: updaterExports, process: { resourcesPath: "/controlled-resources", platform: process.platform },
    setTimeout: timer, setInterval: timer, clearTimeout() {}, clearInterval() {},
    require: (id: string) => id === "electron" ? electron : id === "electron-updater" ? { autoUpdater }
      : id === "electron-log/main" ? logger : id === "node:fs" ? { existsSync: () => true } : loadCompiled(id),
  });
  runInNewContext(fs.readFileSync(path.join(output, "main.js"), "utf8"), {
    exports: {}, process, __dirname: output, URL,
    require: (id: string) => id === "electron" ? electron : id === "electron-log/main" ? logger
      : id === "./updater" ? updaterExports : id === "./runtime" ? {
        startPackagedRuntime() { localStarts++; return Promise.resolve({ webUrl: "http://127.0.0.1:3000",
          apiUrl: "http://127.0.0.1:8000", failure: new Promise(() => {}), stop: async () => {}, inspectActivity: async () => false }); },
      } : id === "./fnos-host-context" ? { FnOsHostContext: class {
        ready = async () => true; close() {} hide() {}
      } } : loadCompiled(id),
  });
  for (let attempt = 0; attempt < 30 && !windows.some(window => window.url); attempt++) {
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.equal(windows.some(window => window.file.endsWith("workspaces.html")), false);
  assert.equal(windows.find(window => window.url)?.url, scenario === "nas"
    ? "https://nas.example/app/sag/chat" : "http://127.0.0.1:3000");
  assert.equal(localStarts, scenario === "nas" ? 0 : 1);
  assert.equal(scheduled.length, 1, "normal startup retains updater discovery");
  assert.ok(handlers.has("desktop:get-update-state"));
  scheduled[0](); await Promise.resolve();
  assert.equal(checks, 1);
  menu.find(item => item.label === "工作区")?.submenu?.[0].click?.();
  assert.ok(windows.some(window => window.file.endsWith("workspaces.html")), "connection settings remain an optional menu action");
});
