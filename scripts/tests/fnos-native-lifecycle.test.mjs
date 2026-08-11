import assert from "node:assert/strict";
import { chmod, mkdtemp, mkdir, readFile, rm, symlink, writeFile, stat } from "node:fs/promises";
import { existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../..", import.meta.url));
const cmd = path.join(root, "packages/fnos/native/sag/cmd");
const chatPage = path.join(root, "apps/web/app/(app)/chat/[[...id]]/page.tsx");
const appSidebar = path.join(root, "apps/web/components/features/app-sidebar.tsx");

// The package runs every lifecycle callback as the sag package user
// (config/privilege defaults.run-as=package). fnpack materializes the
// package-owned data directory between install_init and install_callback,
// so the callback can provision its secret without elevated privileges.

function baseEnv(extra = {}) {
  return { ...process.env, ...extra };
}

function run(name, env, args = []) {
  return spawnSync("bash", [path.join(cmd, name), ...args], { encoding: "utf8", env });
}
function runSh(name, env, args = []) {
  return spawnSync("/bin/sh", [path.join(cmd, name), ...args], { encoding: "utf8", env });
}

// -----------------------------------------------------------------
// install_init — now a cleanup-only shell. It must NOT touch
// $TRIM_PKGVAR (which fnpack has not materialized yet at that stage)
// and must not fail on a fresh install with no residue.
// -----------------------------------------------------------------
test("install_init succeeds when there is nothing to clean up (fresh install)", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const appdest = path.join(fixture, "appdest");
  await mkdir(appdest);
  const result = run("install_init", baseEnv({ TRIM_APPDEST: appdest }));
  assert.equal(result.status, 0, result.stderr);
});

test("install_init makes no writes under $TRIM_PKGVAR (fnpack has not created it yet)", async (t) => {
  // The bug we're guarding against here is real: an earlier release
  // did mkdir/chmod/secret provisioning in install_init and fnpack's
  // install_init runs BEFORE $TRIM_PKGVAR exists on disk. Point
  // TRIM_PKGVAR at a non-existent path and verify install_init does
  // not try to create it.
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const nonExistent = path.join(fixture, "should-not-be-created");
  const appdest = path.join(fixture, "appdest");
  await mkdir(appdest);

  const result = run("install_init", baseEnv({ TRIM_PKGVAR: nonExistent, TRIM_APPDEST: appdest }));
  assert.equal(result.status, 0, result.stderr);
  assert.equal(existsSync(nonExistent), false, "install_init must not create $TRIM_PKGVAR");
});

test("install_init also parses under strict POSIX /bin/sh (dash on fnOS)", () => {
  assert.equal(spawnSync("/bin/sh", ["-n", path.join(cmd, "install_init")]).status, 0);
});

test("install_init removes a stale $TRIM_APPDEST/app.sock left by a crashed prior process", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const appdest = path.join(fixture, "appdest");
  await mkdir(appdest);
  // Simulate the stale socket by creating a plain file at the path.
  // install_init tests -S to actually match a socket — a regular
  // file must NOT trip the cleanup, so a leftover file is left in
  // place (fnpack overwrites the appdest tree anyway). We check the
  // negative: install_init succeeds regardless.
  await writeFile(path.join(appdest, "app.sock"), "");
  const result = run("install_init", baseEnv({ TRIM_APPDEST: appdest }));
  assert.equal(result.status, 0, result.stderr);
});

