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

// install_init resolves the package user from SAG_INSTALL_PACKAGE_USER so
// tests can run on hosts without a "sag" account. Production always uses
// the default "sag" (fnpack creates it before install_init from
// config/privilege).
const currentUser = os.userInfo().username;
function baseEnv(extra = {}) {
  return { ...process.env, SAG_INSTALL_PACKAGE_USER: currentUser, ...extra };
}

function run(name, env, args = []) {
  return spawnSync("bash", [path.join(cmd, name), ...args], { encoding: "utf8", env });
}
function runSh(name, env, args = []) {
  return spawnSync("/bin/sh", [path.join(cmd, name), ...args], { encoding: "utf8", env });
}

test("install_init creates one private, stable internal secret", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  const pkgvar = path.join(fixture, "pkgvar");
  await mkdir(pkgvar);
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const env = baseEnv({ TRIM_PKGVAR: pkgvar });

  const first = run("install_init", env);
  assert.equal(first.status, 0, first.stderr);
  const secretPath = path.join(pkgvar, "internal-secret");
  const material = await readFile(secretPath, "utf8");
  assert.match(material, /^[0-9a-f]{64}$/);
  assert.equal((await stat(secretPath)).mode & 0o777, 0o600);
  const second = run("install_init", env);
  assert.equal(second.status, 0, second.stderr);
  assert.equal(await readFile(secretPath, "utf8"), material);
});

test("install_init also runs under strict POSIX /bin/sh (dash on fnOS)", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  const pkgvar = path.join(fixture, "pkgvar");
  await mkdir(pkgvar);
  t.after(() => rm(fixture, { recursive: true, force: true }));

  const result = runSh("install_init", baseEnv({ TRIM_PKGVAR: pkgvar }));
  assert.equal(result.status, 0, result.stderr);
  assert.match(await readFile(path.join(pkgvar, "internal-secret"), "utf8"), /^[0-9a-f]{64}$/);
});

test("install_init grants ownership to the fnOS package user", async () => {
  const source = await readFile(path.join(cmd, "install_init"), "utf8");
  // Production installs must chown to sag; hardcoded name outside test envs.
  assert.match(source, /package_user="\$\{SAG_INSTALL_PACKAGE_USER:-sag\}"/);
  assert.match(source, /chown "\$package_user"/);
});

test("native start repairs the package-owned secret to mode 0600", async () => {
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

test("install_init discards a symlinked residue instead of aborting", async (t) => {
  // The user has authorised discarding old-install data on overwrite,
  // and cmd/main self-heals in any case — install_init just removes
  // the symlink so a valid file replaces it.
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  const pkgvar = path.join(fixture, "pkgvar");
  const target = path.join(fixture, "target");
  await mkdir(pkgvar);
  await writeFile(target, "0".repeat(64));
  await symlink(target, path.join(pkgvar, "internal-secret"));
  t.after(() => rm(fixture, { recursive: true, force: true }));

  const result = run("install_init", baseEnv({ TRIM_PKGVAR: pkgvar }));
  assert.equal(result.status, 0, result.stderr);
  const stats = await stat(path.join(pkgvar, "internal-secret"));
  assert.equal(stats.mode & 0o777, 0o600);
  assert.match(await readFile(path.join(pkgvar, "internal-secret"), "utf8"), /^[0-9a-f]{64}$/);
});

test("install_init regenerates a residue secret left by a prior Docker install", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  const pkgvar = path.join(fixture, "pkgvar");
  await mkdir(pkgvar);
  // Docker version of appname=sag may have written the file as an opaque
  // base64 blob or a JSON envelope; both fail 64-hex validation.
  await writeFile(path.join(pkgvar, "internal-secret"), '{"legacy":"docker","token":"AAAA"}');
  await chmod(path.join(pkgvar, "internal-secret"), 0o644);
  t.after(() => rm(fixture, { recursive: true, force: true }));

  const logFile = path.join(fixture, "install.log");
  await writeFile(logFile, "");
  const result = run("install_init", baseEnv({ TRIM_PKGVAR: pkgvar, TRIM_TEMP_LOGFILE: logFile }));
  assert.equal(result.status, 0, result.stderr);
  const material = await readFile(path.join(pkgvar, "internal-secret"), "utf8");
  assert.match(material, /^[0-9a-f]{64}$/);
  assert.equal((await stat(path.join(pkgvar, "internal-secret"))).mode & 0o777, 0o600);
  const log = await readFile(logFile, "utf8");
  assert.match(log, /regenerating/i);
});

test("install_init regenerates a truncated secret rather than dying at the length check", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  const pkgvar = path.join(fixture, "pkgvar");
  await mkdir(pkgvar);
  await writeFile(path.join(pkgvar, "internal-secret"), "abcdef"); // 6 bytes
  t.after(() => rm(fixture, { recursive: true, force: true }));

  const result = run("install_init", baseEnv({ TRIM_PKGVAR: pkgvar }));
  assert.equal(result.status, 0, result.stderr);
  assert.match(await readFile(path.join(pkgvar, "internal-secret"), "utf8"), /^[0-9a-f]{64}$/);
});

