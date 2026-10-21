import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { access, chmod, cp, mkdir, mkdtemp, readFile, rm, unlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("../..", import.meta.url));
const template = path.join(repoRoot, "packages/fnos/native/sag");
const validator = path.join(repoRoot, "scripts/validate-fnos-native-package.mjs");
const builder = path.join(repoRoot, "scripts/build-fnos-native-probe.mjs");
const releaseBuilder = path.join(repoRoot, "scripts/build-fnos-native-package.mjs");
const webPackage = path.join(repoRoot, "apps/web/package.json");
const webLockfile = path.join(repoRoot, "apps/web/package-lock.json");
const requiredOpenApiScopes = [
  "trim.file.sharedAccess",
  "trim.file.userAcl",
  "trim.file.path",
  "trim.system.getPlatformConfig",
];

async function renderedPackage(t, platform) {
  const root = await mkdtemp(path.join(os.tmpdir(), "sag-native-package-test-"));
  t.after(async () => rm(root, { recursive: true, force: true }));
  const destination = path.join(root, "sag");
  await cp(template, destination, { recursive: true });
  const manifest = await readFile(path.join(destination, "manifest"), "utf8");
  await writeFile(
    path.join(destination, "manifest"),
    manifest
      .replace("__SAG_VERSION__", "1.6.0-fnos")
      .replace("__SAG_PLATFORM__", platform),
  );
  return destination;
}

async function loadValidator() {
  return import(validator);
}

async function expectRejected(root, platform, message) {
  const { validateNativeTemplate } = await loadValidator();
  await assert.rejects(validateNativeTemplate(root, platform), message);
}

async function writeUnifiedGatewayEntry(root, overrides = {}) {
  await writeFile(path.join(root, "app/ui/config"), JSON.stringify({
    ".url": {
      "sag.Application": {
        title: "SAG知识库",
        icon: "images/icon_{0}.png",
        type: "iframe",
        protocol: "",
        gatewayPrefix: "/app/sag",
        gatewaySocket: "app.sock",
        url: "/app/sag/chat",
        allUsers: true,
        ...overrides,
      },
    },
  }));
}

async function fakeProbeBuild(t, { corruptManifest = false, useRealFnpack = false, vendorPayload } = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), "sag-native-probe-build-test-"));
  t.after(async () => rm(root, { recursive: true, force: true }));
  const bin = path.join(root, "bin");
  const captured = path.join(root, "captured");
  const probe = path.join(root, "fnos-native-probe.py");
  const output = path.join(root, "probe.fpk");
  const log = path.join(root, "commands.log");
  await mkdir(bin, { recursive: true });
  await mkdir(captured, { recursive: true });
  await writeFile(probe, "# fake probe\n");
  await writeFile(path.join(bin, "uv"), `#!/bin/bash
set -eu
{ printf 'uv'; printf ' %s' "$@"; printf '\\n'; } >> "$FAKE_NATIVE_LOG"
if [[ "$1" == "export" ]]; then
  [[ " $* " == *" --locked "* ]]
  [[ " $* " == *" --no-dev "* ]] || { echo 'export must exclude dev dependencies' >&2; exit 19; }
  output=""
  for ((i=1; i <= $#; i++)); do
    if [[ "\${!i}" == "--output-file" ]]; then next=$((i + 1)); output="\${!next}"; fi
  done
  : > "$output"
elif [[ "$1" == "pip" ]]; then
  [[ " $* " == *" --python-platform aarch64-unknown-linux-gnu "* || " $* " == *" --python-platform x86_64-unknown-linux-gnu "* ]] || { echo 'pip must target a supported linux triple' >&2; exit 21; }
  [[ " $* " == *" --python-version 3.12 "* ]]
  [[ " $* " == *" --only-binary :all: "* ]] || { echo 'pip must install binary wheels only' >&2; exit 20; }
  if [[ -n "\${FAKE_NATIVE_VENDOR_PAYLOAD:-}" ]]; then
    for ((i=1; i <= $#; i++)); do
      if [[ "\${!i}" == "--target" ]]; then next=$((i + 1)); target="\${!next}"; fi
    done
    mkdir -p "$target"
    printf 'native fixture\n' > "$target/$FAKE_NATIVE_VENDOR_PAYLOAD"
  fi
fi
`);
  if (!useRealFnpack) await writeFile(path.join(bin, "fnpack"), `#!/bin/bash
set -eu
test "$1" = build
printf 'fnpack %s\\n' "$1" >> "$FAKE_NATIVE_LOG"
cp manifest "$FAKE_NATIVE_CAPTURE/manifest"
cp cmd/main "$FAKE_NATIVE_CAPTURE/main"
pwd > "$FAKE_NATIVE_CAPTURE/rendered-path"
${corruptManifest ? "sed 's/^platform=arm$/platform=all/' manifest > manifest.next && mv manifest.next manifest" : ""}
touch sag.fpk
`);
  await chmod(path.join(bin, "uv"), 0o755);
  if (!useRealFnpack) await chmod(path.join(bin, "fnpack"), 0o755);
  return {
    captured,
    log,
    output,
    renderedPath: path.join(captured, "rendered-path"),
    env: {
      ...process.env,
      PATH: `${bin}:${process.env.PATH}`,
      FAKE_NATIVE_CAPTURE: captured,
      FAKE_NATIVE_LOG: log,
      ...(vendorPayload ? { FAKE_NATIVE_VENDOR_PAYLOAD: vendorPayload } : {}),
      SAG_FNOS_NATIVE_PROBE_SOURCE: probe,
    },
  };
}

