import { existsSync } from "node:fs";
import { randomBytes } from "node:crypto";
import { createServer } from "node:net";
import path from "node:path";
import {
  spawn,
  type ChildProcessByStdio,
} from "node:child_process";
import type { Readable } from "node:stream";

import { app, utilityProcess, type UtilityProcess } from "electron";
import log from "electron-log/main";

import { desktopConfig } from "./config";
import { manageProcess, terminateProcessTree, waitForHttp } from "./managed-process";
import { RuntimeStartError } from "./runtime-controller";
import {
  loadOrCreateRuntimeSecret,
  resolveStableWebPort,
} from "./runtime-state";
import {
  desktopApiEnvironment,
  localHttpOrigin,
  storageBootstrapPolicy,
} from "./runtime-policy";

export interface ManagedRuntime {
  readonly webUrl: string;
  readonly apiUrl: string;
  readonly failure: Promise<Error>;
  inspectActivity(): Promise<boolean>;
  stop(): Promise<void>;
}

interface StartedProcess {
  readonly failure: Promise<Error>;
  stop(): Promise<void>;
}

async function isPortAvailable(host: string, port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const server = createServer();
    server.unref();
    server.once("error", () => resolve(false));
    server.listen({ host, port, exclusive: true }, () => {
      server.close(() => resolve(true));
    });
  });
}

function pipeUtilityLogs(child: UtilityProcess, name: string): void {
  child.stdout?.on("data", (chunk) => log.info(`[${name}] ${String(chunk).trimEnd()}`));
  child.stderr?.on("data", (chunk) => log.error(`[${name}] ${String(chunk).trimEnd()}`));
  child.on("exit", (code) => log.info(`${name} exited with code ${code}`));
}

function startNextRuntime(webRoot: string, port: number): StartedProcess {
  const serverEntry = path.join(webRoot, "server.js");
  if (!existsSync(serverEntry)) {
    throw new Error(`Packaged Next.js server not found: ${serverEntry}`);
  }
  const bootstrapEntry = path.join(__dirname, "web-runtime.js");
  const child = utilityProcess.fork(bootstrapEntry, [], {
    cwd: webRoot,
    env: {
      ...process.env,
      NODE_ENV: "production",
      HOSTNAME: desktopConfig.apiHost,
      NODE_PATH: path.join(webRoot, "runtime_modules"),
      PORT: String(port),
      SAG_WEB_ROOT: webRoot,
    },
    stdio: "pipe",
    serviceName: "SAG Web Runtime",
  });
  pipeUtilityLogs(child, "web");
  return manageProcess(child, {
    name: "Web", hasPid: () => child.pid !== undefined,
    requestStop: () => { child.kill(); },
    forceStop: async () => {
      if (child.pid) await terminateProcessTree(child.pid, false);
    },
    graceMs: 5_000,
  });
}

function pipeChildLogs(
  child: ChildProcessByStdio<null, Readable, Readable>,
  name: string,
): void {
  child.stdout.on("data", (chunk) => log.info(`[${name}] ${String(chunk).trimEnd()}`));
  child.stderr.on("data", (chunk) => log.error(`[${name}] ${String(chunk).trimEnd()}`));
  child.on("exit", (code, signal) => {
    log.info(`${name} exited`, { code, signal });
  });
}

function backendExecutable(resourcesPath: string): string {
  const filename = process.platform === "win32" ? "sag-api.exe" : "sag-api";
  return path.join(resourcesPath, "backend", "sag-api", filename);
}

function startPythonRuntime(
  resourcesPath: string,
  userDataDir: string,
  webOrigin: string,
  controlToken: string,
): StartedProcess {
  const executable = backendExecutable(resourcesPath);
  if (!existsSync(executable)) {
    throw new Error(`Packaged Python backend not found: ${executable}`);
  }
  const secretKey = loadOrCreateRuntimeSecret(userDataDir);
  const child = spawn(executable, [], {
    cwd: userDataDir,
    env: {
      ...process.env,
      PYTHONUNBUFFERED: "1",
      PYTHONDONTWRITEBYTECODE: "1",
      SAG_ENVIRONMENT: "prod",
      SAG_DEBUG: "false",
      SAG_SECRET_KEY: secretKey,
      SAG_DESKTOP_CONTROL_TOKEN: controlToken,
      SAG_CORS_ORIGINS: webOrigin,
      ...desktopApiEnvironment(desktopConfig.apiHost, desktopConfig.apiPort),
      SAG_STORAGE_BOOTSTRAP_POLICY: storageBootstrapPolicy(process.platform),
    },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
    detached: process.platform !== "win32",
  });
  pipeChildLogs(child, "api");
  return manageProcess(child, {
    name: "API", hasPid: () => child.pid !== undefined,
    requestStop: async () => {
      await controlRequest(localHttpOrigin(desktopConfig.apiHost, desktopConfig.apiPort), controlToken, "shutdown");
    },
    forceStop: async () => {
      if (child.pid) await terminateProcessTree(child.pid, true);
    },
    // An exited API can still leave OCTX workers in its POSIX process group.
    // Windows workers are tied to the API lifetime by desktop_process.py.
    afterExit: async () => {
      if (child.pid && process.platform !== "win32") await terminateProcessTree(child.pid, true);
    },
  });
}

