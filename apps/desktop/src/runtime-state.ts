import { randomBytes } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

interface RuntimeStateFile {
  secretKey: string;
  webPort?: number;
}

type PortAvailability = (port: number) => Promise<boolean>;

function runtimeStateFile(userDataDir: string): string {
  return path.join(userDataDir, "desktop-runtime.json");
}

function isValidRuntimeState(value: unknown): value is RuntimeStateFile {
  if (!value || typeof value !== "object") return false;
  return typeof (value as RuntimeStateFile).secretKey === "string"
    && (value as RuntimeStateFile).secretKey.length >= 64;
}

function readRuntimeState(userDataDir: string): RuntimeStateFile | null {
  const file = runtimeStateFile(userDataDir);
  if (!existsSync(file)) return null;
  try {
    const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
    return isValidRuntimeState(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function writeRuntimeState(userDataDir: string, state: RuntimeStateFile): void {
  writeFileSync(
    runtimeStateFile(userDataDir),
    `${JSON.stringify(state, null, 2)}\n`,
    { encoding: "utf8", mode: 0o600 },
  );
}

function isValidPort(value: unknown): value is number {
  return typeof value === "number"
    && Number.isInteger(value)
    && value >= 1
    && value <= 65535;
}

export function loadOrCreateRuntimeSecret(userDataDir: string): string {
  const existing = readRuntimeState(userDataDir);
  if (existing) return existing.secretKey;
  const secretKey = randomBytes(48).toString("hex");
  writeRuntimeState(userDataDir, { secretKey });
  return secretKey;
}

export async function resolveStableWebPort(
  userDataDir: string,
  preferredPort: number,
  isAvailable: PortAvailability,
): Promise<number> {
  const existing = readRuntimeState(userDataDir);
  if (isValidPort(existing?.webPort) && (await isAvailable(existing.webPort))) {
    return existing.webPort;
  }

  for (
    let port = preferredPort;
    port < Math.min(65535, preferredPort + 100);
    port += 1
  ) {
    if (!(await isAvailable(port))) continue;
    const secretKey = existing?.secretKey ?? loadOrCreateRuntimeSecret(userDataDir);
    writeRuntimeState(userDataDir, { secretKey, webPort: port });
    return port;
  }
  throw new Error(`No available local port found near ${preferredPort}`);
}
