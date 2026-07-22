import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import {
  copyFile,
  mkdir,
  mkdtemp,
  readFile,
  rm,
  writeFile,
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const sourceScript = fileURLToPath(new URL("../release-public.mjs", import.meta.url));
const releaseTagger = Object.freeze({
  name: "Zleap-AI Release",
  email: "Zleap-Admin@users.noreply.github.com",
});
const releaseTestEnvironment = Object.freeze({
  SAG_RELEASE_TEST_MODE: "1",
  SAG_PUBLIC_ALLOW_LOCAL_REMOTE: null,
  SAG_PUBLIC_REMOTE: null,
  SAG_PUBLIC_REPOSITORY: null,
  SAG_RELEASE_BRANCH: null,
});
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

function execute(command, args, { cwd, allowFailure = false, env = {} } = {}) {
  const childEnvironment = { ...process.env };
  for (const [name, value] of Object.entries(env)) {
    if (value === null) delete childEnvironment[name];
    else childEnvironment[name] = value;
  }
  const result = spawnSync(command, args, {
    cwd,
    encoding: "utf8",
    env: childEnvironment,
  });
  if (result.error) throw result.error;
  if (!allowFailure && result.status !== 0) {
    throw new Error(
      `${command} ${args.join(" ")} failed (${result.status})\n${result.stdout}${result.stderr}`,
    );
  }
  return result;
}

function git(cwd, ...args) {
  return execute("git", args, { cwd }).stdout.trim();
}

function bareGit(bareRepository, ...args) {
  return execute("git", [`--git-dir=${bareRepository}`, ...args]).stdout.trim();
}

function bareRefExists(bareRepository, ref) {
  return execute("git", [`--git-dir=${bareRepository}`, "show-ref", "--verify", "--quiet", ref], {
    allowFailure: true,
  }).status === 0;
}

function runRelease(repository, ...args) {
  return runReleaseWithEnvironment(repository, args);
}

function runReleaseWithEnvironment(repository, args, env = {}) {
  return execute(process.execPath, ["scripts/release-public.mjs", ...args], {
    cwd: repository,
    allowFailure: true,
    env: { ...releaseTestEnvironment, ...env },
  });
}

function createReleaseTag(repository, tag, commit) {
  execute("git", ["tag", "-a", tag, commit, "-m", `SAG ${tag}`], {
    cwd: repository,
    env: {
      GIT_AUTHOR_NAME: releaseTagger.name,
      GIT_AUTHOR_EMAIL: releaseTagger.email,
      GIT_COMMITTER_NAME: releaseTagger.name,
      GIT_COMMITTER_EMAIL: releaseTagger.email,
    },
  });
}

function runInteractiveRelease(repository, args, beforeConfirm) {
  const versionIndex = args.indexOf("--tag-only") + 1;
  const tag = `v${String(args[versionIndex] || "").replace(/^v/, "")}`;
  const childEnvironment = { ...process.env };
  for (const [name, value] of Object.entries(releaseTestEnvironment)) {
    if (value === null) delete childEnvironment[name];
    else childEnvironment[name] = value;
  }

  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ["scripts/release-public.mjs", ...args], {
      cwd: repository,
      env: childEnvironment,
      stdio: ["pipe", "pipe", "pipe"],
    });
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    let stdout = "";
    let stderr = "";
    let confirmed = false;
    const timeout = setTimeout(() => {
      child.kill();
      reject(new Error(`release confirmation timed out\n${stdout}${stderr}`));
    }, 15_000);

    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      if (confirmed || !stdout.includes(`Type ${tag} to continue:`)) return;
      confirmed = true;
      try {
        beforeConfirm();
        child.stdin.end(`${tag}\n`);
      } catch (error) {
        child.kill();
        clearTimeout(timeout);
        reject(error);
      }
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    child.on("close", (status) => {
      clearTimeout(timeout);
      if (!confirmed) {
        reject(new Error(`release exited before confirmation\n${stdout}${stderr}`));
        return;
      }
      resolve({ status, stdout, stderr });
    });
  });
}

