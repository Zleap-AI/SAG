import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("../..", import.meta.url));
const workflowPath = path.join(repoRoot, ".github/workflows/fnos-release.yml");

test("one fnOS Delivery workflow separates one-day candidate packages from final publication", async () => {
  const workflow = await readFile(workflowPath, "utf8");

  assert.match(workflow, /^name: fnOS Delivery$/m);
  assert.match(workflow, /candidate:|quality:|gateway-security:|build-images:|inspect-images:|smoke-images:|anonymous-digest-postcheck:|promote:|anonymous-final-postcheck:/);
  assert.match(workflow, /default: candidate/);
  assert.match(workflow, /options: \[candidate, publish\]/);
  assert.match(workflow, /name: Upload one-day Candidate FPK/);
  assert.match(workflow, /retention-days: 1/);
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
  assert.match(workflow, /push-by-digest=true/);
  assert.match(workflow, /name-canonical=true/);
  assert.match(workflow, /tags: \$\{\{ matrix\.image \}\}/);
  assert.doesNotMatch(workflow, /staging-fnos-|COMMIT_TAG|commit-tag|sha-\$\{\{/);
  assert.match(workflow, /gateway-security:\n[\s\S]*?needs: verify-release-request/);
  assert.match(workflow, /build-images:\n[\s\S]*?needs: verify-release-request/);
  assert.match(workflow, /promote:\n[\s\S]*?needs: \[verify-release-request, quality, gateway-security, inspect-images, smoke-images\]/);
  assert.match(workflow, /promote:\n[\s\S]*?if: \$\{\{ github\.event_name == 'workflow_dispatch' && inputs\.mode == 'publish' \}\}/);
  assert.match(workflow, /anonymous-digest-postcheck:\n[\s\S]*?verify-public-digests/);
  assert.match(workflow, /delivery-endpoint-check:\n[\s\S]*?name: Verify FPK delivery image endpoints/);
  assert.match(workflow, /delivery-endpoint-check:\n[\s\S]*?ghcr\.1ms\.run\/zleap-ai\/sag-api@\$\{\{ needs\.inspect-images\.outputs\.api_digest \}\}/);
  assert.match(workflow, /delivery-endpoint-check:\n[\s\S]*?ghcr\.1ms\.run\/zleap-ai\/sag-web@\$\{\{ needs\.inspect-images\.outputs\.web_digest \}\}/);
  assert.match(workflow, /delivery-endpoint-check:\n[\s\S]*?--gateway-image "\$GATEWAY_IMAGE"/);
  assert.match(workflow, /delivery-endpoint-check:\n[\s\S]*?verify-delivery-endpoints/);
  assert.match(workflow, /build-package:\n[\s\S]*?needs: \[verify-release-request, quality, gateway-security, inspect-images, smoke-images, anonymous-digest-postcheck, delivery-endpoint-check\]/);
  assert.match(workflow, /build-package:\n[\s\S]*?if: \$\{\{ github\.event_name == 'workflow_dispatch' && inputs\.mode == 'candidate' \}\}/);
  assert.match(workflow, /--allow-unpromoted-images true/);
  assert.match(workflow, /publish-release:\n[\s\S]*?needs: \[verify-release-request, gateway-security, inspect-images, anonymous-final-postcheck, delivery-endpoint-check\]/);
  assert.doesNotMatch(workflow, /local-amd64-smoke/);
  assert.match(workflow, /smoke-fnos-release-images\.mjs smoke/);
});
