import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../..", import.meta.url));
test("fnOS delivery is a single manual publish flow guarded by explicit confirmation", async () => {
  const workflow = await readFile(path.join(root, ".github/workflows/fnos-release.yml"), "utf8");
  assert.match(workflow, /^name: fnOS Delivery$/m);
  // Manual-only trigger: no push branch, only workflow_dispatch.
  assert.doesNotMatch(workflow, /^\s*push:/m);
  assert.match(workflow, /workflow_dispatch:/);
  // Version is a required input, not read from the stale Docker manifest.
  assert.match(workflow, /version:/);
  assert.match(workflow, /inputs\.version/);
  assert.doesNotMatch(workflow, /packages\/fnos\/sag\/manifest/);
  // Guardrails: only from fnos/develop and only after PUBLISH confirmation.
  assert.match(workflow, /refs\/heads\/fnos\/develop/);
  assert.match(workflow, /inputs\.publish_confirmation.*PUBLISH/);
  // The build environment must disable window scaling for fnOS.
  assert.match(workflow, /NEXT_PUBLIC_ENABLE_WINDOW_SCALING=0/);
  // Tests and packaging still run.
  assert.match(workflow, /uv run --extra dev pytest -q/);
  assert.match(workflow, /node scripts\/build-fnos-native-package\.mjs/);
  assert.match(workflow, /sha256sum/);
  // Release tag encodes the version explicitly.
  assert.match(workflow, /fnos-v\$SAG_VERSION/);
});
