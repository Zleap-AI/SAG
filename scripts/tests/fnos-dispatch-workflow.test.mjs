import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("../..", import.meta.url));
const workflowPath = path.join(repoRoot, ".github/workflows/fnos-release.yml");

test("fnOS Delivery is the only user-facing push and manual entry", async () => {
  const workflow = await readFile(workflowPath, "utf8");

  assert.match(workflow, /^name: fnOS Delivery$/m);
  assert.match(workflow, /push:\n    branches: \[fnos\/develop\]/);
  assert.match(workflow, /workflow_dispatch:/);
  assert.doesNotMatch(workflow, /uses: .*fnos-image-release\.yml|resolve-candidate|Candidate Images|workflow_runs\?event=push/);
  assert.match(workflow, /name: Build digest-only multi-platform images/);
  assert.doesNotMatch(workflow, /staging-fnos-|COMMIT_TAG|commit-tag|sha-\$\{\{/);
});

test("fnOS delivery is implemented by one workflow file", async () => {
  const workflows = await readdir(path.join(repoRoot, ".github/workflows"));

  assert.deepEqual(workflows.filter((name) => name.startsWith("fnos-")), ["fnos-release.yml"]);
});

test("manual delivery packages only images built in its own run", async () => {
  const workflow = await readFile(workflowPath, "utf8");

  assert.match(workflow, /build-package:\n[\s\S]*?if: \$\{\{ github\.event_name == 'workflow_dispatch' && inputs\.mode == 'candidate' \}\}/);
  assert.match(workflow, /CANDIDATE_RUN_ID: \$\{\{ github\.run_id \}\}/);
  assert.match(workflow, /API_DIGEST: \$\{\{ needs\.inspect-images\.outputs\.api_digest \}\}/);
  assert.match(workflow, /WEB_DIGEST: \$\{\{ needs\.inspect-images\.outputs\.web_digest \}\}/);
  assert.match(workflow, /GATEWAY_IMAGE: \$\{\{ needs\.gateway-security\.outputs\.gateway \}\}/);
  assert.match(workflow, /inputs\.mode == 'publish'/);
  assert.match(workflow, /inputs\.mode == 'candidate'/);
  assert.match(workflow, /name: Upload one-day Candidate FPK/);
  assert.match(workflow, /retention-days: 1/);
  assert.match(workflow, /publish_confirmation \}\}" = "PUBLISH"/);
  assert.doesNotMatch(workflow, /mkdir -p release/);
});