async function controlRequest(apiUrl: string, token: string, action: "activity" | "shutdown"): Promise<{ active?: boolean }> {
  const response = await fetch(`${apiUrl}/_desktop/${action}`, {
    method: "POST", headers: { "x-sag-desktop-token": token }, signal: AbortSignal.timeout(2_000),
  });
  if (!response.ok) { await response.body?.cancel(); throw new Error(`Desktop ${action} unavailable`); }
  return await response.json() as { active?: boolean };
}

export async function startPackagedRuntime(signal: AbortSignal): Promise<ManagedRuntime> {
  const host = desktopConfig.apiHost;
  if (!(await isPortAvailable(host, desktopConfig.apiPort))) {
    throw new Error(
      `Local API port ${desktopConfig.apiPort} is already in use. `
      + "Close the conflicting service or configure SAG_DESKTOP_API_PORT.",
    );
  }
  const userDataDir = app.getPath("userData");
  const webPort = await resolveStableWebPort(
    userDataDir,
    desktopConfig.preferredWebPort,
    (port) => isPortAvailable(host, port),
  );
  log.info(`Resolved stable web port ${webPort} (origin http://localhost:${webPort})`);
  const webHealthUrl = localHttpOrigin(host, webPort);
  // Next.js standalone normalizes redirects to localhost. Use that as the UI
  // origin while keeping the actual listener restricted to 127.0.0.1.
  const webUrl = `http://localhost:${webPort}`;
  const apiUrl = localHttpOrigin(host, desktopConfig.apiPort);
  const webRoot = path.join(process.resourcesPath, "web");

  signal.throwIfAborted();
  const controlToken = randomBytes(32).toString("hex");
  const processes: StartedProcess[] = [];
  let stopping: Promise<void> | undefined;
  const stop = (): Promise<void> => stopping ??= (async () => {
    const results = await Promise.allSettled([...processes].reverse().map((child) => child.stop()));
    const errors = results.filter((result) => result.status === "rejected").map((result) => result.reason);
    if (errors.length) throw new AggregateError(errors, "本地服务清理失败，请退出 SAG 后重试");
  })().catch((error: unknown) => { stopping = undefined; throw error; });
  try {
    processes.push(startNextRuntime(webRoot, webPort));
    processes.push(startPythonRuntime(process.resourcesPath, userDataDir, webUrl, controlToken));
    const failure = Promise.race(processes.map((child) => child.failure));
    const probing = new AbortController();
    try {
      await Promise.race([
        failure.then((error) => { throw error; }),
        Promise.all([
          waitForHttp(webHealthUrl, desktopConfig.startupTimeoutMs, AbortSignal.any([signal, probing.signal])),
          waitForHttp(
            `${apiUrl}/api/v1/system/health`,
            desktopConfig.startupTimeoutMs,
            AbortSignal.any([signal, probing.signal]),
          ),
        ]),
      ]);
      signal.throwIfAborted();
    } finally { probing.abort(); }
    return {
      webUrl, apiUrl, failure, stop,
      inspectActivity: async () => {
        const result = await controlRequest(apiUrl, controlToken, "activity");
        if (typeof result.active !== "boolean") throw new Error("Invalid activity response");
        return result.active;
      },
    };
  } catch (error) {
    try { await stop(); } catch (cleanupError) {
      throw new RuntimeStartError([error, cleanupError], stop);
    }
    throw error;
  }
}

export async function waitForDevelopmentRuntime(webUrl: string, signal: AbortSignal): Promise<ManagedRuntime> {
  await waitForHttp(webUrl, desktopConfig.startupTimeoutMs, signal);
  return {
    webUrl,
    apiUrl: localHttpOrigin(desktopConfig.apiHost, desktopConfig.apiPort),
    failure: new Promise<Error>(() => {}),
    inspectActivity: async () => false,
    stop: async () => {},
  };
}