function buildProbe(args, env) {
  return spawnSync(process.execPath, [builder, ...args], {
    cwd: repoRoot,
    encoding: "utf8",
    env,
  });
}

test("rendered x86 and ARM packages satisfy the native package contract", async (t) => {
  const { validateNativeTemplate } = await loadValidator();
  for (const platform of ["x86", "arm"]) {
    const root = await renderedPackage(t, platform);
    await validateNativeTemplate(root, platform);
    const manifest = await readFile(path.join(root, "manifest"), "utf8");
    const resource = JSON.parse(await readFile(path.join(root, "config/resource"), "utf8"));
    assert.match(manifest, /^micro_app=true$/m);
    assert.match(manifest, /^os_min_version=1\.2\.0302$/m);
    assert.deepEqual(resource["api-scope"], requiredOpenApiScopes);
  }
});

test("rendered package rejects a launcher that skips the NOFILE bootstrap", async (t) => {
  const root = await renderedPackage(t, "x86");
  const main = path.join(root, "cmd/main");
  const source = await readFile(main, "utf8");
  await writeFile(main, source.replace(/^[ \t]+configure_nofile[ \t]+[^\n]*$/m, ""));
  await expectRejected(root, "x86", /launcher.*NOFILE.*before.*daemon/i);
});

test("rendered package rejects a launcher that could lower the hard NOFILE limit", async (t) => {
  const root = await renderedPackage(t, "x86");
  const main = path.join(root, "cmd/main");
  const source = await readFile(main, "utf8");
  await writeFile(main, source.replace('ulimit -S -n "$_sag_nofile_effective"', 'ulimit -n "$_sag_nofile_effective"'));
  await expectRejected(root, "x86", /launcher.*NOFILE.*soft/i);
});

test("native package rejects an upgrade that bypasses the offline lifecycle boundary", async (t) => {
  const root = await renderedPackage(t, "x86");
  const filename = path.join(root, "cmd/main");
  await writeFile(filename, (await readFile(filename, "utf8")).replace("-m sag_api.fnos.package_upgrade", "-m sag_api.fnos.worker"));
  await expectRejected(root, "x86", /offline knowledge upgrade/i);
});

test("rendered package rejects the legacy numbered fnOS suffix", async (t) => {
  const root = await renderedPackage(t, "x86");
  const manifestPath = path.join(root, "manifest");
  const manifest = await readFile(manifestPath, "utf8");
  await writeFile(manifestPath, manifest.replace("1.6.0-fnos", "1.6.0-fnos.1"));
  await expectRejected(root, "x86", /version.*x\.y\.z-fnos/i);
});

