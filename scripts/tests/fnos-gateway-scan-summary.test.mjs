import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("../..", import.meta.url));
const summarizer = path.join(repoRoot, "scripts/summarize-fnos-gateway-scan.mjs");
const reference = "ghcr.io/zleap-ai/sag-gateway:1.4.0-fnos.8@sha256:758f0377a23257333a8957eb5d1f67ccc4b84dfc8a5c3f939e440b087076453c";
const expectedTarget = `${reference} (alpine 3.24.1)`;

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

function completeReport(overrides = {}) {
  return {
    SchemaVersion: 2,
    CreatedAt: "2026-07-29T16:33:41.448549+08:00",
    ArtifactName: reference,
    ArtifactType: "container_image",
    Metadata: {
      OS: { Family: "alpine", Name: "3.24.1" },
      ImageID: `sha256:${"6".repeat(64)}`,
      RepoDigests: ["nginx@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46"],
    },
    Results: [{
      Target: expectedTarget,
      Class: "os-pkgs",
      Type: "alpine",
      Packages: [validPackage()],
      Vulnerabilities: null,
    }],
    ...overrides,
  };
}

async function fixture(t, reportContents = JSON.stringify(completeReport())) {
  const root = await mkdtemp(path.join(os.tmpdir(), "sag-gateway-scan-"));
  t.after(async () => rm(root, { recursive: true, force: true }));
  const report = path.join(root, "report.json");
  const output = path.join(root, "summary.json");
  if (reportContents !== null) await writeFile(report, reportContents);
  return { report, output };
}

function summarize(report, output, scannerExitCode = 0, sourceReport = report) {
  return spawnSync(process.execPath, [
    summarizer,
    "--report", report,
    "--source-report", sourceReport,
    "--output", output,
    "--scanner-version", "0.70.0",
    "--scanner-exit-code", String(scannerExitCode),
  ], { cwd: repoRoot, encoding: "utf8" });
}

test("stores compact evidence for a passed exact-digest gateway scan", async (t) => {
  const { report, output } = await fixture(t);
  const sourceReport = path.join(path.dirname(report), "raw.json");
  const sourceBytes = '{"raw":"Trivy scanner report"}\n';
  await writeFile(sourceReport, sourceBytes);
  const result = summarize(report, output, 0, sourceReport);
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);

  const evidence = JSON.parse(await readFile(output, "utf8"));
  assert.equal(evidence.image.reference, reference);
  assert.equal(evidence.scan.result, "passed");
  assert.equal(evidence.scan.fixableHighCriticalFindings, 0);
  assert.equal(evidence.scan.scanner, "aquasecurity/trivy");
  assert.equal(evidence.scan.scannerVersion, "0.70.0");
  assert.equal(evidence.scan.platform, "linux/amd64");
  assert.equal(
    evidence.scan.sourceReportSha256,
    createHash("sha256").update(sourceBytes).digest("hex"),
  );
  assert.equal(Object.hasOwn(evidence, "Results"), false);
});

test("refuses to summarize a report containing a vulnerability finding", async (t) => {
  const finding = validVulnerability();
  const contents = completeReport({
    Results: [{
      Target: expectedTarget,
      Class: "os-pkgs",
      Type: "alpine",
      Packages: [validPackage()],
      Vulnerabilities: [finding],
    }],
  });
  const { report, output } = await fixture(t, JSON.stringify(contents));
  const result = summarize(report, output, 1);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /contains 1 finding/i);
  const evidence = JSON.parse(await readFile(output, "utf8"));
  assert.equal(evidence.scan.result, "failed");
  assert.equal(evidence.scan.fixableHighCriticalFindings, 1);
  assert.equal(evidence.scan.scannerExitCode, 1);
});

test("records operational scanner failure even when a complete zero-finding report exists", async (t) => {
  const { report, output } = await fixture(t);
  const result = summarize(report, output, 2);
  assert.notEqual(result.status, 0);
  const evidence = JSON.parse(await readFile(output, "utf8"));
  assert.equal(evidence.scan.result, "failed");
  assert.equal(evidence.scan.scannerExitCode, 2);
  assert.match(evidence.scan.failureReasons.join("\n"), /scanner exited with status 2/i);
});

test("writes an honest compact failure summary when the report is absent or corrupt", async (t) => {
  for (const contents of [null, "{not-json"]) {
    const { report, output } = await fixture(t, contents);
    const result = summarize(report, output, 0);
    assert.notEqual(result.status, 0);
    const evidence = JSON.parse(await readFile(output, "utf8"));
    assert.equal(evidence.scan.result, "failed");
    assert.equal(evidence.scan.scannerExitCode, 0);
    assert.equal(evidence.scan.fixableHighCriticalFindings, null);
    assert.match(evidence.scan.failureReasons.join("\n"), /unavailable|not valid JSON/i);
  }
});

