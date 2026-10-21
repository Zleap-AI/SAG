#!/usr/bin/env node

import { lstat, readdir, readFile, stat } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { assertCurrentFnOSVersion } from "./fnos-version.mjs";

const requiredIcons = [
  "ICON.PNG",
  "ICON_256.PNG",
  "app/ui/images/icon_64.png",
  "app/ui/images/icon_256.png",
];
const requiredOpenApiScopes = [
  "trim.file.sharedAccess",
  "trim.file.userAcl",
  "trim.file.path",
  "trim.system.getPlatformConfig",
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
    // Wheels are copied verbatim, never rendered by this builder. SDK 0.15
    // uses __SAG_FIXED_REFERENCE_TIME__ as an extraction data marker.
    if (relativePath.startsWith(`app${path.sep}server${path.sep}vendor${path.sep}`)) continue;
    const content = await readFile(path.join(root, relativePath));
    if (content.includes(Buffer.from("__SAG_"))) fail(`rendered package contains unresolved __SAG_*__ token in ${relativePath}`);
  }
}

// Fresh installs have no data form; all upgrades show one native notice before
// fnOS stops the old application. Continue is the current operation's consent.
async function assertUpgradeNoticeWizard(root) {
  const entry = await lstat(path.join(root, "wizard", "install")).catch((error) => {
    if (error.code === "ENOENT") return null;
    throw error;
  });
  if (entry) fail("wizard/install must be absent for fresh installation");
  let steps;
  try { steps = JSON.parse(await readFile(path.join(root, "wizard", "upgrade"), "utf8")); }
  catch { fail("wizard/upgrade must contain one read-only upgrade notice"); }
  if (!Array.isArray(steps) || steps.length !== 1 || !Array.isArray(steps[0].items)
      || steps[0].items.length !== 1 || steps[0].items[0].type !== "tips"
      || typeof steps[0].items[0].helpText !== "string" || !steps[0].items[0].helpText.trim()
      || steps[0].items[0].field || steps[0].items[0].options) {
    fail("wizard/upgrade must contain one read-only upgrade notice");
  }
}

