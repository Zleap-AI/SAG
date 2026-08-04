#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("..", import.meta.url));
export const gatewayPolicyPath = path.join(repoRoot, "packages/fnos/gateway-policy.json");
const digestPattern = /^sha256:[a-f0-9]{64}$/;
const revisionPattern = /^[a-f0-9]{40}$/;
const rfc3339Pattern = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$/;
const maximumReviewAgeMilliseconds = 30 * 24 * 60 * 60 * 1000;
const indexMediaTypes = new Set([
  "application/vnd.oci.image.index.v1+json",
  "application/vnd.docker.distribution.manifest.list.v2+json",
]);

function invariant(condition, message) {
  if (!condition) throw new Error(`fnOS gateway policy: ${message}`);
}

function isLeapYear(year) {
  return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
}

export function parseRfc3339Timestamp(
  value,
  { label = "timestamp", utcOnly = false, maximumFractionDigits = 9 } = {},
) {
  const match = typeof value === "string" ? rfc3339Pattern.exec(value) : null;
  if (!match) throw new Error(`${label} must be a strict RFC3339 timestamp`);
  const [
    , yearText, monthText, dayText, hourText, minuteText, secondText, fraction = "", zone,
  ] = match;
  if (utcOnly && zone !== "Z") throw new Error(`${label} must be a strict RFC3339 UTC timestamp ending in Z`);
  if (fraction.length - 1 > maximumFractionDigits) {
    throw new Error(`${label} exceeds supported millisecond precision`);
  }

  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  const monthLengths = [
    31, isLeapYear(year) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31,
  ];
  if (
    year < 1
    || month < 1
    || month > 12
    || day < 1
    || day > monthLengths[month - 1]
    || hour > 23
    || minute > 59
    || second > 59
  ) {
    throw new Error(`${label} must be a valid round-trip RFC3339 timestamp`);
  }

  let offsetMinutes = 0;
  if (zone !== "Z") {
    const offsetHour = Number(zone.slice(1, 3));
    const offsetMinute = Number(zone.slice(4, 6));
    if (offsetHour > 23 || offsetMinute > 59) {
      throw new Error(`${label} must have a valid RFC3339 timezone offset`);
    }
    offsetMinutes = (offsetHour * 60 + offsetMinute) * (zone[0] === "+" ? 1 : -1);
  }
  const milliseconds = Number((fraction.slice(1) + "000").slice(0, 3));
  const instant = new Date(0);
  instant.setUTCFullYear(year, month - 1, day);
  instant.setUTCHours(hour, minute, second, milliseconds);
  return instant.getTime() - offsetMinutes * 60_000;
}

export function validateGatewayPolicy(policy, now = new Date()) {
  invariant(policy && typeof policy === "object", "policy must be an object");
  invariant(policy.schemaVersion === 1, "schemaVersion must be 1");

  const { image, requiredPlatforms, review, vulnerabilityGate } = policy;
  invariant(image?.repository === "ghcr.io/zleap-ai/sag-gateway", "repository must be ghcr.io/zleap-ai/sag-gateway");
  invariant(typeof image.tag === "string" && image.tag.length > 0, "tag is required");
  invariant(typeof image.upstreamTag === "string" && image.upstreamTag.length > 0, "upstreamTag is required");
  invariant(digestPattern.test(image.indexDigest), "indexDigest must be an exact lowercase sha256 digest");
  const exactReference = `${image.repository}:${image.tag}@${image.indexDigest}`;
  invariant(image.reference === exactReference, "image reference must bind repository, tag, and index digest");

  invariant(Array.isArray(requiredPlatforms) && requiredPlatforms.length >= 2, "requiredPlatforms must include amd64 and arm64");
  const expectedPlatforms = new Set(["linux/amd64", "linux/arm64"]);
  const seenPlatforms = new Set();
  for (const entry of requiredPlatforms) {
    const platform = `${entry?.os}/${entry?.architecture}`;
    invariant(expectedPlatforms.has(platform), `unsupported required platform ${platform}`);
    invariant(!seenPlatforms.has(platform), `duplicate required platform ${platform}`);
    invariant(digestPattern.test(entry.manifestDigest), `${platform} manifestDigest must be an exact lowercase sha256 digest`);
    invariant(revisionPattern.test(entry.upstreamRevision), `${platform} upstreamRevision must be a lowercase full Git revision`);
    seenPlatforms.add(platform);
  }
  for (const platform of expectedPlatforms) {
    invariant(seenPlatforms.has(platform), `requiredPlatforms is missing ${platform}`);
  }

  invariant(review && typeof review === "object", "review metadata is required");
  let reviewed;
  let expires;
  try {
    reviewed = parseRfc3339Timestamp(review.reviewedAt, {
      label: "reviewedAt",
      utcOnly: true,
      maximumFractionDigits: 3,
    });
    expires = parseRfc3339Timestamp(review.expiresAt, {
      label: "expiresAt",
      utcOnly: true,
      maximumFractionDigits: 3,
    });
  } catch (error) {
    invariant(false, error.message);
  }
  invariant(
    review.maximumAgeHours === 720,
    "maximumAgeHours must be exactly 720",
  );
  invariant(expires > reviewed, "expiresAt must be after reviewedAt");
  invariant(
    expires - reviewed <= maximumReviewAgeMilliseconds,
    "review window must not exceed exactly 30 days (30*24h)",
  );
  const nowMilliseconds = now instanceof Date ? now.getTime() : Number.NaN;
  invariant(Number.isFinite(nowMilliseconds), "current time must be a valid Date");
  invariant(nowMilliseconds >= reviewed, "review timestamp is in the future");
  invariant(nowMilliseconds < expires, `review expired at ${review.expiresAt}`);
  invariant(Array.isArray(review.sources) || review.sources === undefined, "review sources must be an array");

  invariant(vulnerabilityGate?.scanner === "aquasecurity/trivy", "scanner must be aquasecurity/trivy");
  invariant(/^\d+\.\d+\.\d+$/.test(vulnerabilityGate.scannerVersion), "scannerVersion must be explicit");
  invariant(vulnerabilityGate.platform === "linux/amd64", "scan platform must be linux/amd64");
  invariant(
    Array.isArray(vulnerabilityGate.severities)
      && vulnerabilityGate.severities.length === 2
      && vulnerabilityGate.severities.includes("CRITICAL")
      && vulnerabilityGate.severities.includes("HIGH"),
    "scan severities must be exactly CRITICAL and HIGH",
  );
  invariant(vulnerabilityGate.ignoreUnfixed === true, "scan must gate fixable findings with ignoreUnfixed");
  invariant(vulnerabilityGate.exitCodeOnFindings === 1, "scan must exit 1 on findings");
  invariant(vulnerabilityGate.result === "passed", "vulnerability scan result must be passed");
  invariant(vulnerabilityGate.fixableFindings === 0, "vulnerability scan must record zero fixable findings");
  let scanned;
  try {
    scanned = parseRfc3339Timestamp(vulnerabilityGate.scannedAt, {
      label: "scannedAt",
      utcOnly: true,
    });
  } catch (error) {
    invariant(false, error.message);
  }
  invariant(scanned >= reviewed && scanned < expires, "scanner evidence must fall within the review window");
  invariant(
    digestPattern.test(`sha256:${vulnerabilityGate.linuxAmd64ArchiveSha256}`),
    "Linux scanner archive SHA-256 is required",
  );
  invariant(
    digestPattern.test(`sha256:${vulnerabilityGate.macosArm64ArchiveSha256}`),
    "macOS scanner archive SHA-256 is required",
  );
  invariant(digestPattern.test(vulnerabilityGate.imageId), "scanned image ID is required");
  invariant(
    typeof vulnerabilityGate.os?.family === "string"
      && vulnerabilityGate.os.family.length > 0
      && typeof vulnerabilityGate.os?.version === "string"
      && vulnerabilityGate.os.version.length > 0,
    "scanned operating-system evidence is required",
  );
  const expectedResultTarget = `${exactReference} (${vulnerabilityGate.os.family} ${vulnerabilityGate.os.version})`;
  invariant(
    vulnerabilityGate.expectedTarget === expectedResultTarget,
    "expectedTarget must bind the exact image reference and reviewed operating-system evidence",
  );
  invariant(
    typeof vulnerabilityGate.sourceReportSha256 === "string"
      && /^[a-f0-9]{64}$/.test(vulnerabilityGate.sourceReportSha256),
    "source report SHA-256 is required",
  );

  return exactReference;
}

