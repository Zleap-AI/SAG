#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { appendFile, writeFile } from "node:fs/promises";
import process from "node:process";

const digestPattern = /^sha256:[a-f0-9]{64}$/;

function fail(message) {
  throw new Error(`fnos-release-registry: ${message}`);
}

function parseArgs(argv) {
  const [command, ...rest] = argv;
  if (!command) fail("a command is required");
  const options = { command };
  for (let index = 0; index < rest.length; index += 2) {
    const name = rest[index];
    const value = rest[index + 1];
    if (!name?.startsWith("--") || !value || value.startsWith("--")) fail(`value required for ${name ?? "argument"}`);
    options[name.slice(2).replaceAll("-", "_")] = value;
  }
  return options;
}

function requireOption(options, name) {
  if (!options[name]) fail(`--${name.replaceAll("_", "-")} is required`);
  return options[name];
}

function requireDigest(value, label) {
  if (!digestPattern.test(value)) fail(`${label} must be an exact lowercase sha256 digest`);
  return value;
}

function docker(options, args) {
  const executable = requireOption(options, "docker");
  const result = spawnSync(executable, args, { encoding: "utf8" });
  if (result.error) fail(`could not run ${executable}: ${result.error.message}`);
  return result;
}

function exactDigest(output, label) {
  if (!/^sha256:[a-f0-9]{64}\r?\n?$/.test(output)) fail(`${label} did not emit exactly one sha256 digest`);
  return requireDigest(output.trim(), label);
}

function requireIndex(raw, image) {
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    fail(`${image} raw inspection did not return JSON: ${error.message}`);
  }
  const indexTypes = new Set([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
  ]);
  if (!indexTypes.has(parsed.mediaType) || !Array.isArray(parsed.manifests)) fail(`${image} must resolve to a multi-platform index`);
  const platforms = new Set(parsed.manifests.map(({ platform }) => `${platform?.os}/${platform?.architecture}`));
  for (const platform of ["linux/amd64", "linux/arm64"]) {
    if (!platforms.has(platform)) fail(`${image} index is missing ${platform}`);
  }
}

function verifyStaging(options) {
  const image = requireOption(options, "image");
  const stagingTag = requireOption(options, "staging_tag");
  const revision = requireOption(options, "revision");
  const version = requireOption(options, "version");
  const stagedReference = `${image}:${stagingTag}`;
  const resolution = docker(options, ["buildx", "imagetools", "inspect", "--format", "{{.Manifest.Digest}}", stagedReference]);
  if (resolution.status !== 0) fail(`could not resolve staging tag ${stagedReference}: ${(resolution.stderr || resolution.stdout).trim()}`);
  const digest = exactDigest(resolution.stdout, `staging tag ${stagedReference}`);
  const immutableReference = `${image}@${digest}`;
  const raw = docker(options, ["buildx", "imagetools", "inspect", "--raw", immutableReference]);
  if (raw.status !== 0) fail(`could not inspect ${immutableReference}: ${(raw.stderr || raw.stdout).trim()}`);
  requireIndex(raw.stdout, immutableReference);
  const pull = docker(options, ["pull", "--platform", "linux/amd64", immutableReference]);
  if (pull.status !== 0) fail(`could not pull ${immutableReference}: ${(pull.stderr || pull.stdout).trim()}`);
  const revisionResult = docker(options, ["image", "inspect", immutableReference, "--format", '{{ index .Config.Labels "org.opencontainers.image.revision" }}']);
  const versionResult = docker(options, ["image", "inspect", immutableReference, "--format", '{{ index .Config.Labels "org.opencontainers.image.version" }}']);
  if (revisionResult.status !== 0 || revisionResult.stdout.trim() !== revision) fail(`${immutableReference} revision label does not match the candidate commit`);
  if (versionResult.status !== 0 || versionResult.stdout.trim() !== version) fail(`${immutableReference} version label does not match the candidate version`);
  return digest;
}

async function writeHandoff(options) {
  const apiDigest = requireDigest(requireOption(options, "api_digest"), "api digest");
  const webDigest = requireDigest(requireOption(options, "web_digest"), "web digest");
  const githubOutput = requireOption(options, "github_output");
  const artifact = requireOption(options, "artifact");
  await appendFile(githubOutput, `api_digest=${apiDigest}\nweb_digest=${webDigest}\n`);
  await writeFile(artifact, `${JSON.stringify({ api_digest: apiDigest, web_digest: webDigest })}\n`);
}

