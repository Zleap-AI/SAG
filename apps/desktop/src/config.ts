const DEFAULT_API_PORT = 8000;
const DEFAULT_WEB_PORT = 32100;

function readPort(name: string, fallback: number): number {
  const value = process.env[name];
  if (!value) return fallback;
  const parsed = Number.parseInt(value, 10);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new Error(`${name} must be an integer between 1 and 65535`);
  }
  return parsed;
}

export const desktopConfig = {
  apiHost: "127.0.0.1",
  apiPort: readPort("SAG_DESKTOP_API_PORT", DEFAULT_API_PORT),
  preferredWebPort: readPort("SAG_DESKTOP_WEB_PORT", DEFAULT_WEB_PORT),
  startupTimeoutMs: 45_000,
  updateCheckDelayMs: 30_000,
  updateCheckIntervalMs: 6 * 60 * 60 * 1000,
} as const;
