import { rm } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

await rm(path.join(desktopRoot, "dist"), { recursive: true, force: true });