test("web package locks the fnOS SDK as a production dependency", async () => {
  const packageJson = JSON.parse(await readFile(webPackage, "utf8"));
  const lockfile = JSON.parse(await readFile(webLockfile, "utf8"));
  assert.equal(typeof packageJson.dependencies?.["@trimjs/web-app"], "string");
  assert.equal(
    lockfile.packages?.[""]?.dependencies?.["@trimjs/web-app"],
    packageJson.dependencies["@trimjs/web-app"],
  );
  assert.equal(typeof lockfile.packages?.["node_modules/@trimjs/web-app"]?.version, "string");
  assert.notEqual(lockfile.packages["node_modules/@trimjs/web-app"].dev, true);
});

test("rendered x86 and ARM packages accept the documented unified-gateway entry", async (t) => {
  const { validateNativeTemplate } = await loadValidator();
  for (const platform of ["x86", "arm"]) {
    const root = await renderedPackage(t, platform);
    await writeUnifiedGatewayEntry(root);
    await validateNativeTemplate(root, platform);
  }
});

test("native iframe opens a real Next application route", async (t) => {
  for (const platform of ["x86", "arm"]) {
    const root = await renderedPackage(t, platform);
    const ui = JSON.parse(await readFile(path.join(root, "app/ui/config"), "utf8"));
    assert.equal(ui[".url"]["sag.Application"].url, "/app/sag/chat");
  }
});

test("rendered native package rejects a non-iframe unified-gateway entry", async (t) => {
  const root = await renderedPackage(t, "x86");
  await writeUnifiedGatewayEntry(root, { type: "url" });
  await expectRejected(root, "x86", /type.*iframe/i);
});

for (const field of ["protocol", "url"]) {
  test(`rendered native package rejects a unified-gateway entry without ${field}`, async (t) => {
    const root = await renderedPackage(t, "x86");
    await writeUnifiedGatewayEntry(root, { [field]: undefined });
    const configPath = path.join(root, "app/ui/config");
    const config = JSON.parse(await readFile(configPath, "utf8"));
    delete config[".url"]["sag.Application"][field];
    await writeFile(configPath, JSON.stringify(config));
    await expectRejected(root, "x86", new RegExp(field));
  });
}

test("rendered native package rejects a Docker resource", async (t) => {
  const root = await renderedPackage(t, "x86");
  await writeFile(path.join(root, "config/resource"), JSON.stringify({ "docker-project": {} }));
  await expectRejected(root, "x86", /docker-project/i);
});

test("rendered native package rejects a missing Open API scope", async (t) => {
  const root = await renderedPackage(t, "x86");
  await writeFile(path.join(root, "config/resource"), JSON.stringify({
    "api-scope": requiredOpenApiScopes.slice(0, -1),
  }));
  await expectRejected(root, "x86", /api-scope.*exact four/i);
});

test("rendered native package rejects an unrelated Open API scope", async (t) => {
  const root = await renderedPackage(t, "x86");
  await writeFile(path.join(root, "config/resource"), JSON.stringify({
    "api-scope": [...requiredOpenApiScopes, "trim.file.userAccess"],
  }));
  await expectRejected(root, "x86", /api-scope.*exact four/i);
});

test("rendered native package rejects micro_app=false", async (t) => {
  const root = await renderedPackage(t, "x86");
  const manifestPath = path.join(root, "manifest");
  await writeFile(
    manifestPath,
    (await readFile(manifestPath, "utf8")).replace("micro_app=true", "micro_app=false"),
  );
  await expectRejected(root, "x86", /manifest micro_app must be true/i);
});

test("rendered native package requires the non-reserved public MCP service port", async (t) => {
  const root = await renderedPackage(t, "x86");
  const manifestPath = path.join(root, "manifest");
  await writeFile(manifestPath, (await readFile(manifestPath, "utf8")).replace(/^service_port=15167\n/m, ""));
  await expectRejected(root, "x86", /service_port.*15167/i);
});

test("rendered native package accepts exactly the public MCP service port", async (t) => {
  const root = await renderedPackage(t, "x86");
  const { validateNativeTemplate } = await loadValidator();
  await validateNativeTemplate(root, "x86");
});

test("rendered native package rejects a non-public MCP service port", async (t) => {
  const root = await renderedPackage(t, "x86");
  await writeFile(path.join(root, "manifest"), (await readFile(path.join(root, "manifest"), "utf8")).replace("service_port=15167", "service_port=3080"));
  await expectRejected(root, "x86", /service_port.*15167/i);
});

