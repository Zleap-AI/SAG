#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { chmod, cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { validateNativeTemplate } from "./validate-fnos-native-package.mjs";

const repoRoot = fileURLToPath(new URL("..", import.meta.url));
const template = path.join(repoRoot, "packages/fnos/native/sag");
const apiRoot = path.join(repoRoot, "apps/api");
const probe = process.env.SAG_FNOS_NATIVE_PROBE_SOURCE ?? path.join(repoRoot, "scripts/fnos-native-probe.py");
const platforms = {
  x86: "x86_64-unknown-linux-gnu",
  arm: "aarch64-unknown-linux-gnu",
};

function fail(message) {
  throw new Error(`fnos-native-probe: ${message}`);
}

function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument !== "--platform" && argument !== "--output" && argument !== "--version") fail(`unknown argument: ${argument}`);
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) fail(`${argument} is required`);
    result[argument.slice(2)] = value;
    index += 1;
  }
  if (!platforms[result.platform]) fail("--platform must be x86 or arm");
  if (!result.output?.endsWith(".fpk")) fail("--output must use the .fpk suffix");
  if (result.version && !/^[0-9]+\.[0-9]+\.[0-9]+-fnos\.[0-9]+$/.test(result.version)) fail("--version must match <major>.<minor>.<patch>-fnos.<number>");
  return result;
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { encoding: "utf8", ...options });
  if (result.error) fail(`could not run ${command}: ${result.error.message}`);
  if (result.status !== 0) fail(`${command} failed: ${(result.stderr || result.stdout).trim()}`);
}

async function probeVersion() {
  const manifest = await readFile(path.join(repoRoot, "packages/fnos/sag/manifest"), "utf8");
  const version = /^version\s*=\s*(\S+)\s*$/m.exec(manifest)?.[1];
  if (!version) fail("could not determine package version");
  return version;
}

function mainScript() {
  return `#!/bin/bash
set -eu
pid_file="$TRIM_PKGVAR/native-p0.pid"
socket="$TRIM_APPDEST/app.sock"
runtime="/var/apps/python312/target/bin/python3"
server="$TRIM_APPDEST/server"
vendor="$server/vendor"
log="$TRIM_PKGVAR/native-p0.log"
case "\${1:-}" in
  start)
    mkdir -p "$TRIM_PKGVAR"
    if ! test -x "$runtime" || ! test -d "$server" || ! test -d "$vendor"; then
      echo "native probe prerequisites unavailable" >> "$log"
      exit 1
    fi
    PYTHONPATH="$vendor:$server\${PYTHONPATH:+:$PYTHONPATH}" "$runtime" "$server/fnos-native-probe.py" serve --socket "$TRIM_APPDEST/app.sock" --output "$TRIM_PKGVAR/native-p0.json" >> "$log" 2>&1 &
    echo $! > "$pid_file"
    ;;
  status)
    test -f "$pid_file"
    pid="$(cat "$pid_file")"
    kill -0 "$pid"
    test -S "$socket"
    ;;
  stop)
    if test -f "$pid_file"; then
      pid="$(cat "$pid_file")"
      if kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid"
        waited=0
        while kill -0 "$pid" 2>/dev/null && test "$waited" -lt 15; do
          sleep 1
          waited=$((waited + 1))
        done
        if kill -0 "$pid" 2>/dev/null; then kill -KILL "$pid"; fi
      fi
    fi
    rm -f "$pid_file" "$socket"
    ;;
  *)
    exit 1
    ;;
esac
`;
}

async function build(options) {
  const temporaryRoot = await mkdtemp(path.join(os.tmpdir(), "sag-native-probe-"));
  try {
    const rendered = path.join(temporaryRoot, "sag");
    await cp(template, rendered, { recursive: true });
    const server = path.join(rendered, "app", "server");
    await mkdir(path.join(server, "vendor"), { recursive: true });
    const requirements = path.join(temporaryRoot, "requirements.txt");
    run("uv", ["export", "--locked", "--no-dev", "--no-emit-project", "--no-hashes", "--output-file", requirements], { cwd: apiRoot });
    run("uv", ["pip", "install", "--python-platform", platforms[options.platform], "--python-version", "3.12", "--only-binary", ":all:", "--target", path.join(server, "vendor"), "-r", requirements]);
    await cp(probe, path.join(server, "fnos-native-probe.py"));
    await mkdir(path.join(rendered, "cmd"), { recursive: true });
    const main = path.join(rendered, "cmd/main");
    await writeFile(main, mainScript());
    await chmod(main, 0o755);
    const manifestPath = path.join(rendered, "manifest");
    const manifest = await readFile(manifestPath, "utf8");
    const version = options.version ?? await probeVersion();
    await writeFile(manifestPath, manifest.replaceAll("__SAG_VERSION__", version).replaceAll("__SAG_PLATFORM__", options.platform));
    run("fnpack", ["build"], { cwd: rendered });
    await validateNativeTemplate(rendered, options.platform);
    await mkdir(path.dirname(options.output), { recursive: true });
    await cp(path.join(rendered, "sag.fpk"), options.output, { force: true });
  } finally {
    await rm(temporaryRoot, { recursive: true, force: true });
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  await build({ ...options, output: path.resolve(options.output) });
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
});
