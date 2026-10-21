import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { cp, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { validateNativeTemplate } from "../validate-fnos-native-package.mjs";

const root = fileURLToPath(new URL("../..", import.meta.url));
const fixture = path.join(root, "scripts/tests/fixtures/fnos-native-runtime-lifecycle.py");

// Approved public seams: fnpack's config_init/main callbacks. The fixture
// owns real child processes and UDS listeners, never installed SAG paths.
function check(name, { linux = false } = {}) {
  const image = process.env.SAG_NATIVE_LIFECYCLE_TEST_IMAGE;
  const container = linux && process.platform !== "linux" && image;
  test(name, { skip: linux && process.platform !== "linux" && !container }, () => {
    const executable = container ? "docker" : "python3";
    const args = container
      ? ["run", "--rm", "--network", "none", "-v", `${root}:/repo:ro`, image,
        "python3", `/repo/scripts/tests/fixtures/${path.basename(fixture)}`, "/repo", name]
      : [fixture, root, name];
    const result = spawnSync(executable, args, { encoding: "utf8", timeout: 90000 });
    assert.equal(result.status, 0, result.stdout + result.stderr + (result.error ?? ""));
  });
}

check("config_preserves_running_services");
check("config_preserves_stopped_state");
check("missing_lock_tool_preserves_live_service");
check("unwritable_lock_reports_failure");
check("upgrade_cleanup_failure_reports_cause", { linux: true });
check("new_commands_stop_workers_before_payload_replacement", { linux: true });
check("new_commands_verify_workers_before_upgrade_owner", { linux: true });
check("start_reaps_workers_left_by_dead_gateway", { linux: true });
check("healthy_start_keeps_active_tenant_workers", { linux: true });
check("start_reaps_workers_if_gateway_dies_while_web_becomes_ready", { linux: true });
check("start_refuses_when_old_worker_identity_cannot_be_pinned", { linux: true });
check("stop_reaps_legacy_orphan_workers", { linux: true });
check("upgrade_refuses_when_worker_identity_cannot_be_pinned", { linux: true });
check("stop_signals_the_validated_pid_only", { linux: true });
check("concurrent_restart_keeps_the_new_socket", { linux: true });
check("concurrent_starts_reuse_one_service_set", { linux: true });

for (const [name, file, mutate, error] of [
  ["configuration cleanup", "config_init", () => '#!/bin/sh\nexec "$(dirname "$0")/install_init"\n', /config_init must preserve/],
  ["unlocked startup", "main", (text) => text.replace("    acquire_lifecycle_lock || exit 1", ""), /serialize start and stop/],
  ["inherited daemon lock", "main", (text) => text.replace('exec 9>&-; "$runtime" -m', '"$runtime" -m'), /close the lifecycle lock/],
  ["PID record re-read", "main", (text) => text.replaceAll('"$service" "$pid"', '"$service"'), /validate the captured PID/],
  ["unverified tenant shutdown", "main", (text) => text.replaceAll("    stop_tenant_workers || exit 1", ""), /verify all tenant workers stopped/],
  ["unverified orphan startup", "main", (text) => text.replace(/    _START_STEP="stop-orphan-workers"\n    if ! valid_pid "\$gateway_pid" "sag_api\.fnos\.cli"; then\n      stop_tenant_workers \|\| exit 1\n    fi\n/, ""), /verify orphan tenant workers stopped/],
  ["unverified replacement gateway", "main", (text) => text.replace(/(    _START_STEP="start-gateway"\n    if ! valid_pid [^\n]+\n)      stop_tenant_workers \|\| exit 1\n/, "$1"), /verify orphan tenant workers stopped/],
  ["silent offline upgrade", "main", (text) => text.replace("upgrade-knowledge failed", "offline failed"), /report actionable offline upgrade failures/],
]) {
  test(`package validator rejects ${name}`, async (t) => {
    const directory = await mkdtemp(path.join(os.tmpdir(), "sag-lifecycle-package-"));
    t.after(() => rm(directory, { recursive: true, force: true }));
    await cp(path.join(root, "packages/fnos/native/sag"), directory, { recursive: true });
    const manifest = path.join(directory, "manifest");
    await writeFile(manifest, (await readFile(manifest, "utf8"))
      .replace("__SAG_VERSION__", "1.6.0-fnos").replace("__SAG_PLATFORM__", "x86"));
    await validateNativeTemplate(directory, "x86");
    const target = path.join(directory, "cmd", file);
    await writeFile(target, mutate(await readFile(target, "utf8")));
    await assert.rejects(validateNativeTemplate(directory, "x86"), error);
  });
}
