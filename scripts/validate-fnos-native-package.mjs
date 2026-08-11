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
  // fnpack applies defaults.run-as to every lifecycle callback. Run all
  // callbacks as the package user so neither installation nor runtime
  // commands receive root privileges.
  if (privilege?.defaults?.["run-as"] !== "package") fail("privilege defaults run-as must be package");
  if (privilege.username !== "sag") fail("privilege username must be sag");
  if (privilege.groupname !== "sag") fail("privilege groupname must be sag");

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