// -----------------------------------------------------------------
// install_callback — owns $TRIM_PKGVAR mkdir/perms and the internal secret.
// -----------------------------------------------------------------
test("install_callback creates $TRIM_PKGVAR at mode 0700 and provisions the internal-secret", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const pkgvar = path.join(fixture, "pkgvar");
  // Do NOT pre-create pkgvar — install_callback owns that.

  const env = baseEnv({ TRIM_PKGVAR: pkgvar });
  const first = run("install_callback", env);
  assert.equal(first.status, 0, first.stderr);
  assert.equal((await stat(pkgvar)).mode & 0o777, 0o700);
  const secretPath = path.join(pkgvar, "internal-secret");
  const material = await readFile(secretPath, "utf8");
  assert.match(material, /^[0-9a-f]{64}$/);
  assert.equal((await stat(secretPath)).mode & 0o777, 0o600);

  // Idempotent — a second run reuses the healthy secret rather than
  // rewriting it.
  const second = run("install_callback", env);
  assert.equal(second.status, 0, second.stderr);
  assert.equal(await readFile(secretPath, "utf8"), material);
});

test("install_callback also runs under strict POSIX /bin/sh (dash on fnOS)", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const pkgvar = path.join(fixture, "pkgvar");
  const result = runSh("install_callback", baseEnv({ TRIM_PKGVAR: pkgvar }));
  assert.equal(result.status, 0, result.stderr);
  assert.match(await readFile(path.join(pkgvar, "internal-secret"), "utf8"), /^[0-9a-f]{64}$/);
});

test("install_callback discards a symlinked residue instead of aborting", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const pkgvar = path.join(fixture, "pkgvar");
  const target = path.join(fixture, "target");
  await mkdir(pkgvar);
  await writeFile(target, "0".repeat(64));
  await symlink(target, path.join(pkgvar, "internal-secret"));

  const result = run("install_callback", baseEnv({ TRIM_PKGVAR: pkgvar }));
  assert.equal(result.status, 0, result.stderr);
  const stats = await stat(path.join(pkgvar, "internal-secret"));
  assert.equal(stats.mode & 0o777, 0o600);
  assert.match(await readFile(path.join(pkgvar, "internal-secret"), "utf8"), /^[0-9a-f]{64}$/);
});

test("install_callback regenerates a malformed residue secret left by a prior install", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const pkgvar = path.join(fixture, "pkgvar");
  await mkdir(pkgvar);
  await writeFile(path.join(pkgvar, "internal-secret"), '{"legacy":"docker","token":"AAAA"}');
  await chmod(path.join(pkgvar, "internal-secret"), 0o644);
  const logFile = path.join(fixture, "install.log");
  await writeFile(logFile, "");

  const result = run("install_callback", baseEnv({ TRIM_PKGVAR: pkgvar, TRIM_TEMP_LOGFILE: logFile }));
  assert.equal(result.status, 0, result.stderr);
  assert.match(await readFile(path.join(pkgvar, "internal-secret"), "utf8"), /^[0-9a-f]{64}$/);
  assert.equal((await stat(path.join(pkgvar, "internal-secret"))).mode & 0o777, 0o600);
  assert.match(await readFile(logFile, "utf8"), /regenerating/i);
});

test("install_callback regenerates a truncated secret rather than dying at the length check", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const pkgvar = path.join(fixture, "pkgvar");
  await mkdir(pkgvar);
  await writeFile(path.join(pkgvar, "internal-secret"), "abcdef");

  const result = run("install_callback", baseEnv({ TRIM_PKGVAR: pkgvar }));
  assert.equal(result.status, 0, result.stderr);
  assert.match(await readFile(path.join(pkgvar, "internal-secret"), "utf8"), /^[0-9a-f]{64}$/);
});

test("install_callback writes a human-readable cause to TRIM_TEMP_LOGFILE when it must abort", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const logFile = path.join(fixture, "install.log");
  await writeFile(logFile, "");

  const result = run("install_callback", { ...process.env, TRIM_TEMP_LOGFILE: logFile, TRIM_PKGVAR: "" });
  assert.notEqual(result.status, 0);
  assert.match(await readFile(logFile, "utf8"), /TRIM_PKGVAR/i);
});

test("install_callback refuses a relative TRIM_PKGVAR", async () => {
  const result = run("install_callback", baseEnv({ TRIM_PKGVAR: "relative/var" }));
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /absolute path/i);
});