async function writeJson(filename, value) {
  await mkdir(path.dirname(filename), { recursive: true });
  await writeFile(filename, `${JSON.stringify(value, null, 2)}\n`);
}

async function createFixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "sag-release-public-"));
  t.after(async () => rm(root, { recursive: true, force: true }));

  const remote = path.join(root, "Zleap-AI", "SAG.git");
  const repository = path.join(root, "public-clone");
  await mkdir(path.dirname(remote), { recursive: true });
  execute("git", ["init", "--bare", remote]);
  execute("git", ["init", "-b", "main", repository]);
  git(repository, "config", "user.name", "Release Test");
  git(repository, "config", "user.email", "release-test@example.com");

  await mkdir(path.join(repository, "scripts"), { recursive: true });
  await copyFile(sourceScript, path.join(repository, "scripts", "release-public.mjs"));
  await writeFile(
    path.join(repository, "CHANGELOG.md"),
    "# Changelog\n\n## Unreleased\n\n- next public change\n\n## v1.2.2 · 2026-01-01\n\n- previous release\n",
  );
  await writeFile(path.join(repository, "README.md"), "SAG-v1.2.2-18181b\n");
  await writeFile(path.join(repository, "README-CN.md"), "SAG-v1.2.2-18181b\n");
  await mkdir(path.join(repository, "apps", "api", "sag_api"), { recursive: true });
  await writeFile(
    path.join(repository, "apps", "api", "sag_api", "__init__.py"),
    '__version__ = "1.2.2"\n',
  );

  for (const application of ["desktop", "web"]) {
    const appRoot = path.join(repository, "apps", application);
    await writeJson(path.join(appRoot, "package.json"), {
      name: `@sag/${application}`,
      version: "1.2.2",
    });
    await writeJson(path.join(appRoot, "package-lock.json"), {
      name: `@sag/${application}`,
      version: "1.2.2",
      lockfileVersion: 3,
      packages: {
        "": {
          name: `@sag/${application}`,
          version: "1.2.2",
        },
      },
    });
  }

  git(repository, "add", ".");
  git(repository, "commit", "-m", "feat: public baseline");
  git(repository, "tag", "-a", "v1.2.2", "-m", "SAG v1.2.2");
  git(repository, "remote", "add", "origin", remote);
  git(repository, "push", "--atomic", "-u", "origin", "main", "refs/tags/v1.2.2");
  bareGit(remote, "symbolic-ref", "HEAD", "refs/heads/main");

  return { remote, repository, root };
}

async function prepareAndPushReleaseCommit(fixture) {
  const prepared = runRelease(fixture.repository, "--prepare", "1.3.0");
  assert.equal(prepared.status, 0, prepared.stderr);
  git(fixture.repository, "add", "--", ...releaseFiles);
  git(fixture.repository, "commit", "-m", "release: v1.3.0");
  git(fixture.repository, "push", "origin", "main");
  return git(fixture.repository, "rev-parse", "HEAD");
}

