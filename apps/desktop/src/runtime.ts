import { randomBytes } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
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

interface RuntimeSecretFile {
  secretKey: string;
}

export interface ManagedRuntime {
  readonly webUrl: string;
  readonly apiUrl: string;
  stop(): void;
}

interface StartedProcess {
  stop(): void;
}

function isValidSecretFile(value: unknown): value is RuntimeSecretFile {
  if (!value || typeof value !== "object") return false;
  return typeof (value as RuntimeSecretFile).secretKey === "string"
    && (value as RuntimeSecretFile).secretKey.length >= 64;
}

function loadOrCreateSecret(userDataDir: string): string {
  const file = path.join(userDataDir, "desktop-runtime.json");
  if (existsSync(file)) {
    try {
      const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
      if (isValidSecretFile(parsed)) return parsed.secretKey;
    } catch (error) {
      log.warn("Ignoring invalid desktop runtime secret", error);
    }
  }
  const secretKey = randomBytes(48).toString("hex");
  writeFileSync(file, `${JSON.stringify({ secretKey }, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  return secretKey;
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

async function findAvailablePort(host: string, preferred: number): Promise<number> {
  for (let port = preferred; port < Math.min(65535, preferred + 100); port += 1) {
    if (await isPortAvailable(host, port)) return port;
  }
  throw new Error(`No available local port found near ${preferred}`);
}

async function waitForHttp(url: string, timeoutMs: number): Promise<void> {
  const startedAt = Date.now();
  let lastError: unknown;
  while (Date.now() - startedAt < timeoutMs) {
    try {
      const response = await fetch(url, { cache: "no-store" });
      if (response.ok) return;
      lastError = new Error(`${url} returned ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new Error(
    `Timed out waiting for ${url}: ${
      lastError instanceof Error ? lastError.message : String(lastError)
    }`,
  );
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
  return {
    stop: () => {
      child.kill();
    },
  };
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
): StartedProcess {
  const executable = backendExecutable(resourcesPath);
  if (!existsSync(executable)) {
    throw new Error(`Packaged Python backend not found: ${executable}`);
  }
  const secretKey = loadOrCreateSecret(userDataDir);
  const child = spawn(executable, [], {
    cwd: userDataDir,
    env: {
      ...process.env,
      PYTHONUNBUFFERED: "1",
      PYTHONDONTWRITEBYTECODE: "1",
      SAG_ENVIRONMENT: "prod",
      SAG_DEBUG: "false",
      SAG_SECRET_KEY: secretKey,
      SAG_CORS_ORIGINS: webOrigin,
      SAG_DESKTOP_HOST: desktopConfig.apiHost,
      SAG_DESKTOP_PORT: String(desktopConfig.apiPort),
    },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  pipeChildLogs(child, "api");
  return {
    stop: () => {
      child.kill();
    },
  };
}

export async function startPackagedRuntime(): Promise<ManagedRuntime> {
  const host = desktopConfig.apiHost;
  if (!(await isPortAvailable(host, desktopConfig.apiPort))) {
    throw new Error(
      `Local API port ${desktopConfig.apiPort} is already in use. `
      + "Close the conflicting service or configure SAG_DESKTOP_API_PORT.",
    );
  }
  const webPort = await findAvailablePort(host, desktopConfig.preferredWebPort);
  const webHealthUrl = `http://${host}:${webPort}`;
  // Next.js standalone normalizes redirects to localhost. Use that as the UI
  // origin while keeping the actual listener restricted to 127.0.0.1.
  const webUrl = `http://localhost:${webPort}`;
  const apiUrl = `http://${host}:${desktopConfig.apiPort}`;
  const webRoot = path.join(process.resourcesPath, "web");
  const userDataDir = app.getPath("userData");

  const processes: StartedProcess[] = [];
  try {
    processes.push(startNextRuntime(webRoot, webPort));
    processes.push(startPythonRuntime(process.resourcesPath, userDataDir, webUrl));
    await Promise.all([
      waitForHttp(webHealthUrl, desktopConfig.startupTimeoutMs),
      waitForHttp(
        `${apiUrl}/api/v1/system/ready`,
        desktopConfig.startupTimeoutMs,
      ),
    ]);
  } catch (error) {
    for (const processHandle of [...processes].reverse()) processHandle.stop();
    throw error;
  }

  return {
    webUrl,
    apiUrl,
    stop: () => {
      for (const processHandle of [...processes].reverse()) processHandle.stop();
    },
  };
}

export async function waitForDevelopmentRuntime(webUrl: string): Promise<ManagedRuntime> {
  await waitForHttp(webUrl, desktopConfig.startupTimeoutMs);
  return {
    webUrl,
    apiUrl: `http://${desktopConfig.apiHost}:${desktopConfig.apiPort}`,
    stop: () => {},
  };
}
