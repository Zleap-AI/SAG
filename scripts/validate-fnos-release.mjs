#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import path from "node:path";
import process from "node:process";

import {
  loadGatewayPolicy,
  validateGatewayImageReference,
} from "./fnos-gateway-policy.mjs";

if (process.argv.length !== 3) {
  console.error("usage: node scripts/validate-fnos-release.mjs <rendered-compose-path>");
  process.exit(2);
}
const composePath = path.resolve(process.cwd(), process.argv[2]);
const immutableDigest = /@sha256:[a-f0-9]{64}$/i;
const requiredSecretReference = /^\$\{SAG_SECRET_KEY:?\?[^}]*\}$/;
const fnosSecretEnvFile = "${TRIM_PKGETC}/sag.env";

function fail(messages) {
  for (const message of messages) console.error(`release-compose: ${message}`);
  process.exit(1);
}

function loadCompose(file) {
  const result = spawnSync(
    "docker",
    ["compose", "-f", file, "config", "--no-interpolate", "--no-path-resolution", "--format", "json"],
    { encoding: "utf8" },
  );
  if (result.error) {
    fail([`could not run docker compose: ${result.error.message}`]);
  }
  if (result.status !== 0) {
    fail([`could not parse ${file}: ${result.stderr.trim() || result.stdout.trim()}`]);
  }
  try {
    return JSON.parse(result.stdout);
  } catch (error) {
    fail([`docker compose did not return JSON for ${file}: ${error.message}`]);
  }
}

function validateImage(name, service, errors) {
  if (Object.hasOwn(service, "build")) {
    errors.push(`${name} must not define build in a release Compose file`);
  }
  if (typeof service.image !== "string") {
    errors.push(`${name} must define an image pinned by digest`);
    return;
  }
  if (/:latest(?:@|$)/.test(service.image)) {
    errors.push(`${name} must not use the latest tag`);
  }
  if (!immutableDigest.test(service.image)) {
    errors.push(`${name} image must use an immutable sha256 digest`);
  }
}

function hasFnosSecretEnvFile(api) {
  if (!Array.isArray(api.env_file) || api.env_file.length !== 1) return false;
  const [entry] = api.env_file;
  return entry
    && typeof entry === "object"
    && entry.path === fnosSecretEnvFile
    && entry.required === true;
}

function validateApiSecret(api, errors) {
  const secret = api.environment?.SAG_SECRET_KEY;
  if (typeof secret === "string" && requiredSecretReference.test(secret)) return;
  if (secret === undefined && hasFnosSecretEnvFile(api)) return;
  if (typeof secret === "string") {
    errors.push("api must use a required SAG_SECRET_KEY reference, not a literal secret");
  } else {
    errors.push("api must use a required SAG_SECRET_KEY reference or exactly one required env_file at ${TRIM_PKGETC}/sag.env");
  }
}

function validateApiAuth(api, errors) {
  if (api.environment?.SAG_AUTH_MODE !== "single_user") {
    errors.push("api must set SAG_AUTH_MODE=single_user for the fnOS no-auth release");
  }
  if (api.environment?.SAG_AUTH_BOOTSTRAP_TOKEN !== undefined) {
    errors.push("api must not define SAG_AUTH_BOOTSTRAP_TOKEN in single-user mode");
  }
}

const expectedGatewayPort = "${TRIM_SERVICE_PORT}:80";

function validateHostExposure(name, service, errors) {
  if (service.network_mode === "host") {
    errors.push(`${name} must not use host networking in a release Compose file`);
  }
  if (name === "gateway") {
    if (
      !Array.isArray(service.ports)
      || service.ports.length !== 1
      || service.ports[0] !== expectedGatewayPort
    ) {
      errors.push(`gateway must publish exactly ${expectedGatewayPort}`);
    }
    return;
  }
  if (
    service.ports !== undefined
    && (!Array.isArray(service.ports) || service.ports.length > 0)
  ) {
    errors.push(`${name} must not publish host ports in a release Compose file`);
  }
}

function validate(compose, gatewayPolicy) {
  const errors = [];
  const services = compose.services;
  if (!services || typeof services !== "object") return ["Compose must define services"];

  for (const [name, service] of Object.entries(services)) {
    if (!service || typeof service !== "object") {
      errors.push(`${name} must be a service object`);
      continue;
    }
    validateImage(name, service, errors);
    validateHostExposure(name, service, errors);
  }

  for (const name of ["api", "web"]) {
    if (!services[name] || typeof services[name] !== "object") {
      errors.push(`Compose must define the ${name} service`);
      continue;
    }
  }
  if (services.api && typeof services.api === "object") {
    validateApiSecret(services.api, errors);
    validateApiAuth(services.api, errors);
  }
  if (!services.gateway || typeof services.gateway !== "object") {
    errors.push("Compose must define the gateway service");
  } else if (typeof services.gateway.image === "string") {
    try {
      validateGatewayImageReference(gatewayPolicy, services.gateway.image);
    } catch (error) {
      errors.push(error.message.replace(/^fnOS gateway policy:\s*/, ""));
    }
  }
  return errors;
}

let gatewayPolicy;
try {
  gatewayPolicy = await loadGatewayPolicy();
} catch (error) {
  fail([error.message]);
}
const errors = validate(loadCompose(composePath), gatewayPolicy);
if (errors.length) fail(errors);
console.log(`release Compose validation passed: ${composePath}`);