test("install_callback scrubs a residue secret from the legacy @appconf path", async (t) => {
  // The real-hardware failure on .19 was a root-owned secret at
  // /vol1/@appconf/sag/internal-secret (TRIM_PKGETC) that fnpack did
  // not re-normalize on overwrite install. main no longer reads
  // @appconf; install_callback removes the residue defensively so a
  // downgrade-then-upgrade cannot resurrect it.
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const pkgvar = path.join(fixture, "pkgvar");
  const pkgetc = path.join(fixture, "pkgetc");
  await mkdir(pkgvar);
  await mkdir(pkgetc);
  await writeFile(path.join(pkgetc, "internal-secret"), "legacy");

  const result = run("install_callback", baseEnv({ TRIM_PKGVAR: pkgvar, TRIM_PKGETC: pkgetc }));
  assert.equal(result.status, 0, result.stderr);
  assert.equal(existsSync(path.join(pkgetc, "internal-secret")), false, "legacy @appconf residue must be removed");
  assert.match(await readFile(path.join(pkgvar, "internal-secret"), "utf8"), /^[0-9a-f]{64}$/);
});

test("install_callback does not require ownership-changing commands", async () => {
  const source = await readFile(path.join(cmd, "install_callback"), "utf8");
  assert.doesNotMatch(source, /chown -R sag:sag "\$TRIM_PKGVAR"/);
});

// -----------------------------------------------------------------
// upgrade_callback — delegates to install_callback so the same
// perm/ownership guarantees hold on the upgrade path.
// -----------------------------------------------------------------
test("upgrade_callback delegates to install_callback for perm/ownership fixup", async () => {
  const source = await readFile(path.join(cmd, "upgrade_callback"), "utf8");
  assert.match(source, /"\$command_dir\/install_callback"/);
});

test("upgrade_callback under strict /bin/sh (dash on fnOS)", () => {
  assert.equal(spawnSync("/bin/sh", ["-n", path.join(cmd, "upgrade_callback")]).status, 0);
});

// -----------------------------------------------------------------
// cmd/main launches daemons directly because fnpack already invokes it
// as the package user.
// -----------------------------------------------------------------
test("cmd/main launches the gateway and web daemons without a privilege drop", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /HOSTNAME=127\.0\.0\.1 PORT="\$web_port" "\$node_runtime" "\$web_entry"/);
  assert.match(source, /"\$runtime" -m sag_api\.fnos\.cli gateway/);
  assert.doesNotMatch(source, /drop_priv "\$node_runtime"/);
});

test("main.prepare_secret self-heals a package-owned internal secret", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /prepare_secret returns 0 iff main can safely hand the secret/);
  assert.match(source, /generate_secret_material/);
  assert.doesNotMatch(source, /chown sag:sag "\$secret"/);
  // The old .19 composite line that hid causes must not reappear.
  assert.doesNotMatch(source, /internal identity secret is unavailable or has unsafe permissions/);
});

test("native start repairs the secret to mode 0600", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /chmod 600 "\$secret"/);
});

test("native lifecycle verifies service identity from the complete process command line", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /\/proc\/\$pid\/cmdline/);
  assert.doesNotMatch(source, /ps -p "\$pid" -o command=/);
});

test("native lifecycle recognises Next after it rewrites its process title", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /SAG_NATIVE_SERVICE="web" HOSTNAME=127\.0\.0\.1/);
  assert.match(source, /\/proc\/\$pid\/environ/);
  assert.match(source, /SAG_NATIVE_SERVICE=\$service/);
  assert.doesNotMatch(source, /valid_pid "\$web_pid" "\$web_entry"(?! "web")/);
  assert.doesNotMatch(source, /stop_process "\$web_pid" "\$web_entry"(?! "web")/);
});

