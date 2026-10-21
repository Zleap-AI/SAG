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
test("fresh first-run manager initializes updater scheduling and IPC before a workspace is selected", async t => {
  const directory = fs.mkdtempSync(path.join(tmpdir(), "sag-main-startup-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const output = path.join(directory, "compiled");
  execFileSync(process.execPath, [fileURLToPath(new URL("../node_modules/typescript/bin/tsc", import.meta.url)),
    "--project", fileURLToPath(new URL("../tsconfig.json", import.meta.url)), "--outDir", output]);
  const loadCompiled = createRequire(path.join(output, "main.js"));
  const handlers = new Map<string, unknown>(); const scheduled: Array<() => void> = [];
  const windows: Array<{ file: string }> = []; let checks = 0;
  class Window extends EventEmitter {
    file = "";
    webContents = { on() {}, setWindowOpenHandler() {}, send() {} };
    constructor() { super(); windows.push(this); }
    async loadFile(file: string) { this.file = file; }
    isDestroyed() { return false; }
    close() {}
    show() {}
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
    Menu: { setApplicationMenu() {}, buildFromTemplate: () => [] }, dialog: {}, shell: {}, session: {} };
  const timer = (callback: () => void) => { scheduled.push(callback); return { unref() {} }; };
  const updaterExports: Record<string, unknown> = {};
  runInNewContext(fs.readFileSync(path.join(output, "updater.js"), "utf8"), {
    exports: updaterExports, process: { resourcesPath: "/controlled-resources", platform: process.platform },
    setTimeout: timer, setInterval: timer, clearTimeout() {}, clearInterval() {},
    require: (id: string) => id === "electron" ? electron : id === "electron-updater" ? { autoUpdater }
      : id === "electron-log/main" ? logger : id === "node:fs" ? { existsSync: () => true } : loadCompiled(id),
  });
  runInNewContext(fs.readFileSync(path.join(output, "main.js"), "utf8"), {
    exports: {}, process, __dirname: output,
    require: (id: string) => id === "electron" ? electron : id === "electron-log/main" ? logger
      : id === "./updater" ? updaterExports : id === "./runtime" ? {
        startPackagedRuntime() { throw new Error("No sidecar before workspace choice"); },
      } : loadCompiled(id),
  });
  for (let attempt = 0; attempt < 30 && !windows.some(window => window.file.endsWith("workspaces.html")); attempt++) {
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.ok(windows.some(window => window.file.endsWith("workspaces.html")));
  assert.equal(scheduled.length, 1, "the real updater schedules discovery during first-run management");
  assert.ok(handlers.has("desktop:get-update-state"));
  scheduled[0](); await Promise.resolve();
  assert.equal(checks, 1);
});
