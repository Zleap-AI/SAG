import assert from "node:assert/strict";
import test from "node:test";

import {
  validateGatewayImageReference,
  validateGatewayIndex,
  validateGatewayPolicy,
} from "../fnos-gateway-policy.mjs";

const digest = "sha256:758f0377a23257333a8957eb5d1f67ccc4b84dfc8a5c3f939e440b087076453c";
const revision = "ccdab6c99ae2e2fc53a144dc68d6b8f44163adf2";
const reference = `ghcr.1ms.run/zleap-ai/sag-gateway:1.5.0-fnos.1@${digest}`;
const expectedTarget = `${reference} (alpine 3.24.1)`;

function reviewedPolicy(overrides = {}) {
  return {
    schemaVersion: 1,
    image: {
      repository: "ghcr.1ms.run/zleap-ai/sag-gateway",
      tag: "1.5.0-fnos.1",
      upstreamTag: "1.30.4-alpine",
      indexDigest: digest,
      reference,
    },
    requiredPlatforms: [
      {
        os: "linux",
        architecture: "amd64",
        manifestDigest: `sha256:${"8".repeat(64)}`,
        upstreamRevision: revision,
      },
      {
        os: "linux",
        architecture: "arm64",
        manifestDigest: `sha256:${"d".repeat(64)}`,
        upstreamRevision: revision,
      },
    ],
    review: {
      reviewedAt: "2026-07-29T08:33:41Z",
      expiresAt: "2026-08-28T08:33:41Z",
      maximumAgeHours: 720,
    },
    vulnerabilityGate: {
      scanner: "aquasecurity/trivy",
      scannerVersion: "0.70.0",
      linuxAmd64ArchiveSha256: "8b4376d5d6befe5c24d503f10ff136d9e0c49f9127a4279fd110b727929a5aa9",
      macosArm64ArchiveSha256: "68e543c51dcc96e1c344053a4fde9660cf602c25565d9f09dc17dd41e13b838a",
      platform: "linux/amd64",
      severities: ["CRITICAL", "HIGH"],
      ignoreUnfixed: true,
      exitCodeOnFindings: 1,
      scannedAt: "2026-07-29T08:33:41.448549Z",
      result: "passed",
      fixableFindings: 0,
      imageId: `sha256:${"6".repeat(64)}`,
      os: { family: "alpine", version: "3.24.1" },
      expectedTarget,
      sourceReportSha256: "9".repeat(64),
    },
    ...overrides,
  };
}

function reviewedIndex({ amd64Revision = revision, amd64Digest = `sha256:${"8".repeat(64)}` } = {}) {
  return {
    schemaVersion: 2,
    mediaType: "application/vnd.oci.image.index.v1+json",
    manifests: [
      {
        digest: amd64Digest,
        platform: { os: "linux", architecture: "amd64" },
        annotations: {
          "org.opencontainers.image.revision": amd64Revision,
          "org.opencontainers.image.version": "1.30.4-alpine",
        },
      },
      {
        digest: `sha256:${"d".repeat(64)}`,
        platform: { os: "linux", architecture: "arm64", variant: "v8" },
        annotations: {
          "org.opencontainers.image.revision": revision,
          "org.opencontainers.image.version": "1.30.4-alpine",
        },
      },
    ],
  };
}

test("accepts a current reviewed gateway policy and its exact OCI index metadata", () => {
  const policy = reviewedPolicy();
  assert.equal(validateGatewayPolicy(policy, new Date("2026-07-30T12:00:00Z")), reference);
  assert.doesNotThrow(() => validateGatewayIndex(policy, reviewedIndex()));
});

test("rejects an arbitrary Nginx digest that was not reviewed", () => {
  assert.throws(
    () => validateGatewayImageReference(
      reviewedPolicy(),
      `ghcr.1ms.run/zleap-ai/sag-gateway:1.5.0-fnos.1@sha256:${"f".repeat(64)}`,
    ),
    /reviewed gateway reference/i,
  );
});