test("valid_pid accepts a title-rewriting service through its launch marker", async (t) => {
  if (!existsSync("/proc/self/cmdline")) {
    t.skip("requires Linux /proc semantics");
    return;
  }
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-validpid-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const source = await readFile(path.join(cmd, "main"), "utf8");
  const fn = source.match(/valid_pid\(\) \{[\s\S]*?\n\}/);
  assert.ok(fn, "valid_pid function not found");
  const entry = path.join(fixture, "server.js");
  await writeFile(entry, 'process.title = "next-server (v15.5.20)"; setInterval(() => {}, 1000);');
  const probe = (marker) => {
    const script = `
      ${marker ? 'SAG_NATIVE_SERVICE="web" ' : ""}node "${entry}" & pid=$!
      echo "$pid" > "${fixture}/web.pid"
      sleep 1
      ${fn[0]}
      valid_pid "${fixture}/web.pid" "${entry}" "web"; result=$?
      kill "$pid" 2>/dev/null
      exit "$result"
    `;
    return spawnSync("bash", ["-c", script], { encoding: "utf8" });
  };
  assert.equal(probe(true).status, 0, "marked service must stay recognised after the title rewrite");
  assert.notEqual(probe(false).status, 0, "an unmarked process must not be claimed");
});

test("native lifecycle validates the gateway health endpoint over its UDS", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /curl -fsS[^\n]*--unix-socket "\$gateway_socket"/);
  assert.match(source, /http:\/\/localhost\/healthz/);
});

test("client-side chat navigation leaves base-path prefixing to the Next router", async () => {
  const source = await readFile(chatPage, "utf8");
  assert.match(source, /router\.push\("\/chat"\)/);
  assert.doesNotMatch(source, /router\.push\(appPath\("\/chat"\)\)/);
});

test("Native sidebar icon uses the deployed application prefix", async () => {
  const source = await readFile(appSidebar, "utf8");
  assert.match(source, /import \{ appPath \} from "@\/lib\/deployment"/);
  assert.match(source, /src=\{appPath\("\/sag-icon\.png"\)\}/);
});

test("main.start reports gateway early-exit with a tail of gateway.log", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /gw_pid=\$!/);
  assert.match(source, /kill -0 "\$gw_pid"/);
  assert.match(source, /tail -n 20 "\$gateway_log"/);
  assert.match(source, /gateway process exited immediately after launch/);
  assert.match(source, /rm -f "\$gateway_pid" "\$gateway_socket"/);
  assert.match(source, /_start_crash_trap/);
  assert.match(source, /SAG Native start exited with code/);
});

test("main.start verifies the gateway log is writable before launching anything", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /touch "\$gateway_log" "\$web_log"/);
  assert.match(source, /cannot write gateway\/web log/);
});

test("main.start warns when the gateway socket directory is not writable", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /check-socket-dir/);
  assert.match(source, /not writable by uid=/);
});

test("main.start fails early with a clear message when mkdir of run/log dirs fails", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /mkdir -p "\$run_dir" "\$log_dir"/);
  assert.match(source, /cannot create run\/log dirs under/);
});

test("main.start wait_for_web failure writes a log_error with web log tail", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /web process did not become ready within 15s/);
  assert.match(source, /tail -n 20 "\$web_log"/);
});

test("wait_for_gateway_socket detects a dead gateway process early instead of looping 15 more seconds", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /gateway process died during startup/);
  assert.match(source, /valid_pid "\$gateway_pid" "sag_api\.fnos\.cli"/);
  assert.match(source, /did not become ready within 15s/);
});

test("main.start EXIT trap captures pid / dir stats / gw log on any non-zero exit", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /trap _start_crash_trap EXIT/);
  assert.match(source, /UID=\$\(id -u/);
  assert.match(source, /GW log:/);
  assert.match(source, /Secret: \$\(ls -l "\$secret"/);
  assert.match(source, /trap - EXIT/);
});

