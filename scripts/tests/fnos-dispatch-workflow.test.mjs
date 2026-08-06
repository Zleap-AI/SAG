import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../..", import.meta.url));
test("fnOS Delivery is the sole user-facing fnOS workflow", async () => {
  const workflow = await readFile(path.join(root, ".github/workflows/fnos-release.yml"), "utf8");
  assert.match(workflow, /workflow_dispatch:/);
  // The workflow is now guarded so it can only run from fnos/develop.
  assert.match(workflow, /refs\/heads\/fnos\/develop/);
  assert.deepEqual((await readdir(path.join(root, ".github/workflows"))).filter((name) => name.startsWith("fnos-")), ["fnos-release.yml"]);
});
