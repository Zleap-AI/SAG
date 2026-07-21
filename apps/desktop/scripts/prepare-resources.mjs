import {
  access,
  cp,
  mkdir,
  readFile,
  rename,
  rm,
  writeFile,
} from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = path.resolve(desktopRoot, "../..");
const webRoot = path.join(repoRoot, "apps", "web");
const resourcesRoot = path.join(desktopRoot, "build", "resources");
const webTarget = path.join(resourcesRoot, "web");
const backendTarget = path.join(resourcesRoot, "backend");
const standaloneSource = path.join(webRoot, ".next", "standalone");
const staticSource = path.join(webRoot, ".next", "static");
const publicSource = path.join(webRoot, "public");
const backendSource =
  process.env.SAG_PYTHON_DIST_DIR
  || path.join(repoRoot, "apps", "api", "dist", "desktop", "sag-api");

async function exists(value) {
  try {
    await access(value);
    return true;
  } catch {
    return false;
  }
}

if (!(await exists(path.join(standaloneSource, "server.js")))) {
  throw new Error(
    "Next.js standalone output is missing. Run npm run build:web first.",
  );
}

if (!(await exists(path.join(backendSource, process.platform === "win32" ? "sag-api.exe" : "sag-api")))) {
  throw new Error(
    `Python onedir output is missing at ${backendSource}. `
    + "Run npm run build:backend first or set SAG_PYTHON_DIST_DIR.",
  );
}

await rm(resourcesRoot, { recursive: true, force: true });
await mkdir(resourcesRoot, { recursive: true });
await cp(standaloneSource, webTarget, { recursive: true });
const standaloneModules = path.join(webTarget, "node_modules");
const packagedModules = path.join(webTarget, "runtime_modules");
if (!(await exists(standaloneModules))) {
  throw new Error("Next.js standalone output does not contain node_modules.");
}
// electron-builder excludes every directory named node_modules, including
// extraResources. Rename the standalone dependency directory and expose it
// through NODE_PATH when Electron starts the local web runtime.
await rename(standaloneModules, packagedModules);
await mkdir(path.join(webTarget, ".next"), { recursive: true });
await cp(staticSource, path.join(webTarget, ".next", "static"), { recursive: true });
if (await exists(publicSource)) {
  await cp(publicSource, path.join(webTarget, "public"), { recursive: true });
}
await mkdir(backendTarget, { recursive: true });
await cp(backendSource, path.join(backendTarget, "sag-api"), { recursive: true });

const desktopPackage = JSON.parse(
  await readFile(path.join(desktopRoot, "package.json"), "utf8"),
);
const webPackage = JSON.parse(
  await readFile(path.join(webRoot, "package.json"), "utf8"),
);
const backendInit = await readFile(
  path.join(repoRoot, "apps", "api", "sag_api", "__init__.py"),
  "utf8",
);
const backendVersion = /^__version__ = "(\d+\.\d+\.\d+)"$/m.exec(backendInit)?.[1];
if (!backendVersion) {
  throw new Error("Backend runtime version is missing from apps/api/sag_api/__init__.py.");
}
await writeFile(
  path.join(resourcesRoot, "runtime-manifest.json"),
  `${JSON.stringify(
    {
      desktopVersion: desktopPackage.version,
      webVersion: webPackage.version,
      backendVersion,
      apiPort: Number(process.env.SAG_DESKTOP_API_PORT || 8000),
      generatedAt: new Date().toISOString(),
    },
    null,
    2,
  )}\n`,
  "utf8",
);

console.log(`Prepared desktop resources in ${resourcesRoot}`);