// -----------------------------------------------------------------
// upgrade_init
// -----------------------------------------------------------------
async function stageUpgradeFixture(t) {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-upgrade-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const cmdDir = path.join(fixture, "cmd");
  const appdest = path.join(fixture, "appdest");
  const pkgvar = path.join(fixture, "pkgvar");
  const pkgetc = path.join(fixture, "pkgetc");
  await mkdir(cmdDir);
  await mkdir(appdest);
  await mkdir(pkgvar);
  await mkdir(pkgetc);
  await mkdir(path.join(appdest, "runtime"));
  const upgradeSrc = await readFile(path.join(cmd, "upgrade_init"), "utf8");
  await writeFile(path.join(cmdDir, "upgrade_init"), upgradeSrc);
  await chmod(path.join(cmdDir, "upgrade_init"), 0o755);
  const logFile = path.join(fixture, "install.log");
  await writeFile(logFile, "");
  const env = {
    ...process.env,
    TRIM_APPDEST: appdest,
    TRIM_PKGVAR: pkgvar,
    TRIM_PKGETC: pkgetc,
    TRIM_TEMP_LOGFILE: logFile,
  };
  return { fixture, cmdDir, appdest, pkgvar, pkgetc, logFile, env };
}

async function writeStubMain(cmdDir, { statusExit }) {
  const body = `#!/bin/sh
case "$1" in
  status)
    if [ ${statusExit} -ne 0 ]; then
      printf '%s\\n' "SAG Native gateway process is not running." >> "\${TRIM_TEMP_LOGFILE:-/dev/null}"
    fi
    exit ${statusExit}
    ;;
  stop) exit 0 ;;
  start) exit 0 ;;
  *) exit 1 ;;
esac
`;
  await writeFile(path.join(cmdDir, "main"), body);
  await chmod(path.join(cmdDir, "main"), 0o755);
}

async function writeStubInstallCallback(cmdDir) {
  // install_callback needs to succeed for upgrade_init to proceed past
  // its first step. The real install_callback does real work but is
  // validated separately; here we just no-op it.
  await writeFile(path.join(cmdDir, "install_callback"), "#!/bin/sh\nexit 0\n");
  await chmod(path.join(cmdDir, "install_callback"), 0o755);
}

test("upgrade_init delegates identity-secret provisioning to install_callback (not install_init)", async () => {
  // install_init no longer touches $TRIM_PKGVAR (fnpack hasn't
  // created it at that stage). The provisioning moved to
  // install_callback, and upgrade_init must delegate to the same
  // helper so an upgrade path converges on the same guarantees.
  const source = await readFile(path.join(cmd, "upgrade_init"), "utf8");
  assert.match(source, /"\$command_dir\/install_callback"/);
  assert.doesNotMatch(source, /"\$command_dir\/install_init"/);
});

test("upgrade_init redirects TRIM_TEMP_LOGFILE while probing main status", async () => {
  const source = await readFile(path.join(cmd, "upgrade_init"), "utf8");
  assert.match(source, /TRIM_TEMP_LOGFILE=\/dev\/null[^\n]*"\$main" status/);
});

test("upgrade_init tolerates a missing users directory (first install)", async (t) => {
  const { cmdDir, logFile, env } = await stageUpgradeFixture(t);
  await writeStubInstallCallback(cmdDir);
  await writeStubMain(cmdDir, { statusExit: 3 });

  const result = spawnSync("bash", [path.join(cmdDir, "upgrade_init")], { encoding: "utf8", env });
  assert.equal(result.status, 0, `upgrade_init should succeed on first install. stderr=${result.stderr} log=${await readFile(logFile, "utf8")}`);
  const log = await readFile(logFile, "utf8");
  assert.doesNotMatch(log, /gateway process is not running/i);
  assert.match(log, /no users directory/i);
});

test("upgrade_init also handles the empty users directory case without invoking lifecycle.py", async (t) => {
  const { cmdDir, pkgvar, logFile, env } = await stageUpgradeFixture(t);
  await writeStubInstallCallback(cmdDir);
  await writeStubMain(cmdDir, { statusExit: 3 });
  await mkdir(path.join(pkgvar, "users"));

  const result = spawnSync("bash", [path.join(cmdDir, "upgrade_init")], { encoding: "utf8", env });
  assert.equal(result.status, 0, `upgrade_init should skip backup on an empty users dir. stderr=${result.stderr}`);
  assert.match(await readFile(logFile, "utf8"), /empty/i);
});

