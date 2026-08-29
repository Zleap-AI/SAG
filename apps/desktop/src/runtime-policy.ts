export type StorageBootstrapPolicy = "prompt" | "windows_fresh";

/** Format the local HTTP origin used by the desktop API and its consumers. */
export function localHttpOrigin(host: string, port: number): string {
  const authority = host.includes(":") && !host.startsWith("[")
    ? `[${host}]`
    : host;
  return `http://${authority}:${port}`;
}

/** Build backend environment values from the desktop runtime's selected API address. */
export function desktopApiEnvironment(
  host: string,
  port: number,
): Record<
  "SAG_DESKTOP_HOST" | "SAG_DESKTOP_PORT" | "SAG_DSH_PUBLIC_URL",
  string
> {
  return {
    SAG_DESKTOP_HOST: host,
    SAG_DESKTOP_PORT: String(port),
    SAG_DSH_PUBLIC_URL: localHttpOrigin(host, port),
  };
}

export function storageBootstrapPolicy(
  platform: NodeJS.Platform,
): StorageBootstrapPolicy {
  return platform === "win32" ? "windows_fresh" : "prompt";
}
