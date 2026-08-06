import assert from "node:assert/strict";
import { chmod, mkdtemp, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
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

function run(name, env, args = []) {
  return spawnSync("bash", [path.join(cmd, name), ...args], { encoding: "utf8", env });
}

test("install_init creates one private, stable internal secret", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  const etc = path.join(fixture, "etc");
  await mkdir(etc);
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const env = { ...process.env, TRIM_PKGETC: etc };

  assert.equal(run("install_init", env).status, 0);
  const first = await readFile(path.join(etc, "internal-secret"), "utf8");
  assert.match(first, /^[0-9a-f]{64}$/);
  assert.equal((await (await import("node:fs/promises")).stat(path.join(etc, "internal-secret"))).mode & 0o777, 0o600);
  assert.equal(run("install_init", env).status, 0);
  assert.equal(await readFile(path.join(etc, "internal-secret"), "utf8"), first);
});

test("install gives the package user access to the private gateway secret", async () => {
  const source = await readFile(path.join(cmd, "install_init"), "utf8");
  assert.match(source, /chown sag:sag/);
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
  // Next.js rewrites argv (and therefore /proc cmdline) to "next-server",
  // so identity must fall back to the launch environment marker.
  assert.match(source, /SAG_NATIVE_SERVICE="web" HOSTNAME=127\.0\.0\.1/);
  assert.match(source, /\/proc\/\$pid\/environ/);
  assert.match(source, /SAG_NATIVE_SERVICE=\$service/);
  // Every web identity check must pass the marker, or status/stop regress
  // into "web process is not running" the moment Next boots.
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

test("install_init refuses a symlinked internal secret", async (t) => {
  const fixture = await mkdtemp(path.join(os.tmpdir(), "sag-native-lifecycle-"));
  const etc = path.join(fixture, "etc");
  const target = path.join(fixture, "target");
  await mkdir(etc);
  await writeFile(target, "0".repeat(64));
  await symlink(target, path.join(etc, "internal-secret"));
  t.after(() => rm(fixture, { recursive: true, force: true }));

  assert.notEqual(run("install_init", { ...process.env, TRIM_PKGETC: etc }).status, 0);
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

test("Native lifecycle shell scripts parse cleanly", () => {
  for (const name of ["main", "install_init", "install_callback", "upgrade_init", "upgrade_callback", "uninstall_init", "uninstall_callback", "config_init", "config_callback"]) {
    assert.equal(spawnSync("bash", ["-n", path.join(cmd, name)]).status, 0, name);
  }
});