export async function validateNativeTemplate(root, platform) {
  if (platform !== "x86" && platform !== "arm") fail("platform must be x86 or arm");
  await assertNoDockerOrTokens(root);
  const manifest = parseManifest(await readFile(path.join(root, "manifest"), "utf8"));
  const requiredManifest = {
    appname: "sag",
    display_name: "SAG知识库",
    desc: "<b>最强 AI 知识库 · 让散落文档汇聚为可检索、可追溯的知识</b><br><br><b>导入知识</b> - 添加文档,SAG 自动完成解析、分块、事件与实体抽取<br><img src=\"https://raw.githubusercontent.com/Zleap-AI/SAG/fnos/develop/docs/assets/readme/product-import.png\" width=480 /><br><br><b>带引用的回答</b> - Agent 基于选定知识源作答,附带可点击的原文引用<br><img src=\"https://raw.githubusercontent.com/Zleap-AI/SAG/fnos/develop/docs/assets/readme/product-chat.png\" width=480 /><br><br><b>探索模式</b> - 将整个知识库展开为可交互的知识宇宙,穿梭事件与实体<br><img src=\"https://raw.githubusercontent.com/Zleap-AI/SAG/fnos/develop/docs/assets/readme/product-explore-mode.gif\" width=480 /><br><br><b>事件实体图谱</b> - 将来源切换为图谱视图,查看事件、实体及其关联<br><img src=\"https://raw.githubusercontent.com/Zleap-AI/SAG/fnos/develop/docs/assets/readme/product-graph.png\" width=480 />",
    os_min_version: "1.2.0302",
    source: "thirdparty",
    maintainer: "Zleap AI",
    distributor: "Zleap AI",
    install_dep_apps: "python312:nodejs_v22",
    ctl_stop: "true",
    desktop_uidir: "ui",
    desktop_applaunchname: "sag.Application",
    micro_app: "true",
  };
  for (const [key, value] of Object.entries(requiredManifest)) {
    if (manifest.get(key) !== value) fail(`manifest ${key} must be ${value}`);
  }
  if (manifest.get("service_port") !== "15167") fail("manifest service_port must be 15167");
  if (manifest.get("platform") === "all") fail("manifest platform must not be all");
  if (manifest.get("platform") !== platform) fail(`manifest platform must be ${platform}`);
  try {
    assertCurrentFnOSVersion(manifest.get("version"));
  } catch {
    fail("manifest version must match x.y.z-fnos");
  }

  const privilege = await readJson(root, "config/privilege");
  // fnpack applies defaults.run-as to every lifecycle callback. Run all
  // callbacks as the package user so neither installation nor runtime
  // commands receive root privileges.
  if (privilege?.defaults?.["run-as"] !== "package") fail("privilege defaults run-as must be package");
  if (privilege.username !== "sag") fail("privilege username must be sag");
  if (privilege.groupname !== "sag") fail("privilege groupname must be sag");

  const resource = await readJson(root, "config/resource");
  if (Object.hasOwn(resource, "docker-project")) fail("native package resource must not define docker-project");
  if (Object.keys(resource).length !== 1 || !Array.isArray(resource["api-scope"])) {
    fail("native package resource api-scope must contain the exact four required scopes");
  }
  if (
    resource["api-scope"].length !== requiredOpenApiScopes.length
    || resource["api-scope"].some((scope, index) => scope !== requiredOpenApiScopes[index])
  ) {
    fail("native package resource api-scope must contain the exact four required scopes");
  }

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

  await assertUpgradeNoticeWizard(root);
  const launcher = await readFile(path.join(root, "cmd/main"), "utf8");
  // The diagnostic probe builder replaces main with its own standalone
  // probe; it does not start the production gateway, web or tenant workers.
  const runtimeProbe = launcher.includes('"$server/fnos-native-probe.py" serve --socket "$TRIM_APPDEST/app.sock" --output "$TRIM_PKGVAR/native-p0.json"');
  if (!runtimeProbe) {
    // The source template has no server yet; the production builder copies it.
    const server = await stat(path.join(root, "app/server")).catch(() => null);
    if (server?.isDirectory()) {
      const sourceChecker = path.join(root, "app/server/sag_api/fnos/knowledge_compatibility.py");
      const checker = await lstat(sourceChecker).catch(() => null);
      if (!checker?.isFile()) fail("production server must contain the shared knowledge compatibility checker");
      const commandChecker = path.join(root, "cmd/knowledge_compatibility.py");
      const command = await lstat(commandChecker).catch(() => null);
      if (!command?.isFile() || !(await readFile(commandChecker)).equals(await readFile(sourceChecker))) {
        fail("production cmd compatibility checker must match the shared server checker before payload replacement");
      }
    }
    if (!launcher.startsWith("#!/bin/sh\nset -u\n")) {
      fail("production launcher must use POSIX sh with explicit failure handling");
    }
    const nofileBootstrap = launcher.indexOf('    configure_nofile >> "$gateway_log" 2>&1 || exit 1');
    const firstDaemon = launcher.indexOf('    _START_STEP="start-web"');
    if (nofileBootstrap < 0 || firstDaemon < 0 || nofileBootstrap > firstDaemon) {
      fail("launcher must configure NOFILE before starting any daemon");
    }
    if (!launcher.includes('ulimit -S -n "$_sag_nofile_effective"')
        || /ulimit\s+(?:-H\s+)?-n\s+(?:["'$]|[0-9]+(?:\s|$))/m.test(launcher)) {
      fail("launcher NOFILE bootstrap must change only the soft limit");
    }
    const lockBootstrap = launcher.indexOf('    acquire_lifecycle_lock || exit 1');
    const stop = launcher.split(/\n  stop\)\n/)[1]?.split(/\n  status\)\n/)[0] ?? "";
    if (lockBootstrap < 0 || lockBootstrap > firstDaemon
        || !launcher.includes('if ! command exec 9>"${run_dir}/lifecycle.lock"; then')
        || !launcher.includes('flock -w 60 "${1:--x}" 9')
        || !stop.includes("acquire_lifecycle_lock || exit 1")
        || stop.indexOf("acquire_lifecycle_lock") > stop.indexOf("stop_process")) {
      fail("launcher must serialize start and stop before process or socket changes");
    }
    const daemonLaunches = launcher.split("\n").filter((line) => /^\s*\( .*\) >> .* &$/.test(line));
    if (daemonLaunches.length !== 3 || daemonLaunches.some((line) => !line.includes("exec 9>&-;"))) {
      fail("launcher daemons must close the lifecycle lock descriptor before starting");
    }
    if (!launcher.includes('valid_pid "$pid_file" "$expected" "$service" "$pid"')) {
      fail("launcher stop must validate the captured PID before signalling it");
    }
    const upgrade = launcher.split(/\n  upgrade-knowledge\)\n/)[1]?.split(/\n  stop\)\n/)[0] ?? "";
    if (!upgrade.includes("-m sag_api.fnos.package_upgrade")
        || !upgrade.includes('--backup-tool "$TRIM_APPDEST/runtime/lifecycle.py"')
        || !upgrade.includes("acquire_lifecycle_lock || exit 1")
        || upgrade.indexOf("acquire_lifecycle_lock") > upgrade.indexOf("stop_process")) {
      fail("launcher must serialize the offline knowledge upgrade through cold backup and rebuilding");
    }
    if (!stop.includes("stop_tenant_workers || exit 1")
        || !upgrade.includes("stop_tenant_workers || exit 1")
        || upgrade.indexOf("stop_tenant_workers") > upgrade.indexOf("-m sag_api.fnos.package_upgrade")
        || !launcher.includes('"$command_dir/stop_workers.py" --root "$TRIM_PKGVAR"')) {
      fail("launcher must verify all tenant workers stopped before returning or backing up knowledge");
    }
    const start = launcher.split(/\n  start\)\n/)[1]?.split(/\n  upgrade-knowledge\)\n/)[0] ?? "";
    const orphanCheck = start.indexOf('    _START_STEP="stop-orphan-workers"');
    const gatewayStart = start.split('    _START_STEP="start-gateway"')[1] ?? "";
    if (orphanCheck < start.indexOf("acquire_lifecycle_lock || exit 1")
        || orphanCheck > start.indexOf('    _START_STEP="start-web"')
        || !start.includes('if ! valid_pid "$gateway_pid" "sag_api.fnos.cli"; then\n      stop_tenant_workers || exit 1\n    fi')
        || !gatewayStart.includes('if ! valid_pid "$gateway_pid" "sag_api.fnos.cli"; then\n      stop_tenant_workers || exit 1\n')) {
      fail("launcher must verify orphan tenant workers stopped before starting a replacement gateway");
    }
    if (!upgrade.includes("trap '") || !upgrade.includes("upgrade-knowledge failed")
        || !upgrade.includes("cannot remove stopped socket/port")) {
      fail("launcher must report actionable offline upgrade failures to fnpack");
    }
    const stopHelper = await lstat(path.join(root, "cmd/stop_workers.py")).catch(() => null);
    if (!stopHelper?.isFile()) fail("production commands must include tenant worker stop verification before payload replacement");
  }
  const configInit = await readFile(path.join(root, "cmd/config_init"), "utf8");
  const configCommands = configInit.split(/\r?\n/).map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"));
  if (configCommands.join("\n") !== "set -u\nexit 0") {
    fail("config_init must preserve running services and stopped state without install cleanup");
  }
  const install = await readFile(path.join(root, "cmd/install_callback"), "utf8");
  if (!install.includes("SAG_INSTALL_FROM_UPGRADE") || !install.includes("$command_dir/upgrade_init")) {
    fail("install_callback must apply the upgrade format check and cold backup gate to retained data");
  }
  for (const callback of ["install_callback", "upgrade_init", "upgrade_callback"]) {
    const source = await readFile(path.join(root, "cmd", callback), "utf8");
    if (source.includes("SAG_KNOWLEDGE_UPGRADE_MODE") || source.includes("SAG_ACCEPT_REINGEST_UPGRADE")) {
      fail(`${callback} must not consume historical package wizard consent`);
    }
    if (callback === "upgrade_init" &&
        (!source.includes('"$command_dir/knowledge_compatibility.py" --root')
         || !source.includes("legacy") || !source.includes("compatible"))) {
      fail(`${callback} must preserve recognized knowledge and reject unknown engine updates`);
    }
    if (callback === "upgrade_callback" &&
        (!source.includes('"$command_dir/main" upgrade-knowledge')
         || source.includes('"$command_dir/knowledge_compatibility.py" --root'))) {
      fail("upgrade_callback must delegate to the stopped offline lifecycle owner before knowledge classification");
    }
  }
  const lifecycle = await readFile(path.join(root, "app/runtime/lifecycle.py"), "utf8");
  if (!lifecycle.includes("client-access-policy.json") || !lifecycle.includes("internal-secret")) {
    fail("lifecycle must preserve client policy and credential recovery key");
  }
  if (!lifecycle.includes("def upgrade_check") || !lifecycle.includes("def migration_ready")
      || !lifecycle.includes("knowledge_compatibility.py")) {
    fail("lifecycle must classify existing tenant engine formats before migration");
  }
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