function escapeRegex(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function inspectFinal(options, reference) {
  const result = docker(options, ["buildx", "imagetools", "inspect", "--format", "{{.Manifest.Digest}}", reference]);
  if (result.status === 0) return { state: "present", digest: exactDigest(result.stdout, `final tag ${reference}`) };
  const message = `${result.stderr}${result.stdout}`.trim();
  if (new RegExp(`^ERROR: ${escapeRegex(reference)}: not found$`).test(message)) return { state: "absent" };
  fail(`could not inspect final tag ${reference}: ${message}`);
}

function reconcile(options, image, tag, expected) {
  const reference = `${image}:${tag}`;
  let observed = inspectFinal(options, reference);
  if (observed.state === "present") {
    if (observed.digest !== expected) fail(`final tag ${reference} exists but points to a different digest (${observed.digest})`);
    return;
  }
  observed = inspectFinal(options, reference);
  if (observed.state === "present") {
    if (observed.digest !== expected) fail(`final tag ${reference} appeared with a different digest (${observed.digest})`);
    return;
  }
  const created = docker(options, ["buildx", "imagetools", "create", "--tag", reference, `${image}@${expected}`]);
  if (created.status !== 0) fail(`could not create final tag ${reference}: ${(created.stderr || created.stdout).trim()}`);
}

function promote(options) {
  const apiImage = requireOption(options, "api_image");
  const webImage = requireOption(options, "web_image");
  const candidateVersion = requireOption(options, "candidate_version");
  const commitTag = requireOption(options, "commit_tag");
  const apiDigest = requireDigest(requireOption(options, "api_digest"), "api digest");
  const webDigest = requireDigest(requireOption(options, "web_digest"), "web digest");
  const finalTags = [
    [apiImage, candidateVersion, apiDigest], [apiImage, commitTag, apiDigest],
    [webImage, candidateVersion, webDigest], [webImage, commitTag, webDigest],
  ];
  for (const [image, tag, digest] of finalTags) reconcile(options, image, tag, digest);
  for (const [image, tag, digest] of finalTags) {
    const observed = inspectFinal(options, `${image}:${tag}`);
    if (observed.state !== "present" || observed.digest !== digest) fail(`post-verify final tag ${image}:${tag} does not resolve to ${digest}`);
  }
}

function resolvePublicTag(options, image, candidateVersion, expectedDigest) {
  const reference = `${image}:${candidateVersion}`;
  const result = docker(options, ["buildx", "imagetools", "inspect", "--format", "{{.Manifest.Digest}}", reference]);
  if (result.status !== 0) fail(`could not anonymously resolve ${reference}: ${(result.stderr || result.stdout).trim()}`);
  const observedDigest = exactDigest(result.stdout, `public candidate tag ${reference}`);
  if (observedDigest !== expectedDigest) {
    fail(`${reference} does not resolve to the captured digest ${expectedDigest}`);
  }
}

function inspectPublicDigest(options, image, digest) {
  const reference = `${image}@${digest}`;
  const result = docker(options, ["buildx", "imagetools", "inspect", "--raw", reference]);
  if (result.status !== 0) fail(`could not anonymously inspect ${reference}: ${(result.stderr || result.stdout).trim()}`);
  requireIndex(result.stdout, reference);
}

function verifyPublic(options) {
  const apiImage = requireOption(options, "api_image");
  const webImage = requireOption(options, "web_image");
  const candidateVersion = requireOption(options, "candidate_version");
  const apiDigest = requireDigest(requireOption(options, "api_digest"), "api digest");
  const webDigest = requireDigest(requireOption(options, "web_digest"), "web digest");

  resolvePublicTag(options, apiImage, candidateVersion, apiDigest);
  resolvePublicTag(options, webImage, candidateVersion, webDigest);
  inspectPublicDigest(options, apiImage, apiDigest);
  inspectPublicDigest(options, webImage, webDigest);
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.command === "verify-staging") {
    process.stdout.write(`${verifyStaging(options)}\n`);
    return;
  }
  if (options.command === "write-handoff") {
    await writeHandoff(options);
    return;
  }
  if (options.command === "promote") {
    promote(options);
    return;
  }
  if (options.command === "verify-public") {
    verifyPublic(options);
    return;
  }
  fail(`unknown command: ${options.command}`);
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
