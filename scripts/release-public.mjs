#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { createInterface } from "node:readline/promises";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const publicRemote = process.env.SAG_PUBLIC_REMOTE || "origin";
const publicRepository = process.env.SAG_PUBLIC_REPOSITORY || "Zleap-AI/SAG";
const releaseBranch = process.env.SAG_RELEASE_BRANCH || "main";
const releaseFiles = [
  "CHANGELOG.md",
  "README.md",
  "README-CN.md",
  "apps/api/sag_api/__init__.py",
  "apps/desktop/package.json",
  "apps/desktop/package-lock.json",
  "apps/web/package.json",
  "apps/web/package-lock.json",
];

function fail(message) {
  console.error(`release: ${message}`);
  process.exit(1);
}

function run(command, args, { capture = false, allowFailure = false } = {}) {
  const result = spawnSync(command, args, {
    cwd: repoRoot,
    encoding: "utf8",
    stdio: capture ? ["ignore", "pipe", "pipe"] : "inherit",
  });
  if (result.error) fail(`${command} failed: ${result.error.message}`);
  if (result.status !== 0 && !allowFailure) {
    const detail = capture ? result.stderr.trim() : "";
    fail(`${command} ${args.join(" ")} failed${detail ? `: ${detail}` : ""}`);
  }
  return result;
}

function git(args, options) {
  return run("git", args, options);
}

function gitOutput(args) {
  return git(args, { capture: true }).stdout.trim();
}

function gitLines(args) {
  const output = gitOutput(args);
  return output ? output.split(/\r?\n/).filter(Boolean) : [];
}

function normalizeVersion(value) {
  const version = String(value || "").replace(/^v/, "");
  if (!/^\d+\.\d+\.\d+$/.test(version)) {
    fail(`expected a stable semantic version such as 1.3.0, received ${value || "nothing"}`);
  }
  return version;
}