test("rendered native package rejects a root run-as default", async (t) => {
  const root = await renderedPackage(t, "x86");
  await writeFile(path.join(root, "config/privilege"), JSON.stringify({
    defaults: { "run-as": "root" }, username: "sag", groupname: "sag",
  }));
  await expectRejected(root, "x86", /run-as.*package/i);
});

test("rendered native package rejects platform all", async (t) => {
  const root = await renderedPackage(t, "x86");
  const manifest = await readFile(path.join(root, "manifest"), "utf8");
  await writeFile(path.join(root, "manifest"), manifest.replace("platform=x86", "platform=all"));
  await expectRejected(root, "x86", /platform.*all/i);
});

test("rendered native package rejects unresolved builder tokens", async (t) => {
  const root = await renderedPackage(t, "x86");
  await writeFile(path.join(root, "manifest"), `${await readFile(path.join(root, "manifest"), "utf8")}desc=__SAG_PLATFORM__\n`);
  await expectRejected(root, "x86", /unresolved.*SAG/i);
});

test("SDK internal SAG markers are data, while application builder tokens remain invalid", async (t) => {
  const root = await renderedPackage(t, "x86");
  await mkdir(path.join(root, "app/server/sag_api/fnos"), { recursive: true });
  await cp(path.join(repoRoot, "apps/api/sag_api/fnos/knowledge_compatibility.py"),
    path.join(root, "app/server/sag_api/fnos/knowledge_compatibility.py"));
  await cp(path.join(repoRoot, "apps/api/sag_api/fnos/knowledge_compatibility.py"),
    path.join(root, "cmd/knowledge_compatibility.py"));
  const vendor = path.join(root, "app/server/vendor/zleap/sag/modules/extract");
  await mkdir(vendor, { recursive: true });
  await writeFile(path.join(vendor, "extractor.py"), 'non_time = key("__SAG_FIXED_REFERENCE_TIME__")\n');
  const { validateNativeTemplate } = await loadValidator();
  await validateNativeTemplate(root, "x86");
  await writeFile(path.join(root, "app/unresolved.txt"), "__SAG_PLATFORM__");
  await expectRejected(root, "x86", /unresolved.*SAG/i);
});

test("rendered native package rejects a missing required icon", async (t) => {
  const root = await renderedPackage(t, "x86");
  await unlink(path.join(root, "app/ui/images/icon_256.png"));
  await expectRejected(root, "x86", /icon/i);
});

test("rendered native package rejects Docker Compose content", async (t) => {
  const root = await renderedPackage(t, "x86");
  await writeFile(path.join(root, "docker-compose.yaml"), "services: {}\n");
  await expectRejected(root, "x86", /docker-compose/i);
});

test("rendered native package rejects a non-sag package user", async (t) => {
  const root = await renderedPackage(t, "x86");
  await writeFile(path.join(root, "config/privilege"), JSON.stringify({
    defaults: { "run-as": "package" }, username: "other", groupname: "other",
  }));
  await expectRejected(root, "x86", /username.*sag/i);
});

test("rendered native package rejects a non-gateway entry", async (t) => {
  const root = await renderedPackage(t, "x86");
  const configPath = path.join(root, "app/ui/config");
  const config = JSON.parse(await readFile(configPath, "utf8"));
  config[".url"]["sag.Application"].gatewaySocket = "other.sock";
  await writeFile(configPath, JSON.stringify(config));
  await expectRejected(root, "x86", /gatewaySocket/i);
});

