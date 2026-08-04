#!/usr/bin/env node

import { randomBytes } from "node:crypto";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { setTimeout as delay } from "node:timers/promises";

const digest = "sha256:[a-f0-9]{64}";
const apiReference = new RegExp(`^ghcr\\.io/luoshuai990529/sag-api@${digest}$`);
const webReference = new RegExp(`^ghcr\\.io/luoshuai990529/sag-web@${digest}$`);
const staticAssetProbe = String.raw`
const fs = require("node:fs");
const path = require("node:path");
const nextRoot = "/app/.next";
const buildId = path.join(nextRoot, "BUILD_ID");
if (fs.existsSync(buildId)) {
  const value = fs.readFileSync(buildId, "utf8");
  if (!value.trim()) throw new Error("empty Next BUILD_ID");
}
const staticRoot = "/app/.next/static";
function findAsset(directory) {
  const entries = fs.readdirSync(directory, { withFileTypes: true })
    .sort((left, right) => left.name.localeCompare(right.name));
  for (const entry of entries) {
    const candidate = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      const nested = findAsset(candidate);
      if (nested) return nested;
    } else if (entry.isFile() && (entry.name.endsWith(".js") || entry.name.endsWith(".css"))) {
      return candidate;
    }
  }
  return null;
}
const asset = findAsset(staticRoot);
if (!asset) throw new Error("no regular JavaScript or CSS asset in Next static tree");
const relative = path.relative(staticRoot, asset);
const segments = relative.split(path.sep);
if (!segments.length || segments.some((segment) => !segment || segment === "." || segment === "..")) {
  throw new Error("unsafe Next static asset path");
}
process.stdout.write("/_next/static/" + segments.map(encodeURIComponent).join("/"));
`;

function fail(message) {
  throw new Error(`fnOS exact-digest smoke: ${message}`);
}

function parseArgs(argv) {
  const [command, ...rest] = argv;
  if (!command) fail("a command is required");
  const options = { command };
  for (let index = 0; index < rest.length; index += 2) {
    const name = rest[index];
    const value = rest[index + 1];
    if (!name?.startsWith("--") || !value || value.startsWith("--")) fail(`value required for ${name ?? "argument"}`);
    options[name.slice(2).replaceAll("-", "_")] = value;
  }
  return options;
}

function required(options, name) {
  if (!options[name]) fail(`--${name.replaceAll("_", "-")} is required`);
  return options[name];
}

function names(options) {
  const scope = required(options, "scope");
  if (!/^[a-z0-9][a-z0-9-]{0,40}$/.test(scope)) fail("--scope must contain only lowercase letters, digits, and hyphens");
  return {
    api: `sag-fnos-api-${scope}`,
    web: `sag-fnos-web-${scope}`,
    data: `sag-fnos-data-${scope}`,
  };
}

function execute(executable, args) {
  const result = spawnSync(executable, args, { encoding: "utf8" });
  if (result.error) fail(`could not run ${executable}: ${result.error.message}`);
  return result;
}

function requireSuccess(result, operation) {
  if (result.status !== 0) fail(`${operation} failed: ${(result.stderr || result.stdout).trim()}`);
  return result;
}

function docker(options, args, operation) {
  return requireSuccess(execute(required(options, "docker"), args), operation);
}

function cleanup(options) {
  const resource = names(options);
  const executable = required(options, "docker");
  const containers = execute(executable, ["rm", "--force", resource.api, resource.web]);
  const volume = execute(executable, ["volume", "rm", "--force", resource.data]);
  const containerError = (containers.stderr || containers.stdout).trim();
  const volumeError = (volume.stderr || volume.stdout).trim();
  const containerErrorLines = containerError.split("\n").filter(Boolean);
  const containersAlreadyAbsent = containerErrorLines.length > 0
    && containerErrorLines.every((line) => /no such container:/i.test(line));
  const volumeAlreadyAbsent = /no such volume|volume .* not found/i.test(volumeError);
  const failures = [];
  if (containers.status !== 0 && !containersAlreadyAbsent) failures.push(`container cleanup failed: ${containerError}`);
  if (volume.status !== 0 && !volumeAlreadyAbsent) failures.push(`volume cleanup failed: ${volumeError}`);
  if (failures.length) fail(failures.join("; "));
}

function curl(options, args, operation) {
  return requireSuccess(execute(required(options, "curl"), args), operation);
}