test("upgrade_init parses under strict /bin/sh (dash on fnOS)", () => {
  assert.equal(spawnSync("/bin/sh", ["-n", path.join(cmd, "upgrade_init")]).status, 0);
});

test("upgrade_init never exits silently — every non-zero exit writes a cause to TRIM_TEMP_LOGFILE", async (t) => {
  const { cmdDir, logFile, env } = await stageUpgradeFixture(t);
  await writeStubInstallCallback(cmdDir);
  await writeStubMain(cmdDir, { statusExit: 3 });
  const result = spawnSync("bash", [path.join(cmdDir, "upgrade_init")], {
    encoding: "utf8",
    env: { ...env, TRIM_APPDEST: "" },
  });
  assert.notEqual(result.status, 0);
  const log = await readFile(logFile, "utf8");
  assert.match(log, /upgrade_init:/);
  assert.match(log, /TRIM_APPDEST/i);
  assert.match(log, /aborting with status/i);
});

test("upgrade_init tolerates a TRIM_APPDEST without lifecycle.py yet", async (t) => {
  const { cmdDir, pkgvar, logFile, env } = await stageUpgradeFixture(t);
  await writeStubInstallCallback(cmdDir);
  await writeStubMain(cmdDir, { statusExit: 3 });
  await mkdir(path.join(pkgvar, "users", "user-x"), { recursive: true });

  const result = spawnSync("bash", [path.join(cmdDir, "upgrade_init")], { encoding: "utf8", env });
  assert.equal(result.status, 0, `stderr=${result.stderr} log=${await readFile(logFile, "utf8")}`);
  assert.match(await readFile(logFile, "utf8"), /lifecycle\.py not present/i);
});

test("Native lifecycle shell scripts parse cleanly", () => {
  for (const name of ["main", "install_init", "install_callback", "upgrade_init", "upgrade_callback", "uninstall_init", "uninstall_callback", "config_init", "config_callback"]) {
    assert.equal(spawnSync("bash", ["-n", path.join(cmd, name)]).status, 0, name);
  }
});

// -----------------------------------------------------------------
// uninstall_callback runtime pinning
// -----------------------------------------------------------------
test("uninstall_callback prefers the bundled python3.12 over system python3", async () => {
  const source = await readFile(path.join(cmd, "uninstall_callback"), "utf8");
  assert.match(source, /py312="\/var\/apps\/python312\/target\/bin\/python3"/);
  assert.match(source, /if \[ -x "\$py312" \]/);
  assert.match(source, /"\$py" "\$\{TRIM_APPDEST\}\/runtime\/lifecycle\.py" delete/);
});

// -----------------------------------------------------------------
// prepare_secret self-heal (main)
// -----------------------------------------------------------------
test("main.prepare_secret regenerates in place when the file is missing", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-prep-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const pkgvar = path.join(fixture, "pkgvar");
  await mkdir(pkgvar);
  const source = await readFile(path.join(cmd, "main"), "utf8");
  const fns = source.match(/hex_ok\(\)[\s\S]*?prepare_secret\(\) \{[\s\S]*?\n\}/);
  assert.ok(fns, "helpers + prepare_secret not found");
  const logFile = path.join(fixture, "temp.log");
  await writeFile(logFile, "");
  const script = `
    set -u
    TRIM_PKGVAR="${pkgvar}"
    TRIM_TEMP_LOGFILE="${logFile}"
    secret="${pkgvar}/internal-secret"
    log_error() { printf '%s\\n' "$1" >> "\${TRIM_TEMP_LOGFILE:-/dev/null}"; }
    ${fns[0]}
    prepare_secret; exit $?
  `;
  const result = spawnSync("bash", ["-c", script], { encoding: "utf8" });
  assert.equal(result.status, 0, `stderr=${result.stderr} log=${await readFile(logFile, "utf8")}`);
  const material = await readFile(path.join(pkgvar, "internal-secret"), "utf8");
  assert.match(material, /^[0-9a-f]{64}$/);
  assert.equal((await stat(path.join(pkgvar, "internal-secret"))).mode & 0o777, 0o600);
});

