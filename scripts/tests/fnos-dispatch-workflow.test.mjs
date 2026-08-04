import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
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
  assert.match(workflow, /uses: Zleap-AI\/SAG\/\.github\/workflows\/fnos-image-release\.yml@fnos\/develop/);
  assert.doesNotMatch(workflow, /resolve-candidate|Candidate Images|workflow_runs\?event=push/);
});

test("manual delivery packages only images built in its own run", async () => {
  const workflow = await readFile(workflowPath, "utf8");

  assert.match(workflow, /if: \$\{\{ github\.event_name == 'workflow_dispatch' \}\}/);
  assert.match(workflow, /CANDIDATE_RUN_ID: \$\{\{ github\.run_id \}\}/);
  assert.match(workflow, /API_DIGEST: \$\{\{ needs\.candidate\.outputs\.api_digest \}\}/);
  assert.match(workflow, /WEB_DIGEST: \$\{\{ needs\.candidate\.outputs\.web_digest \}\}/);
  assert.match(workflow, /GATEWAY_IMAGE: \$\{\{ needs\.candidate\.outputs\.gateway \}\}/);
  assert.match(workflow, /inputs\.mode == 'publish'/);
  assert.match(workflow, /publish_confirmation \}\}" = "PUBLISH"/);
  assert.doesNotMatch(workflow, /mkdir -p release/);
});
