import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("../..", import.meta.url));
const workflowPath = path.join(repoRoot, ".github/workflows/fnos-release.yml");

test("fnOS release entry is manual, isolated, and checks out fnos/develop", async () => {
  const workflow = await readFile(workflowPath, "utf8");
  assert.match(workflow, /^name: fnOS Release$/m);
  assert.match(workflow, /workflow_dispatch:/);
  assert.doesNotMatch(workflow, /\n      version:/);
  assert.match(workflow, /mode:/);
  assert.match(workflow, /publish_confirmation:/);
  assert.match(workflow, /ref: fnos\/develop/);
  assert.match(workflow, /git rev-parse HEAD.*git rev-parse origin\/fnos\/develop/);
  assert.match(workflow, /publish_confirmation \}\}" = "PUBLISH"/);
  assert.doesNotMatch(workflow, /on:\n  push:/);
  assert.doesNotMatch(workflow, /\.github\/workflows\/ci\.yml/);
});
