import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("../..", import.meta.url));
const script = path.join(repoRoot, "scripts/fnos-registry-channel.mjs");
const digest = `sha256:${"a".repeat(64)}`;

function run(channel, api = `ghcr.1ms.run/zleap-ai/sag-api@${digest}`, prefix) {
  return spawnSync(process.execPath, [
    script,
    "validate",
    "--channel", channel,
    "--api-image", api,
    "--web-image", `ghcr.1ms.run/zleap-ai/sag-web@${digest}`,
    "--gateway-image", `ghcr.1ms.run/zleap-ai/sag-gateway:1.5.0-fnos.1@${digest}`,
    ...(prefix ? ["--cn-repository-prefix", prefix] : []),
  ], { cwd: repoRoot, encoding: "utf8" });
}

test("fnOS release channel accepts only the approved 1ms SAG repositories", () => {
  const result = run("global");
  assert.equal(result.status, 0, result.stderr);
});

test("global channel rejects an arbitrary registry even with a digest", () => {
  const result = run("global", `registry.example/sag-api@${digest}`);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /global.*api|approved/i);
});

test("fnOS release channel rejects direct and personal registry references", () => {
  for (const image of [
    `ghcr.io/luoshuai990529/sag-api@${digest}`,
    `ghcr.io/zleap-ai/sag-api@${digest}`,
  ]) {
    const result = run("global", image);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /global.*api|approved/i);
  }
});

test("cn channel rejects publication until an approved repository prefix is supplied", () => {
  const result = run("cn");
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /cn.*requires|repository-prefix/i);
});

test("cn channel accepts only its explicitly approved mirror repositories", () => {
  const prefix = "registry.sag.example.cn/fnos";
  const result = run(
    "cn",
    `${prefix}/sag-api@${digest}`,
    prefix,
  );
  const args = [
    script, "validate", "--channel", "cn",
    "--cn-repository-prefix", prefix,
    "--api-image", `${prefix}/sag-api@${digest}`,
    "--web-image", `${prefix}/sag-web@${digest}`,
    "--gateway-image", `${prefix}/sag-gateway@${digest}`,
  ];
  const accepted = spawnSync(process.execPath, args, { cwd: repoRoot, encoding: "utf8" });
  assert.equal(accepted.status, 0, accepted.stderr);
  assert.notEqual(result.status, 0, "the helper's global web/gateway references must not pass cn validation");
});