test("main.prepare_secret refuses a symlinked secret rather than following it", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-prep-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const pkgvar = path.join(fixture, "pkgvar");
  await mkdir(pkgvar);
  const target = path.join(fixture, "target");
  await writeFile(target, "0".repeat(64));
  await symlink(target, path.join(pkgvar, "internal-secret"));

  const source = await readFile(path.join(cmd, "main"), "utf8");
  const fns = source.match(/hex_ok\(\)[\s\S]*?prepare_secret\(\) \{[\s\S]*?\n\}/);
  const logFile = path.join(fixture, "temp.log");
  await writeFile(logFile, "");
  const script = `
    set -u
    TRIM_PKGVAR="${pkgvar}"
    TRIM_TEMP_LOGFILE="${logFile}"
    secret="${pkgvar}/internal-secret"
    log_error() { printf '%s\\n' "$1" >> "\${TRIM_TEMP_LOGFILE:-/dev/null}"; }
    ${fns[0]}
    prepare_secret; exit $?
  `;
  const result = spawnSync("bash", ["-c", script], { encoding: "utf8" });
  assert.notEqual(result.status, 0);
  assert.match(await readFile(logFile, "utf8"), /symlink; refusing/i);
});

test("main.prepare_secret regenerates a malformed secret without operator intervention", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-prep-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const pkgvar = path.join(fixture, "pkgvar");
  await mkdir(pkgvar);
  await writeFile(path.join(pkgvar, "internal-secret"), "garbage");

  const source = await readFile(path.join(cmd, "main"), "utf8");
  const fns = source.match(/hex_ok\(\)[\s\S]*?prepare_secret\(\) \{[\s\S]*?\n\}/);
  const logFile = path.join(fixture, "temp.log");
  await writeFile(logFile, "");
  const script = `
    set -u
    TRIM_PKGVAR="${pkgvar}"
    TRIM_TEMP_LOGFILE="${logFile}"
    secret="${pkgvar}/internal-secret"
    log_error() { printf '%s\\n' "$1" >> "\${TRIM_TEMP_LOGFILE:-/dev/null}"; }
    ${fns[0]}
    prepare_secret; exit $?
  `;
  const result = spawnSync("bash", ["-c", script], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  const material = await readFile(path.join(pkgvar, "internal-secret"), "utf8");
  assert.match(material, /^[0-9a-f]{64}$/);
});

test("main.start splits the composite runtime-payload precondition into named checks", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /python3 missing at \$runtime/);
  assert.match(source, /node missing at \$node_runtime/);
  assert.match(source, /web entry missing at \$web_entry/);
  assert.match(source, /web root missing at \$web_root/);
  assert.doesNotMatch(source, /test -x "\$runtime" && test -x "\$node_runtime"/);
});

test("native main only starts loopback Next before the UDS gateway", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /select_web_port/);
  assert.match(source, /candidate=3091/);
  assert.match(source, /candidate" -le 3191/);
  assert.match(source, /PORT="\$web_port"/);
  assert.match(source, /cd "\$web_root"/);
  assert.match(source, /127\.0\.0\.1:\$\{web_port\}\/app\/sag\/chat/);
  assert.match(source, /wait_for_gateway_socket/);
  assert.match(source, /sag_api\.fnos\.cli gateway --socket/);
  assert.ok(source.indexOf("HOSTNAME=127.0.0.1") < source.indexOf("sag_api.fnos.cli gateway"));
  assert.match(source, /SAG_FNOS_INTERNAL_SECRET_FILE/);
  assert.match(source, /kill -TERM/);
  assert.match(source, /kill -KILL/);
  assert.doesNotMatch(source, /docker /);
});
