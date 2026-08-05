import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("../..", import.meta.url));
const helper = path.join(repoRoot, "scripts/fnos-release-registry.mjs");
const digestA = `sha256:${"a".repeat(64)}`;
const digestB = `sha256:${"b".repeat(64)}`;
const api = "ghcr.io/zleap-ai/sag-api";
const web = "ghcr.io/zleap-ai/sag-web";
const manifestText = readFileSync(path.join(repoRoot, "packages/fnos/sag/manifest"), "utf8");
const version = manifestText.match(/^version\s*=\s*(\S+)\s*$/m)?.[1];
if (!version) throw new Error("packages/fnos/sag/manifest is missing a version line");

async function fakeDocker(t, state = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), "sag-fnos-release-registry-"));
  t.after(async () => rm(root, { recursive: true, force: true }));
  const executable = path.join(root, "docker");
  const statePath = path.join(root, "state.json");
  await writeFile(statePath, JSON.stringify({ refs: {}, errors: {}, raw: {}, log: [], ...state }));
  await writeFile(executable, `#!/usr/bin/env node
const fs = require("node:fs");
const statePath = process.env.FAKE_DOCKER_STATE;
const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
const args = process.argv.slice(2);
state.log.push(args);
const save = () => fs.writeFileSync(statePath, JSON.stringify(state));
const format = args.join(" ") === "buildx imagetools inspect --format {{.Manifest.Digest}}";
if (format) process.exit(2);
if (args.slice(0, 5).join(" ") === "buildx imagetools inspect --format {{.Manifest.Digest}}") {
  const ref = args[5];
  if (state.errors[ref]) { process.stderr.write(state.errors[ref] + "\\n"); save(); process.exit(1); }
  if (state.refs[ref]) { process.stdout.write(state.refs[ref] + "\\n"); save(); process.exit(0); }
  process.stderr.write("ERROR: " + ref + ": not found\\n"); save(); process.exit(1);
}
if (args.slice(0, 4).join(" ") === "buildx imagetools inspect --raw") {
  const ref = args[4];
  const index = state.raw[ref] || { mediaType: "application/vnd.oci.image.index.v1+json", manifests: [
    { platform: { os: "linux", architecture: "amd64" } },
    { platform: { os: "linux", architecture: "arm64" } },
  ] };
  process.stdout.write(JSON.stringify(index) + "\\n"); save(); process.exit(0);
}
if (args.slice(0, 3).join(" ") === "pull --platform linux/amd64") {
  process.stdout.write("pull progress that must not contaminate a digest\\n"); save(); process.exit(0);
}
if (args.slice(0, 2).join(" ") === "image inspect") {
  const label = args.at(-1).includes("revision") ? process.env.FAKE_REVISION : process.env.FAKE_VERSION;
  process.stdout.write(label + "\\n"); save(); process.exit(0);
}
if (args.slice(0, 4).join(" ") === "buildx imagetools create --tag") {
  const ref = args[4];
  const digest = args[5].slice(args[5].lastIndexOf("@") + 1);
  state.refs[ref] = digest; save(); process.exit(0);
}
save(); process.stderr.write("unexpected docker arguments: " + args.join(" ") + "\\n"); process.exit(2);
`);
  await chmod(executable, 0o755);
  return { executable, statePath, env: { FAKE_DOCKER_STATE: statePath, FAKE_REVISION: "0123456789abcdef", FAKE_VERSION: version } };
}

function run(args, env) {
  return spawnSync(process.execPath, [helper, ...args], {
    cwd: repoRoot,
    encoding: "utf8",
    env: { ...process.env, ...env },
  });
}

async function stateOf(statePath) {
  return JSON.parse(await readFile(statePath, "utf8"));
}

function promoteArgs(docker) {
  return ["promote", "--docker", docker, "--api-image", api, "--web-image", web, "--candidate-version", version, "--api-digest", digestA, "--web-digest", digestB];
}

function verifyPublicArgs(docker) {
  return ["verify-public", "--docker", docker, "--api-image", api, "--web-image", web, "--candidate-version", version, "--api-digest", digestA, "--web-digest", digestB];
}

function verifyPublicDigestsArgs(docker) {
  return ["verify-public-digests", "--docker", docker, "--api-image", api, "--web-image", web, "--api-digest", digestA, "--web-digest", digestB];
}

test("digest verification emits only the immutable digest despite docker pull stdout and writes exact JSON/job outputs", async (t) => {
  const fake = await fakeDocker(t);
  const githubOutput = path.join(path.dirname(fake.statePath), "github-output");
  const artifact = path.join(path.dirname(fake.statePath), "verified-digests.json");
  const verified = run(["verify-digest", "--docker", fake.executable, "--image", api, "--digest", digestA, "--revision", "0123456789abcdef", "--version", version], fake.env);

  assert.equal(verified.status, 0, verified.stderr);
  assert.equal(verified.stdout, `${digestA}\n`);
  const handoff = run(["write-handoff", "--api-digest", verified.stdout.trim(), "--web-digest", digestB, "--github-output", githubOutput, "--artifact", artifact], fake.env);
  assert.equal(handoff.status, 0, handoff.stderr);
  assert.equal(await readFile(githubOutput, "utf8"), `api_digest=${digestA}\nweb_digest=${digestB}\n`);
  assert.deepEqual(JSON.parse(await readFile(artifact, "utf8")), { api_digest: digestA, web_digest: digestB });
});

