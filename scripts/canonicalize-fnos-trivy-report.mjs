#!/usr/bin/env node

import { readFile, writeFile } from "node:fs/promises";
import process from "node:process";

import { gatewayPolicyPath } from "./fnos-gateway-policy.mjs";

function fail(message) {
  throw new Error(`fnOS Trivy report: ${message}`);
}

function nonemptyString(value) {
  return typeof value === "string" && value.trim().length > 0;
}

function validatePackage(result, resultIndex) {
  if (!Array.isArray(result.Packages) || result.Packages.length === 0) {
    fail(`Results[${resultIndex}].Packages must be a nonempty array`);
  }
  for (const [packageIndex, pkg] of result.Packages.entries()) {
    if (
      !pkg
      || typeof pkg !== "object"
      || Array.isArray(pkg)
      || !nonemptyString(pkg.Name)
      || !nonemptyString(pkg.Version)
    ) {
      fail(
        `Results[${resultIndex}].Packages[${packageIndex}] must be a Trivy package with nonempty Name and Version`,
      );
    }
  }
}

function validateVulnerabilities(result, resultIndex) {
  if (result.Vulnerabilities === null) return;
  if (!Array.isArray(result.Vulnerabilities)) {
    fail(`Results[${resultIndex}].Vulnerabilities must be an array or explicit null`);
  }
  for (const [findingIndex, finding] of result.Vulnerabilities.entries()) {
    if (
      !finding
      || typeof finding !== "object"
      || Array.isArray(finding)
      || !nonemptyString(finding.VulnerabilityID)
      || !nonemptyString(finding.PkgName)
      || !nonemptyString(finding.InstalledVersion)
      || !nonemptyString(finding.FixedVersion)
      || !["HIGH", "CRITICAL"].includes(finding.Severity)
    ) {
      fail(
        `Results[${resultIndex}].Vulnerabilities[${findingIndex}] must be a fixable High/Critical Trivy vulnerability with package identity and versions`,
      );
    }
  }
}

function parseArgs(argv) {
  const options = {};
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (value === undefined) fail(`${name || "argument"} requires a value`);
    if (name === "--input") options.input = value;
    else if (name === "--output") options.output = value;
    else if (name === "--scanner-exit-code") options.scannerExitCode = Number(value);
    else fail(`unknown argument ${name}`);
  }
  if (!options.input) fail("--input is required");
  if (!options.output) fail("--output is required");
  if (
    !Number.isInteger(options.scannerExitCode)
    || options.scannerExitCode < 0
    || options.scannerExitCode > 255
  ) {
    fail("--scanner-exit-code must be an integer from 0 through 255");
  }
  return options;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  let report;
  try {
    report = JSON.parse(await readFile(options.input, "utf8"));
  } catch (error) {
    fail(`input is unavailable or not valid JSON: ${error.message}`);
  }
  if (!report || typeof report !== "object" || Array.isArray(report)) {
    fail("input must be a Trivy report object");
  }
  if (!Array.isArray(report.Results) || report.Results.length === 0) {
    fail("input Results must be a nonempty array");
  }
  let reviewedGate;
  try {
    reviewedGate = JSON.parse(await readFile(gatewayPolicyPath, "utf8")).vulnerabilityGate;
  } catch (error) {
    fail(`reviewed gateway policy is unavailable or not valid JSON: ${error.message}`);
  }
  const reviewedOs = reviewedGate?.os;
  if (!nonemptyString(reviewedOs?.family) || !nonemptyString(reviewedOs?.version)) {
    fail("reviewed gateway policy must contain operating-system family and version evidence");
  }
  if (!nonemptyString(reviewedGate?.expectedTarget)) {
    fail("reviewed gateway policy must contain the exact Trivy result Target");
  }
  const expectedType = report.Metadata?.OS?.Family;
  if (!nonemptyString(expectedType) || !nonemptyString(report.Metadata?.OS?.Name)) {
    fail("input Metadata.OS Family and Name are required before canonicalizing OS-package results");
  }
  if (expectedType !== reviewedOs.family || report.Metadata.OS.Name !== reviewedOs.version) {
    fail("input operating-system evidence differs from the reviewed gateway policy");
  }

  for (const [index, result] of report.Results.entries()) {
    if (!result || typeof result !== "object" || Array.isArray(result)) {
      fail(`Results[${index}] must be an object`);
    }
    if (result.Target !== reviewedGate.expectedTarget) {
      fail(`Results[${index}].Target must equal the exact reviewed Target`);
    }
    if (
      result.Class !== "os-pkgs"
      || !nonemptyString(result.Type)
      || result.Type !== expectedType
    ) {
      fail(
        `Results[${index}] must be an OS-package result with Class os-pkgs and Type ${expectedType}`,
      );
    }
    validatePackage(result, index);
    if (Object.hasOwn(result, "Vulnerabilities")) {
      validateVulnerabilities(result, index);
      continue;
    }
    if (options.scannerExitCode !== 0) {
      fail(`cannot infer zero findings for Results[${index}] after nonzero scanner exit`);
    }
    // Trivy 0.70.0 uses json:",omitempty" for a nil vulnerability slice.
    // Exit zero plus a populated OS-package result is the narrow evidence that
    // the omitted field means no matching findings rather than scan failure.
    result.Vulnerabilities = null;
  }

  await writeFile(options.output, `${JSON.stringify(report, null, 2)}\n`);
}

try {
  await main();
} catch (error) {
  console.error(error.message);
  process.exit(1);
}
