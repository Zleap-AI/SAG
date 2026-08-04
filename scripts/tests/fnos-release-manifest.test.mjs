import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("../..", import.meta.url));
const script = path.join(repoRoot, "scripts/fnos-release-manifest.mjs");
const revision = "a".repeat(40);

function validManifest() {
  return {
    schema_version: 1,
    appname: "sag",
    version: "1.5.0-fnos.1",
    channel: "global",
    revision,
    build_id: "20260804.112701Z",
    built_at_utc: "2026-08-04T11:27:01Z",
    source_branch: "fnos/develop",
    candidate_tag: "fnos-candidate-1.5.0-fnos.1-aaaaaaaaaaaa",
    candidate_workflow: {
      run_id: "30798626087",
      url: "https://github.com/Zleap-AI/SAG/actions/runs/30798626087",
    },
    images: {
      api: `ghcr.1ms.run/zleap-ai/sag-api@sha256:${"b".repeat(64)}`,
      web: `ghcr.1ms.run/zleap-ai/sag-web@sha256:${"c".repeat(64)}`,
      gateway: `ghcr.1ms.run/zleap-ai/sag-gateway:1.5.0-fnos.1@sha256:${"d".repeat(64)}`,
    },
    fpk: { filename: "sag-1.5.0-fnos.1.fpk", sha256: "e".repeat(64) },
  };
}

async function withManifest(t, manifest) {
  const root = await mkdtemp(path.join(os.tmpdir(), "sag-fnos-release-manifest-"));
  t.after(async () => rm(root, { recursive: true, force: true }));
  const input = path.join(root, "release-manifest.json");
  await writeFile(input, `${JSON.stringify(manifest)}\n`);
  return input;
}

function validate(input) {
  return spawnSync(process.execPath, [script, "validate", "--input", input], {
    cwd: repoRoot,
    encoding: "utf8",
  });
}

test("release manifest accepts a complete global immutable release record", async (t) => {
  const input = await withManifest(t, validManifest());
  const result = validate(input);

  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), validManifest());
});

test("release manifest rejects mutable image tags and inconsistent identity fields", async (t) => {
  const manifest = validManifest();
  manifest.images.api = "ghcr.1ms.run/zleap-ai/sag-api:1.5.0-fnos.1";
  manifest.candidate_tag = "fnos-candidate-1.4.0-fnos.6-aaaaaaaaaaaa";
  manifest.fpk.filename = "sag-1.4.0-fnos.8.fpk";
  const input = await withManifest(t, manifest);
  const result = validate(input);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /immutable|candidate tag|filename/i);
});

test("release manifest rejects missing or malformed build provenance", async (t) => {
  const manifest = validManifest();
  manifest.build_id = "2026-08-04";
  delete manifest.built_at_utc;
  const result = validate(await withManifest(t, manifest));

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /build_id|built_at_utc/i);
});

test("release manifest rejects a cn channel without an approved repository prefix", async (t) => {
  const manifest = validManifest();
  manifest.channel = "cn";
  const input = await withManifest(t, manifest);
  const result = validate(input);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /cn.*requires|repository-prefix/i);
});

test("release manifest accepts an immutable cn release only when all images use its approved prefix", async (t) => {
  const manifest = validManifest();
  const prefix = "registry.sag.example.cn/fnos";
  manifest.channel = "cn";
  manifest.cn_repository_prefix = prefix;
  manifest.images = {
    api: `${prefix}/sag-api@sha256:${"b".repeat(64)}`,
    web: `${prefix}/sag-web@sha256:${"c".repeat(64)}`,
    gateway: `${prefix}/sag-gateway@sha256:${"d".repeat(64)}`,
  };
  const input = await withManifest(t, manifest);
  const result = validate(input);

  assert.equal(result.status, 0, result.stderr);
});
