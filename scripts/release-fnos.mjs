#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { validateChannelConfiguration, validateChannelImages } from "./fnos-registry-channel.mjs";

const repoRoot = fileURLToPath(new URL("..", import.meta.url));
const versionPattern = /^1\.\d+\.\d+-fnos\.\d+$/;
const releaseBranch = process.env.FNOS_RELEASE_BRANCH ?? "fnos/develop";
const releaseRepository = process.env.FNOS_RELEASE_REPOSITORY ?? "Zleap-AI/SAG";

function fail(message) {
  throw new Error(`release-fnos: ${message}`);
}

function parseArgs(argv) {
  const [command, ...rest] = argv;
  if (command !== "prepare" && command !== "package") fail("command must be prepare or package");
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
  const value = options[name];
  if (!value) fail(`--${name.replaceAll("_", "-")} is required`);
  return value;
}

function git(...args) {
  const result = spawnSync("git", args, { cwd: repoRoot, encoding: "utf8" });
  if (result.error || result.status !== 0) fail(`git ${args.join(" ")} failed: ${(result.stderr || result.error?.message || result.stdout).trim()}`);
  return result.stdout.trim();
}

function readPackageVersion() {
  const output = git("show", "HEAD:packages/fnos/sag/manifest");
  const match = /^version\s*=\s*(\S+)\s*$/m.exec(output);
  if (!match) fail("packages/fnos/sag/manifest must define version");
  return match[1];
}

async function prepare(options) {
  const version = requireOption(options, "version");
  const channel = requireOption(options, "channel");
  const candidateRunId = requireOption(options, "candidate_run_id");
  const cnRepositoryPrefix = options.cn_repository_prefix;
  const output = path.resolve(requireOption(options, "output"));
  if (!versionPattern.test(version)) fail("--version must match <major>.<minor>.<patch>-fnos.<number>");
  if (channel !== "global" && channel !== "cn") fail("--channel must be global or cn");
  validateChannelConfiguration({ channel, cnRepositoryPrefix });
  if (!/^\d+$/.test(candidateRunId)) fail("--candidate-run-id must be numeric");
  if (git("branch", "--show-current") !== releaseBranch) fail(`must run from ${releaseBranch}`);
  if (git("status", "--porcelain", "--untracked-files=no") !== "") fail("tracked worktree changes must be committed before release prepare");
  if (readPackageVersion() !== version) fail("--version must match packages/fnos/sag/manifest");
  if (existsSync(output)) fail("--output must not already exist");
  await mkdir(path.dirname(output), { recursive: true });
  const revision = git("rev-parse", "HEAD");
  const releaseInput = {
    schema_version: 1,
    appname: "sag",
    version,
    channel,
    ...(channel === "cn" ? { cn_repository_prefix: cnRepositoryPrefix } : {}),
    revision,
    candidate_tag: `fnos-candidate-${version}-${revision.slice(0, 12)}`,
    candidate_workflow: {
      run_id: candidateRunId,
      url: `https://github.com/${releaseRepository}/actions/runs/${candidateRunId}`,
    },
  };
  await writeFile(output, `${JSON.stringify(releaseInput, null, 2)}\n`, { mode: 0o600 });
  process.stdout.write(`release input written: ${output}\n`);
}

async function packageRelease(options) {
  const inputPath = requireOption(options, "input");
  const evidencePath = requireOption(options, "candidate_evidence");
  const output = path.resolve(requireOption(options, "output"));
  let input;
  let evidence;
  try {
    input = JSON.parse(await readFile(inputPath, "utf8"));
    evidence = JSON.parse(await readFile(evidencePath, "utf8"));
  } catch (error) {
    fail(`could not read release input or candidate evidence: ${error.message}`);
  }
  if (input?.appname !== "sag" || !versionPattern.test(input?.version ?? "")) fail("release input is invalid");
  validateChannelImages({
    channel: input.channel,
    cnRepositoryPrefix: input.cn_repository_prefix,
    api: evidence?.api,
    web: evidence?.web,
    gateway: evidence?.gateway,
  });
  if (existsSync(output)) fail("--output must not already exist");
  await mkdir(output, { recursive: true, mode: 0o700 });
  const packagePath = path.join(output, `sag-${input.version}.fpk`);
  const build = spawnSync(process.execPath, [
    path.join(repoRoot, "scripts/build-fnos-package.mjs"),
    "--api-image", evidence.api,
    "--web-image", evidence.web,
    "--nginx-image", evidence.gateway,
    "--output", packagePath,
  ], { cwd: repoRoot, encoding: "utf8" });
  if (build.error || build.status !== 0) fail(`FPK build failed: ${(build.stderr || build.error?.message || build.stdout).trim()}`);
  const checksum = spawnSync("shasum", ["-a", "256", packagePath], { encoding: "utf8" });
  if (checksum.error || checksum.status !== 0) fail(`could not calculate FPK SHA-256: ${(checksum.stderr || checksum.error?.message || checksum.stdout).trim()}`);
  const sha256 = checksum.stdout.trim().split(/\s+/)[0];
  if (!/^[a-f0-9]{64}$/.test(sha256)) fail("could not parse FPK SHA-256");
  const manifest = { ...input, images: { api: evidence.api, web: evidence.web, gateway: evidence.gateway }, fpk: { filename: path.basename(packagePath), sha256 } };
  await writeFile(path.join(output, "release-manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`, { mode: 0o600 });
  await writeFile(path.join(output, `${path.basename(packagePath)}.sha256`), `${sha256}  ${path.basename(packagePath)}\n`, { mode: 0o600 });
  process.stdout.write(`release package written: ${packagePath}\n`);
}

const options = parseArgs(process.argv.slice(2));
(options.command === "prepare" ? prepare(options) : packageRelease(options)).catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
