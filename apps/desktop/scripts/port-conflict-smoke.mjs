import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const directory = mkdtempSync(path.join(tmpdir(), "sag-port-conflict-"));
try {
  const child = spawn(createRequire(import.meta.url)("electron"), [
    fileURLToPath(new URL("./port-conflict-smoke.cjs", import.meta.url)),
  ], { stdio: "inherit", env: { ...process.env, SAG_PORT_SMOKE_DIR: directory } });
  process.exitCode = await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", code => resolve(code ?? 1));
  });
} finally {
  // Windows keeps Chromium profile files locked until Electron has exited.
  rmSync(directory, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
}
