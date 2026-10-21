import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { cp, mkdir, mkdtemp, readFile, readdir, rm, writeFile, chmod } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const template = path.join(root, "packages/fnos/native/sag");
const python = process.env.PYTHON ?? "python3";

function py(script, args = [], env = process.env) {
  const result = spawnSync(python, ["-c", script, ...args], { env, encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout;
}

async function hashes(directory, prefix = "") {
  const result = {};
  for (const item of await readdir(directory, { withFileTypes: true })) {
    const relative = path.join(prefix, item.name);
    if (item.isDirectory()) Object.assign(result, await hashes(path.join(directory, item.name), relative));
    else result[relative] = createHash("sha256").update(await readFile(path.join(directory, item.name))).digest("hex");
  }
  return result;
}

async function stage(t, version) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "sag-upgrade-classification-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const pkgvar = path.join(directory, "pkgvar");
  const appdest = path.join(directory, "appdest");
  const commands = path.join(directory, "cmd");
  await mkdir(commands);
  await cp(path.join(root, "apps/api/sag_api/fnos/knowledge_compatibility.py"), path.join(commands, "knowledge_compatibility.py"));
  await mkdir(path.join(appdest, "runtime"), { recursive: true });
  await cp(path.join(template, "app/runtime/lifecycle.py"), path.join(appdest, "runtime/lifecycle.py"));
  await cp(path.join(root, "apps/api/sag_api/fnos"), path.join(appdest, "server/sag_api/fnos"), { recursive: true });
  for (const name of ["upgrade_init", "upgrade_callback"]) await cp(path.join(template, "cmd", name), path.join(commands, name));
  const lifecycleOwner = `printf '%s\\n' "$1" >> "$TRACE"
case "$1" in
  status) exit 1 ;;
  stop) : > "$TRIM_PKGVAR/.stopped" ;;
  upgrade-knowledge)
    : > "$TRIM_PKGVAR/.stopped"
    exec "$CHECK_PYTHON" "$(dirname "$0")/knowledge_compatibility.py" --root "$TRIM_PKGVAR/users"
    ;;
esac`;
  for (const [name, body] of Object.entries({ main: lifecycleOwner, install_callback: "exit 0" })) {
    await writeFile(path.join(commands, name), "#!/bin/sh\n" + body + "\n");
    await chmod(path.join(commands, name), 0o755);
  }
  const fixture = path.join(root, `apps/api/tests/fixtures/fnos_${version}/native-${version}.tar.gz`);
  py("import sys,tarfile; tarfile.open(sys.argv[1]).extractall(sys.argv[2])", [fixture, pkgvar]);
  const env = { ...process.env, TRIM_APPDEST: appdest, TRIM_PKGVAR: pkgvar,
    TRIM_APP_STATUS: "UPGRADE", TRIM_TEMP_LOGFILE: path.join(directory, "upgrade.log"), TRACE: path.join(directory, "trace"), CHECK_PYTHON: python };
  return { directory, pkgvar, appdest, commands, env };
}

async function runUpgrade(fixture, choice, callback = "upgrade_callback") {
  return spawnSync("/bin/sh", [path.join(fixture.commands, callback)], {
    env: { ...fixture.env, SAG_KNOWLEDGE_UPGRADE_MODE: choice }, encoding: "utf8", timeout: 15000,
  });
}

for (const callback of ["upgrade_init", "upgrade_callback", "install_callback"]) {
  test(`${callback} checks SQLite only after the lifecycle owner has stopped writers`, async (t) => {
    const fixture = await stage(t, "013");
    if (callback === "install_callback") {
      await cp(path.join(template, "cmd/install_callback"), path.join(fixture.commands, callback));
    }
    const checker = path.join(fixture.commands, "knowledge_compatibility.py");
    await cp(checker, checker + ".cold");
    await writeFile(checker, `import os,pathlib,runpy,sys
if not (pathlib.Path(os.environ['TRIM_PKGVAR'])/'.stopped').exists():
    sys.exit('cannot classify a live SQLite copy')
runpy.run_path(__file__+'.cold',run_name='__main__')
`);
    const before = await hashes(path.join(fixture.pkgvar, "users"));
    const result = await runUpgrade(fixture, undefined, callback);
    assert.equal(result.status, 0, result.stderr + result.stdout);
    assert.deepEqual(await hashes(path.join(fixture.pkgvar, "users")), before);
  });
}

test("old upgrade_init preserves real 0.13 data and verifies its cold backup", async (t) => {
  const fixture = await stage(t, "013");
  const lifecycle = path.join(fixture.appdest, "runtime/lifecycle.py");
  await cp(lifecycle, lifecycle + ".backup-operations");
  // The installed payload supports backup, but not the new format-check action.
  await writeFile(lifecycle, `import os,sys\nif sys.argv[1] not in ('size','backup','validate','delete','migration-ready'): sys.exit(2)\nos.execv(sys.executable,[sys.executable,__file__+'.backup-operations',*sys.argv[1:]])\n`);
  const before = await hashes(path.join(fixture.pkgvar, "users"));
  const result = await runUpgrade(fixture, "preserve", "upgrade_init");
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(await hashes(path.join(fixture.pkgvar, "users")), before);
  const archives = (await readdir(path.join(fixture.pkgvar, "backup"))).filter((name) => name.endsWith(".tar.gz"));
  assert.equal(archives.length, 1);
});

for (const choice of ["preserve", "cancel", "rebuild-legacy-071", "invalid"]) {
  test(`upgrade_init ignores historical ${choice} and only backs up legacy data`, async (t) => {
    const fixture = await stage(t, "071");
    const before = await hashes(path.join(fixture.pkgvar, "users"));
    const result = await runUpgrade(fixture, choice, "upgrade_init");
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(await hashes(path.join(fixture.pkgvar, "users")), before);
    const archives = (await readdir(path.join(fixture.pkgvar, "backup"))).filter((name) => name.endsWith(".tar.gz"));
    assert.equal(archives.length, 1);
  });

  test(`upgrade_callback dispatches one offline upgrade regardless of historical ${choice}`, async (t) => {
    const fixture = await stage(t, "071");
    const result = await runUpgrade(fixture, choice);
    assert.equal(result.status, 0, result.stderr);
    // main is the explicit test seam: real cold backup/reset/rollback are covered
    // by test_fnos_package_upgrade.py against the actual SQLite fixtures.
    assert.equal(await readFile(fixture.env.TRACE, "utf8"), "upgrade-knowledge\n");
  });

  test(`retained legacy reinstall refuses historical ${choice} without native upgrade`, async (t) => {
    const fixture = await stage(t, "071");
    await cp(path.join(template, "cmd/install_callback"), path.join(fixture.commands, "install_callback"));
    const before = await hashes(path.join(fixture.pkgvar, "users"));
    const result = spawnSync("/bin/sh", [path.join(fixture.commands, "install_callback")], {
      env: { ...fixture.env, TRIM_APP_STATUS: "INSTALL", SAG_ACCEPT_REINGEST_UPGRADE: "true", SAG_KNOWLEDGE_UPGRADE_MODE: choice },
      encoding: "utf8", timeout: 15000,
    });
    assert.notEqual(result.status, 0);
    assert.deepEqual(await hashes(path.join(fixture.pkgvar, "users")), before);
    assert.match(await readFile(fixture.env.TRIM_TEMP_LOGFILE, "utf8"), /本次安装未重建知识库/);
    assert.equal(await readFile(fixture.env.TRACE, "utf8"), "stop\n");
  });
}

test("native upgrade install phase preserves legacy data for the later offline callback", async (t) => {
  const fixture = await stage(t, "071");
  await cp(path.join(template, "cmd/install_callback"), path.join(fixture.commands, "install_callback"));
  const before = await hashes(path.join(fixture.pkgvar, "users"));
  const result = await runUpgrade(fixture, undefined, "install_callback");
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(await hashes(path.join(fixture.pkgvar, "users")), before);
  assert.equal(await readFile(fixture.env.TRACE, "utf8"), "stop\nstatus\nstop\n");
});

for (const status of [0, 1]) test(`upgrade refuses a failed stop after status ${status} before creating a cold backup`, async (t) => {
  const fixture = await stage(t, "013");
  await writeFile(path.join(fixture.commands, "main"), `#!/bin/sh
printf '%s\\n' "$1" >> "$TRACE"
if [ "$1" = status ]; then exit ${status}; fi
exit 1
`);
  const before = await hashes(path.join(fixture.pkgvar, "users"));
  const result = await runUpgrade(fixture, "preserve", "upgrade_init");
  assert.notEqual(result.status, 0, "a failed health probe does not prove the service is stopped");
  assert.deepEqual(await hashes(path.join(fixture.pkgvar, "users")), before);
  assert.equal((await readdir(fixture.pkgvar)).includes("backup"), false);
  assert.match(await readFile(fixture.env.TRIM_TEMP_LOGFILE, "utf8"), /failed to stop/);
});

test("real 0.13 without migration markers is backed up without changing data", async (t) => {
  const fixture = await stage(t, "013");
  py("import sqlite3,sys,pathlib; [(lambda c:(c.execute(\"DELETE FROM settings WHERE key='fnos_knowledge_engine_0_13'\"),c.commit(),c.close()))(sqlite3.connect(p)) for p in pathlib.Path(sys.argv[1]).glob('users/*/meta/sag.db')]", [fixture.pkgvar]);
  const before = await hashes(path.join(fixture.pkgvar, "users"));
  const result = await runUpgrade(fixture, undefined, "upgrade_init");
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(await hashes(path.join(fixture.pkgvar, "users")), before);
  const backups = (await readdir(path.join(fixture.pkgvar, "backup"))).filter((name) => name.endsWith(".tar.gz"));
  assert.equal(backups.length, 1);
  const restored = path.join(fixture.directory, "restored");
  py("import sys,tarfile; tarfile.open(sys.argv[1]).extractall(sys.argv[2])", [path.join(fixture.pkgvar, "backup", backups[0]), restored]);
  assert.deepEqual(await hashes(path.join(restored, "users")), before);
});

for (const callback of ["upgrade_init", "upgrade_callback"]) {
  for (const defect of ["unknown-modern", "corrupt-current", "empty-current"]) {
    test(`${callback} refuses ${defect} after stopping writers without changing knowledge`, async (t) => {
      const fixture = await stage(t, "013");
      py("import pathlib,sqlite3,sys; p=pathlib.Path(sys.argv[1])/'users/1000/engine-v0.13-clean/sag.db'; d=sys.argv[2]; p.write_bytes(b'broken' if d=='corrupt-current' else b'') if d!='unknown-modern' else (lambda c:(c.execute('UPDATE sag_schema_meta SET schema_version=999'),c.commit(),c.close()))(sqlite3.connect(p))", [fixture.pkgvar, defect]);
      const before = await hashes(path.join(fixture.pkgvar, "users"));
      const result = await runUpgrade(fixture, "rebuild-legacy-071", callback);
      assert.notEqual(result.status, 0);
      assert.deepEqual(await hashes(path.join(fixture.pkgvar, "users")), before);
      assert.equal(await readFile(fixture.env.TRACE, "utf8"), callback === "upgrade_init" ? "status\nstop\n" : "upgrade-knowledge\n");
      assert.equal((await readdir(fixture.pkgvar)).includes("backup"), false);
    });
  }
}

test("retained-data reinstall refuses unknown data even with a historical rebuild value", async (t) => {
  const fixture = await stage(t, "013");
  await cp(path.join(template, "cmd/install_callback"), path.join(fixture.commands, "install_callback"));
  py("import sqlite3,sys,pathlib; p=pathlib.Path(sys.argv[1])/'users/1000/engine-v0.13-clean/sag.db'; c=sqlite3.connect(p); c.execute('UPDATE sag_schema_meta SET schema_version=999'); c.commit(); c.close()", [fixture.pkgvar]);
  const before = await hashes(path.join(fixture.pkgvar, "users"));
  const result = await runUpgrade(fixture, "rebuild-legacy-071", "install_callback");
  assert.notEqual(result.status, 0);
  assert.deepEqual(await hashes(path.join(fixture.pkgvar, "users")), before);
  assert.equal(await readFile(fixture.env.TRACE, "utf8"), "stop\n");
});

for (const choice of ["cancel", "invalid"]) test(`fresh install ignores obsolete ${choice} wizard value`, async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "sag-fresh-no-wizard-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const pkgvar = path.join(directory, "pkgvar");
  await mkdir(pkgvar);
  const result = spawnSync("/bin/sh", [path.join(template, "cmd/install_callback")], {
    env: { ...process.env, TRIM_PKGVAR: pkgvar, TRIM_TEMP_LOGFILE: path.join(directory, "install.log"),
      SAG_KNOWLEDGE_UPGRADE_MODE: choice }, encoding: "utf8", timeout: 15000,
  });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(await readdir(pkgvar), ["internal-secret"]);
  assert.equal((await readFile(path.join(pkgvar, "internal-secret"))).length, 64);
});

test("upgrade callback refuses a failed offline migration without starting services", async (t) => {
  const fixture = await stage(t, "071");
  await writeFile(path.join(fixture.commands, "main"), '#!/bin/sh\nprintf "%s\\n" "$1" >> "$TRACE"\nexit 1\n');
  const before = await hashes(path.join(fixture.pkgvar, "users"));
  const result = await runUpgrade(fixture, undefined);
  assert.notEqual(result.status, 0);
  assert.equal(await readFile(fixture.env.TRACE, "utf8"), "upgrade-knowledge\n");
  assert.deepEqual(await hashes(path.join(fixture.pkgvar, "users")), before);
  assert.match(await readFile(fixture.env.TRIM_TEMP_LOGFILE, "utf8"), /knowledge update failed/);
});
