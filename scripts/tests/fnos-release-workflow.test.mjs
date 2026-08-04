import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("../..", import.meta.url));
const workflowPath = path.join(repoRoot, ".github/workflows/fnos-release.yml");

test("one fnOS Delivery workflow contains the candidate build and immutable evidence", async () => {
  const workflow = await readFile(workflowPath, "utf8");

  assert.match(workflow, /^name: fnOS Delivery$/m);
  assert.match(workflow, /candidate:|quality:|gateway-security:|staging:|inspect-staging:|smoke-staging:|promote:|anonymous-postcheck:/);
  assert.match(workflow, /uses: Zleap-AI\/SAG\/\.github\/workflows\/ci\.yml@fnos\/develop/);
  assert.doesNotMatch(workflow, /workflow_call:|fnos-image-release\.yml|fnos-candidate-/);
});

test("fnOS Delivery validates the dedicated branch and caches parallel multiarch builds", async () => {
  const workflow = await readFile(workflowPath, "utf8");

  assert.match(workflow, /ref: fnos\/develop/);
  assert.match(workflow, /git rev-parse HEAD.*git rev-parse origin\/fnos\/develop/);
  assert.match(workflow, /cache-from: type=gha,scope=\$\{\{ matrix\.cache_scope \}\}/);
  assert.match(workflow, /cache-to: type=gha,mode=max,scope=\$\{\{ matrix\.cache_scope \}\}/);
  assert.match(workflow, /platforms: linux\/amd64,linux\/arm64/);
  assert.match(workflow, /gateway-security:\n[\s\S]*?needs: verify-release-request/);
  assert.match(workflow, /staging:\n[\s\S]*?needs: verify-release-request/);
  assert.match(workflow, /promote:\n[\s\S]*?needs: \[verify-release-request, quality, gateway-security, inspect-staging, smoke-staging\]/);
  assert.doesNotMatch(workflow, /local-amd64-smoke/);
  assert.match(workflow, /smoke-fnos-release-images\.mjs smoke/);
});