test("--prepare only edits local release metadata on a dirty feature branch", async (t) => {
  const fixture = await createFixture(t);
  git(fixture.repository, "switch", "-c", "sync/internal-snapshot");
  await writeFile(path.join(fixture.repository, "sync-change.txt"), "public snapshot\n");
  const headBefore = git(fixture.repository, "rev-parse", "HEAD");
  const remoteMainBefore = bareGit(fixture.remote, "rev-parse", "refs/heads/main");
  git(fixture.repository, "remote", "set-url", "origin", path.join(fixture.root, "unreachable.git"));

  const result = runRelease(fixture.repository, "--prepare", "1.3.0");

  assert.equal(result.status, 0, `${result.stdout}${result.stderr}`);
  assert.match(result.stdout, /no files were staged or committed, and no remote or tag was touched/);
  assert.equal(git(fixture.repository, "rev-parse", "HEAD"), headBefore);
  assert.equal(bareGit(fixture.remote, "rev-parse", "refs/heads/main"), remoteMainBefore);
  assert.equal(git(fixture.repository, "diff", "--cached", "--name-only"), "");
  assert.equal(git(fixture.repository, "tag", "--list", "v1.3.0"), "");
  assert.equal(
    JSON.parse(await readFile(path.join(fixture.repository, "apps", "desktop", "package.json"), "utf8")).version,
    "1.3.0",
  );
  assert.match(await readFile(path.join(fixture.repository, "CHANGELOG.md"), "utf8"), /^## v1\.3\.0 · \d{4}-\d{2}-\d{2}$/m);
});

test("local release remotes require explicit SAG_RELEASE_TEST_MODE", async (t) => {
  const fixture = await createFixture(t);
  await prepareAndPushReleaseCommit(fixture);

  const result = runReleaseWithEnvironment(
    fixture.repository,
    ["--tag-only", "1.3.0", "--yes"],
    { SAG_RELEASE_TEST_MODE: null },
  );

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /expected Zleap-AI\/SAG/);
  assert.equal(bareRefExists(fixture.remote, "refs/tags/v1.3.0"), false);
});

test("release target environment overrides are rejected", async (t) => {
  const fixture = await createFixture(t);

  const result = runReleaseWithEnvironment(
    fixture.repository,
    ["--tag-only", "1.3.0", "--yes"],
    { SAG_RELEASE_BRANCH: "release" },
  );

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /release target is fixed at origin Zleap-AI\/SAG main/);
  assert.match(result.stderr, /SAG_RELEASE_BRANCH/);
});

test("--tag-only rejects multiple fetch URLs", async (t) => {
  const fixture = await createFixture(t);
  const secondRemote = path.join(fixture.root, "second-fetch.git");
  execute("git", ["init", "--bare", secondRemote]);
  git(fixture.repository, "remote", "set-url", "--add", "origin", secondRemote);

  const result = runRelease(fixture.repository, "--tag-only", "1.3.0", "--yes");

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /origin must have exactly one fetch URL; found 2/);
  assert.equal(bareRefExists(fixture.remote, "refs/tags/v1.3.0"), false);
  assert.equal(bareRefExists(secondRemote, "refs/tags/v1.3.0"), false);
});

test("--tag-only rejects multiple push URLs before any destination changes", async (t) => {
  const fixture = await createFixture(t);
  const secondRemote = path.join(fixture.root, "second-push.git");
  execute("git", ["init", "--bare", secondRemote]);
  git(fixture.repository, "remote", "set-url", "--add", "--push", "origin", fixture.remote);
  git(fixture.repository, "remote", "set-url", "--add", "--push", "origin", secondRemote);

  const result = runRelease(fixture.repository, "--tag-only", "1.3.0", "--yes");

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /origin must have exactly one push URL; found 2/);
  assert.equal(bareRefExists(fixture.remote, "refs/tags/v1.3.0"), false);
  assert.equal(bareRefExists(secondRemote, "refs/tags/v1.3.0"), false);
});

test("--tag-only publishes an annotated tag at exact remote main without changing main", async (t) => {
  const fixture = await createFixture(t);
  const preparedCommit = await prepareAndPushReleaseCommit(fixture);
  const remoteMainBefore = bareGit(fixture.remote, "rev-parse", "refs/heads/main");

  const result = runRelease(fixture.repository, "--tag-only", "1.3.0", "--yes");

  assert.equal(result.status, 0, `${result.stdout}${result.stderr}`);
  assert.equal(remoteMainBefore, preparedCommit);
  assert.equal(bareGit(fixture.remote, "rev-parse", "refs/heads/main"), remoteMainBefore);
  assert.equal(bareGit(fixture.remote, "cat-file", "-t", "refs/tags/v1.3.0"), "tag");
  assert.equal(
    bareGit(fixture.remote, "rev-parse", "refs/tags/v1.3.0^{commit}"),
    remoteMainBefore,
  );
  assert.equal(
    bareGit(
      fixture.remote,
      "for-each-ref",
      "--format=%(taggername)|%(taggeremail)",
      "refs/tags/v1.3.0",
    ),
    `${releaseTagger.name}|<${releaseTagger.email}>`,
  );
});