function compareVersions(left, right) {
  const a = left.split(".").map(Number);
  const b = right.split(".").map(Number);
  for (let index = 0; index < 3; index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return 0;
}

function readText(relativePath) {
  return readFileSync(path.join(repoRoot, relativePath), "utf8");
}

function writeText(relativePath, value) {
  writeFileSync(path.join(repoRoot, relativePath), value, "utf8");
}

function readJson(relativePath) {
  return JSON.parse(readText(relativePath));
}

function writeJson(relativePath, value) {
  writeText(relativePath, `${JSON.stringify(value, null, 2)}\n`);
}

function assertJsonVersion(relativePath, version, { lockfile = false } = {}) {
  const value = readJson(relativePath);
  if (value.version !== version) {
    fail(`${relativePath} has version ${value.version || "<missing>"}, expected ${version}`);
  }
  if (lockfile && value.packages?.[""]?.version !== version) {
    fail(`${relativePath} root package has version ${value.packages?.[""]?.version || "<missing>"}, expected ${version}`);
  }
}

function apiVersion() {
  const match = /^__version__ = "(\d+\.\d+\.\d+)"$/m.exec(
    readText("apps/api/sag_api/__init__.py"),
  );
  if (!match) fail("apps/api/sag_api/__init__.py has no stable __version__");
  return match[1];
}

function assertReleaseMetadata(version) {
  assertJsonVersion("apps/desktop/package.json", version);
  assertJsonVersion("apps/desktop/package-lock.json", version, { lockfile: true });
  assertJsonVersion("apps/web/package.json", version);
  assertJsonVersion("apps/web/package-lock.json", version, { lockfile: true });
  if (apiVersion() !== version) {
    fail(`apps/api/sag_api/__init__.py has version ${apiVersion()}, expected ${version}`);
  }

  for (const readme of ["README.md", "README-CN.md"]) {
    if (!readText(readme).includes(`SAG-v${version}-18181b`)) {
      fail(`${readme} does not contain the v${version} version badge`);
    }
  }

  const heading = new RegExp(`^## v${version.replaceAll(".", "\\.")} · \\d{4}-\\d{2}-\\d{2}$`, "m");
  if (!heading.test(readText("CHANGELOG.md"))) {
    fail(`CHANGELOG.md does not contain a dated v${version} release section`);
  }
}

function releaseNotes(version) {
  const changelog = readText("CHANGELOG.md");
  const heading = new RegExp(`^## v${version.replaceAll(".", "\\.")} · [^\\n]+$`, "m");
  const match = heading.exec(changelog);
  if (!match) fail(`CHANGELOG.md has no release notes for v${version}`);
  const remainder = changelog.slice(match.index + match[0].length).replace(/^\s+/, "");
  const nextHeading = remainder.search(/^## /m);
  const notes = (nextHeading === -1 ? remainder : remainder.slice(0, nextHeading)).trim();
  if (!notes) fail(`CHANGELOG.md release notes for v${version} are empty`);
  return `${notes}\n`;
}

function bumpJsonVersion(relativePath, version, { lockfile = false } = {}) {
  const value = readJson(relativePath);
  value.version = version;
  if (lockfile) {
    if (!value.packages?.[""]) fail(`${relativePath} has no root package entry`);
    value.packages[""].version = version;
  }
  writeJson(relativePath, value);
}

function bumpApiVersion(currentVersion, nextVersion) {
  const relativePath = "apps/api/sag_api/__init__.py";
  const current = `__version__ = "${currentVersion}"`;
  const contents = readText(relativePath);
  if (!contents.includes(current)) fail(`${relativePath} does not contain ${current}`);
  writeText(relativePath, contents.replace(current, `__version__ = "${nextVersion}"`));
}

function replaceVersionBadge(relativePath, currentVersion, nextVersion) {
  const current = `SAG-v${currentVersion}-18181b`;
  const next = `SAG-v${nextVersion}-18181b`;
  const contents = readText(relativePath);
  if (!contents.includes(current)) fail(`${relativePath} does not contain the ${currentVersion} version badge`);
  writeText(relativePath, contents.replaceAll(current, next));
}

function updateChangelog(version) {
  const changelog = readText("CHANGELOG.md");
  if (new RegExp(`^## v${version.replaceAll(".", "\\.")} · `, "m").test(changelog)) {
    fail(`CHANGELOG.md already contains v${version}`);
  }
  const unreleased = /^## Unreleased[^\n]*\n([\s\S]*?)(?=^## )/m.exec(changelog);
  if (!unreleased || !unreleased[1].trim()) fail("CHANGELOG.md Unreleased section is empty");
  const date = new Date().toISOString().slice(0, 10);
  writeText(
    "CHANGELOG.md",
    changelog.replace(/^## Unreleased[^\n]*\n/m, `## Unreleased\n\n## v${version} · ${date}\n`),
  );
}

function remoteMatchesRepository(remoteUrl) {
  const normalized = remoteUrl.replace(/\/$/, "").replace(/\.git$/, "");
  return normalized.endsWith(`/${publicRepository}`) || normalized.endsWith(`:${publicRepository}`);
}

function assertPublicRemote() {
  const fetchUrl = gitOutput(["remote", "get-url", publicRemote]);
  const pushUrl = gitOutput(["remote", "get-url", "--push", publicRemote]);
  for (const [kind, remoteUrl] of [["fetch", fetchUrl], ["push", pushUrl]]) {
    if (!remoteMatchesRepository(remoteUrl)) {
      fail(`${publicRemote} ${kind} URL points to ${remoteUrl}; expected ${publicRepository}`);
    }
  }
  return fetchUrl;
}

function assertPublicHistory(remoteBranchRef) {
  if (git(["merge-base", "--is-ancestor", remoteBranchRef, "HEAD"], { allowFailure: true }).status !== 0) {
    fail(`local ${releaseBranch} does not contain ${publicRemote}/${releaseBranch}; reconcile before releasing`);
  }
  const localRoots = gitLines(["rev-list", "--max-parents=0", "HEAD"]).sort();
  const remoteRoots = gitLines(["rev-list", "--max-parents=0", remoteBranchRef]).sort();
  if (localRoots.join("\n") !== remoteRoots.join("\n")) {
    fail("local history contains roots outside the public repository; release from an independent public clone");
  }
}

function stableTagsMergedInto(ref) {
  return gitLines(["tag", "--merged", ref, "--list", "v*.*.*", "--sort=-version:refname"])
    .filter((tag) => /^v\d+\.\d+\.\d+$/.test(tag));
}

function highestStableTag(ref) {
  return stableTagsMergedInto(ref)[0] || null;
}

async function confirmRelease(tag, remoteUrl, assumeYes) {
  if (assumeYes) return;
  if (!process.stdin.isTTY || !process.stdout.isTTY) {
    fail("interactive confirmation is unavailable; pass --yes to confirm explicitly");
  }
  console.log(`\nRelease plan:`);
  console.log(`  source: ${releaseBranch} at ${gitOutput(["rev-parse", "--short", "HEAD"])}`);
  console.log(`  target: ${publicRemote} (${remoteUrl})`);
  console.log(`  tag:    ${tag}`);
  console.log("  action: bump versions, commit, create an annotated tag, and atomically push main + tag");
  const prompt = createInterface({ input: process.stdin, output: process.stdout });
  const answer = await prompt.question(`\nType ${tag} to continue: `);
  prompt.close();
  if (answer.trim() !== tag) fail("release cancelled");
}

async function prepareRelease(rawVersion, flags) {
  const version = normalizeVersion(rawVersion);
  const tag = `v${version}`;
  const dryRun = flags.has("--dry-run");
  const noPush = flags.has("--no-push");
  const assumeYes = flags.has("--yes");

  const status = gitOutput(["status", "--porcelain=v1", "--untracked-files=all"]);
  if (status) fail("working tree must be clean before preparing a release");
  const branch = gitOutput(["branch", "--show-current"]);
  if (branch !== releaseBranch) fail(`release from ${releaseBranch}, not ${branch || "detached HEAD"}`);

  const remoteUrl = assertPublicRemote();

  git(["fetch", "--prune", publicRemote]);
  git(["fetch", publicRemote, "--tags"]);
  const remoteBranchRef = `refs/remotes/${publicRemote}/${releaseBranch}`;
  if (git(["show-ref", "--verify", "--quiet", remoteBranchRef], { allowFailure: true }).status !== 0) {
    fail(`missing ${publicRemote}/${releaseBranch} after fetch`);
  }
  assertPublicHistory(remoteBranchRef);
  if (git(["show-ref", "--verify", "--quiet", `refs/tags/${tag}`], { allowFailure: true }).status === 0) {
    fail(`tag ${tag} already exists`);
  }

  const desktopVersion = normalizeVersion(readJson("apps/desktop/package.json").version);
  const webVersion = normalizeVersion(readJson("apps/web/package.json").version);
  if (desktopVersion !== webVersion) fail(`desktop ${desktopVersion} and web ${webVersion} versions differ`);
  const backendVersion = normalizeVersion(apiVersion());
  if (desktopVersion !== backendVersion) {
    fail(`desktop ${desktopVersion} and backend ${backendVersion} versions differ`);
  }
  if (compareVersions(version, desktopVersion) <= 0) {
    fail(`new version ${version} must be greater than current version ${desktopVersion}`);
  }
  const latestPublicTag = highestStableTag(remoteBranchRef);
  if (latestPublicTag && compareVersions(version, normalizeVersion(latestPublicTag)) <= 0) {
    fail(`new version ${version} must be greater than latest public tag ${latestPublicTag}`);
  }

  if (dryRun) {
    console.log(`release: preflight passed; v${desktopVersion} can advance to ${tag} on ${publicRemote}/${releaseBranch}`);
    return;
  }

  await confirmRelease(tag, remoteUrl, assumeYes);
  bumpJsonVersion("apps/desktop/package.json", version);
  bumpJsonVersion("apps/desktop/package-lock.json", version, { lockfile: true });
  bumpJsonVersion("apps/web/package.json", version);
  bumpJsonVersion("apps/web/package-lock.json", version, { lockfile: true });
  bumpApiVersion(desktopVersion, version);
  replaceVersionBadge("README.md", desktopVersion, version);
  replaceVersionBadge("README-CN.md", desktopVersion, version);
  updateChangelog(version);
  assertReleaseMetadata(version);
  git(["diff", "--check"]);
  git(["diff", "--stat", "--", ...releaseFiles]);
  git(["add", "--", ...releaseFiles]);
  git(["commit", "-m", `release: ${tag}`]);
  git(["tag", "-a", tag, "-m", `SAG ${tag}`]);

  if (noPush) {
    console.log(`release: created local ${tag}; push it with the release commit when ready`);
    return;
  }

  git([
    "push",
    "--atomic",
    publicRemote,
    `HEAD:refs/heads/${releaseBranch}`,
    `refs/tags/${tag}:refs/tags/${tag}`,
  ]);
  console.log(`release: pushed ${tag}; follow https://github.com/${publicRepository}/actions`);
}

function verifyLatestRelease(rawTag, expectedSha) {
  const version = normalizeVersion(rawTag);
  const tag = `v${version}`;
  if (rawTag !== tag) fail(`expected canonical tag ${tag}, received ${rawTag || "nothing"}`);
  if (!/^[0-9a-f]{40,64}$/i.test(String(expectedSha || ""))) {
    fail(`expected a Git commit SHA, received ${expectedSha || "nothing"}`);
  }

  assertPublicRemote();
  const remoteBranchRef = `refs/remotes/${publicRemote}/${releaseBranch}`;
  git(["fetch", "--prune", "--force", publicRemote, `+refs/heads/${releaseBranch}:${remoteBranchRef}`]);
  git(["fetch", "--force", publicRemote, `+refs/tags/${tag}:refs/tags/${tag}`]);
  git(["fetch", "--force", publicRemote, "--tags"]);

  if (gitOutput(["cat-file", "-t", tag]) !== "tag") {
    fail(`${tag} must remain an annotated tag`);
  }
  const tagCommit = gitOutput(["rev-parse", `${tag}^{commit}`]);
  const expectedCommit = gitOutput(["rev-parse", `${expectedSha}^{commit}`]);
  if (tagCommit !== expectedCommit) {
    fail(`${tag} moved during the build; expected ${expectedCommit}, found ${tagCommit}`);
  }
  assertPublicHistory(remoteBranchRef);
  const latestPublicTag = highestStableTag(remoteBranchRef);
  if (latestPublicTag !== tag) {
    fail(`${tag} is no longer the newest stable tag on ${publicRemote}/${releaseBranch}; found ${latestPublicTag || "none"}`);
  }
  console.log(`release: ${tag} is immutable, on public main, and newest`);
}

const args = process.argv.slice(2);
if (args[0] === "--verify") {
  const version = normalizeVersion(args[1]);
  assertReleaseMetadata(version);
  console.log(`release: metadata verified for v${version}`);
} else if (args[0] === "--notes") {
  process.stdout.write(releaseNotes(normalizeVersion(args[1])));
} else if (args[0] === "--verify-latest") {
  verifyLatestRelease(args[1], args[2]);
} else {
  const versionArg = args.find((argument) => !argument.startsWith("--"));
  const flags = new Set(args.filter((argument) => argument.startsWith("--")));
  const supportedFlags = new Set(["--dry-run", "--no-push", "--yes"]);
  for (const flag of flags) {
    if (!supportedFlags.has(flag)) fail(`unknown flag ${flag}`);
  }
  await prepareRelease(versionArg, flags);
}
