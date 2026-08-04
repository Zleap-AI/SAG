import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("../..", import.meta.url));
const workflowPath = path.join(repoRoot, ".github/workflows/fnos-image-release.yml");
const ciPath = path.join(repoRoot, ".github/workflows/ci.yml");

function job(workflow, name) {
  const match = new RegExp(`^  ${name}:\\n([\\s\\S]*?)(?=^  [a-z][a-z-]+:|(?![\\s\\S]))`, "m").exec(workflow);
  assert.ok(match, `missing ${name} job`);
  return match[1];
}

test("dedicated fnOS branch CI and immutable candidate tag gate release writes", async () => {
  const [workflow, ci] = await Promise.all([
    readFile(workflowPath, "utf8"),
    readFile(ciPath, "utf8"),
  ]);
  const candidate = job(workflow, "candidate");

  assert.match(ci, /branches: \[main, dev, fnos\/develop\]/);
  assert.match(workflow, /push:\n    tags:\n      - "fnos-candidate-\*"/);
  assert.doesNotMatch(workflow, /workflow_dispatch|refs\/heads\/main|\binputs\./);
  assert.match(workflow, /concurrency:\n  group: fnos-candidate-\$\{\{ github\.repository \}\}\n  cancel-in-progress: false/);
  assert.match(candidate, /git ls-remote --heads origin fnos\/develop/);
  assert.doesNotMatch(candidate, /test "\$version" = "1\.[0-9]+\.[0-9]+-fnos\.\d+"/);
  assert.match(candidate, /case "\$version" in\n            1\.\[0-9\]\*\.\[0-9\]\*-fnos\.\[0-9\]\*\) ;;/);
  assert.match(candidate, /expected_tag="fnos-candidate-\$\{version\}-\$\{GITHUB_SHA:0:12\}"/);
  assert.match(candidate, /test "\$GITHUB_REF_NAME" = "\$expected_tag"/);
  assert.match(candidate, /test "\$GITHUB_SHA" = "\$remote_revision"/);
  assert.match(candidate, /revision: \$\{\{ steps\.metadata\.outputs\.revision \}\}/);
  assert.match(job(workflow, "quality"), /needs: candidate/);

  for (const name of [
    "gateway-security",
    "local-amd64-smoke",
    "staging",
    "inspect-staging",
    "smoke-staging",
    "promote",
    "anonymous-postcheck",
  ]) {
    const releaseJob = job(workflow, name);
    assert.match(releaseJob, /needs(?:\.candidate|: [^\n]*candidate)/);
    assert.match(releaseJob, /ref: \$\{\{ needs\.candidate\.outputs\.revision \}\}/);
  }
  assert.match(candidate, /ref: \$\{\{ github\.sha \}\}/);
  assert.doesNotMatch(workflow.replace(candidate, ""), /ref: \$\{\{ github\.sha \}\}/);
  assert.doesNotMatch(workflow, /revision=\$\{\{ github\.sha \}\}|sha-\$\{\{ github\.sha \}\}/);
});

