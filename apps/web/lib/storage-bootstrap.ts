import type { StorageBootstrapStatus } from "./types";

export type StorageBootstrapView =
  | { kind: "ready" }
  | { kind: "choice" }
  | { kind: "processing"; stage: string | null }
  | { kind: "failed"; recoverable: boolean; error: string | null };

export function viewForStorageBootstrap(
  status: StorageBootstrapStatus,
): StorageBootstrapView {
  switch (status.phase) {
    case "ready":
      return { kind: "ready" };
    case "choice_required":
      return { kind: "choice" };
    case "processing":
      return { kind: "processing", stage: status.stage };
    case "failed":
      return {
        kind: "failed",
        recoverable: status.recoverable,
        error: status.error,
      };
  }
}

export function createStorageBootstrapPoller(
  load: () => Promise<StorageBootstrapStatus>,
  onStatus: (status: StorageBootstrapStatus) => void,
  onError: (error: unknown) => void = () => {},
): () => void {
  let stopped = false;
  let loading = false;
  const interval = setInterval(async () => {
    if (stopped || loading) return;
    loading = true;
    try {
      const status = await load();
      if (stopped) return;
      onStatus(status);
      if (status.phase !== "processing") {
        stopped = true;
        clearInterval(interval);
      }
    } catch (error) {
      if (!stopped) onError(error);
    } finally {
      loading = false;
    }
  }, 1_000);

  return () => {
    stopped = true;
    clearInterval(interval);
  };
}
