import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("../..", import.meta.url));
const script = path.join(repoRoot, "scripts/release-fnos.mjs");
const checkedOutBranch = spawnSync("git", ["branch", "--show-current"], { cwd: repoRoot, encoding: "utf8" }).stdout.trim();

test("prepare records a global release input for the checked-out fnOS manifest", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "sag-fnos-release-prepare-"));
  t.after(async () => rm(root, { recursive: true, force: true }));
  const output = path.join(root, "release-input.json");
  const result = spawnSync(process.execPath, [
    script,
    "prepare",
    "--version", "1.5.0-fnos.1",
    "--channel", "global",
    "--candidate-run-id", "30798626087",
    "--output", output,
  ], {
    cwd: repoRoot,
    encoding: "utf8",
    env: { ...process.env, FNOS_RELEASE_BRANCH: checkedOutBranch },
  });

  assert.equal(result.status, 0, result.stderr);
  const releaseInput = JSON.parse(await (await import("node:fs/promises")).readFile(output, "utf8"));
  assert.equal(releaseInput.candidate_workflow.url, "https://github.com/Zleap-AI/SAG/actions/runs/30798626087");
  assert.match(releaseInput.build_id, /^\d{8}\.\d{6}Z$/);
  assert.match(releaseInput.built_at_utc, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  assert.equal(releaseInput.source_branch, checkedOutBranch);
});

test("prepare rejects a release request with an invalid channel", () => {
  const result = spawnSync(process.execPath, [
    script,
    "prepare",
    "--version", "1.5.0-fnos.1",
    "--channel", "mirror.example",
    "--candidate-run-id", "30798626087",
    "--output", path.join(os.tmpdir(), "sag-fnos-invalid-release-input.json"),
  ], { cwd: repoRoot, encoding: "utf8" });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /channel.*global.*cn/i);
});

test("package rejects candidate evidence that is not pinned to approved registries", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "sag-fnos-release-package-"));
  t.after(async () => rm(root, { recursive: true, force: true }));
  const input = path.join(root, "release-input.json");
  const evidence = path.join(root, "candidate-evidence.json");
  await writeFile(input, JSON.stringify({
    schema_version: 1,
    appname: "sag",
    version: "1.5.0-fnos.1",
    channel: "global",
    revision: "a".repeat(40),
    candidate_tag: "fnos-candidate-1.5.0-fnos.1-aaaaaaaaaaaa",
    candidate_workflow: { run_id: "30798626087", url: "https://github.com/Zleap-AI/SAG/actions/runs/30798626087" },
  }));
  await writeFile(evidence, JSON.stringify({
    api: `registry.example/sag-api@sha256:${"b".repeat(64)}`,
    web: `ghcr.1ms.run/zleap-ai/sag-web@sha256:${"c".repeat(64)}`,
    gateway: `ghcr.1ms.run/zleap-ai/sag-gateway:1.4.0-fnos.8@sha256:${"d".repeat(64)}`,
  }));
  const result = spawnSync(process.execPath, [
    script, "package", "--input", input, "--candidate-evidence", evidence,
    "--output", path.join(root, "out"),
  ], { cwd: repoRoot, encoding: "utf8" });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /approved|global.*api/i);
});
