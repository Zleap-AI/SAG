import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { chmod, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repo = fileURLToPath(new URL("../..", import.meta.url));
const sampler = path.join(repo, "scripts/fnos-fd-sample.py");

test("CI runs the manual FD sampler regression suite", async () => {
  const workflow = await readFile(path.join(repo, ".github/workflows/ci.yml"), "utf8");
  assert.ok([...workflow.matchAll(/^\s+run: node --test (.+)$/gm)]
    .some(([, args]) => args.split(/\s+/).includes("scripts/tests/fnos-fd-sample.test.mjs")),
  "CI must execute fnos-fd-sample.test.mjs with node --test");
});

function harness(body, args = []) {
  return spawnSync("python3", ["-c", `import pathlib,runpy,sys\nmodule=runpy.run_path(sys.argv[1])\n${body}`, sampler, ...args], {
    cwd: repo, encoding: "utf8",
  });
}

function processStat(start = 123) {
  const fields = Array(20).fill("0");
  fields[0] = "S";
  fields[19] = String(start);
  return `777 (private name with ) parentheses) ${fields.join(" ")}\n`;
}

async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "sag-fd-sample-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const processRoot = path.join(root, "777");
  const fd = path.join(processRoot, "fd");
  await mkdir(fd, { recursive: true });
  await writeFile(path.join(processRoot, "stat"), processStat());
  await writeFile(path.join(processRoot, "limits"), "Limit                     Soft Limit           Hard Limit           Units\nMax open files            4096                 unlimited            files\n");
  return { root, processRoot, fd };
}

test("FD sampler classifies aggregate counts without exposing paths, socket IDs or process names", async (t) => {
  const f = await fixture(t);
  const sqlite = path.join(f.root, "customer-secret.sqlite-wal");
  const lance = path.join(f.root, "customer-secret.lance", "_versions", "1.manifest");
  const regular = path.join(f.root, "customer-secret.txt");
  await mkdir(path.dirname(lance), { recursive: true });
  for (const file of [sqlite, lance, regular]) await writeFile(file, "private content");
  const targets = ["socket:[987654]", "pipe:[987655]", "anon_inode:[eventpoll]", sqlite, lance, regular, "/dev/null"];
  for (const [index, target] of targets.entries()) await symlink(target, path.join(f.fd, String(index)));
  const result = harness("import json\nprint(json.dumps(module['sample_process'](777, proc_root=pathlib.Path(sys.argv[2]))))", [f.root]);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), {
    pid: 777, start_time_ticks: 123, total: 7,
    counts: { socket: 1, pipe: 1, anon_inode: 1, sqlite: 1, lancedb: 1, regular: 1, other: 1 },
    nofile: { soft: 4096, hard: "unlimited" }, unresolved: { vanished: 0, permission_denied: 0 },
  });
  assert.doesNotMatch(result.stdout, /customer-secret|987654|private name|private content/);
  assert.ok(!result.stdout.includes(f.root));
});

test("FD sampler tolerates a descriptor disappearing during readlink", async (t) => {
  const f = await fixture(t);
  await symlink("socket:[123]", path.join(f.fd, "9"));
  const result = harness(`import json,os
from unittest.mock import patch
original=os.readlink
def disappear(name, *args, **kwargs):
    pathlib.Path(name).unlink()
    return original(name, *args, **kwargs)
with patch('os.readlink', side_effect=disappear):
    print(json.dumps(module['sample_process'](777, proc_root=pathlib.Path(sys.argv[2]))))`, [f.root]);
  assert.equal(result.status, 0, result.stderr);
  const row = JSON.parse(result.stdout);
  assert.equal(row.total, 1);
  assert.equal(row.counts.other, 1);
  assert.equal(row.unresolved.vanished, 1);
});