test("rejects incomplete Trivy report structure and still writes failure evidence", async (t) => {
  const cases = [
    ["missing Results", { Results: undefined }, /Results.*nonempty/i],
    ["empty Results", { Results: [] }, /Results.*nonempty/i],
    ["invalid CreatedAt", { CreatedAt: "2026-02-30T00:00:00Z" }, /CreatedAt/i],
    ["invalid ImageID", { Metadata: { OS: { Family: "alpine", Name: "3.24.1" }, ImageID: "short" } }, /ImageID/i],
    ["missing OS", { Metadata: { ImageID: `sha256:${"6".repeat(64)}` } }, /operating-system/i],
    [
      "wrong OS evidence",
      {
        Metadata: {
          OS: { Family: "debian", Name: "12" },
          ImageID: `sha256:${"6".repeat(64)}`,
        },
      },
      /reviewed policy/i,
    ],
  ];
  for (const [name, overrides, expected] of cases) {
    const reportObject = completeReport(overrides);
    if (overrides.Results === undefined) delete reportObject.Results;
    const { report, output } = await fixture(t, JSON.stringify(reportObject));
    const result = summarize(report, output);
    assert.notEqual(result.status, 0, name);
    const evidence = JSON.parse(await readFile(output, "utf8"));
    assert.equal(evidence.scan.result, "failed", name);
    assert.match(evidence.scan.failureReasons.join("\n"), expected, name);
  }
});

test("rejects a result that omits the explicit Vulnerabilities property", async (t) => {
  const reportObject = completeReport({
    Results: [{
      Target: expectedTarget,
      Class: "os-pkgs",
      Type: "alpine",
      Packages: [validPackage()],
    }],
  });
  const { report, output } = await fixture(t, JSON.stringify(reportObject));
  const result = summarize(report, output);
  assert.notEqual(result.status, 0);
  const evidence = JSON.parse(await readFile(output, "utf8"));
  assert.match(evidence.scan.failureReasons.join("\n"), /Vulnerabilities property/i);
});

test("rejects invalid OS-package evidence even with explicit null vulnerabilities", async (t) => {
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
  for (const [name, resultOverrides] of cases) {
    const result = {
      Target: expectedTarget,
      Class: "os-pkgs",
      Type: "alpine",
      Packages: [validPackage()],
      Vulnerabilities: null,
      ...resultOverrides,
    };
    const { report, output } = await fixture(t, JSON.stringify(completeReport({
      Results: [result],
    })));
    const execution = summarize(report, output);
    assert.notEqual(execution.status, 0, name);
    const evidence = JSON.parse(await readFile(output, "utf8"));
    assert.equal(evidence.scan.result, "failed", name);
    assert.equal(evidence.scan.fixableHighCriticalFindings, null, name);
    assert.match(
      evidence.scan.failureReasons.join("\n"),
      /OS-package|Packages|package|Target|Class|Type/i,
      name,
    );
  }
});

test("rejects any Trivy result target other than the exact reviewed target", async (t) => {
  for (const target of [
    "nginx",
    `prefix-${expectedTarget}`,
    `${expectedTarget}-suffix`,
    expectedTarget.toUpperCase(),
  ]) {
    const { report, output } = await fixture(t, JSON.stringify(completeReport({
      Results: [{
        Target: target,
        Class: "os-pkgs",
        Type: "alpine",
        Packages: [validPackage()],
        Vulnerabilities: null,
      }],
    })));
    const execution = summarize(report, output);
    assert.notEqual(execution.status, 0, target);
    const evidence = JSON.parse(await readFile(output, "utf8"));
    assert.equal(evidence.scan.result, "failed", target);
    assert.equal(evidence.scan.fixableHighCriticalFindings, null, target);
    assert.match(evidence.scan.failureReasons.join("\n"), /exact reviewed Target/i, target);
  }
});

test("rejects malformed explicit vulnerability values", async (t) => {
  const cases = [
    ["wrong container", "none", /Vulnerabilities must be an array or explicit null/i],
    ["null finding", [null], /Vulnerabilities\[0\].*Trivy vulnerability/i],
    [
      "missing vulnerability ID",
      [validVulnerability({ VulnerabilityID: undefined })],
      /Vulnerabilities\[0\].*Trivy vulnerability/i,
    ],
    [
      "missing package name",
      [validVulnerability({ PkgName: undefined })],
      /Vulnerabilities\[0\].*Trivy vulnerability/i,
    ],
    [
      "missing installed version",
      [validVulnerability({ InstalledVersion: undefined })],
      /Vulnerabilities\[0\].*Trivy vulnerability/i,
    ],
    [
      "wrong severity",
      [validVulnerability({ Severity: "MEDIUM" })],
      /Vulnerabilities\[0\].*Trivy vulnerability/i,
    ],
    [
      "missing fixed version",
      [validVulnerability({ FixedVersion: undefined })],
      /Vulnerabilities\[0\].*Trivy vulnerability/i,
    ],
  ];
  for (const [name, vulnerabilities, expected] of cases) {
    const { report, output } = await fixture(t, JSON.stringify(completeReport({
      Results: [{
        Target: expectedTarget,
        Class: "os-pkgs",
        Type: "alpine",
        Packages: [validPackage()],
        Vulnerabilities: vulnerabilities,
      }],
    })));
    const execution = summarize(report, output, 1);
    assert.notEqual(execution.status, 0, name);
    const evidence = JSON.parse(await readFile(output, "utf8"));
    assert.equal(evidence.scan.result, "failed", name);
    assert.match(evidence.scan.failureReasons.join("\n"), expected, name);
  }
});

test("rejects a complete report for the wrong artifact reference or type", async (t) => {
  for (const overrides of [
    { ArtifactName: "docker.io/library/nginx:other@sha256:wrong" },
    { ArtifactType: "filesystem" },
  ]) {
    const { report, output } = await fixture(t, JSON.stringify(completeReport(overrides)));
    const result = summarize(report, output);
    assert.notEqual(result.status, 0);
    const evidence = JSON.parse(await readFile(output, "utf8"));
    assert.equal(evidence.scan.result, "failed");
    assert.match(evidence.scan.failureReasons.join("\n"), /exact reviewed image reference|container image/i);
  }
});
