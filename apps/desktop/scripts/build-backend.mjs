import { access } from "node:fs/promises";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const apiRoot = path.resolve(desktopRoot, "../api");
const pyinstallerConfig = path.join(apiRoot, "build", "pyinstaller", ".config");
const python =
  process.env.SAG_DESKTOP_PYTHON
  || path.join(
    apiRoot,
    ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );

try {
  await access(python);
} catch {
  throw new Error(
    `Python virtual environment not found at ${python}. `
    + "Set SAG_DESKTOP_PYTHON or create apps/api/.venv first.",
  );
}

const probe = spawn(python, ["-c", "import PyInstaller"], {
  cwd: apiRoot,
  stdio: "ignore",
});
const probeCode = await new Promise((resolve) => {
  probe.once("exit", (code) => resolve(code ?? 1));
});
if (probeCode !== 0) {
  throw new Error(
    "PyInstaller is not installed in apps/api/.venv. "
    + "Install the API desktop extra before building a release.",
  );
}

const child = spawn(
  python,
  [
    "-m",
    "PyInstaller",
    "--clean",
    "--noconfirm",
    "--distpath",
    path.join(apiRoot, "dist", "desktop"),
    "--workpath",
    path.join(apiRoot, "build", "pyinstaller"),
    path.join(apiRoot, "packaging", "sag-api.spec"),
  ],
  {
    cwd: apiRoot,
    env: {
      ...process.env,
      PYINSTALLER_CONFIG_DIR: pyinstallerConfig,
    },
    stdio: "inherit",
  },
);

const exitCode = await new Promise((resolve) => {
  child.once("exit", (code) => resolve(code ?? 1));
});

if (exitCode !== 0) process.exit(exitCode);
