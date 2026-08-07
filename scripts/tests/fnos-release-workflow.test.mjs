import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../..", import.meta.url));
test("fnOS delivery is a single manual publish flow guarded by explicit confirmation", async () => {
  const workflow = await readFile(path.join(root, ".github/workflows/fnos-release.yml"), "utf8");
  assert.match(workflow, /^name: fnOS Delivery$/m);
  // Manual-only trigger: no push branch, only workflow_dispatch.
  assert.doesNotMatch(workflow, /^\s*push:/m);
  assert.match(workflow, /workflow_dispatch:/);
  // Version input exists but is OPTIONAL — auto-derived from the latest
  // main semver tag + the next available fnos build number.
  assert.match(workflow, /version:/);
  assert.match(workflow, /inputs\.version/);
  assert.doesNotMatch(workflow, /packages\/fnos\/sag\/manifest/);
  // Guardrails: only from fnos/develop and only after PUBLISH confirmation.
  assert.match(workflow, /refs\/heads\/fnos\/develop/);
  assert.match(workflow, /inputs\.publish_confirmation.*PUBLISH/);
  // Version auto-derivation from git tags + release listing.
  assert.match(workflow, /git tag -l/);
  assert.match(workflow, /gh release list/);
  // Validate format: <semver>-fnos.<int> (regex in YAML has escaped dots)
  assert.match(workflow, /\[1-9\]\[0-9\]\*/);
  // Two-phase: resolve-version job feeds version to native-x86 job.
  assert.match(workflow, /resolve-version/);
  assert.match(workflow, /needs:.*resolve-version/);
  // The build environment must disable window scaling for fnOS.
  assert.match(workflow, /NEXT_PUBLIC_ENABLE_WINDOW_SCALING=0/);
  // fnpack is pinned to the official 1.2.3 binary and verified before use.
  assert.match(workflow, /https:\/\/static2\.fnnas\.com\/fnpack\/fnpack-1\.2\.3-linux-amd64/);
  assert.match(workflow, /curl --fail --location --proto '=https' --tlsv1\.2/);
  assert.match(workflow, /54b97fa7b70968c4d05c79840f5daeff508957d0bb2062fdb0376d00d9615c93 {2}fnpack/);
  assert.match(workflow, /sha256sum --check --strict/);
  assert.match(workflow, /\$GITHUB_PATH/);
  // Structural tests may shell out to the verified fnpack binary.
  assert.match(workflow, /SAG_FNPACK_TESTS: "1"/);
  // uv is pinned to a release that resolves current PyPI wheel metadata.
  assert.match(workflow, /setup-uv@v5/);
  assert.match(workflow, /version: "0\.10\.8"/);
  // Tests and packaging still run.
  assert.match(workflow, /uv run --extra dev pytest -q/);
  assert.match(workflow, /node scripts\/build-fnos-native-package\.mjs/);
  assert.match(workflow, /sha256sum/);
  // Release tag encodes the version explicitly.
  assert.match(workflow, /fnos-v\$SAG_VERSION/);
  // Release notes include base version and SHA256 context.
  assert.match(workflow, /Base version/);
});