test("rejects a gateway policy without recorded scanner approval", () => {
  const policy = reviewedPolicy({
    vulnerabilityGate: {
      ...reviewedPolicy().vulnerabilityGate,
      result: "pending",
    },
  });
  assert.throws(() => validateGatewayPolicy(policy, new Date("2026-07-30T12:00:00Z")), /scan.*passed/i);
});

test("rejects scanner approval without exact artifact evidence", () => {
  const policy = reviewedPolicy({
    vulnerabilityGate: {
      ...reviewedPolicy().vulnerabilityGate,
      sourceReportSha256: undefined,
    },
  });
  assert.throws(
    () => validateGatewayPolicy(policy, new Date("2026-07-30T12:00:00Z")),
    /source report sha-256/i,
  );
});

test("rejects missing or mismatched exact Trivy result target evidence", () => {
  for (const target of [
    undefined,
    reference,
    `${expectedTarget}-suffix`,
    expectedTarget.toUpperCase(),
  ]) {
    const policy = reviewedPolicy({
      vulnerabilityGate: {
        ...reviewedPolicy().vulnerabilityGate,
        expectedTarget: target,
      },
    });
    assert.throws(
      () => validateGatewayPolicy(policy, new Date("2026-07-30T12:00:00Z")),
      /expectedTarget|result target/i,
    );
  }
});

test("accepts one millisecond before expiry and rejects the exact expiry instant", () => {
  assert.equal(
    validateGatewayPolicy(reviewedPolicy(), new Date("2026-08-28T08:33:40.999Z")),
    reference,
  );
  assert.throws(
    () => validateGatewayPolicy(reviewedPolicy(), new Date("2026-08-28T08:33:41.000Z")),
    /expired/i,
  );
});

test("accepts exactly thirty times twenty-four hours and rejects one millisecond more", () => {
  assert.equal(
    validateGatewayPolicy(reviewedPolicy(), new Date("2026-07-30T12:00:00Z")),
    reference,
  );
  const policy = reviewedPolicy({
    review: {
      reviewedAt: "2026-07-29T08:33:41Z",
      expiresAt: "2026-08-28T08:33:41.001Z",
      maximumAgeHours: 720,
    },
  });
  assert.throws(() => validateGatewayPolicy(policy, new Date("2026-07-30T12:00:00Z")), /30 days/i);
});

test("rejects impossible or non-UTC review timestamps", () => {
  for (const review of [
    {
      reviewedAt: "2026-02-30T08:33:41Z",
      expiresAt: "2026-03-30T08:33:41Z",
      maximumAgeHours: 720,
    },
    {
      reviewedAt: "2026-07-29T16:33:41+08:00",
      expiresAt: "2026-08-28T16:33:41+08:00",
      maximumAgeHours: 720,
    },
    {
      reviewedAt: "2026-07-29T08:33:41.0001Z",
      expiresAt: "2026-08-28T08:33:41.0001Z",
      maximumAgeHours: 720,
    },
  ]) {
    assert.throws(
      () => validateGatewayPolicy(reviewedPolicy({ review }), new Date("2026-07-30T12:00:00Z")),
      /RFC3339 UTC|valid|precision/i,
    );
  }
});

test("rejects a zero-length review window", () => {
  const instant = "2026-07-29T08:33:41Z";
  assert.throws(
    () => validateGatewayPolicy(reviewedPolicy({
      review: { reviewedAt: instant, expiresAt: instant, maximumAgeHours: 720 },
    }), new Date("2026-07-29T08:33:41Z")),
    /after reviewedAt/i,
  );
});

test("uses absolute UTC instants rather than the Asia Shanghai calendar date", () => {
  assert.equal(
    validateGatewayPolicy(reviewedPolicy(), new Date("2026-08-28T00:00:00+08:00")),
    reference,
  );
});

test("rejects raw platform metadata whose manifest digest differs from the review", () => {
  assert.throws(
    () => validateGatewayIndex(reviewedPolicy(), reviewedIndex({ amd64Digest: `sha256:${"a".repeat(64)}` })),
    /linux\/amd64.*manifest digest/i,
  );
});