export function validateGatewayImageReference(policy, reference) {
  const expected = policy?.image?.reference;
  invariant(reference === expected, `gateway must use the reviewed gateway reference ${expected}`);
  return reference;
}

export function validateGatewayIndex(policy, index) {
  invariant(
    index && indexMediaTypes.has(index.mediaType) && Array.isArray(index.manifests),
    "gateway image must resolve to a multi-platform image index",
  );
  for (const expected of policy.requiredPlatforms) {
    const platformName = `${expected.os}/${expected.architecture}`;
    const matches = index.manifests.filter(({ platform }) => (
      platform?.os === expected.os && platform?.architecture === expected.architecture
    ));
    invariant(matches.length === 1, `${platformName} must occur exactly once in the raw image index`);
    const [actual] = matches;
    invariant(
      actual.digest === expected.manifestDigest,
      `${platformName} manifest digest differs from the reviewed policy`,
    );
    // The copied GHCR index is pinned by its own digest. OCI registry copies
    // do not consistently retain descriptor annotations, so the review record
    // retains upstream provenance while the exact index digest guards runtime
    // content identity.
  }
}

export async function loadGatewayPolicy(now = new Date()) {
  let policy;
  try {
    policy = JSON.parse(await readFile(gatewayPolicyPath, "utf8"));
  } catch (error) {
    throw new Error(`fnOS gateway policy: could not read ${gatewayPolicyPath}: ${error.message}`);
  }
  validateGatewayPolicy(policy, now);
  return policy;
}

function inspectRaw(reference, docker = "docker") {
  const result = spawnSync(docker, ["buildx", "imagetools", "inspect", "--raw", reference], {
    encoding: "utf8",
  });
  if (result.error) throw new Error(`could not run ${docker}: ${result.error.message}`);
  if (result.status !== 0) throw new Error(`${docker} failed: ${(result.stderr || result.stdout).trim()}`);
  try {
    return JSON.parse(result.stdout);
  } catch (error) {
    throw new Error(`image inspection did not return JSON: ${error.message}`);
  }
}

async function cli() {
  const [command, ...args] = process.argv.slice(2);
  invariant(command === "verify", "usage: fnos-gateway-policy.mjs verify [--docker <command>]");
  let docker = "docker";
  if (args.length > 0) {
    invariant(args.length === 2 && args[0] === "--docker" && args[1], "only --docker <command> is supported");
    docker = args[1];
  }
  const policy = await loadGatewayPolicy();
  const reference = validateGatewayImageReference(policy, policy.image.reference);
  validateGatewayIndex(policy, inspectRaw(reference, docker));
  console.log(reference);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    await cli();
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
}
