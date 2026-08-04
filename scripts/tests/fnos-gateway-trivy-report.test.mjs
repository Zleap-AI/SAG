import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { access, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("../..", import.meta.url));
const canonicalizer = path.join(repoRoot, "scripts/canonicalize-fnos-trivy-report.mjs");
const reference = "ghcr.1ms.run/zleap-ai/sag-gateway:1.5.0-fnos.1@sha256:758f0377a23257333a8957eb5d1f67ccc4b84dfc8a5c3f939e440b087076453c";
const expectedTarget = `${reference} (alpine 3.24.1)`;

async function fixture(t, report) {
  const root = await mkdtemp(path.join(os.tmpdir(), "sag-trivy-canonical-"));
  t.after(async () => rm(root, { recursive: true, force: true }));
  const input = path.join(root, "raw.json");
  const output = path.join(root, "canonical.json");
  await writeFile(input, JSON.stringify(report));
  return { input, output };
}

function run(input, output, scannerExitCode) {
  return spawnSync(process.execPath, [
    canonicalizer,
    "--input", input,
    "--output", output,
    "--scanner-exit-code", String(scannerExitCode),
  ], { cwd: repoRoot, encoding: "utf8" });
}

function validPackage(overrides = {}) {
  return {
    ID: "nginx@1.30.4-r1",
    Name: "nginx",
    Version: "1.30.4-r1",
    ...overrides,
  };
}

function validVulnerability(overrides = {}) {
  return {
    VulnerabilityID: "CVE-TEST-1",
    PkgName: "nginx",
    InstalledVersion: "1.30.4-r0",
    FixedVersion: "1.30.4-r1",
    Severity: "HIGH",
    ...overrides,
  };
}

function reportWithResult(overrides = {}) {
  return {
    Metadata: {
      OS: { Family: "alpine", Name: "3.24.1" },
    },
    Results: [{
      Target: expectedTarget,
      Class: "os-pkgs",
      Type: "alpine",
      Packages: [validPackage()],
      ...overrides,
    }],
  };
}

test("canonicalizes Trivy's documented zero-finding omission to an explicit null only after exit zero", async (t) => {
  const { input, output } = await fixture(t, reportWithResult());
  const result = run(input, output, 0);
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  const canonical = JSON.parse(await readFile(output, "utf8"));
  assert.equal(Object.hasOwn(canonical.Results[0], "Vulnerabilities"), true);
  assert.equal(canonical.Results[0].Vulnerabilities, null);
});

test("refuses to infer zero findings from a missing property after nonzero scanner exit", async (t) => {
  const { input, output } = await fixture(t, reportWithResult());
  const result = run(input, output, 2);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /cannot infer zero findings/i);
  await assert.rejects(access(output));
});

test("rejects OS evidence that differs from the reviewed Alpine policy", async (t) => {
  for (const report of [
    {
      ...reportWithResult({ Type: "debian" }),
      Metadata: { OS: { Family: "debian", Name: "12" } },
    },
    {
      ...reportWithResult(),
      Metadata: { OS: { Family: "alpine", Name: "3.24.0" } },
    },
  ]) {
    const { input, output } = await fixture(t, report);
    const result = run(input, output, 0);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /reviewed.*operating-system|operating-system.*reviewed/i);
    await assert.rejects(access(output));
  }
});

test("rejects any Trivy result target other than the exact reviewed target", async (t) => {
  for (const target of [
    "nginx (alpine 3.24.1)",
    `prefix-${expectedTarget}`,
    `${expectedTarget}-suffix`,
    expectedTarget.toUpperCase(),
  ]) {
    const { input, output } = await fixture(t, reportWithResult({ Target: target }));
    const result = run(input, output, 0);
    assert.notEqual(result.status, 0, target);
    assert.match(result.stderr, /exact reviewed Target/i, target);
    await assert.rejects(access(output), target);
  }
});

test("preserves an explicit Trivy vulnerability array without changing findings", async (t) => {
  const vulnerability = validVulnerability();
  const { input, output } = await fixture(t, reportWithResult({
    Vulnerabilities: [vulnerability],
  }));
  const result = run(input, output, 1);
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  const canonical = JSON.parse(await readFile(output, "utf8"));
  assert.deepEqual(canonical.Results[0].Vulnerabilities, [vulnerability]);
});

test("refuses to infer zero findings from invalid OS-package or package evidence", async (t) => {
  const cases = [
    ["null package", { Packages: [null] }],
    ["empty packages", { Packages: [] }],
    ["missing packages", { Packages: undefined }],
    ["wrong class", { Class: "lang-pkgs" }],
    ["missing class", { Class: undefined }],
    ["wrong type", { Type: "debian" }],
    ["missing type", { Type: undefined }],
    ["missing target", { Target: "" }],
    ["missing package name", { Packages: [validPackage({ Name: undefined })] }],
    ["missing package version", { Packages: [validPackage({ Version: undefined })] }],
  ];
  for (const [name, overrides] of cases) {
    const { input, output } = await fixture(t, reportWithResult(overrides));
    const result = run(input, output, 0);
    assert.notEqual(result.status, 0, name);
    assert.match(result.stderr, /OS-package|Packages|package|Target|Class|Type/i, name);
    await assert.rejects(access(output), name);
  }
});

test("rejects explicit null without valid packages and malformed vulnerability values", async (t) => {
  const cases = [
    ["null without packages", { Packages: [], Vulnerabilities: null }],
    ["wrong container", { Vulnerabilities: "none" }],
    ["null finding", { Vulnerabilities: [null] }],
    ["missing vulnerability ID", {
      Vulnerabilities: [validVulnerability({ VulnerabilityID: undefined })],
    }],
    ["missing package name", {
      Vulnerabilities: [validVulnerability({ PkgName: undefined })],
    }],
    ["missing installed version", {
      Vulnerabilities: [validVulnerability({ InstalledVersion: undefined })],
    }],
    ["wrong severity", {
      Vulnerabilities: [validVulnerability({ Severity: "MEDIUM" })],
    }],
    ["missing fixed version", {
      Vulnerabilities: [validVulnerability({ FixedVersion: undefined })],
    }],
  ];
  for (const [name, overrides] of cases) {
    const { input, output } = await fixture(t, reportWithResult(overrides));
    const result = run(input, output, 1);
    assert.notEqual(result.status, 0, name);
    assert.match(result.stderr, /Packages|Vulnerabilities|vulnerability|finding/i, name);
    await assert.rejects(access(output), name);
  }
});
