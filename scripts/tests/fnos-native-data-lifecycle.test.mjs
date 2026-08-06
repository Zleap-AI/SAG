import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../..", import.meta.url));
const tool = path.join(root, "packages/fnos/native/sag/app/runtime/lifecycle.py");

function run(args, env) {
  return spawnSync("python3", [tool, ...args], { encoding: "utf8", env });
}

test("backup and validation preserve a two-user tree without changing it", async (t) => {
  const pkg = await mkdtemp(path.join(os.tmpdir(), "sag-native-data-"));
  const users = path.join(pkg, "users");
  await mkdir(path.join(users, "1000/meta"), { recursive: true });
  await mkdir(path.join(users, "1001/uploads"), { recursive: true });
  await writeFile(path.join(users, "1000/meta/sag.db"), "not-a-real-sqlite");
  await writeFile(path.join(users, "1001/uploads/.hidden"), "private");
  t.after(() => rm(pkg, { recursive: true, force: true }));
  const env = { ...process.env, TRIM_PKGVAR: pkg };
  const before = await readFile(path.join(users, "1001/uploads/.hidden"), "utf8");

  const archive = path.join(pkg, "backup/users.tar.gz.tmp");
  assert.equal(run(["backup", "--root", users, "--output", archive], env).status, 0);
  // Invalid SQLite must block a restore without altering live data.
  assert.notEqual(run(["validate", "--archive", archive], env).status, 0);
  assert.equal(await readFile(path.join(users, "1001/uploads/.hidden"), "utf8"), before);
  assert.equal((await stat(archive)).mode & 0o777, 0o600);
});

test("delete requires explicit consent and keeps live data otherwise", async (t) => {
  const pkg = await mkdtemp(path.join(os.tmpdir(), "sag-native-data-"));
  const users = path.join(pkg, "users");
  await mkdir(path.join(users, "1000/meta"), { recursive: true });
  await writeFile(path.join(users, "1000/meta/value"), "private");
  t.after(() => rm(pkg, { recursive: true, force: true }));

  assert.notEqual(run(["delete", "--root", users], { ...process.env, TRIM_PKGVAR: pkg }).status, 0);
  assert.equal(await readFile(path.join(users, "1000/meta/value"), "utf8"), "private");
  assert.equal(run(["delete", "--root", users], { ...process.env, TRIM_PKGVAR: pkg, SAG_DELETE_DATA: "true" }).status, 0);
  assert.deepEqual(await (await import("node:fs/promises")).readdir(users), []);
});
