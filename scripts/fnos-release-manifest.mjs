#!/usr/bin/env node

import { readFile } from "node:fs/promises";
import process from "node:process";

import { validateChannelImages } from "./fnos-registry-channel.mjs";

const revisionPattern = /^[a-f0-9]{40}$/;
const sha256Pattern = /^[a-f0-9]{64}$/;
const versionPattern = /^1\.\d+\.\d+-fnos\.\d+$/;
const workflowUrlPattern = /^https:\/\/github\.com\/Zleap-AI\/SAG\/actions\/runs\/\d+$/;
const buildIdPattern = /^\d{8}\.\d{6}Z$/;
const utcTimestampPattern = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

function fail(message) {
  throw new Error(`fnos-release-manifest: ${message}`);
}

function parseArgs(argv) {
  if (argv.length !== 3 || argv[0] !== "validate" || argv[1] !== "--input") {
    fail("usage: validate --input <release-manifest.json>");
  }
  return argv[2];
}

function requireObject(value, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail(`${name} must be an object`);
  return value;
}

export function validateReleaseManifest(value) {
  const manifest = requireObject(value, "release manifest");
  if (manifest.schema_version !== 1) fail("schema_version must be 1");
  if (manifest.appname !== "sag") fail("appname must be sag");
  if (typeof manifest.version !== "string" || !versionPattern.test(manifest.version)) {
    fail("version must match <major>.<minor>.<patch>-fnos.<number>");
  }
  if (typeof manifest.revision !== "string" || !revisionPattern.test(manifest.revision)) {
    fail("revision must be a lowercase 40-character commit SHA");
  }
  if (typeof manifest.build_id !== "string" || !buildIdPattern.test(manifest.build_id)) {
    fail("build_id must match YYYYMMDD.HHMMSSZ");
  }
  if (typeof manifest.built_at_utc !== "string" || !utcTimestampPattern.test(manifest.built_at_utc)) {
    fail("built_at_utc must be an RFC3339 UTC timestamp with second precision");
  }
  if (manifest.source_branch !== "fnos/develop") fail("source_branch must be fnos/develop");
  const workflow = requireObject(manifest.candidate_workflow, "candidate_workflow");
  if (typeof workflow.run_id !== "string" || !/^\d+$/.test(workflow.run_id)) fail("candidate workflow run_id must be numeric");
  if (typeof workflow.url !== "string" || !workflowUrlPattern.test(workflow.url)) fail("candidate workflow URL is invalid");
  if (!workflow.url.endsWith(`/${workflow.run_id}`)) fail("candidate workflow URL must end with run_id");

  const images = requireObject(manifest.images, "images");
  validateChannelImages({
    channel: manifest.channel,
    cnRepositoryPrefix: manifest.cn_repository_prefix,
    api: images.api,
    web: images.web,
    gateway: images.gateway,
  });

  const fpk = requireObject(manifest.fpk, "fpk");
  if (fpk.filename !== `sag-${manifest.version}.fpk`) fail("FPK filename must match version");
  if (typeof fpk.sha256 !== "string" || !sha256Pattern.test(fpk.sha256)) fail("FPK sha256 must be lowercase hex");
  return manifest;
}

async function main() {
  const input = parseArgs(process.argv.slice(2));
  let parsed;
  try {
    parsed = JSON.parse(await readFile(input, "utf8"));
  } catch (error) {
    fail(`could not read JSON input: ${error.message}`);
  }
  process.stdout.write(`${JSON.stringify(validateReleaseManifest(parsed))}\n`);
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