test("--tag-only safely retries an unpublished local annotated tag", async (t) => {
  const fixture = await createFixture(t);
  const preparedCommit = await prepareAndPushReleaseCommit(fixture);
  createReleaseTag(fixture.repository, "v1.3.0", preparedCommit);
  assert.equal(bareRefExists(fixture.remote, "refs/tags/v1.3.0"), false);

  const result = runRelease(fixture.repository, "--tag-only", "1.3.0", "--yes");

  assert.equal(result.status, 0, `${result.stdout}${result.stderr}`);
  assert.equal(bareGit(fixture.remote, "cat-file", "-t", "refs/tags/v1.3.0"), "tag");
  assert.equal(
    bareGit(fixture.remote, "rev-parse", "refs/tags/v1.3.0^{commit}"),
    preparedCommit,
  );
});

test("--tag-only rejects a local tag that does not point to exact remote main", async (t) => {
  const fixture = await createFixture(t);
  await prepareAndPushReleaseCommit(fixture);
  const previousCommit = git(fixture.repository, "rev-parse", "v1.2.2^{commit}");
  createReleaseTag(fixture.repository, "v1.3.0", previousCommit);

  const result = runRelease(fixture.repository, "--tag-only", "1.3.0", "--yes");

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /local v1\.3\.0 points to .* expected public main/);
  assert.equal(bareRefExists(fixture.remote, "refs/tags/v1.3.0"), false);
});

test("--tag-only rejects an unpublished local tag with an arbitrary tagger", async (t) => {
  const fixture = await createFixture(t);
  const preparedCommit = await prepareAndPushReleaseCommit(fixture);
  git(fixture.repository, "tag", "-a", "v1.3.0", preparedCommit, "-m", "wrong tagger");

  const result = runRelease(fixture.repository, "--tag-only", "1.3.0", "--yes");

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /tagger must be Zleap-AI Release <Zleap-Admin@users\.noreply\.github\.com>/);
  assert.equal(bareRefExists(fixture.remote, "refs/tags/v1.3.0"), false);
});

test("--tag-only rejects a local main that is ahead of remote main", async (t) => {
  const fixture = await createFixture(t);
  await prepareAndPushReleaseCommit(fixture);
  await writeFile(path.join(fixture.repository, "local-only.txt"), "not reviewed\n");
  git(fixture.repository, "add", "local-only.txt");
  git(fixture.repository, "commit", "-m", "fix: unreviewed local change");

  const result = runRelease(fixture.repository, "--tag-only", "1.3.0", "--yes");

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /must exactly match origin\/main/);
  assert.equal(bareRefExists(fixture.remote, "refs/tags/v1.3.0"), false);
});

test("--tag-only rejects a local main that is behind remote main", async (t) => {
  const fixture = await createFixture(t);
  await prepareAndPushReleaseCommit(fixture);
  const otherClone = path.join(fixture.root, "other-clone");
  execute("git", ["clone", fixture.remote, otherClone]);
  git(otherClone, "config", "user.name", "Other Writer");
  git(otherClone, "config", "user.email", "other@example.com");
  await writeFile(path.join(otherClone, "remote-only.txt"), "advanced remotely\n");
  git(otherClone, "add", "remote-only.txt");
  git(otherClone, "commit", "-m", "fix: advance public main");
  git(otherClone, "push", "origin", "main");

  const result = runRelease(fixture.repository, "--tag-only", "1.3.0", "--yes");

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /must exactly match origin\/main/);
  assert.equal(bareRefExists(fixture.remote, "refs/tags/v1.3.0"), false);
});