test("probe builder refuses an output that cannot be an fnpack artifact", () => {
  const result = spawnSync(process.execPath, [builder, "--platform", "x86", "--output", "probe.zip"], {
    cwd: repoRoot,
    encoding: "utf8",
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /\.fpk suffix/i);
});

test("probe builder accepts an explicit package version for device replacement", async (t) => {
  const fixture = await fakeProbeBuild(t);
  const result = buildProbe([
    "--platform", "x86",
    "--version", "1.5.4-fnos",
    "--output", fixture.output,
  ], fixture.env);
  assert.equal(result.status, 0, result.stderr);
  assert.match(await readFile(path.join(fixture.captured, "manifest"), "utf8"), /^version=1\.5\.4-fnos$/m);
});

test("native builders reject legacy numbered fnOS versions before reading inputs", () => {
  const cases = [
    [builder, ["--platform", "x86", "--version", "1.5.4-fnos.1", "--output", "/tmp/legacy-probe.fpk"]],
    [releaseBuilder, [
      "--platform", "x86", "--vendor", "/missing/vendor", "--web", "/missing/web",
      "--version", "1.5.4-fnos.1", "--output", "/tmp/legacy-package.fpk",
    ]],
  ];
  for (const [executable, args] of cases) {
    const result = spawnSync(process.execPath, [executable, ...args], {
      cwd: repoRoot,
      encoding: "utf8",
    });
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /version.*x\.y\.z-fnos/i);
  }
});

test("release builder rejects a web bundle without the fnOS gateway base path", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "sag-native-release-base-path-test-"));
  t.after(async () => rm(root, { recursive: true, force: true }));
  const vendor = path.join(root, "vendor");
  const web = path.join(root, "web");
  const bin = path.join(root, "bin");
  await mkdir(vendor);
  await mkdir(path.join(web, ".next"), { recursive: true });
  await mkdir(bin);
  await writeFile(path.join(web, "server.js"), "// standalone fixture\n");
  await writeFile(path.join(web, ".next", "required-server-files.json"), JSON.stringify({
    config: { basePath: "" },
  }));
  await writeFile(path.join(bin, "fnpack"), "#!/bin/sh\ntouch sag.fpk\n");
  await chmod(path.join(bin, "fnpack"), 0o755);

  const result = spawnSync(process.execPath, [releaseBuilder,
    "--platform", "x86", "--vendor", vendor, "--web", web,
    "--version", "1.6.7-fnos", "--output", path.join(root, "broken.fpk"),
  ], {
    cwd: repoRoot,
    encoding: "utf8",
    env: { ...process.env, PATH: `${bin}:${process.env.PATH}` },
  });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /basePath.*\/app\/sag/i);
});

test("probe builder exports locked production dependencies and installs ARM binary wheels", async (t) => {
  const fixture = await fakeProbeBuild(t);
  const result = buildProbe(["--platform", "arm", "--output", fixture.output], fixture.env);
  assert.equal(result.status, 0, result.stderr);
  assert.match(await readFile(fixture.log, "utf8"), /uv export .*--locked.*--no-dev.*--output-file/);
  assert.match(await readFile(fixture.log, "utf8"), /uv pip install .*--python-platform aarch64-unknown-linux-gnu.*--python-version 3\.12.*--only-binary :all:/);
  assert.match(await readFile(path.join(fixture.captured, "manifest"), "utf8"), /^platform=arm$/m);
  assert.match(await readFile(path.join(fixture.captured, "main"), "utf8"), /serve --socket "\$TRIM_APPDEST\/app\.sock" --output "\$TRIM_PKGVAR\/native-p0\.json"/);
  const rendered = (await readFile(fixture.renderedPath, "utf8")).trim();
  await assert.rejects(access(rendered), { code: "ENOENT" });
});

test("probe launcher pins its runtime, import paths, and persistent log", async (t) => {
  const fixture = await fakeProbeBuild(t);
  const built = buildProbe(["--platform", "arm", "--output", fixture.output], fixture.env);
  assert.equal(built.status, 0, built.stderr);
  const main = path.join(fixture.captured, "main");
  const source = await readFile(main, "utf8");
  assert.match(source, /runtime="\/var\/apps\/python312\/target\/bin\/python3"/);
  assert.match(source, /PYTHONPATH="\$vendor:\$server/);
  assert.match(source, />> "\$log" 2>&1 &/);
  assert.doesNotMatch(source, /\n    python3 /);

  const root = path.dirname(fixture.output);
  const appdest = path.join(root, "app");
  const pkgvar = path.join(root, "var");
  const started = spawnSync("bash", [main, "start"], {
    encoding: "utf8",
    env: { ...process.env, TRIM_APPDEST: appdest, TRIM_PKGVAR: pkgvar },
  });
  assert.notEqual(started.status, 0);
  await assert.rejects(access(path.join(pkgvar, "native-p0.pid")), { code: "ENOENT" });
  assert.match(await readFile(path.join(pkgvar, "native-p0.log"), "utf8"), /prerequisites/i);
});

test("probe builder validates the rendered package after fnpack builds it", async (t) => {
  const fixture = await fakeProbeBuild(t, { corruptManifest: true });
  const result = buildProbe(["--platform", "arm", "--output", fixture.output], fixture.env);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /platform must not be all/i);
  await assert.rejects(access(fixture.output), { code: "ENOENT" });
  const rendered = (await readFile(fixture.renderedPath, "utf8")).trim();
  await assert.rejects(access(rendered), { code: "ENOENT" });
});