test("install_init writes a human-readable cause to TRIM_TEMP_LOGFILE when it must abort", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const logFile = path.join(fixture, "install.log");
  await writeFile(logFile, "");

  const result = run("install_init", { ...process.env, TRIM_TEMP_LOGFILE: logFile, TRIM_PKGVAR: "" });
  assert.notEqual(result.status, 0);
  const log = await readFile(logFile, "utf8");
  assert.match(log, /TRIM_PKGVAR/i);
});

test("install_init refuses a relative TRIM_PKGVAR instead of writing to /internal-secret", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const result = run("install_init", baseEnv({ TRIM_PKGVAR: "relative/var" }));
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /absolute path/i);
});

test("install_init warns but does not abort when the package user is missing", async (t) => {
  // main.prepare_secret now self-heals under the process uid, so an
  // absent package user (unusual — fnpack normally provisions it from
  // config/privilege) is no longer fatal at install time.
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  const pkgvar = path.join(fixture, "pkgvar");
  await mkdir(pkgvar);
  t.after(() => rm(fixture, { recursive: true, force: true }));

  const logFile = path.join(fixture, "install.log");
  await writeFile(logFile, "");
  const result = run("install_init", {
    ...process.env,
    TRIM_PKGVAR: pkgvar,
    TRIM_TEMP_LOGFILE: logFile,
    SAG_INSTALL_PACKAGE_USER: "sag_missing_user_for_test_12345",
  });
  assert.equal(result.status, 0, result.stderr);
  const log = await readFile(logFile, "utf8");
  assert.match(log, /package user.*does not exist/i);
  assert.match(log, /main will self-heal/i);
});

test("install_init tolerates a missing group for the package user", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  const pkgvar = path.join(fixture, "pkgvar");
  await mkdir(pkgvar);
  t.after(() => rm(fixture, { recursive: true, force: true }));

  // Use the current user but claim a nonexistent group by setting the
  // package user to a name whose primary group won't happen to match
  // "sag_missing_group_...". We can't really force getent to lie without
  // faking a chroot, but we can assert the script has no un-guarded
  // "sag:sag" chown that would blow up under `set -e` on missing group.
  const source = await readFile(path.join(cmd, "install_init"), "utf8");
  assert.doesNotMatch(source, /chown[^\n]*"\$package_user":"\$package_user"/);
  assert.doesNotMatch(source, /chown[^\n]*sag:sag/);
  assert.match(source, /chgrp[^\n]*\|\| :/);
});

test("install_init scrubs a root-owned residue secret from the legacy @appconf path", async (t) => {
  // The real-hardware failure on .19 was a root-owned secret at
  // /vol1/@appconf/sag/internal-secret (TRIM_PKGETC) that fnpack did
  // not re-normalize on overwrite install. .20 stops using @appconf
  // entirely and best-effort removes the residue so it can't be
  // mistaken for the current one by a downgrade-then-upgrade path.
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  const pkgvar = path.join(fixture, "pkgvar");
  const pkgetc = path.join(fixture, "pkgetc");
  await mkdir(pkgvar);
  await mkdir(pkgetc);
  await writeFile(path.join(pkgetc, "internal-secret"), "legacy");
  t.after(() => rm(fixture, { recursive: true, force: true }));

  const result = run("install_init", baseEnv({ TRIM_PKGVAR: pkgvar, TRIM_PKGETC: pkgetc }));
  assert.equal(result.status, 0, result.stderr);
  assert.equal(existsSync(path.join(pkgetc, "internal-secret")), false, "legacy @appconf residue must be removed");
  assert.match(await readFile(path.join(pkgvar, "internal-secret"), "utf8"), /^[0-9a-f]{64}$/);
});

