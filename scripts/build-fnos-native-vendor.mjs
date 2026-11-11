#!/usr/bin/env node
import { appendFile, lstat, mkdir, mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const api = path.join(root, "apps/api");
const platforms = { "linux/amd64": "x86_64-unknown-linux-gnu", "linux/arm64": "aarch64-unknown-linux-gnu" };
const fail = (message) => { throw new Error(`fnos-native-vendor: ${message}`); };
export function vendorDependencyPlan(platform) {
  return platform === "linux/amd64"
    ? { excluded: ["lancedb", "lance-namespace", "lance-namespace-urllib3-client"],
        installFlags: ["--no-deps", "--require-hashes"], keepHashes: true }
    : { excluded: [], installFlags: [], keepHashes: false };
}
function command(bin, args, options) { const result = spawnSync(bin, args, { encoding: "utf8", ...options }); if (result.status !== 0) fail(`${bin}: ${(result.stderr || result.stdout).trim()}`); }
async function files(dir, base = dir) { const out = []; for (const entry of await readdir(dir, { withFileTypes: true })) { const child = path.join(dir, entry.name); if (entry.isDirectory()) out.push(...await files(child, base)); else if (entry.isFile()) out.push(path.relative(base, child)); else fail(`unsupported entry ${child}`); } return out; }
async function pruneDevelopmentFiles(dir) { for (const entry of await readdir(dir, { withFileTypes: true })) { const child = path.join(dir, entry.name); if (entry.isDirectory()) { if (["test", "tests", "__pycache__"].includes(entry.name)) await rm(child, { recursive: true, force: true }); else await pruneDevelopmentFiles(child); } else if (entry.isFile() && entry.name.endsWith(".pyc")) await rm(child, { force: true }); } }
async function main() {
  const [flag, platform, outFlag, output] = process.argv.slice(2);
  if (flag !== "--platform" || outFlag !== "--output" || !platforms[platform] || !output) fail("usage: --platform linux/amd64|linux/arm64 --output <empty-dir>");
  const target = path.resolve(output);
  try { const entries = await readdir(target); if (entries.length) fail("output must be empty"); } catch (error) { if (error.code === "ENOENT") await mkdir(target, { recursive: true }); else throw error; }
  if ((await lstat(target)).isSymbolicLink()) fail("output must not be a symlink");
  const temp = await mkdtemp(path.join(os.tmpdir(), "sag-native-vendor-"));
  try {
    const requirements = path.join(temp, "requirements.txt");
    const plan = vendorDependencyPlan(platform);
    command("uv", ["export", "--frozen", "--no-dev", ...(!plan.keepHashes ? ["--no-hashes"] : []),
      "--no-emit-project", ...plan.excluded.flatMap((name) => ["--no-emit-package", name]),
      "--output-file", requirements], { cwd: api });
    if (plan.keepHashes) await appendFile(requirements, `\n${await readFile(path.join(root, "scripts/fnos-x86-vendor-requirements.txt"), "utf8")}`);
    command("uv", ["pip", "install", "--target", target, "--python-version", "3.12", "--python-platform", platforms[platform],
      "--only-binary", ":all:", ...plan.installFlags, "-r", requirements]);
    if (plan.keepHashes) command(process.env.PYTHON ?? "python3", [path.join(root, "scripts/validate-fnos-native-vendor.py"),
      "--vendor", target, "--platform", platform]);
    await pruneDevelopmentFiles(target);
    const entries = await files(target);
    if (entries.some((entry) => /(^|\/)(__pycache__|tests?)(\/|$)|\.pyc$|\.(dll|dylib)$/i.test(entry))) fail("vendor contains forbidden development or host files");
    const manifest = await Promise.all(entries.sort().map(async (entry) => { const content = await readFile(path.join(target, entry)); return { path: entry, bytes: content.length, sha256: createHash("sha256").update(content).digest("hex") }; }));
    await writeFile(path.join(target, "vendor-manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);
  } finally { await rm(temp, { recursive: true, force: true }); }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; });
}
