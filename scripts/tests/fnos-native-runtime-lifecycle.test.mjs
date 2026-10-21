import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { cp, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { validateNativeTemplate } from "../validate-fnos-native-package.mjs";

const root = fileURLToPath(new URL("../..", import.meta.url));
const fixture = path.join(root, "scripts/tests/fixtures/fnos-native-runtime-lifecycle.py");

// Approved public seams: fnpack's config_init/main callbacks. The fixture
// owns real child processes and UDS listeners, never installed SAG paths.
function check(name, { linux = false } = {}) {
  const image = process.env.SAG_NATIVE_LIFECYCLE_TEST_IMAGE;
  const container = linux && process.platform !== "linux" && image;
  test(name, { skip: linux && process.platform !== "linux" && !container }, () => {
    const executable = container ? "docker" : "python3";
    const args = container
      ? ["run", "--rm", "--network", "none", "-v", `${root}:/repo:ro`, image,
        "python3", `/repo/scripts/tests/fixtures/${path.basename(fixture)}`, "/repo", name]
      : [fixture, root, name];
    const result = spawnSync(executable, args, { encoding: "utf8", timeout: 90000 });
    assert.equal(result.status, 0, result.stdout + result.stderr + (result.error ?? ""));
  });
}

check("config_preserves_running_services");
check("config_preserves_stopped_state");
for (const [name, file, mutate, error] of [
  ["configuration cleanup", "config_init", () => '#!/bin/sh\nexec "$(dirname "$0")/install_init"\n', /config_init must preserve/],
]) {
  test(`package validator rejects ${name}`, async (t) => {
    const directory = await mkdtemp(path.join(os.tmpdir(), "sag-lifecycle-package-"));
    t.after(() => rm(directory, { recursive: true, force: true }));
    await cp(path.join(root, "packages/fnos/native/sag"), directory, { recursive: true });
    const manifest = path.join(directory, "manifest");
    await writeFile(manifest, (await readFile(manifest, "utf8"))
      .replace("__SAG_VERSION__", "1.6.0-fnos").replace("__SAG_PLATFORM__", "x86"));
    await validateNativeTemplate(directory, "x86");
    const target = path.join(directory, "cmd", file);
    await writeFile(target, mutate(await readFile(target, "utf8")));
    await assert.rejects(validateNativeTemplate(directory, "x86"), error);
  });
}