test("probe builder copies the FPK emitted by real fnpack from the rendered package", {
  skip: process.env.SAG_FNPACK_TESTS !== "1",
}, async (t) => {
  const fixture = await fakeProbeBuild(t, { useRealFnpack: true });
  const result = buildProbe(["--platform", "x86", "--output", fixture.output], fixture.env);

  assert.equal(result.status, 0, result.stderr);
  const archive = await readFile(fixture.output);
  assert.ok(archive.length > 0);
  const listed = spawnSync("tar", ["-tzf", fixture.output], { encoding: "utf8" });
  assert.equal(listed.status, 0, listed.stderr);
  for (const callback of ["main", "install_init", "install_callback", "uninstall_init", "uninstall_callback", "upgrade_init", "upgrade_callback", "config_init", "config_callback"])
    assert.match(listed.stdout, new RegExp(`cmd/${callback}`));
});

test("probe builder embeds the probe and vendor payload inside app.tgz", {
  skip: process.env.SAG_FNPACK_TESTS !== "1",
}, async (t) => {
  const fixture = await fakeProbeBuild(t, { useRealFnpack: true, vendorPayload: "fixture-native.so" });
  const result = buildProbe(["--platform", "x86", "--output", fixture.output], fixture.env);

  assert.equal(result.status, 0, result.stderr);
  const expanded = path.join(path.dirname(fixture.output), "expanded");
  await mkdir(expanded);
  const outer = spawnSync("tar", ["-xzf", fixture.output, "-C", expanded], { encoding: "utf8" });
  assert.equal(outer.status, 0, outer.stderr);
  const contents = spawnSync("tar", ["-tzf", path.join(expanded, "app.tgz")], { encoding: "utf8" });
  assert.equal(contents.status, 0, contents.stderr);
  assert.match(contents.stdout, /server\/fnos-native-probe\.py/);
  assert.match(contents.stdout, /server\/vendor\/fixture-native\.so/);
});


for (const phase of ["install", "upgrade"]) {
  for (const body of ["[]", "{}", JSON.stringify([{stepTitle:"升级",items:[{type:"checkbox",field:"SAG_KNOWLEDGE_UPGRADE_MODE"}]}])]) {
    test(`native package rejects a static ${phase} data-choice page: ${body}`, async (t) => {
      const root = await renderedPackage(t, "x86");
      await writeFile(path.join(root, "wizard", phase), body);
      await expectRejected(root, "x86", phase === "install" ? /wizard.*must be absent/i : /upgrade.*read-only.*notice/i);
    });
  }
}

for (const callback of ["install_callback", "upgrade_init", "upgrade_callback"]) {
  test(`native package rejects historical wizard authorization in ${callback}`, async (t) => {
    const root = await renderedPackage(t, "x86");
    const filename = path.join(root, "cmd", callback);
    await writeFile(filename, (await readFile(filename, "utf8")) + '\ncase "$SAG_KNOWLEDGE_UPGRADE_MODE" in rebuild-legacy-071) : ;; esac\n');
    await expectRejected(root, "x86", /historical package wizard consent/i);
  });
}

for (const file of ["cmd/upgrade_init", "cmd/upgrade_callback", "cmd/install_callback", "app/runtime/lifecycle.py"]) {
  test(`native package rejects absent engine-update contract: ${file}`,async(t)=>{
    const root=await renderedPackage(t,"x86");
    await writeFile(path.join(root,file),"# contract removed");
    await expectRejected(root,"x86",/consent|engine|lifecycle|install_callback/i);
  });
}


