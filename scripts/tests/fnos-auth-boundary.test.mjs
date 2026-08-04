import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("../..", import.meta.url));
const sourceConfig = path.join(repoRoot, "deploy/fnos/nginx.conf");
const packageConfig = path.join(repoRoot, "packages/fnos/sag/app/docker/nginx.conf");

test("source and packaged gateways apply conservative per-peer auth throttling", async () => {
  const source = await readFile(sourceConfig, "utf8");
  const packaged = await readFile(packageConfig, "utf8");
  assert.equal(packaged, source);

  assert.match(
    source,
    /limit_req_zone\s+\$binary_remote_addr\s+zone=sag_auth:[^;]+\s+rate=5r\/m;/,
  );
  assert.match(
    source,
    /location\s+~\s+\^\/api\/v1\/auth\/\(login\|register\)\/\?\$/,
  );
  assert.match(source, /limit_req\s+zone=sag_auth\s+burst=3\s+nodelay;/);
  assert.match(source, /limit_req_status\s+429;/);
  assert.match(source, /error_page\s+429\s+=\s+@sag_auth_limited;/);
  assert.match(
    source,
    /location\s+@sag_auth_limited\s*\{[\s\S]*add_header\s+Retry-After\s+"60"\s+always;[\s\S]*return\s+429;/,
  );
  assert.doesNotMatch(source, /limit_req_zone\s+\$http_x_forwarded_for/);
  assert.doesNotMatch(source, /set_real_ip_from|real_ip_header/);
});

test("locally cached release Nginx accepts the packaged gateway config", (t) => {
  const image = "docker.io/library/nginx:1.30.4-alpine@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46";
  const available = spawnSync("docker", ["image", "inspect", image], {
    encoding: "utf8",
  });
  if (available.status !== 0) {
    t.skip("release Nginx image is not cached locally");
    return;
  }

  const checked = spawnSync(
    "docker",
    [
      "run",
      "--rm",
      "--network",
      "none",
      "--add-host",
      "api:127.0.0.1",
      "--add-host",
      "web:127.0.0.1",
      "--volume",
      `${packageConfig}:/etc/nginx/conf.d/default.conf:ro`,
      image,
      "nginx",
      "-t",
    ],
    { encoding: "utf8" },
  );
  assert.equal(checked.status, 0, `${checked.stdout}\n${checked.stderr}`);
});