test("install_init recursively normalizes TRIM_PKGVAR ownership to the package user", async (t) => {
  // Docker-era SAG wrote /vol1/@appdata/sag as root inside a container.
  // On Docker→Native overwrite install the sag package user (uid 984)
  // cannot read those files; the gateway then crashes on the first
  // sqlite open and status returns "gateway process is not running"
  // with no other clue. install_init recursively chowns the tree to
  // sag so start-up just works — the fix that unblocks Docker→Native
  // covers without asking the user to uninstall first.
  const source = await readFile(path.join(cmd, "install_init"), "utf8");
  assert.match(source, /chown -R "\$package_user" "\$TRIM_PKGVAR"/);
  // Must be best-effort — a partial failure logs and continues, so the
  // rest of install_init (secret provisioning etc.) still runs.
  assert.match(source, /recursive chown of \$TRIM_PKGVAR to \$package_user failed \(non-fatal/);
  // And it must sit before secret provisioning so the secret write
  // itself lands under sag's uid on a Docker-residue tree.
  const chownIdx = source.indexOf('chown -R "$package_user" "$TRIM_PKGVAR"');
  const provisionIdx = source.indexOf("secret-provisioning");
  assert.ok(chownIdx > 0 && provisionIdx > 0 && chownIdx < provisionIdx,
    "recursive chown must run before secret provisioning");
});

test("install_init actually chowns Docker-residue files it finds under TRIM_PKGVAR", async (t) => {
  // Full round-trip: pre-seed a Docker-shape tree with a couple of
  // stray files, run install_init as the current user (posing as the
  // package user via SAG_INSTALL_PACKAGE_USER), and confirm the tree
  // ends up owned by that user. We can't test the actual UID transition
  // on macOS without sudo, so this asserts the code path fires without
  // erroring rather than the OS-level uid delta.
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-chown-"));
  const pkgvar = path.join(fixture, "pkgvar");
  await mkdir(pkgvar);
  await mkdir(path.join(pkgvar, "users"));
  await writeFile(path.join(pkgvar, "users", "docker-residue.db"), "sqlite-residue");
  await mkdir(path.join(pkgvar, "logs"));
  await writeFile(path.join(pkgvar, "logs", "app.log"), "old");
  t.after(() => rm(fixture, { recursive: true, force: true }));

  const logFile = path.join(fixture, "install.log");
  await writeFile(logFile, "");
  const result = run("install_init", baseEnv({ TRIM_PKGVAR: pkgvar, TRIM_TEMP_LOGFILE: logFile }));
  assert.equal(result.status, 0, `stderr=${result.stderr} log=${await readFile(logFile, "utf8")}`);
  // Residue files must still exist (data preserved, only ownership normalized).
  assert.equal(existsSync(path.join(pkgvar, "users", "docker-residue.db")), true);
  assert.equal(existsSync(path.join(pkgvar, "logs", "app.log")), true);
  // Secret provisioning still succeeded on top of the chowned tree.
  assert.match(await readFile(path.join(pkgvar, "internal-secret"), "utf8"), /^[0-9a-f]{64}$/);
});

test("main.start reports gateway early-exit with a tail of gateway.log", async () => {
  // Prior to .21 a gateway that died within the first second (permission
  // denied on sqlite, native dep import error, config parse crash) was
  // invisible until wait_for_gateway_socket timed out 15s later with the
  // useless "gateway process is not running" line. .21 does a kill -0
  // 1 second after launch and, if the process is gone, dumps the tail of
  // gateway.log so the operator has a real errno to act on.
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /gw_pid=\$!/);
  assert.match(source, /kill -0 "\$gw_pid"/);
  assert.match(source, /tail -n 20 "\$gateway_log"/);
  assert.match(source, /gateway process exited immediately after launch/);
  assert.match(source, /rm -f "\$gateway_pid" "\$gateway_socket"/);
  // .22 also arms a crash trap for the whole start path so exits from
  // *any* step — not just the gateway launch — leave a breadcrumb.
  assert.match(source, /_start_crash_trap/);
  assert.match(source, /SAG Native start exited with code/);
});

