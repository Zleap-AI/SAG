import type { EventEmitter } from "node:events";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { setTimeout as delay } from "node:timers/promises";

interface ProcessOptions {
  name: string;
  hasPid(): boolean;
  requestStop(): void | Promise<void>;
  forceStop(): void | Promise<void>;
  afterExit?(): void | Promise<void>;
  graceMs?: number;
  forceMs?: number;
}

/** Only pass a group ID for a process launched with detached: true. */
export async function terminateProcessTree(pid: number, processGroup: boolean): Promise<void> {
  if (process.platform === "win32") {
    await promisify(execFile)("taskkill", ["/pid", String(pid), "/t", "/f"], { windowsHide: true, timeout: 5_000 });
  } else {
    try { process.kill(processGroup ? -pid : pid, "SIGKILL"); } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error;
    }
  }
}

async function completedWithin(promise: Promise<void>, milliseconds: number): Promise<boolean> {
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      promise.then(() => true),
      new Promise<false>((resolve) => { timer = setTimeout(() => resolve(false), milliseconds); }),
    ]);
  } finally { clearTimeout(timer); }
}

/** Exit observation is installed immediately, before any asynchronous startup work. */
export function manageProcess(child: EventEmitter, options: ProcessOptions) {
  let exited = false;
  let markExited!: () => void;
  let fail!: (error: Error) => void;
  const exit = new Promise<void>((resolve) => { markExited = resolve; });
  const failure = new Promise<Error>((resolve) => { fail = resolve; });
  child.once("exit", (code: number | null, signal?: string) => {
    exited = true;
    markExited();
    fail(new Error(`${options.name} 服务已退出 (${signal ?? code ?? "unknown"})`));
  });
  child.on("error", (error: unknown) => {
    fail(new Error(`${options.name} 服务异常: ${error instanceof Error ? error.message : String(error)}`));
    // Node spawn failures have no exit event; Electron fatal errors do.
    if (!options.hasPid()) { exited = true; markExited(); }
  });
  let stopping: Promise<void> | undefined;
  return {
    failure,
    stop(): Promise<void> {
      if (stopping) return stopping;
      stopping = (async () => {
        if (exited) { await options.afterExit?.(); return; }
        const requested = Promise.resolve().then(options.requestStop);
        // Failed graceful requests fall back to termination, without an unhandled rejection.
        const graceful = Promise.race([exit.then(() => true), requested.then(() => exit.then(() => true), () => false)]);
        let timer: NodeJS.Timeout | undefined;
        let finished: boolean;
        try {
          finished = await Promise.race([graceful, new Promise<false>((resolve) => {
            timer = setTimeout(() => resolve(false), options.graceMs ?? 20_000);
          })]);
        } finally { clearTimeout(timer); }
        if (!finished && !exited) {
          try { await options.forceStop(); } catch (error) {
            // taskkill can lose a race with normal Windows process exit.
            if (!await completedWithin(exit, options.forceMs ?? 5_000)) throw error;
          }
          if (!await completedWithin(exit, options.forceMs ?? 5_000)) {
            throw new Error(`${options.name} 服务未能退出，请关闭 SAG 后重试`);
          }
        }
        await options.afterExit?.();
      })().catch((error: unknown) => { stopping = undefined; throw error; });
      return stopping;
    },
  };
}

export async function waitForHttp(url: string, timeoutMs: number, signal: AbortSignal): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown;
  while (Date.now() < deadline) {
    signal.throwIfAborted();
    try {
      const response = await fetch(url, {
        cache: "no-store",
        signal: AbortSignal.any([signal, AbortSignal.timeout(Math.max(1, Math.min(1_000, deadline - Date.now())))]),
      });
      await response.body?.cancel();
      if (response.ok) return;
      lastError = new Error(`HTTP ${response.status}`);
    } catch (error) { lastError = error; }
    signal.throwIfAborted();
    if (Date.now() < deadline) await delay(Math.min(150, deadline - Date.now()), undefined, { signal });
  }
  throw new Error(`Timed out waiting for ${url}: ${lastError instanceof Error ? lastError.message : String(lastError)}`);
}
