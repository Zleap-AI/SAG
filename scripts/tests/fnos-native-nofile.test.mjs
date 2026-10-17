import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repo = fileURLToPath(new URL("../..", import.meta.url));
const launcher = path.join(repo, "packages/fnos/native/sag/cmd/main");
const shell = "/bin/sh"; // dash in the Linux/fnOS toolchain

// Only this subprocess lowers its hard limit. The test runner keeps its own
// limits, and the child then execs the same shell that configures the daemons.
async function runNofile(t, { soft = 1024, hard = 4096, target, preamble = "" } = {}) {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-nofile-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const log = path.join(fixture, "startup.log");
  const source = await readFile(launcher, "utf8");
  const helper = source.match(/configure_nofile\(\) \{[\s\S]*?\n\}/)?.[0];
  assert.ok(helper, "launcher must define configure_nofile");
  const childProbe = "import json,resource,subprocess,sys; print(json.dumps({'self':resource.getrlimit(resource.RLIMIT_NOFILE),'child':json.loads(subprocess.check_output([sys.executable,'-c','import json,resource;print(json.dumps(resource.getrlimit(resource.RLIMIT_NOFILE)))']))}))";
  const script = `set -u
log_error() { printf '%s\\n' "$1" >> "$TRIM_TEMP_LOGFILE"; }
${helper}
${preamble}
configure_nofile || exit $?
exec "$SAG_TEST_PYTHON" -c "$SAG_TEST_PROBE"
`;
  const control = "import os,resource,sys;resource.setrlimit(resource.RLIMIT_NOFILE,(int(sys.argv[1]),int(sys.argv[2])));os.execv(sys.argv[3],[sys.argv[3],'-c',sys.argv[4]])";
  const env = { ...process.env, TRIM_TEMP_LOGFILE: log, SAG_TEST_PROBE: childProbe };
  delete env.SAG_NATIVE_NOFILE;
  if (target !== undefined) env.SAG_NATIVE_NOFILE = target;
  const python = spawnSync("python3", ["-c", "import sys;print(sys.executable)"], { encoding: "utf8" }).stdout.trim();
  env.SAG_TEST_PYTHON = python;
  const result = spawnSync(python, ["-c", control, String(soft), String(hard), shell, script], { env, encoding: "utf8" });
  const diagnostic = await readFile(log, "utf8").catch(() => "");
  const limits = result.stdout.trim().split("\n").at(-1);
  return { ...result, diagnostic, limits: limits?.startsWith("{") ? JSON.parse(limits) : undefined };
}

test("native nofile raises the actual launcher soft limit and children inherit it", async (t) => {
  const result = await runNofile(t);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(result.limits, { self: [4096, 4096], child: [4096, 4096] });
  assert.match(result.stdout, /NOFILE.*soft=1024.*effective=4096.*hard=4096/);
});

test("native nofile clamps a configured target to the inherited hard limit", async (t) => {
  const result = await runNofile(t, { target: "65536" });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(result.limits.self, [4096, 4096]);
  assert.match(result.stdout, /target=65536.*effective=4096.*clamped/);
});

test("native nofile honors a lower configured target without lowering an existing higher soft limit", async (t) => {
  const lower = await runNofile(t, { target: "2048" });
  assert.equal(lower.status, 0, lower.stderr);
  assert.deepEqual(lower.limits.self, [2048, 4096]);
  const higher = await runNofile(t, { soft: 8192, hard: 8192 });
  assert.equal(higher.status, 0, higher.stderr);
  assert.deepEqual(higher.limits.self, [8192, 8192]);
});

test("native nofile keeps the package usable when inherited hard is below the default target", async (t) => {
  const result = await runNofile(t, { soft: 512, hard: 1024 });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(result.limits.self, [1024, 1024]);
  assert.match(result.stdout, /clamped/);
});

test("native nofile rejects malformed targets before executing any child", async (t) => {
  for (const target of ["", "0", "-1", "unlimited", "abc", "1; echo unsafe", "999999999999999999999999999"]) {
    const result = await runNofile(t, { target });
    assert.notEqual(result.status, 0, target);
    assert.equal(result.limits, undefined, target);
    assert.match(result.diagnostic, /SAG_NATIVE_NOFILE.*positive integer/i, target);
  }
});

test("native nofile aborts with the unchanged effective limit when a real setrlimit fails", async (t) => {
  // The wrapper lowers hard after the helper reads it. The attempted soft
  // increase then fails in the real shell builtin, rather than a fake setter.
  const result = await runNofile(t, { preamble: `ulimit() {
  if [ "$#" -eq 3 ]; then
    command ulimit -H -n 2048
  fi
  command ulimit "$@"
}` });
  assert.notEqual(result.status, 0);
  assert.equal(result.limits, undefined);
  assert.match(result.diagnostic, /cannot.*NOFILE.*effective=1024.*hard=2048.*restart/i);
  assert.doesNotMatch(result.stdout, /effective=4096/);
});

test("native nofile handles unlimited inherited soft and hard limits", async (t) => {
  // Modern kernels may forbid infinity for NOFILE. Only these reads are
  // substituted; calling a setter is a failure when both are already unlimited.
  const result = await runNofile(t, { preamble: `ulimit() {
  case "$*" in
    '-S -n'|'-H -n') printf '%s\\n' unlimited ;;
    *) return 1 ;;
  esac
}` });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /effective=unlimited.*hard=unlimited/);
});

test("native start validates nofile before runtime checks or daemon launches", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-nofile-start-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const appdest = path.join(fixture, "appdest");
  const pkgvar = path.join(fixture, "pkgvar");
  await mkdir(appdest);
  await mkdir(pkgvar);
  const log = path.join(fixture, "startup.log");
  const result = spawnSync("bash", [launcher, "start"], { encoding: "utf8", env: {
    ...process.env, TRIM_APPDEST: appdest, TRIM_PKGVAR: pkgvar,
    TRIM_PKGTMP: fixture, TRIM_TEMP_LOGFILE: log, SAG_NATIVE_NOFILE: "invalid",
  } });
  assert.notEqual(result.status, 0);
  const diagnostic = await readFile(log, "utf8");
  assert.match(diagnostic, /SAG_NATIVE_NOFILE.*positive integer/i);
  assert.doesNotMatch(diagnostic, /runtime payload is unavailable/);
});