test("main.start verifies the gateway log is writable before launching anything", async () => {
  // A redirect failure on >> gateway.log would swallow uvicorn's stderr
  // (the real PermissionError / ImportError) and leave the log empty —
  // the operator would see "没有任何日志" even though the root cause was
  // captured by the EXIT trap. Pre-touch guarantees the file exists and
  // is writable before we depend on the redirect.
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /touch "\$gateway_log"/);
  assert.match(source, /cannot write gateway log at/);
});

test("main.start warns when the gateway socket directory is not writable", async () => {
  // uvicorn creates app.sock in TRIM_APPDEST. If sag can't write there,
  // uvicorn dies with PermissionError. Surface a pre-launch warning so
  // the operator knows what's coming even if the actual stderr is lost.
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /check-socket-dir/);
  assert.match(source, /not writable by uid=/);
});

test("main.start fails early with a clear message when mkdir of run/log dirs fails", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  // mkdir -p must be checked, not fire-and-forget.
  assert.match(source, /mkdir -p "\$run_dir" "\$log_dir"/);
  assert.match(source, /cannot create run\/log dirs under/);
});

test("main.start wait_for_web failure writes a log_error with web log tail", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  // Prior to .22 wait_for_web failure just exit 1'd silently.
  assert.match(source, /web process did not become ready within 15s/);
  assert.match(source, /tail -n 20 "\$web_log"/);
});

test("wait_for_gateway_socket detects a dead gateway process early instead of looping 15 more seconds", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  // If the process died during the wait loop, don't waste 14 more
  // seconds — capture the log and return immediately.
  assert.match(source, /gateway process died during startup/);
  assert.match(source, /valid_pid "\$gateway_pid" "sag_api\.fnos\.cli"/);
  // The 15s timeout also writes a log_error now (was silent exit 1 before).
  assert.match(source, /did not become ready within 15s/);
});

test("main.start EXIT trap captures pid / dir stats / gw log on any non-zero exit", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  // The trap fires on *any* unexpected non-zero exit, not just specific
  // guarded points. It captures enough context to diagnose root-owned
  // residues, missing env vars, permission failures, etc.
  assert.match(source, /trap _start_crash_trap EXIT/);
  assert.match(source, /UID=\$\(id -u/);
  assert.match(source, /GW log:/);
  assert.match(source, /Secret: \$\(ls -l "\$secret"/);
  // The trap is disarmed on success so stop/status don't inherit it.
  assert.match(source, /trap - EXIT/);
});

// -----------------------------------------------------------------
// upgrade_init: Docker→Native transition tolerance
// -----------------------------------------------------------------
// The user-observed failure on real N150 hardware: with Docker sag
// still installed, .16 aborted with "无法更新 SAG：SAG Native gateway
// process is not running". Two root causes:
//   (a) "$main" status writes via log_error → TRIM_TEMP_LOGFILE
//       (bypasses stderr; 2>&1 cannot mask it). fnpack reads the
//       log and displays that text as the upgrade error.
//   (b) python3 lifecycle.py size --root "$users" hard-fails when
//       the Native users directory does not exist yet (Docker→Native
//       transition).
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
  // Real upgrade_init copied into the scratch dir so its
  // "$command_dir/main" and "$command_dir/install_init" resolve to
  // our stubs, not the real binaries.
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
    SAG_INSTALL_PACKAGE_USER: currentUser,
  };
  return { fixture, cmdDir, appdest, pkgvar, pkgetc, logFile, env };
}