test("fnOS release workflow pins actions and scopes package permissions", async () => {
  const workflow = await readFile(workflowPath, "utf8");
  const externalActions = workflow.match(/^\s*-?\s*uses: (?!\.\/)(.+)$/gm) ?? [];

  assert.ok(externalActions.length > 0);
  for (const action of externalActions) assert.match(action, /@[a-f0-9]{40}\s+# v/);
  assert.match(workflow, /^permissions:\n  contents: read$/m);
  assert.doesNotMatch(workflow, /^permissions:\n(?:.*\n)*?  packages:/m);
  assert.doesNotMatch(job(workflow, "candidate"), /packages:/);
  assert.doesNotMatch(job(workflow, "quality"), /packages: write/);
  assert.doesNotMatch(job(workflow, "local-amd64-smoke"), /packages: write/);
  assert.match(job(workflow, "inspect-staging"), /permissions:\n      contents: read\n      packages: read/);
  assert.match(job(workflow, "smoke-staging"), /permissions:\n      contents: read\n      packages: read/);
  assert.match(job(workflow, "anonymous-postcheck"), /permissions:\n      contents: read/);
  assert.doesNotMatch(job(workflow, "anonymous-postcheck"), /packages:|GITHUB_TOKEN|docker\/login-action/);
  for (const name of ["staging", "promote"]) {
    assert.match(job(workflow, name), /permissions:\n      contents: read\n      packages: write/);
  }
  assert.doesNotMatch(workflow, /visibility=public|--method PATCH/);
});

test("promotion is followed by an unauthenticated exact-digest public check", async () => {
  const workflow = await readFile(workflowPath, "utf8");
  const postcheck = job(workflow, "anonymous-postcheck");

  assert.match(postcheck, /needs: \[candidate, inspect-staging, promote\]/);
  assert.match(postcheck, /fnos-release-registry\.mjs verify-public/);
  assert.match(postcheck, /--candidate-version "\$CANDIDATE_VERSION"/);
  assert.match(postcheck, /--api-digest "\$API_DIGEST"/);
  assert.match(postcheck, /--web-digest "\$WEB_DIGEST"/);
  assert.doesNotMatch(postcheck, /docker\/login-action|username:|password:|GITHUB_TOKEN|packages: read/);
});

test("promotion requires an exact captured-digest runtime smoke", async () => {
  const workflow = await readFile(workflowPath, "utf8");
  const smoke = job(workflow, "smoke-staging");
  const promote = job(workflow, "promote");

  assert.match(smoke, /needs: \[candidate, inspect-staging\]/);
  assert.match(smoke, /API_IMAGE: ghcr\.io\/zleap-ai\/sag-api@\$\{\{ needs\.inspect-staging\.outputs\.api_digest \}\}/);
  assert.match(smoke, /WEB_IMAGE: ghcr\.io\/zleap-ai\/sag-web@\$\{\{ needs\.inspect-staging\.outputs\.web_digest \}\}/);
  assert.match(smoke, /smoke-fnos-release-images\.mjs smoke/);
  assert.match(smoke, /if: \$\{\{ always\(\) \}\}/);
  assert.match(smoke, /smoke-fnos-release-images\.mjs cleanup/);
  assert.doesNotMatch(smoke, /STAGING_TAG|staging-fnos|build-push-action|sag-(api|web)-smoke:/);
  assert.match(promote, /needs: \[candidate, inspect-staging, smoke-staging\]/);
});

test("fnOS release workflow invokes the executable digest handoff and promotion state machine", async () => {
  const workflow = await readFile(workflowPath, "utf8");
  const inspect = job(workflow, "inspect-staging");
  const promote = job(workflow, "promote");

  assert.match(inspect, /outputs:\n      api_digest: \$\{\{ steps\.digests\.outputs\.api_digest \}\}/);
  assert.match(inspect, /web_digest: \$\{\{ steps\.digests\.outputs\.web_digest \}\}/);
  assert.match(inspect, /id: digests/);
  assert.match(inspect, /fnos-release-registry\.mjs verify-staging/);
  assert.match(inspect, /fnos-release-registry\.mjs write-handoff/);
  assert.match(inspect, /--github-output "\$GITHUB_OUTPUT" --artifact verified-digests\.json/);
  assert.match(inspect, /actions\/upload-artifact@[a-f0-9]{40}\s+# v/);
  assert.match(promote, /API_DIGEST: \$\{\{ needs\.inspect-staging\.outputs\.api_digest \}\}/);
  assert.match(promote, /WEB_DIGEST: \$\{\{ needs\.inspect-staging\.outputs\.web_digest \}\}/);
  assert.doesNotMatch(promote, /\$STAGING_TAG/);
  assert.match(promote, /fnos-release-registry\.mjs promote/);
  assert.doesNotMatch(promote, /reconcile_tag\(\)|inspect_final_tag\(\)/);
  assert.match(workflow, /concurrency:\n  group: fnos-candidate-/);
});

test("amd64 smoke starts the API in no-auth single-user mode", async () => {
  const workflow = await readFile(workflowPath, "utf8");
  const smoke = job(workflow, "local-amd64-smoke");

  assert.match(smoke, /--env SAG_AUTH_MODE=single_user/);
  const sessionSecret = /--env SAG_SECRET_KEY=([a-f0-9]{64})/.exec(smoke)?.[1];
  assert.ok(sessionSecret);
  assert.doesNotMatch(smoke, /SAG_AUTH_BOOTSTRAP_TOKEN|bootstrap_token|password_status/);
  assert.match(smoke, /\/api\/v1\/auth\/session/);
  assert.match(smoke, /test "\$initialized_status" = 201/);
  assert.match(smoke, /test "\$anonymous_status" = 200/);
});

test("gateway scan gates publication with a checksum-pinned Trivy binary and exact reviewed digest", async () => {
  const workflow = await readFile(workflowPath, "utf8");
  const gateway = job(workflow, "gateway-security");
  const staging = job(workflow, "staging");

  assert.match(gateway, /permissions:\n      contents: read/);
  assert.doesNotMatch(gateway, /packages: write/);
  assert.match(gateway, /fnos-gateway-policy\.mjs verify/);
  assert.match(gateway, /TRIVY_VERSION: "0\.70\.0"/);
  assert.match(gateway, /TRIVY_SHA256: "8b4376d5d6befe5c24d503f10ff136d9e0c49f9127a4279fd110b727929a5aa9"/);
  assert.match(gateway, /sha256sum --check --strict/);
  assert.match(gateway, /--platform linux\/amd64/);
  assert.match(gateway, /--severity CRITICAL,HIGH/);
  assert.match(gateway, /--ignore-unfixed/);
  assert.match(gateway, /--exit-code 1/);
  assert.match(gateway, /"\$GATEWAY_IMAGE"/);
  assert.match(gateway, /trivy_status="\$\?"/);
  assert.match(gateway, /canonicalize-fnos-trivy-report\.mjs/);
  assert.match(gateway, /summarize-fnos-gateway-scan\.mjs/);
  assert.match(gateway, /--source-report "\$raw_report"/);
  assert.match(gateway, /--scanner-exit-code "\$trivy_status"/);
  assert.match(gateway, /test "\$trivy_status" -eq 0/);
  assert.match(gateway, /if: \$\{\{ always\(\) \}\}/);
  assert.match(staging, /needs: \[candidate, local-amd64-smoke, gateway-security\]/);
});