test("promotion creates only the two user-facing version tags and postchecks them", async (t) => {
  const fake = await fakeDocker(t);
  const result = run(promoteArgs(fake.executable), fake.env);
  assert.equal(result.status, 0, result.stderr);
  const state = await stateOf(fake.statePath);
  const expected = [`${api}:${version}`, `${web}:${version}`];
  assert.deepEqual(state.log.filter((args) => args.slice(0, 4).join(" ") === "buildx imagetools create --tag").map((args) => args[4]), expected);
  assert.deepEqual(Object.fromEntries(expected.map((ref) => [ref, state.refs[ref]])), {
    [`${api}:${version}`]: digestA,
    [`${web}:${version}`]: digestB,
  });
  const inspections = state.log.filter((args) => args.slice(0, 5).join(" ") === "buildx imagetools inspect --format {{.Manifest.Digest}}").map((args) => args[5]);
  assert.deepEqual(inspections.slice(-2), expected);
});

test("promotion accepts existing matching tags without rewriting them", async (t) => {
  const refs = { [`${api}:${version}`]: digestA, [`${web}:${version}`]: digestB };
  const fake = await fakeDocker(t, { refs });
  const result = run(promoteArgs(fake.executable), fake.env);
  assert.equal(result.status, 0, result.stderr);
  assert.equal((await stateOf(fake.statePath)).log.filter((args) => args.includes("create")).length, 0);
});

test("promotion fails closed for a final tag with a different digest", async (t) => {
  const fake = await fakeDocker(t, { refs: { [`${api}:${version}`]: digestB } });
  const result = run(promoteArgs(fake.executable), fake.env);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /different digest/i);
  assert.equal((await stateOf(fake.statePath)).log.filter((args) => args.includes("create")).length, 0);
});

for (const message of [
  "ERROR: unauthorized: authentication required",
  "ERROR: failed to authorize: unexpected status from GET request to https://ghcr.io/v2/: 429 Too Many Requests",
  "ERROR: failed to do request: Get https://ghcr.io/v2/: dial tcp: lookup ghcr.io: no such host",
  "ERROR: registry replied not found while updating cache",
]) {
  test(`promotion fails closed for non-absence Buildx error: ${message}`, async (t) => {
    const fake = await fakeDocker(t, { errors: { [`${api}:${version}`]: message } });
    const result = run(promoteArgs(fake.executable), fake.env);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /could not inspect final tag/i);
    assert.equal((await stateOf(fake.statePath)).log.filter((args) => args.includes("create")).length, 0);
  });
}

test("partial promotion retry fills only absent references", async (t) => {
  const fake = await fakeDocker(t, { refs: { [`${api}:${version}`]: digestA } });
  const result = run(promoteArgs(fake.executable), fake.env);
  assert.equal(result.status, 0, result.stderr);
  const creates = (await stateOf(fake.statePath)).log.filter((args) => args.slice(0, 4).join(" ") === "buildx imagetools create --tag");
  assert.deepEqual(creates.map((args) => args[4]), [`${web}:${version}`]);
});

test("anonymous verification checks candidate tags and exact multi-platform digests", async (t) => {
  const fake = await fakeDocker(t, {
    refs: { [`${api}:${version}`]: digestA, [`${web}:${version}`]: digestB },
  });
  const result = run(verifyPublicArgs(fake.executable), fake.env);

  assert.equal(result.status, 0, result.stderr);
  const log = (await stateOf(fake.statePath)).log;
  assert.deepEqual(
    log.filter((args) => args.slice(0, 5).join(" ") === "buildx imagetools inspect --format {{.Manifest.Digest}}").map((args) => args[5]),
    [`${api}:${version}`, `${web}:${version}`],
  );
  assert.deepEqual(
    log.filter((args) => args.slice(0, 4).join(" ") === "buildx imagetools inspect --raw").map((args) => args[4]),
    [`${api}@${digestA}`, `${web}@${digestB}`],
  );
});

test("candidate verification anonymously checks immutable digests without creating or resolving version tags", async (t) => {
  const fake = await fakeDocker(t);
  const result = run(verifyPublicDigestsArgs(fake.executable), fake.env);

  assert.equal(result.status, 0, result.stderr);
  const log = (await stateOf(fake.statePath)).log;
  assert.deepEqual(
    log.filter((args) => args.slice(0, 4).join(" ") === "buildx imagetools inspect --raw").map((args) => args[4]),
    [`${api}@${digestA}`, `${web}@${digestB}`],
  );
  assert.equal(log.filter((args) => args.includes("create")).length, 0);
  assert.equal(log.filter((args) => args.includes("{{.Manifest.Digest}}")).length, 0);
});

test("anonymous verification fails when a public candidate tag has a different digest", async (t) => {
  const fake = await fakeDocker(t, {
    refs: { [`${api}:${version}`]: digestB, [`${web}:${version}`]: digestB },
  });
  const result = run(verifyPublicArgs(fake.executable), fake.env);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /does not resolve to the captured digest/i);
});

test("anonymous verification fails closed when GHCR requires authentication", async (t) => {
  const fake = await fakeDocker(t, {
    errors: { [`${api}:${version}`]: "ERROR: unauthorized: authentication required" },
  });
  const result = run(verifyPublicArgs(fake.executable), fake.env);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /could not anonymously resolve/i);
});

test("anonymous verification rejects an exact digest missing linux arm64", async (t) => {
  const apiReference = `${api}@${digestA}`;
  const fake = await fakeDocker(t, {
    refs: { [`${api}:${version}`]: digestA, [`${web}:${version}`]: digestB },
    raw: {
      [apiReference]: {
        mediaType: "application/vnd.oci.image.index.v1+json",
        manifests: [{ platform: { os: "linux", architecture: "amd64" } }],
      },
    },
  });
  const result = run(verifyPublicArgs(fake.executable), fake.env);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /missing linux\/arm64/i);
});