function captureHttp(options, url) {
  const captureRoot = mkdtempSync(path.join(os.tmpdir(), "sag-fnos-http-"));
  const bodyPath = path.join(captureRoot, "body");
  const headersPath = path.join(captureRoot, "headers");
  try {
    const result = execute(required(options, "curl"), [
      "--silent", "--show-error", "--max-time", "5",
      "--output", bodyPath,
      "--dump-header", headersPath,
      "--write-out", "%{http_code}",
      url,
    ]);
    const readCapture = (file) => {
      try {
        return readFileSync(file, "utf8");
      } catch (error) {
        if (error.code === "ENOENT") return "";
        throw error;
      }
    };
    return {
      curlStatus: result.status,
      curlError: result.stderr.trim(),
      status: result.stdout.trim(),
      headers: readCapture(headersPath),
      body: readCapture(bodyPath),
    };
  } finally {
    rmSync(captureRoot, { recursive: true, force: true });
  }
}

function headerValues(headers, name) {
  const prefix = `${name.toLowerCase()}:`;
  return headers
    .split(/\r?\n/)
    .filter((line) => line.toLowerCase().startsWith(prefix))
    .map((line) => line.slice(line.indexOf(":") + 1).trim());
}

function requireHttpEnvelope(response, operation) {
  if (!/^\d{3}$/.test(response.status)) fail(`${operation} must emit an exact three-digit HTTP status`);
  const statusLines = response.headers
    .split(/\r?\n/)
    .map((line) => /^HTTP\/(?:1\.0|1\.1|2|3) ([0-9]{3})(?: [\x20-\x7e]*)?$/.exec(line))
    .filter(Boolean);
  if (statusLines.length !== 1) fail(`${operation} must contain exactly one HTTP status line`);
  if (statusLines[0][1] !== response.status) fail(`${operation} header status must match curl HTTP status`);
}

async function requireApiReady(options, attempts = 30) {
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    const response = captureHttp(options, "http://127.0.0.1:18001/api/v1/system/ready");
    if (response.curlStatus !== 0 || /^5\d\d$/.test(response.status)) {
      if (attempt < attempts) {
        await delay(2_000);
        continue;
      }
      fail(`API readiness request failed after ${attempts} attempts: ${response.curlError || response.status}`);
    }
    requireHttpEnvelope(response, "API readiness");
    if (response.status !== "200") fail(`API readiness must return exact HTTP 200, received ${response.status}`);
    let payload;
    try {
      payload = JSON.parse(response.body);
    } catch (error) {
      fail(`API readiness must return valid JSON: ${error.message}`);
    }
    if (payload?.status !== "ready") fail("API readiness JSON status must equal ready");
    if (payload.db !== true) fail("API readiness JSON db must equal true");
    return;
  }
}

function requireWebRoot(options) {
  const url = "http://127.0.0.1:13001/";
  const response = captureHttp(options, url);
  if (response.curlStatus !== 0) fail(`Web root request failed: ${response.curlError}`);
  requireHttpEnvelope(response, "Web root");
  if (!["307", "308"].includes(response.status)) {
    fail(`Web root must return exact HTTP 307 or 308, received ${response.status}`);
  }
  const locations = headerValues(response.headers, "Location");
  if (locations.length !== 1) fail("Web root must return exactly one Location header normalized to /chat");
  if (locations[0] !== "/chat") fail(`Web root raw Location must be exactly /chat, received ${locations[0]}`);
  let location;
  try {
    location = new URL(locations[0], url);
  } catch {
    fail("Web root Location must normalize to /chat");
  }
  if (location.origin !== new URL(url).origin || `${location.pathname}${location.search}${location.hash}` !== "/chat") {
    fail(`Web root Location must normalize to /chat, received ${locations[0]}`);
  }
}

function requireWebLogin(options) {
  const response = captureHttp(options, "http://127.0.0.1:13001/login");
  if (response.curlStatus !== 0) fail(`Web login request failed: ${response.curlError}`);
  requireHttpEnvelope(response, "Web login");
  if (response.status !== "200") fail(`Web login must return exact HTTP 200, received ${response.status}`);
  const contentTypes = headerValues(response.headers, "Content-Type");
  if (contentTypes.length !== 1 || !/^text\/html(?:;|$)/i.test(contentTypes[0])) {
    fail(`Web login Content-Type must be text/html, received ${contentTypes.join(", ") || "missing"}`);
  }
  const body = response.body.trim();
  if (!/^<!doctype html>/i.test(body)) fail("Web login body must begin with an HTML doctype");
}