test("rendered production server requires the shared knowledge compatibility checker", async (t) => {
  const root = await renderedPackage(t, "x86");
  await mkdir(path.join(root, "app/server/sag_api/fnos"), { recursive: true });
  await expectRejected(root, "x86", /compatibility checker/i);
});

for (const defect of ["missing", "different-source"]) {
  test(`production package rejects a ${defect} pre-payload compatibility checker`, async (t) => {
    const root = await renderedPackage(t, "x86");
    const source = path.join(repoRoot, "apps/api/sag_api/fnos/knowledge_compatibility.py");
    await mkdir(path.join(root, "app/server/sag_api/fnos"), { recursive: true });
    await cp(source, path.join(root, "app/server/sag_api/fnos/knowledge_compatibility.py"));
    if (defect === "different-source") await writeFile(path.join(root, "cmd/knowledge_compatibility.py"), "print('compatible')\n");
    await expectRejected(root, "x86", /cmd compatibility checker.*match/i);
  });
}

test("production builder stages the same classifier beside cmd and inside the new payload", async (t) => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "fnos-builder-classifier-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const fixtureRoot = path.join(directory, "repo");
  await mkdir(path.join(fixtureRoot, "scripts"), { recursive: true });
  for (const name of ["build-fnos-native-package.mjs", "validate-fnos-native-package.mjs", "fnos-version.mjs"]) {
    await cp(path.join(repoRoot, "scripts", name), path.join(fixtureRoot, "scripts", name));
  }
  await cp(template, path.join(fixtureRoot, "packages/fnos/native/sag"), { recursive: true });
  const source = path.join(repoRoot, "apps/api/sag_api/fnos/knowledge_compatibility.py");
  await mkdir(path.join(fixtureRoot, "apps/api/sag_api/fnos"), { recursive: true });
  await cp(source, path.join(fixtureRoot, "apps/api/sag_api/fnos/knowledge_compatibility.py"));
  for (const name of ["apps/api/sag_agent", "apps/web/.next/static", "apps/web/public", "vendor", "web/.next", "bin"]) {
    await mkdir(path.join(fixtureRoot, name), { recursive: true });
  }
  await writeFile(path.join(fixtureRoot, "web/.next/required-server-files.json"), JSON.stringify({ config: { basePath: "/app/sag" } }));
  const captured = path.join(directory, "classifier.py");
  const fnpack = path.join(fixtureRoot, "bin/fnpack");
  // Substitute the external packer only. Execute the production builder and
  // validator against real staged files before it can cross that boundary.
  await writeFile(fnpack, `#!/bin/sh\nset -eu\ncmp cmd/knowledge_compatibility.py app/server/sag_api/fnos/knowledge_compatibility.py\ncp cmd/knowledge_compatibility.py "$CLASSIFIER_CAPTURE"\n: > sag.fpk\n`, { mode: 0o755 });
  const result = spawnSync(process.execPath, [path.join(fixtureRoot, "scripts/build-fnos-native-package.mjs"),
    "--platform", "x86", "--vendor", path.join(fixtureRoot, "vendor"), "--web", path.join(fixtureRoot, "web"),
    "--version", "1.5.13-fnos", "--output", path.join(directory, "candidate.fpk")], {
    env: { ...process.env, PATH: `${path.join(fixtureRoot, "bin")}:${process.env.PATH}`, CLASSIFIER_CAPTURE: captured },
    encoding: "utf8", timeout: 15000,
  });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(await readFile(captured), await readFile(source));
});

test("fresh install has no wizard and package upgrades have one read-only notice", async () => {
  await assert.rejects(access(path.join(template, "wizard", "install")), { code: "ENOENT" });
  const steps = JSON.parse(await readFile(path.join(template, "wizard", "upgrade"), "utf8"));
  assert.equal(steps.length, 1);
  assert.deepEqual(steps[0].items.map(item => item.type), ["tips"]);
  assert.ok(steps[0].items[0].helpText.length > 0);
  assert.doesNotMatch(JSON.stringify(steps), /0\.(7|13|15)\.|SAG_KNOWLEDGE_UPGRADE_MODE/);
});
