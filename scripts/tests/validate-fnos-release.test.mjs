import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const validator = fileURLToPath(new URL("../validate-fnos-release.mjs", import.meta.url));
const digest = `sha256:${"a".repeat(64)}`;
const gatewayReference = "ghcr.io/zleap-ai/sag-gateway:1.4.0-fnos.8@sha256:758f0377a23257333a8957eb5d1f67ccc4b84dfc8a5c3f939e440b087076453c";

function validCompose({
  api = "",
  apiSecret = "${SAG_SECRET_KEY:?set in the runtime environment}",
  apiBootstrap = null,
  apiAuthMode = "single_user",
  apiEnvFile = "",
  web = "",
  gateway = "    ports:\n      - \"${TRIM_SERVICE_PORT}:80\"\n",
} = {}) {
  const environment = [
    apiAuthMode === null ? null : `      SAG_AUTH_MODE: ${apiAuthMode}`,
    apiSecret === null ? null : `      SAG_SECRET_KEY: ${apiSecret}`,
    apiBootstrap === null ? null : `      SAG_AUTH_BOOTSTRAP_TOKEN: ${apiBootstrap}`,
  ].filter(Boolean);
  const authEnvironment = environment.length
    ? `    environment:\n${environment.join("\n")}\n`
    : "";
  return `name: sag\nservices:\n  api:\n    image: ghcr.io/zleap-ai/sag-api@${digest}\n${apiEnvFile}${authEnvironment}${api}  web:\n    image: ghcr.io/zleap-ai/sag-web@${digest}\n${web}  gateway:\n    image: ${gatewayReference}\n${gateway}`;
}

async function fixture(t, contents) {
  const root = await mkdtemp(path.join(os.tmpdir(), "sag-fnos-release-"));
  const compose = path.join(root, "docker-compose.yml");
  await writeFile(compose, contents);
  t.after(async () => rm(root, { recursive: true, force: true }));
  return compose;
}

function validate(compose) {
  const result = spawnSync(process.execPath, [validator, compose], { encoding: "utf8" });
  if (result.error) throw result.error;
  return result;
}

test("requires an explicit rendered Compose path", () => {
  const result = spawnSync(process.execPath, [validator], { encoding: "utf8" });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /usage:.*validate-fnos-release\.mjs.*<rendered-compose-path>/i);
  assert.doesNotMatch(result.stderr, /could not parse/i);
});

test("accepts a digest-pinned release Compose with a required runtime API secret", async (t) => {
  const compose = await fixture(t, validCompose());
  const result = validate(compose);

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /release Compose validation passed/);
});

test("accepts an fnOS package env_file as the API secret source", async (t) => {
  const compose = await fixture(t, validCompose({
    apiSecret: null,
    apiBootstrap: null,
    apiEnvFile: "    env_file:\n      - ${TRIM_PKGETC}/sag.env\n",
  }));
  const result = validate(compose);

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /release Compose validation passed/);
});

test("rejects an fnOS secret env_file followed by an override env_file", async (t) => {
  const compose = await fixture(t, validCompose({
    apiSecret: null,
    apiBootstrap: null,
    apiEnvFile: "    env_file:\n      - ${TRIM_PKGETC}/sag.env\n      - ./override.env\n",
  }));
  const result = validate(compose);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /exactly one required env_file/);
});

test("rejects an optional fnOS secret env_file", async (t) => {
  const compose = await fixture(t, validCompose({
    apiSecret: null,
    apiBootstrap: null,
    apiEnvFile: "    env_file:\n      - path: ${TRIM_PKGETC}/sag.env\n        required: false\n",
  }));
  const result = validate(compose);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /exactly one required env_file/);
});

test("rejects a latest image tag", async (t) => {
  const compose = await fixture(t, validCompose().replace(`ghcr.io/zleap-ai/sag-api@${digest}`, "ghcr.io/zleap-ai/sag-api:latest"));
  const result = validate(compose);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /latest tag/);
});

test("rejects Compose build instructions", async (t) => {
  const compose = await fixture(t, validCompose({ api: "    build: ./apps/api\n" }));
  const result = validate(compose);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /must not define build/);
});

test("rejects a weak development secret", async (t) => {
  const compose = await fixture(t, validCompose({
    apiSecret: "dev-insecure-secret-change-me-in-production-0123456789",
  }));
  const result = validate(compose);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /required SAG_SECRET_KEY/);
});

test("rejects a predictable 64-zero literal API secret", async (t) => {
  const compose = await fixture(t, validCompose({ apiSecret: `"${"0".repeat(64)}"` }));
  const result = validate(compose);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /required SAG_SECRET_KEY/);
});

test("rejects a release Compose that does not enable no-auth single-user mode", async (t) => {
  const compose = await fixture(t, validCompose({ apiAuthMode: "legacy" }));
  const result = validate(compose);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /SAG_AUTH_MODE=single_user/);
});

test("rejects any obsolete auth bootstrap credential", async (t) => {
  const compose = await fixture(t, validCompose({ apiBootstrap: `"${"b".repeat(64)}"` }));
  const result = validate(compose);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /must not define SAG_AUTH_BOOTSTRAP_TOKEN/);
});

test("rejects an API host port", async (t) => {
  const compose = await fixture(t, validCompose({ api: "    ports:\n      - \"8000:8000\"\n" }));
  const result = validate(compose);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /api must not publish host ports/);
});

test("rejects a Web host port", async (t) => {
  const compose = await fixture(t, validCompose({ web: "    ports:\n      - \"3000:3000\"\n" }));
  const result = validate(compose);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /web must not publish host ports/);
});

test("rejects a missing gateway host port", async (t) => {
  const compose = await fixture(t, validCompose({ gateway: "" }));
  const result = validate(compose);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /gateway must publish exactly.*TRIM_SERVICE_PORT.*80/i);
});

test("rejects an extra gateway host port", async (t) => {
  const compose = await fixture(t, validCompose({
    gateway: "    ports:\n      - \"${TRIM_SERVICE_PORT}:80\"\n      - \"8443:443\"\n",
  }));
  const result = validate(compose);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /gateway must publish exactly.*TRIM_SERVICE_PORT.*80/i);
});

test("rejects a lifecycle helper host port", async (t) => {
  const compose = await fixture(t, `${validCompose()}  lifecycle-helper:
    image: ghcr.io/zleap-ai/sag-api@${digest}
    ports:
      - "9000:9000"
`);
  const result = validate(compose);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /lifecycle-helper must not publish host ports/i);
});

test("rejects host networking on any service", async (t) => {
  const compose = await fixture(t, validCompose({ web: "    network_mode: host\n" }));
  const result = validate(compose);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /web must not use host networking/i);
});

test("rejects a non-digest image reference", async (t) => {
  const compose = await fixture(t, validCompose().replace(gatewayReference, "nginx:1.30.4-alpine"));
  const result = validate(compose);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /must use an immutable sha256 digest/);
});

test("rejects an immutable but unreviewed Nginx gateway digest", async (t) => {
  const compose = await fixture(t, validCompose().replace(
    gatewayReference,
    `ghcr.io/zleap-ai/sag-gateway:1.4.0-fnos.8@sha256:${"f".repeat(64)}`,
  ));
  const result = validate(compose);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /reviewed gateway reference/i);
});
