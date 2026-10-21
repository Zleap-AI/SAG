// Exercise the shipped Next runtime without user data, API or development modules.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawn, spawnSync } from "node:child_process";
import { createReadStream } from "node:fs";
import { lstat, readdir, readlink } from "node:fs/promises";
import { createServer } from "node:net";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const app = process.argv[2] && path.resolve(process.argv[2]);
if (!app || process.platform !== "darwin") {
  throw new Error("Usage on macOS: node scripts/check-packaged-web.mjs /path/to/SAG.app");
}
const web = path.join(app, "Contents", "Resources", "web");
function verifySignature() {
  const result = spawnSync("codesign", ["--verify", "--deep", "--strict", app], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
}
async function snapshot(directory, prefix = "") {
  const entries = [];
  for (const name of (await readdir(directory)).sort()) {
    const file = path.join(directory, name);
    const relative = path.join(prefix, name);
    const stat = await lstat(file);
    if (stat.isSymbolicLink()) entries.push([relative, "link", await readlink(file)]);
    else if (stat.isDirectory()) entries.push(...await snapshot(file, relative));
    else {
      const hash = createHash("sha256");
      for await (const chunk of createReadStream(file)) hash.update(chunk);
      entries.push([relative, stat.mode, hash.digest("hex")]);
    }
  }
  return entries;
}
verifySignature();
const before = await snapshot(app);
const listener = createServer();
await new Promise(resolve => listener.listen(0, "127.0.0.1", resolve));
const port = listener.address().port;
await new Promise(resolve => listener.close(resolve));
const child = spawn(path.join(app, "Contents", "MacOS", "SAG"), [path.join(web, "server.js")], {
  cwd: web,
  env: { ...process.env, ELECTRON_RUN_AS_NODE: "1", NODE_PATH: path.join(web, "runtime_modules"),
    HOSTNAME: "127.0.0.1", PORT: String(port) },
  stdio: ["ignore", "pipe", "pipe"],
});
let output = "";
child.stdout.on("data", chunk => { output += chunk; });
child.stderr.on("data", chunk => { output += chunk; });
const exited = new Promise(resolve => child.once("exit", (code, signal) => resolve({ code, signal })));
const origin = `http://127.0.0.1:${port}`;
let imageStatus;
let iconType;
let iconBytes;
try {
  let ready = false;
  for (let attempt = 0; attempt < 200; attempt++) {
    if (child.exitCode !== null || child.signalCode !== null) throw new Error(output);
    try { ready = (await fetch(`${origin}/login`, { signal: AbortSignal.timeout(2000) })).ok; } catch { /* wait for Next startup */ }
    if (ready) break;
    await delay(100);
  }
  assert.equal(ready, true, output);
  const optimized = await fetch(`${origin}/_next/image?url=%2Fsag-icon.png&w=64&q=75`, {
    headers: { Accept: "image/webp" },
    signal: AbortSignal.timeout(10000),
  });
  imageStatus = optimized.status;
  await optimized.arrayBuffer();
  const icon = await fetch(`${origin}/sag-icon.png`, { signal: AbortSignal.timeout(10000) });
  assert.equal(icon.status, 200);
  iconType = icon.headers.get("content-type");
  iconBytes = (await icon.arrayBuffer()).byteLength;
} finally {
  child.kill("SIGTERM");
  let timer;
  try {
    await Promise.race([exited, new Promise((_, reject) => {
      timer = setTimeout(() => { child.kill("SIGKILL"); reject(new Error("Packaged Web did not stop")); }, 5000);
    })]);
  } finally { clearTimeout(timer); }
}
const after = await snapshot(app);
assert.equal(imageStatus, 404, "Desktop must not run the disk-writing image optimizer");
assert.match(iconType ?? "", /^image\/png/);
assert.ok(iconBytes > 0);
assert.deepEqual(after, before, "Serving packaged images changed app bundle bytes or files");
verifySignature();
console.log(JSON.stringify({ app, imageOptimizerStatus: imageStatus, iconType, iconBytes,
  immutableBundleFiles: before.length, strictSignatureBeforeAndAfter: "valid" }));
