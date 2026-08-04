import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("../..", import.meta.url));
const workflowPath = path.join(repoRoot, ".github/workflows/fnos-image-release.yml");

test("fnOS internal image workflow is callable only and returns immutable evidence", async () => {
  const workflow = await readFile(workflowPath, "utf8");

  assert.match(workflow, /^name: fnOS Delivery internals$/m);
  assert.match(workflow, /workflow_call:/);
  assert.match(workflow, /revision:\n        required: true\n        type: string/);
  assert.match(workflow, /api_digest:\n        value: \$\{\{ jobs\.inspect-staging\.outputs\.api_digest \}\}/);
  assert.match(workflow, /web_digest:\n        value: \$\{\{ jobs\.inspect-staging\.outputs\.web_digest \}\}/);
  assert.match(workflow, /gateway:\n        value: \$\{\{ jobs\.gateway-security\.outputs\.gateway \}\}/);
  assert.doesNotMatch(workflow, /\n  push:|fnos-candidate-/);
});

test("fnOS internal image workflow validates the requested fnos revision and caches multiarch builds", async () => {
  const workflow = await readFile(workflowPath, "utf8");

  assert.match(workflow, /ref: \$\{\{ inputs\.revision \}\}/);
  assert.match(workflow, /test "\$\{\{ inputs\.revision \}\}" = "\$\(git rev-parse HEAD\)"/);
  assert.match(workflow, /cache-from: type=gha,scope=\$\{\{ matrix\.cache_scope \}\}/);
  assert.match(workflow, /cache-to: type=gha,mode=max,scope=\$\{\{ matrix\.cache_scope \}\}/);
  assert.doesNotMatch(workflow, /local-amd64-smoke/);
  assert.match(workflow, /smoke-fnos-release-images\.mjs smoke/);
});
