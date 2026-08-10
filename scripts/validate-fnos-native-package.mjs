#!/usr/bin/env node

import { readdir, readFile, stat } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const requiredIcons = [
  "ICON.PNG",
  "ICON_256.PNG",
  "app/ui/images/icon_64.png",
  "app/ui/images/icon_256.png",
];

function fail(message) {
  throw new Error(`fnos-native-package: ${message}`);
}

function parseManifest(text) {
  const entries = new Map();
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim() || line.trimStart().startsWith("#")) continue;
    const separator = line.indexOf("=");
    if (separator < 1) fail(`invalid manifest line: ${line}`);
    const key = line.slice(0, separator).trim();
    if (entries.has(key)) fail(`manifest defines ${key} more than once`);
    entries.set(key, line.slice(separator + 1).trim());
  }
  return entries;
}

async function readJson(root, relativePath) {
  try {
    return JSON.parse(await readFile(path.join(root, relativePath), "utf8"));
  } catch (error) {
    fail(`${relativePath} must contain valid JSON: ${error.message}`);
  }
}

async function assertFiles(root, relativePaths) {
  for (const relativePath of relativePaths) {
    try {
      if (!(await stat(path.join(root, relativePath))).isFile()) fail(`${relativePath} must be a file`);
    } catch (error) {
      if (error instanceof Error && error.message.startsWith("fnos-native-package:")) throw error;
      fail(`missing required icon: ${relativePath}`);
    }
  }
}

async function walk(root, relativePath = "") {
  const directory = path.join(root, relativePath);
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const child = path.join(relativePath, entry.name);
    if (entry.isDirectory()) files.push(...await walk(root, child));
    else if (entry.isFile()) files.push(child);
  }
  return files;
}

async function assertNoDockerOrTokens(root) {
  for (const relativePath of await walk(root)) {
    if (path.basename(relativePath) === "docker-compose.yaml") fail("rendered package must not contain docker-compose.yaml");
    const content = await readFile(path.join(root, relativePath));
    if (content.includes(Buffer.from("__SAG_"))) fail(`rendered package contains unresolved __SAG_*__ token in ${relativePath}`);
  }
}

export async function validateNativeTemplate(root, platform) {
  if (platform !== "x86" && platform !== "arm") fail("platform must be x86 or arm");
  await assertNoDockerOrTokens(root);
  const manifest = parseManifest(await readFile(path.join(root, "manifest"), "utf8"));
  const requiredManifest = {
    appname: "sag",
    display_name: "SAG知识库",
    desc: "Self-hosted AI knowledge base and research workspace",
    os_min_version: "1.2.0302",
    source: "thirdparty",
    maintainer: "Zleap AI",
    distributor: "Zleap AI",
    install_dep_apps: "python312:nodejs_v22",
    ctl_stop: "true",
    desktop_uidir: "ui",
    desktop_applaunchname: "sag.Application",
  };
  for (const [key, value] of Object.entries(requiredManifest)) {
    if (manifest.get(key) !== value) fail(`manifest ${key} must be ${value}`);
  }
  if (manifest.has("service_port")) fail("manifest must not define service_port");
  if (manifest.get("platform") === "all") fail("manifest platform must not be all");
  if (manifest.get("platform") !== platform) fail(`manifest platform must be ${platform}`);
  if (!manifest.get("version")) fail("manifest must define version");

  const privilege = await readJson(root, "config/privilege");
  // fnpack's privilege schema has no per-command override: whatever
  // `defaults.run-as` is applies to every callback in cmd/, including
  // install_init, install_callback, main, upgrade_init, and
  // uninstall_callback. This package sets run-as=root so that:
  //   * install_init runs BEFORE fnpack materializes $TRIM_PKGVAR and
  //     stays out of it (see cmd/install_init).
  //   * install_callback (root) mkdirs $TRIM_PKGVAR, chowns sag:sag,
  //     and provisions the internal-secret file (see cmd/install_callback).
  //   * main enters as root, sets up runtime state files, then drops
  //     privileges to the sag user via setpriv (or su as fallback)
  //     before exec'ing the long-lived gateway (python uvicorn) and
  //     web (node) daemons. Neither daemon holds root.
  // fnOS's own guidance is "优先使用 package 用户权限级别" — root is
  // permitted when the install callbacks need it, and many upstream
  // apps ship that way (Fndesk, 1Panel, EasyTier-Web, mihomo, ...).
  // We enforce here that: privilege declares root, the sag user is
  // still requested (fnpack auto-creates it), and cmd/main actually
  // wires the privilege drop for its daemon exec paths.
  if (privilege?.defaults?.["run-as"] !== "root") fail("privilege defaults run-as must be root (install_callback needs root to chown $TRIM_PKGVAR to sag; main drops to sag via setpriv before exec)");
  if (privilege.username !== "sag") fail("privilege username must be sag");
  if (privilege.groupname !== "sag") fail("privilege groupname must be sag");

  const mainSource = await readFile(path.join(root, "cmd/main"), "utf8");
  // main runs as root but MUST drop to sag before exec'ing the
  // gateway and web daemons. Guard the two well-known drop tools
  // shipped on fnOS 1.2.x (setpriv verified present, su as fallback).
  // A run-as=root package that forgets this ships root-owned daemons,
  // which is the exact security regression we changed the privilege
  // model to avoid.
  if (!/\bsetpriv\b/.test(mainSource) && !/\bsu\s+-s\b/.test(mainSource))
    fail("cmd/main must drop privileges via setpriv or su before exec'ing gateway/web daemons (run-as=root means main is entered as root)");

  const resource = await readJson(root, "config/resource");
  if (Object.hasOwn(resource, "docker-project")) fail("native package resource must not define docker-project");
  if (Object.keys(resource).length !== 0) fail("native package resource must be empty");

  const ui = await readJson(root, "app/ui/config");
  const urls = ui?.[".url"];
  const entry = urls?.["sag.Application"];
  if (!entry || Object.keys(urls).length !== 1) fail("UI must expose only sag.Application");
  if (entry.type !== "iframe") fail("UI type must be iframe");
  if (entry.protocol !== "") fail("UI protocol must be empty");
  if (entry.gatewayPrefix !== "/app/sag") fail("UI gatewayPrefix must be /app/sag");
  if (entry.gatewaySocket !== "app.sock") fail("UI gatewaySocket must be app.sock");
  if (entry.url !== "/app/sag/chat") fail("UI url must be /app/sag/chat");
  if (entry.allUsers !== true) fail("UI allUsers must be true");
  if (Object.hasOwn(entry, "port")) fail("UI must not expose a direct service port");

  await assertFiles(root, requiredIcons);
}

async function main() {
  const [root, platform] = process.argv.slice(2);
  if (!root || !platform || process.argv.length !== 4) {
    process.stderr.write("usage: validate-fnos-native-package.mjs <rendered-package> <x86|arm>\n");
    process.exitCode = 1;
    return;
  }
  await validateNativeTemplate(path.resolve(root), platform);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
