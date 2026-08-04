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
  assert.doesNotMatch(workflow, /\n      version:\n        description:/);
  assert.match(workflow, /mode:/);
  assert.match(workflow, /publish_confirmation:/);
  assert.match(workflow, /ref: fnos\/develop/);
  assert.match(workflow, /git rev-parse HEAD.*git rev-parse origin\/fnos\/develop/);
  assert.match(workflow, /publish_confirmation \}\}" = "PUBLISH"/);
  assert.doesNotMatch(workflow, /on:\n  push:/);
  assert.doesNotMatch(workflow, /\.github\/workflows\/ci\.yml/);
  assert.doesNotMatch(workflow, /candidate_tag|CANDIDATE_TAG|fnos-candidate-/);
  assert.match(workflow, /event=push&head_sha=\$\{CANDIDATE_REVISION\}/);
  assert.match(workflow, /\.head_branch == "fnos\/develop" and \.conclusion == "success"/);
});

test("fnOS release never publishes a dry-run package and only creates public assets after explicit confirmation", async () => {
  const workflow = await readFile(workflowPath, "utf8");

  assert.match(workflow, /^permissions:\n  contents: read$/m);
  assert.match(workflow, /resolve-candidate:/);
  assert.match(workflow, /resolve-candidate:[\s\S]*?permissions:\n      actions: read\n      contents: read/);
  assert.match(workflow, /build-package:/);
  assert.match(workflow, /publish-release:/);
  assert.match(workflow, /if: \$\{\{ inputs\.mode == 'publish' \}\}/);
  assert.match(workflow, /permissions:\n      contents: write/);
  assert.match(workflow, /gh release create/);
  assert.match(workflow, /fnpack-1\.2\.3-linux-amd64/);
  assert.match(workflow, /sha256sum --check --strict/);
  assert.match(workflow, /release-fnos\.mjs prepare/);
  assert.match(workflow, /release-fnos\.mjs package/);
  assert.match(workflow, /fnos-release-manifest\.mjs validate/);
  assert.doesNotMatch(workflow, /upload-artifact/);
});

test("fnOS package output is created only by the package command", async () => {
  const workflow = await readFile(workflowPath, "utf8");

  assert.doesNotMatch(workflow, /mkdir -p release/);
  assert.match(workflow, /--output release-input\.json/);
  assert.match(workflow, /candidate-evidence\.json/);
  assert.match(workflow, /--output release/);
});