function requireStaticAssetUrl(options) {
  const resource = names(options);
  const result = docker(
    options,
    ["exec", resource.web, "node", "-e", staticAssetProbe],
    "Web static artifact inspection",
  );
  const output = result.stdout.endsWith("\n") ? result.stdout.slice(0, -1) : result.stdout;
  if (!output || output.includes("\n") || output.includes("\r") || !output.startsWith("/_next/static/")) {
    fail("Web artifact inspection must emit exactly one safe static asset URL");
  }
  const encodedSegments = output.slice("/_next/static/".length).split("/");
  let segments;
  try {
    segments = encodedSegments.map((segment) => decodeURIComponent(segment));
  } catch {
    fail("Web artifact inspection must emit exactly one safe static asset URL");
  }
  const safe = segments.length > 0 && segments.every((segment, index) => (
    segment
    && segment !== "."
    && segment !== ".."
    && !segment.includes("/")
    && !segment.includes("\\")
    && encodeURIComponent(segment) === encodedSegments[index]
  ));
  const extension = segments.at(-1)?.endsWith(".js") ? "js" : segments.at(-1)?.endsWith(".css") ? "css" : null;
  if (!safe || !extension) fail("Web artifact inspection must emit exactly one safe static asset URL");
  return { path: output, extension };
}

function requireWebStaticAsset(options, asset) {
  const response = captureHttp(options, `http://127.0.0.1:13001${asset.path}`);
  if (response.curlStatus !== 0) fail(`Web static asset request failed: ${response.curlError}`);
  requireHttpEnvelope(response, "Web static asset");
  if (response.status !== "200") fail(`Web static asset must return exact HTTP 200, received ${response.status}`);
  const contentTypes = headerValues(response.headers, "Content-Type");
  if (contentTypes.length !== 1) fail("Web static asset must return exactly one Content-Type header");
  const expected = asset.extension === "js"
    ? /^(?:application|text)\/javascript(?:;|$)/i
    : /^text\/css(?:;|$)/i;
  if (!expected.test(contentTypes[0])) {
    const kind = asset.extension === "js" ? "JavaScript" : "CSS";
    fail(`Web static asset must return a ${kind} Content-Type, received ${contentTypes[0]}`);
  }
  if (response.body.length === 0) fail("Web static asset body must be nonempty");
}

function sessionStatus(options, body) {
  const data = body === undefined ? [] : [
    "--header", "Content-Type: application/json",
    "--data", JSON.stringify(body),
  ];
  const result = curl(options, [
    "--silent", "--show-error", "--max-time", "10",
    "--output", "/dev/null", "--write-out", "%{http_code}",
    ...data,
    "http://127.0.0.1:18001/api/v1/auth/session",
  ], "API single-user session request");
  return result.stdout.trim();
}

async function smoke(options) {
  const apiImage = required(options, "api_image");
  const webImage = required(options, "web_image");
  if (!apiReference.test(apiImage)) fail("--api-image must be the reviewed API repository at an exact sha256 digest");
  if (!webReference.test(webImage)) fail("--web-image must be the reviewed Web repository at an exact sha256 digest");
  const resource = names(options);
  const sessionSecret = randomBytes(32).toString("hex");
  try {
    docker(options, ["pull", "--platform", "linux/amd64", apiImage], "exact API digest pull");
    docker(options, ["pull", "--platform", "linux/amd64", webImage], "exact Web digest pull");
    docker(options, ["volume", "create", resource.data], "ephemeral API data volume creation");
    docker(options, [
      "run", "--platform", "linux/amd64", "--detach", "--name", resource.api,
      "--publish", "127.0.0.1:18001:8000",
      "--mount", `type=volume,source=${resource.data},target=/data`,
      "--env", "SAG_ENVIRONMENT=prod",
      "--env", "SAG_DEBUG=false",
      "--env", `SAG_SECRET_KEY=${sessionSecret}`,
      "--env", "SAG_AUTH_MODE=single_user",
      "--env", "SAG_DATABASE_URL=sqlite+aiosqlite:////data/sag.db",
      "--env", "SAG_DATA_DIR=/data/engine",
      "--env", "SAG_UPLOAD_DIR=/data/uploads",
      apiImage,
    ], "exact API digest start");
    docker(options, [
      "run", "--platform", "linux/amd64", "--detach", "--name", resource.web,
      "--publish", "127.0.0.1:13001:3000",
      webImage,
    ], "exact Web digest start");
    const attemptsValue = options.readiness_attempts ?? "30";
    if (!/^[1-9]\d*$/.test(attemptsValue)) fail("--readiness-attempts must be a positive integer");
    await requireApiReady(options, Number(attemptsValue));
    if (sessionStatus(options) !== "200") fail("empty single-user session lookup must return 200");
    if (sessionStatus(options, { name: "Digest Smoke Owner" }) !== "201") {
      fail("single-user initialization must return 201");
    }
    requireWebRoot(options);
    requireWebLogin(options);
    requireWebStaticAsset(options, requireStaticAssetUrl(options));
  } finally {
    cleanup(options);
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.command === "cleanup") {
    cleanup(options);
    return;
  }
  if (options.command === "smoke") {
    required(options, "curl");
    await smoke(options);
    return;
  }
  fail(`unknown command: ${options.command}`);
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
