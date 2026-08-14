import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  assertCurrentFnOSVersion,
  isCurrentFnOSVersion,
  resolveNextFnOSVersion,
} from "../fnos-version.mjs";

const root = fileURLToPath(new URL("../..", import.meta.url));
const cli = path.join(root, "scripts/fnos-version.mjs");

test("current fnOS versions use one fixed fnos suffix", () => {
  assert.equal(isCurrentFnOSVersion("1.5.4-fnos"), true);
  assert.equal(isCurrentFnOSVersion("1.5.4-fnos.1"), false);
  assert.equal(isCurrentFnOSVersion("1.5.4"), false);
  assert.throws(
    () => assertCurrentFnOSVersion("1.5.4-fnos.1"),
    /1\.5\.4-fnos\.1.*x\.y\.z-fnos/,
  );
});

test("next fnOS release increments the highest fnOS patch", () => {
  assert.equal(
    resolveNextFnOSVersion(
      ["fnos-v1.5.3-fnos", "fnos-v1.5.2-fnos", "v9.0.0", "other"],
      "2.0.0",
    ),
    "1.5.4-fnos",
  );
});

test("first new-format release migrates from the highest legacy fnOS base", () => {
  assert.equal(
    resolveNextFnOSVersion(
      ["fnos-v1.5.3-fnos.2", "fnos-v1.5.3-fnos.11", "fnos-v1.4.9-fnos.99"],
      "2.0.0",
    ),
    "1.5.4-fnos",
  );
});

test("version ordering compares numeric components instead of strings", () => {
  assert.equal(
    resolveNextFnOSVersion(["fnos-v1.9.9-fnos", "fnos-v1.10.0-fnos"], "1.0.0"),
    "1.10.1-fnos",
  );
});

test("public semver is used only to bootstrap an empty fnOS release line", () => {
  assert.equal(resolveNextFnOSVersion(["v8.0.0", "malformed"], "1.5.3"), "1.5.3-fnos");
  assert.throws(() => resolveNextFnOSVersion([], "1.5-fnos"), /fallback base.*x\.y\.z/);
});

test("CLI resolves release tags from stdin", () => {
  const result = spawnSync(process.execPath, [cli, "--fallback-base", "2.0.0"], {
    cwd: root,
    encoding: "utf8",
    input: "fnos-v1.5.2-fnos\nfnos-v1.5.3-fnos.7\nnoise\n",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout.trim(), "1.5.4-fnos");
});
