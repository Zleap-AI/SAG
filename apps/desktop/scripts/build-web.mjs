import { spawn } from "node:child_process";
import { rm } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const webRoot = path.resolve(desktopRoot, "../web");
const apiPort = process.env.SAG_DESKTOP_API_PORT || "8000";

await rm(path.join(webRoot, ".next"), { recursive: true, force: true });

const child = spawn("npm", ["run", "build"], {
  cwd: webRoot,
  env: {
    ...process.env,
    NEXT_PUBLIC_API_BASE: `http://localhost:${apiPort}`,
    NEXT_PUBLIC_ENABLE_WINDOW_SCALING: "false",
  },
  stdio: "inherit",
  shell: process.platform === "win32",
});

const exitCode = await new Promise((resolve) => {
  child.once("exit", (code) => resolve(code ?? 1));
});

if (exitCode !== 0) process.exit(exitCode);
