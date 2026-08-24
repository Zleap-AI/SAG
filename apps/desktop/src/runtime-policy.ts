export type StorageBootstrapPolicy = "prompt" | "windows_fresh";

export function storageBootstrapPolicy(
  platform: NodeJS.Platform,
): StorageBootstrapPolicy {
  return platform === "win32" ? "windows_fresh" : "prompt";
}
