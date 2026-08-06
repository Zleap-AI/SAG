import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { chmod, cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("../..", import.meta.url));
const probe = path.join(repoRoot, "scripts/fnos-native-probe.py");

async function temporaryRoot(t, prefix) {
  const root = await mkdtemp(path.join(os.tmpdir(), prefix));
  t.after(async () => rm(root, { recursive: true, force: true }));
  return root;
}

function pythonHarness(script, arguments_, env = {}) {
  return spawnSync("python3", ["-c", script, ...arguments_], {
    cwd: repoRoot,
    encoding: "utf8",
    env: { ...process.env, ...env },
  });
}

test("probe requires every native dependency and UDS behavior", () => {
  const source = readFileSync(probe, "utf8");
  for (const module of ["lancedb", "pyarrow", "onnxruntime", "numpy", "uvloop", "orjson"])
    assert.match(source, new RegExp(`import ${module}`));
  for (const key of ["lancedb_roundtrip", "uds_http", "gateway_headers", "status"])
    assert.match(source, new RegExp(key));
});

test("probe accepts both gateway-prefixed and direct probe paths", () => {
  const source = readFileSync(probe, "utf8");
  assert.match(source, /@app\.get\("\/probe"\)/);
  assert.match(source, /@app\.get\("\/app\/sag\/probe"\)/);
});

test("probe exposes command help before native dependencies are loaded", () => {
  const result = spawnSync("python3", [probe, "serve", "--help"], {
    cwd: repoRoot,
    encoding: "utf8",
  });

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /serve/);
  assert.match(result.stdout, /--socket/);
  assert.match(result.stdout, /--output/);
});

test("serve requires an explicit socket and output path", () => {
  const result = spawnSync("python3", [probe, "serve"], {
    cwd: repoRoot,
    encoding: "utf8",
  });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /--socket/);
  assert.match(result.stderr, /--output/);
});

test("shared-object scan invokes ldd for every vendor extension before failing", async (t) => {
  const root = await temporaryRoot(t, "sag-native-probe-ldd-");
  const copiedProbe = path.join(root, "fnos-native-probe.py");
  const vendor = path.join(root, "vendor");
  const bin = path.join(root, "bin");
  const log = path.join(root, "ldd.log");
  await cp(probe, copiedProbe);
  await mkdir(vendor);
  await mkdir(bin);
  for (const name of ["good.so", "broken-one.so", "broken-two.so"])
    await writeFile(path.join(vendor, name), "not an ELF file\n");
  const fakeLdd = path.join(bin, "ldd");
  await writeFile(fakeLdd, `#!/bin/sh
printf '%s\\n' "$1" >> "$LDD_LOG"
case "$1" in
  *broken*) printf '%s\\n' 'libmissing.so => not found' ;;
  *) printf '%s\\n' 'linux-vdso.so.1 (0x0000)' ;;
esac
`);
  await chmod(fakeLdd, 0o755);

  const result = pythonHarness(`
import importlib.util
import json
import sys
spec = importlib.util.spec_from_file_location("probe", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
value = {"errors": []}
print(json.dumps({"passed": module.check_shared_objects(value), "errors": value["errors"]}))
`, [copiedProbe], {
    PATH: `${bin}:${process.env.PATH}`,
    LDD_LOG: log,
  });

  assert.equal(result.status, 0, result.stderr);
  const checked = (await readFile(log, "utf8")).trim().split("\n").map((checkedPath) => path.basename(checkedPath));
  assert.deepEqual(new Set(checked), new Set(["good.so", "broken-one.so", "broken-two.so"]));
  const value = JSON.parse(result.stdout);
  assert.equal(value.passed, false);
  assert.equal(value.errors.length, 2);
  assert.match(value.errors.join("\n"), /broken-one\.so/);
  assert.match(value.errors.join("\n"), /broken-two\.so/);
});

test("main persists failure and exits nonzero when the server stops before identity capture", async (t) => {
  const root = await temporaryRoot(t, "sag-native-probe-gateway-");
  const output = path.join(root, "native-p0.json");
  const result = pythonHarness(`
import importlib.util
import json
import pathlib
import sys
spec = importlib.util.spec_from_file_location("probe", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
value = {
    "python": "3.12.0",
    "machine": "x86_64",
    "imports": {name: True for name in module.DEPENDENCIES},
    "lancedb_roundtrip": True,
    "uds_http": True,
    "gateway_headers": {name: None for name in module.GATEWAY_HEADERS},
    "status": "pass",
    "errors": [],
}
module.run_initial_checks = lambda output: (value, None)
module.serve = lambda socket, output, result, orjson_module: None
exit_code = module.main(["serve", "--socket", sys.argv[2] + ".sock", "--output", sys.argv[2]])
persisted = json.loads(pathlib.Path(sys.argv[2]).read_text()) if pathlib.Path(sys.argv[2]).exists() else None
print(json.dumps({"exit_code": exit_code, "persisted": persisted}))
`, [probe, output]);

  assert.equal(result.status, 0, result.stderr);
  const value = JSON.parse(result.stdout);
  assert.equal(value.exit_code, 1);
  assert.equal(value.persisted.status, "fail");
  assert.match(value.persisted.errors.join("\n"), /gateway identity headers/i);
});