test("--tag-only revalidates main after interactive confirmation", async (t) => {
  const fixture = await createFixture(t);
  await prepareAndPushReleaseCommit(fixture);
  const otherClone = path.join(fixture.root, "confirmation-race-clone");
  execute("git", ["clone", fixture.remote, otherClone]);
  git(otherClone, "config", "user.name", "Other Writer");
  git(otherClone, "config", "user.email", "other@example.com");
  await writeFile(path.join(otherClone, "after-confirmation.txt"), "advance during confirmation\n");
  git(otherClone, "add", "after-confirmation.txt");
  git(otherClone, "commit", "-m", "fix: advance during release confirmation");

  const result = await runInteractiveRelease(
    fixture.repository,
    ["--tag-only", "1.3.0"],
    () => git(otherClone, "push", "origin", "main"),
  );

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /must exactly match origin\/main/);
  assert.equal(bareRefExists(fixture.remote, "refs/tags/v1.3.0"), false);
  assert.equal(git(fixture.repository, "tag", "--list", "v1.3.0"), "");
});

test("--tag-only revalidates stable tag ordering after confirmation", async (t) => {
  const fixture = await createFixture(t);
  const preparedCommit = await prepareAndPushReleaseCommit(fixture);
  const otherClone = path.join(fixture.root, "tag-race-clone");
  execute("git", ["clone", fixture.remote, otherClone]);
  createReleaseTag(otherClone, "v1.4.0", preparedCommit);

  const result = await runInteractiveRelease(
    fixture.repository,
    ["--tag-only", "1.3.0"],
    () => git(otherClone, "push", "origin", "refs/tags/v1.4.0"),
  );

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /prepared version 1\.3\.0 must be greater than latest public tag v1\.4\.0/);
  assert.equal(bareRefExists(fixture.remote, "refs/tags/v1.3.0"), false);
  assert.equal(git(fixture.repository, "tag", "--list", "v1.3.0"), "");
});

test("--verify-latest accepts a release tag after public main advances", async (t) => {
  const fixture = await createFixture(t);
  const preparedCommit = await prepareAndPushReleaseCommit(fixture);
  const published = runRelease(fixture.repository, "--tag-only", "1.3.0", "--yes");
  assert.equal(published.status, 0, `${published.stdout}${published.stderr}`);
  git(fixture.repository, "switch", "--detach", "v1.3.0");

  const otherClone = path.join(fixture.root, "post-tag-main-clone");
  execute("git", ["clone", fixture.remote, otherClone]);
  git(otherClone, "config", "user.name", "Other Writer");
  git(otherClone, "config", "user.email", "other@example.com");
  await writeFile(path.join(otherClone, "post-tag.txt"), "normal public main advance\n");
  git(otherClone, "add", "post-tag.txt");
  git(otherClone, "commit", "-m", "fix: advance main after release tag");
  git(otherClone, "push", "origin", "main");

  const result = runRelease(
    fixture.repository,
    "--verify-latest",
    "v1.3.0",
    preparedCommit,
  );

  assert.equal(result.status, 0, `${result.stdout}${result.stderr}`);
  assert.match(result.stdout, /v1\.3\.0 is immutable, on public main, and newest/);
});

test("legacy positional release is rejected without changing files, main, or tags", async (t) => {
  const fixture = await createFixture(t);
  const headBefore = git(fixture.repository, "rev-parse", "HEAD");
  const remoteMainBefore = bareGit(fixture.remote, "rev-parse", "refs/heads/main");
  const result = runRelease(fixture.repository, "1.3.0", "--yes");

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /positional release mode and --no-push were removed/);
  assert.equal(git(fixture.repository, "rev-parse", "HEAD"), headBefore);
  assert.equal(git(fixture.repository, "status", "--porcelain=v1", "--untracked-files=all"), "");
  assert.equal(bareGit(fixture.remote, "rev-parse", "refs/heads/main"), remoteMainBefore);
  assert.equal(bareRefExists(fixture.remote, "refs/tags/v1.3.0"), false);
});

test("--no-push is rejected by tag-only mode", async (t) => {
  const fixture = await createFixture(t);
  const result = runRelease(fixture.repository, "--tag-only", "1.3.0", "--no-push");

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /unknown --tag-only flag --no-push/);
  assert.equal(bareRefExists(fixture.remote, "refs/tags/v1.3.0"), false);
});
