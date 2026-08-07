import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../..", import.meta.url));
test("fnOS delivery builds only the Native x86 FPK", async () => {
  const workflow = await readFile(path.join(root, ".github/workflows/fnos-release.yml"), "utf8");
  assert.match(workflow, /runs-on: ubuntu-24\.04/);
  assert.match(workflow, /--platform linux\/amd64/);
  assert.match(workflow, /--platform x86/);
  // No PUBLISH typo-trap: workflow_dispatch no longer takes any human
  // input; the branch guard (fnos/develop only) is the sole gate.
  assert.doesNotMatch(workflow, /publish_confirmation/);
  assert.doesNotMatch(workflow, /docker\/|docker build|build-push-action|ghcr\.io/i);
});