async function writeStubMain(cmdDir, { statusExit }) {
  // The stub reproduces the real main's telltale behavior: when
  // status returns non-zero, it appends the "gateway process is not
  // running" message *to TRIM_TEMP_LOGFILE*, not to stderr. That is
  // exactly the leak upgrade_init must guard against by redirecting
  // TRIM_TEMP_LOGFILE for the duration of the status probe.
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

async function writeStubInstallInit(cmdDir) {
  // install_init needs to succeed for upgrade_init to proceed past
  // its first step. The real install_init handles secret provisioning
  // but is validated separately; here we just no-op it.
  await writeFile(path.join(cmdDir, "install_init"), "#!/bin/sh\nexit 0\n");
  await chmod(path.join(cmdDir, "install_init"), 0o755);
}

test("upgrade_init redirects TRIM_TEMP_LOGFILE while probing main status", async (t) => {
  const source = await readFile(path.join(cmd, "upgrade_init"), "utf8");
  // The status probe must run with TRIM_TEMP_LOGFILE pointed at
  // /dev/null so main's log_error() cannot pollute the fnpack UI.
  assert.match(source, /TRIM_TEMP_LOGFILE=\/dev\/null[^\n]*"\$main" status/);
});

test("upgrade_init tolerates a missing Native users directory (Docker→Native transition)", async (t) => {
  const { cmdDir, logFile, env } = await stageUpgradeFixture(t);
  await writeStubInstallInit(cmdDir);
  // Docker version is installed, Native has never run: TRIM_PKGVAR/users
  // does not exist, and "main status" fails writing its complaint via
  // log_error(). Both must be handled without user-visible failure.
  await writeStubMain(cmdDir, { statusExit: 3 });

  const result = spawnSync("bash", [path.join(cmdDir, "upgrade_init")], { encoding: "utf8", env });
  assert.equal(result.status, 0, `upgrade_init should succeed on Docker→Native transition. stderr=${result.stderr} log=${await readFile(logFile, "utf8")}`);
  const log = await readFile(logFile, "utf8");
  // The user-facing surface must NOT contain the status probe's noise.
  assert.doesNotMatch(log, /gateway process is not running/i);
  // An informational note tells operators (and future us) what happened.
  assert.match(log, /no Native users directory/i);
});

test("upgrade_init also handles the empty users directory case without invoking lifecycle.py", async (t) => {
  const { cmdDir, pkgvar, logFile, env } = await stageUpgradeFixture(t);
  await writeStubInstallInit(cmdDir);
  await writeStubMain(cmdDir, { statusExit: 3 });
  await mkdir(path.join(pkgvar, "users"));
  // No lifecycle.py present under TRIM_APPDEST/runtime — if the
  // script tried to invoke it the run would fail. It must not.

  const result = spawnSync("bash", [path.join(cmdDir, "upgrade_init")], { encoding: "utf8", env });
  assert.equal(result.status, 0, `upgrade_init should skip backup on an empty users dir. stderr=${result.stderr}`);
  assert.match(await readFile(logFile, "utf8"), /empty/i);
});

test("upgrade_init parses under strict /bin/sh (dash on fnOS)", () => {
  assert.equal(spawnSync("/bin/sh", ["-n", path.join(cmd, "upgrade_init")]).status, 0);
});

test("upgrade_init never exits silently — every non-zero exit writes a cause to TRIM_TEMP_LOGFILE", async (t) => {
  // fnpack surfaces empty TRIM_TEMP_LOGFILE as the useless
  // "执行脚本出错且原因未知" message. Whatever else changes about
  // this script, that regression must not come back.
  const { cmdDir, logFile, env } = await stageUpgradeFixture(t);
  await writeStubInstallInit(cmdDir);
  await writeStubMain(cmdDir, { statusExit: 3 });
  // Force a failure at the first environment gate by clearing an
  // essential fnpack variable. Any failure will do; we're asserting
  // the *shape* of the log, not the specific message.
  const result = spawnSync("bash", [path.join(cmdDir, "upgrade_init")], {
    encoding: "utf8",
    env: { ...env, TRIM_APPDEST: "" },
  });
  assert.notEqual(result.status, 0);
  const log = await readFile(logFile, "utf8");
  // Some concrete cause plus the aborting-with-status marker must
  // be present so fnpack has real content to show the user.
  assert.match(log, /upgrade_init:/);
  assert.match(log, /TRIM_APPDEST/i);
  assert.match(log, /aborting with status/i);
});

test("upgrade_init tolerates a Docker-era TRIM_APPDEST without lifecycle.py", async (t) => {
  // fnpack may run upgrade_init BEFORE swapping the new package
  // payload into TRIM_APPDEST, so runtime/lifecycle.py can be
  // missing even when there IS Native user data. The script must
  // not blow up python3 on a nonexistent lifecycle.py in that
  // window — just skip the backup and let fnpack proceed.
  const { cmdDir, pkgvar, logFile, env } = await stageUpgradeFixture(t);
  await writeStubInstallInit(cmdDir);
  await writeStubMain(cmdDir, { statusExit: 3 });
  await mkdir(path.join(pkgvar, "users", "user-x"), { recursive: true });
  // Note: fixture does NOT create appdest/runtime/lifecycle.py.

  const result = spawnSync("bash", [path.join(cmdDir, "upgrade_init")], { encoding: "utf8", env });
  assert.equal(result.status, 0, `stderr=${result.stderr} log=${await readFile(logFile, "utf8")}`);
  assert.match(await readFile(logFile, "utf8"), /lifecycle\.py not present/i);
});

test("Native lifecycle shell scripts parse cleanly", () => {
  for (const name of ["main", "install_init", "install_callback", "upgrade_init", "upgrade_callback", "uninstall_init", "uninstall_callback", "config_init", "config_callback"]) {
    assert.equal(spawnSync("bash", ["-n", path.join(cmd, name)]).status, 0, name);
  }
});

test("install_init parses under strict /bin/sh (dash) as well as bash", () => {
  assert.equal(spawnSync("/bin/sh", ["-n", path.join(cmd, "install_init")]).status, 0);
});

// -----------------------------------------------------------------
// .19: cross-user secret readability
// -----------------------------------------------------------------
// The .18 real-device failure was "SAG Native internal identity secret
// is unavailable or has unsafe permissions" at start time. Root cause:
// install_init runs as root but main runs as the sag package user; the
// dir TRIM_PKGETC was left 0700 root:root by root's umask, so sag could
// not even traverse it. The perms mismatch was invisible to install
// (which returned 0) — it only surfaced hours later at start button.
// These tests pin the fix so a future refactor cannot silently regress
// back to a 0700-dir installation or a generic prepare_secret message.
test("install_init forces TRIM_PKGVAR to 0755 and chowns it to the package user", async (t) => {
  const source = await readFile(path.join(cmd, "install_init"), "utf8");
  assert.match(source, /chmod 0755 "\$TRIM_PKGVAR"/);
  assert.match(source, /chown "\$package_user" "\$TRIM_PKGVAR"/);
});

test("install_init self-checks that the package user can read the secret but does not abort on failure", async (t) => {
  const source = await readFile(path.join(cmd, "install_init"), "utf8");
  assert.match(source, /cross-user-selfcheck/);
  assert.match(source, /runuser -u \$package_user --/);
  assert.match(source, /su -s \/bin\/sh \$package_user -c/);
  // .19 wanted this to `die`. .20 downgrades to a warning because
  // main.prepare_secret self-heals in the same shot.
  assert.match(source, /main\.start will self-heal/);
});

test("install_init cross-user self-check succeeds against the current user", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-selfcheck-"));
  const pkgvar = path.join(fixture, "pkgvar");
  await mkdir(pkgvar);
  t.after(() => rm(fixture, { recursive: true, force: true }));

  const logFile = path.join(fixture, "install.log");
  await writeFile(logFile, "");
  const result = run("install_init", baseEnv({ TRIM_PKGVAR: pkgvar, TRIM_TEMP_LOGFILE: logFile }));
  assert.equal(result.status, 0, `stderr=${result.stderr} log=${await readFile(logFile, "utf8")}`);
  assert.equal((await stat(pkgvar)).mode & 0o777, 0o755);
  assert.equal((await stat(path.join(pkgvar, "internal-secret"))).mode & 0o777, 0o600);
});

test("main.prepare_secret self-heals a missing / unreadable / malformed secret", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  // .20: main is the single source of truth. It must recover, not
  // itemise-and-die like .19 did.
  assert.match(source, /prepare_secret returns 0 iff main can safely hand the secret/);
  assert.match(source, /Slow \/ recovery path — regenerate/);
  assert.match(source, /generate_secret_material/);
  // The old .19 composite line that hid causes must not reappear.
  assert.doesNotMatch(source, /internal identity secret is unavailable or has unsafe permissions/);
});

test("main.start splits the composite runtime-payload precondition into named checks", async () => {
  const source = await readFile(path.join(cmd, "main"), "utf8");
  assert.match(source, /python3 missing at \$runtime/);
  assert.match(source, /node missing at \$node_runtime/);
  assert.match(source, /web entry missing at \$web_entry/);
  assert.match(source, /web root missing at \$web_root/);
  assert.doesNotMatch(source, /test -x "\$runtime" && test -x "\$node_runtime"/);
});

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

