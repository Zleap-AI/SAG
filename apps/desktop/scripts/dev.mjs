import { createRequire } from "node:module";
import { spawn, spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const webRoot = path.resolve(desktopRoot, "../web");
const apiRoot = path.resolve(desktopRoot, "../api");
const apiUrl = "http://127.0.0.1:8000/api/v1/system/ready";
const webUrl = "http://127.0.0.1:3000";
const children = [];
let stopping = false;

async function reachable(url) {
  try {
    const response = await fetch(url, { cache: "no-store" });
    return response.ok;
  } catch {
    return false;
  }
}

async function waitFor(url, timeoutMs = 60_000) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    if (await reachable(url)) return;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`Timed out waiting for ${url}`);
}

function start(name, command, args, options) {
  const child = spawn(command, args, {
    ...options,
    stdio: "inherit",
    shell: process.platform === "win32",
    detached: process.platform !== "win32",
  });
  children.push({ name, child });
  child.once("exit", (code) => {
    if (!stopping && code && code !== 0) {
      console.error(`${name} exited with code ${code}`);
      stopAll(code);
    }
  });
  return child;
}

function stopChild(child) {
  if (!child.pid || child.killed) return;
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/pid", String(child.pid), "/t", "/f"], {
      stdio: "ignore",
    });
    return;
  }
  try {
    process.kill(-child.pid, "SIGTERM");
  } catch {
    child.kill("SIGTERM");
  }
}

function stopAll(exitCode = 0) {
  if (stopping) return;
  stopping = true;
  for (const { child } of children.reverse()) stopChild(child);
  process.exit(exitCode);
}

process.once("SIGINT", () => stopAll(0));
process.once("SIGTERM", () => stopAll(0));

if (!(await reachable(apiUrl))) {
  const python = path.join(
    apiRoot,
    ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
  start(
    "API",
    python,
    [
      "-m",
      "uvicorn",
      "sag_api.main:app",
      "--host",
      "127.0.0.1",
      "--port",
      "8000",
    ],
    { cwd: apiRoot, env: { ...process.env, SAG_ENVIRONMENT: "dev" } },
  );
}

if (!(await reachable(webUrl))) {
  start("Web", "npm", ["run", "dev"], {
    cwd: webRoot,
    env: {
      ...process.env,
      NEXT_PUBLIC_API_BASE: "http://127.0.0.1:8000",
      NEXT_PUBLIC_ENABLE_WINDOW_SCALING: "false",
    },
  });
}

await Promise.all([waitFor(apiUrl), waitFor(webUrl)]);

const electronPath = require("electron");
const electron = start("Electron", electronPath, [desktopRoot], {
  cwd: desktopRoot,
  env: {
    ...process.env,
    SAG_DESKTOP_DEV_WEB_URL: webUrl,
  },
});
electron.once("exit", (code) => stopAll(code ?? 0));