test("FD sampler stops when the target PID is reused between samples", async (t) => {
  const f = await fixture(t);
  const result = harness(`from unittest.mock import patch
stat=pathlib.Path(sys.argv[2])/'777'/'stat'
def replace(_interval):
    stat.write_text(stat.read_text().replace(' 123\\n', ' 456\\n'))
with patch('time.sleep', side_effect=replace):
    sys.exit(module['main'](['777','--count','3','--interval','0.01'], proc_root=pathlib.Path(sys.argv[2])))`, [f.root]);
  assert.equal(result.status, 1, result.stderr);
  const rows = result.stdout.trim().split("\n").map(JSON.parse);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].status, "ok");
  assert.equal(rows[0].start_time_ticks, 123);
  assert.equal(rows[1].status, "process_replaced");
  assert.ok(!result.stdout.includes(f.root));
});

test("FD sampler reports a missing process without a traceback or physical paths", async (t) => {
  const f = await fixture(t);
  const result = harness("sys.exit(module['main'](['778','--count','1'], proc_root=pathlib.Path(sys.argv[2])))", [f.root]);
  assert.equal(result.status, 1, result.stderr);
  assert.equal(JSON.parse(result.stdout).status, "process_gone");
  assert.doesNotMatch(result.stderr, /Traceback/);
  assert.ok(!`${result.stdout}${result.stderr}`.includes(f.root));
});

test("FD sampler reports denied proc access without weakening permissions", async (t) => {
  if (process.getuid?.() === 0) return t.skip("root bypasses directory permissions; run as package user");
  const f = await fixture(t);
  await chmod(f.fd, 0o000);
  try {
    const result = harness("sys.exit(module['main'](['777','--count','1'], proc_root=pathlib.Path(sys.argv[2])))", [f.root]);
    assert.equal(result.status, 1, result.stderr);
    assert.equal(JSON.parse(result.stdout).status, "permission_denied");
    assert.ok(!`${result.stdout}${result.stderr}`.includes(f.root));
  } finally {
    await chmod(f.fd, 0o700);
  }
});

test("FD sampler validates positive finite arguments before reading proc", () => {
  for (const args of [["0"], ["-1"], ["123", "--count", "0"], ["123", "--count", "-1"], ["123", "--interval", "0"], ["123", "--interval", "nan"], ["123", "--interval", "inf"]]) {
    const result = spawnSync("python3", [sampler, ...args], { encoding: "utf8" });
    assert.equal(result.status, 2, result.stderr);
    assert.equal(result.stdout, "");
    assert.match(result.stderr, /positive|finite/i);
  }
});

test("FD sampler emits only the requested finite sample count", async (t) => {
  const f = await fixture(t);
  const result = harness("sys.exit(module['main'](['777','--count','2','--interval','0.001'], proc_root=pathlib.Path(sys.argv[2])))", [f.root]);
  assert.equal(result.status, 0, result.stderr);
  const rows = result.stdout.trim().split("\n").map(JSON.parse);
  assert.deepEqual(rows.map((row) => row.sample), [1, 2]);
  assert.ok(rows.every((row) => row.status === "ok" && row.timestamp));
});

test("FD sampler counts a real Linux child holding file, pipe and socket descriptors", (t) => {
  if (process.platform !== "linux") return t.skip("requires Linux /proc");
  const result = harness(`import json,os,socket,subprocess,tempfile
child_code="""import json,os,socket,sys,tempfile
file=tempfile.TemporaryFile()
pipe=os.pipe()
sock=socket.socket()
print(os.getpid(),flush=True)
sys.stdin.read()
"""
child=subprocess.Popen([sys.executable,'-c',child_code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
try:
    pid=int(child.stdout.readline())
    sampled=subprocess.run([sys.executable,sys.argv[1],str(pid),'--count','2','--interval','0.001'],text=True,capture_output=True)
    print(sampled.stdout,end='')
    sys.stderr.write(sampled.stderr)
    sys.exit(sampled.returncode)
finally:
    child.terminate()
    child.wait(timeout=5)`);
  assert.equal(result.status, 0, result.stderr);
  const rows = result.stdout.trim().split("\n").map(JSON.parse);
  assert.equal(rows.length, 2);
  assert.ok(rows.every((row) => row.counts.socket >= 1 && row.counts.pipe >= 2 && row.counts.regular >= 1));
  assert.ok(rows.every((row) => row.total === Object.values(row.counts).reduce((sum, count) => sum + count, 0)));
  assert.equal(rows[0].start_time_ticks, rows[1].start_time_ticks);
});
