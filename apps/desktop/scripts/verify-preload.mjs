import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
for (const filename of ["preload.js", "workspace-preload.js"]) {
const preloadPath = path.join(desktopRoot, "dist", filename);
const source = await readFile(preloadPath, "utf8");

if (/require\(["']\.[./]/.test(source)) {
  throw new Error(
    "Sandboxed Electron preload must be self-contained; relative require() was found",
  );
}

}
