import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

test("x86 vendor uses a hashed compat-only dependency overlay without changing ARM", async () => {
  const { vendorDependencyPlan } = await import("../build-fnos-native-vendor.mjs");
  const x86 = vendorDependencyPlan("linux/amd64");
  assert.deepEqual(x86.excluded, ["lancedb", "lance-namespace", "lance-namespace-urllib3-client"]);
  assert.deepEqual(x86.installFlags, ["--no-deps", "--require-hashes"]);
  assert.equal(x86.keepHashes, true);
  const arm = vendorDependencyPlan("linux/arm64");
  assert.deepEqual(arm.excluded, []);
  assert.deepEqual(arm.installFlags, []);
  assert.equal(arm.keepHashes, false);
});

test("x86 compatibility overlay pins complete official wheels and their PyPI hashes", async () => {
  const text = await readFile(new URL("../fnos-x86-vendor-requirements.txt", import.meta.url), "utf8");
  const rows = text.split("\n").filter((line) => line && !line.startsWith("#"));
  assert.equal(rows.length, 3);
  assert.ok(rows.every((line) => / @ https:\/\/files\.pythonhosted\.org\/.*\.whl --hash=sha256:[a-f0-9]{64}$/.test(line)));
  assert.match(rows[0], /^lancedb-compat @ .*lancedb_compat-0\.41\.0-.*x86_64\.whl /);
  assert.match(rows[1], /^lance-namespace @ .*lance_namespace-0\.13\.0-py3-none-any\.whl /);
  assert.match(rows[2], /^lance-namespace-urllib3-client @ .*0\.13\.0-py3-none-any\.whl /);
});

for (const platform of ["linux/amd64", "linux/arm64"]) {
  test(`${platform} builder installs once and validates only the x86 replacement`, async (t) => {
    const directory = await mkdtemp(path.join(os.tmpdir(), "fnos-vendor-contract-"));
    t.after(() => rm(directory, { recursive: true, force: true }));
    const bin = path.join(directory, "bin");
    await mkdir(bin);
    const log = path.join(directory, "calls.jsonl");
    const requirements = path.join(directory, "installed.txt");
    for (const command of ["uv", "python"]) {
      const script = path.join(bin, command);
      await writeFile(script, `#!${process.execPath}
const fs = require("node:fs");
const args = process.argv.slice(2);
fs.appendFileSync(process.env.CALLS, JSON.stringify({command: ${JSON.stringify(command)}, args}) + "\\n");
if (args[0] === "export") fs.writeFileSync(args[args.indexOf("--output-file") + 1], "base==1 --hash=sha256:abc\\n");
if (args[0] === "pip") fs.copyFileSync(args[args.indexOf("-r") + 1], process.env.INSTALLED_REQUIREMENTS);
`);
      await chmod(script, 0o755);
    }
    const result = spawnSync(process.execPath, [new URL("../build-fnos-native-vendor.mjs", import.meta.url).pathname,
      "--platform", platform, "--output", path.join(directory, "vendor")], {
      env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, PYTHON: path.join(bin, "python"),
        CALLS: log, INSTALLED_REQUIREMENTS: requirements }, encoding: "utf8",
    });
    assert.equal(result.status, 0, result.stderr);
    const calls = (await readFile(log, "utf8")).trim().split("\n").map(JSON.parse);
    assert.equal(calls.filter((item) => item.args[0] === "pip").length, 1);
    const exported = calls.find((item) => item.args[0] === "export").args;
    const installed = calls.find((item) => item.args[0] === "pip").args;
    const text = await readFile(requirements, "utf8");
    assert.ok(exported.includes("--frozen"));
    if (platform === "linux/amd64") {
      assert.equal(exported.includes("--no-hashes"), false);
      for (const name of ["lancedb", "lance-namespace", "lance-namespace-urllib3-client"]) {
        assert.ok(exported.some((value, index) => value === "--no-emit-package" && exported[index + 1] === name));
      }
      assert.ok(installed.includes("--no-deps") && installed.includes("--require-hashes"));
      assert.match(text, /lancedb-compat @/);
      assert.equal(calls.filter((item) => item.command === "python").length, 1);
    } else {
      assert.ok(exported.includes("--no-hashes"));
      assert.equal(exported.includes("--no-emit-package"), false);
      assert.equal(installed.includes("--no-deps"), false);
      assert.doesNotMatch(text, /lancedb-compat/);
      assert.equal(calls.filter((item) => item.command === "python").length, 0);
    }
    assert.ok(text.startsWith("base==1 --hash=sha256:abc\n"));
  });
}
