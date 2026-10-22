// Native Electron/loopback acceptance. Only child service implementations are fixtures;
// runtime selection, health checks, shutdown, preload and Web API resolution are real.
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { createServer } = require("node:http");
const { createRequire, stripTypeScriptTypes } = require("node:module");
const { mkdirSync, readFileSync, writeFileSync } = require("node:fs");
const path = require("node:path");
const { PassThrough } = require("node:stream");
const { runInNewContext } = require("node:vm");
const { app, BrowserWindow } = require("electron");

const directory = process.env.SAG_PORT_SMOKE_DIR;
if (!directory) throw new Error("Run this acceptance via npm run test:port-conflict");
app.setPath("userData", directory);
const desktopRoot = path.resolve(__dirname, "..");
const output = path.join(desktopRoot, "dist");
const loadCompiled = createRequire(path.join(output, "runtime.js"));
const { isPortAvailable } = loadCompiled("./runtime-ports");
const resources = path.join(directory, "resources");
mkdirSync(path.join(resources, "web"), { recursive: true });
mkdirSync(path.join(resources, "backend", "sag-api"), { recursive: true });
writeFileSync(path.join(resources, "web", "server.js"), "fixture");
writeFileSync(path.join(resources, "backend", "sag-api", process.platform === "win32" ? "sag-api.exe" : "sag-api"), "fixture");
const resolver = stripTypeScriptTypes(readFileSync(path.join(desktopRoot, "../web/lib/api-base.ts"), "utf8"))
  .replace("export function resolveApiBase", "function resolveApiBase");
const servers = new Set();
const windows = new Set();
const controls = [];
let apiEnvironment;

function listen(server, port = 0) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", () => { servers.add(server); resolve(server.address().port); });
  });
}
async function close(server) {
  if (!server.listening) return;
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  servers.delete(server);
}
function child(server) {
  const process = Object.assign(new EventEmitter(), { stdout: new PassThrough(), stderr: new PassThrough() });
  process.kill = () => { void close(server).then(() => process.emit("exit", 0)); };
  return process;
}
function startWeb(_entry, _args, options) {
  const server = createServer((_req, res) => {
    res.setHeader("Content-Type", "text/html");
    res.end(`<html><body><h1>Port conflict acceptance</h1><script>${resolver}\nwindow.resolveApiBase = resolveApiBase;</script></body></html>`);
  });
  const process = child(server);
  void listen(server, Number(options.env.PORT)).catch(error => process.emit("error", error));
  return process;
}
function startApi(_executable, _args, options) {
  apiEnvironment = options.env;
  const server = createServer((req, res) => {
    res.setHeader("Access-Control-Allow-Origin", options.env.SAG_CORS_ORIGINS);
    res.setHeader("Content-Type", "application/json");
    if (req.url.startsWith("/_desktop/")) {
      assert.equal(req.headers["x-sag-desktop-token"], options.env.SAG_DESKTOP_CONTROL_TOKEN);
      assert.equal(req.method, "POST");
      controls.push(req.url);
    }
    res.end(JSON.stringify({ active: false, marker: "selected-api" }));
    if (req.url === "/_desktop/shutdown") res.on("finish", () => process.kill());
  });
  const process = child(server);
  void listen(server, Number(options.env.SAG_DESKTOP_PORT)).catch(error => process.emit("error", error));
  return process;
}

async function run() {
  await app.whenReady();
  // Do not disturb an existing 8000 owner. On clean CI this fixture owns it.
  const occupied = createServer((_req, res) => { res.statusCode = 503; res.end("foreign service"); });
  try { await listen(occupied, 8000); } catch (error) {
    if (error.code !== "EADDRINUSE") throw error;
  }
  assert.equal(await isPortAvailable("127.0.0.1", 8000), false);
  const candidate = createServer();
  const webPort = await listen(candidate);
  await close(candidate);
  const exports = {};
  runInNewContext(readFileSync(path.join(output, "runtime.js"), "utf8"), {
    exports, __dirname: output, AbortController, AbortSignal, fetch, URL,
    process: { platform: process.platform, env: process.env, resourcesPath: resources },
    require: id => id === "electron" ? {
      app: { getPath: () => directory }, utilityProcess: { fork: startWeb },
    } : id === "electron-log/main" ? { info() {}, error() {} }
      : id === "node:child_process" ? { spawn: startApi }
      : id === "./config" ? { desktopConfig: {
        apiHost: "127.0.0.1", apiPort: 8000, preferredWebPort: webPort, startupTimeoutMs: 10_000,
      } } : loadCompiled(id),
  });
  let previousWebUrl;
  for (let attempt = 0; attempt < 2; attempt++) {
    const runtime = await exports.startPackagedRuntime(new AbortController().signal);
    assert.notEqual(new URL(runtime.apiUrl).port, "8000");
    assert.equal(runtime.apiUrl, apiEnvironment.SAG_DSH_PUBLIC_URL);
    assert.equal(apiEnvironment.SAG_DESKTOP_HOST, "127.0.0.1");
    assert.equal(apiEnvironment.SAG_CORS_ORIGINS, runtime.webUrl);
    if (previousWebUrl) assert.equal(runtime.webUrl, previousWebUrl, "restart preserves browser origin");
    previousWebUrl = runtime.webUrl;
    const window = new BrowserWindow({ show: false, webPreferences: {
      preload: path.join(output, "preload.js"), sandbox: true, contextIsolation: true,
      nodeIntegration: false, additionalArguments: [`--sag-api-port=${new URL(runtime.apiUrl).port}`],
    } });
    windows.add(window);
    await window.loadURL(runtime.webUrl);
    const result = await window.webContents.executeJavaScript(`(async () => {
      const base = window.resolveApiBase('http://localhost:8000');
      const response = await fetch(base + '/api/v1/auth/status');
      return { base, status: response.status, data: await response.json() };
    })()`);
    assert.equal(result.base, runtime.apiUrl);
    assert.equal(result.status, 200);
    assert.equal(result.data.marker, "selected-api");
    assert.equal(await runtime.inspectActivity(), false);
    window.destroy(); windows.delete(window);
    await runtime.stop();
    assert.equal(await isPortAvailable("127.0.0.1", Number(new URL(runtime.apiUrl).port)), true);
    assert.equal(await isPortAvailable("127.0.0.1", 8000), false, "foreign owner remains untouched");
  }
  assert.deepEqual(controls, ["/_desktop/activity", "/_desktop/shutdown", "/_desktop/activity", "/_desktop/shutdown"]);
  console.log(`PASS ${process.platform}: occupied 8000, renderer API, activity/shutdown, restart origin, foreign owner preserved`);
}

app.on("window-all-closed", () => {});
const timeout = setTimeout(() => { console.error("Port conflict smoke timed out"); app.exit(1); }, 60_000);
run().then(() => cleanup(0), error => { console.error(error); return cleanup(1); });
async function cleanup(code) {
  clearTimeout(timeout);
  for (const window of windows) window.destroy();
  await Promise.all([...servers].map(close));
  app.exit(code);
}
