#!/usr/bin/env node

import process from "node:process";
import { fileURLToPath } from "node:url";

const digestReference = /^[a-z0-9.-]+(?:\/[a-z0-9._-]+)+(?::[a-z0-9._-]+)?@sha256:[a-f0-9]{64}$/;

const globalRepositories = {
  api: "ghcr.1ms.run/luoshuai990529/sag-api@",
  web: "ghcr.1ms.run/luoshuai990529/sag-web@",
  gateway: "ghcr.1ms.run/luoshuai990529/sag-gateway:",
};
const cnRepositoryPrefix = /^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]+)?(?:\/[a-z0-9][a-z0-9._-]*)+$/;

function fail(message) {
  throw new Error(`fnos-registry-channel: ${message}`);
}

function parseArgs(argv) {
  const [command, ...rest] = argv;
  if (!command) fail("a command is required");
  const options = { command };
  for (let index = 0; index < rest.length; index += 2) {
    const name = rest[index];
    const value = rest[index + 1];
    if (!name?.startsWith("--") || !value || value.startsWith("--")) {
      fail(`value required for ${name ?? "argument"}`);
    }
    options[name.slice(2).replaceAll("-", "_")] = value;
  }
  return options;
}

function requireOption(options, name) {
  const value = options[name];
  if (!value) fail(`--${name.replaceAll("_", "-")} is required`);
  return value;
}

export function validateChannelConfiguration({ channel, cnRepositoryPrefix: prefix }) {
  if (channel !== "global" && channel !== "cn") fail("--channel must be global or cn");
  if (channel !== "cn") return { channel, repositories: globalRepositories };
  if (!prefix) fail("cn channel requires an approved --cn-repository-prefix");
  if (!cnRepositoryPrefix.test(prefix) || !prefix.split("/")[0].includes(".")) {
    fail("cn repository prefix must be a DNS registry host plus a lowercase namespace");
  }
  return {
    channel,
    repositories: {
      api: `${prefix}/sag-api@`,
      web: `${prefix}/sag-web@`,
      gateway: `${prefix}/sag-gateway@`,
    },
  };
}

export function validateChannelImages({ channel, cnRepositoryPrefix: prefix, api, web, gateway }) {
  const configuration = validateChannelConfiguration({ channel, cnRepositoryPrefix: prefix });
  const images = { api, web, gateway };
  for (const [name, image] of Object.entries(images)) {
    if (!digestReference.test(image)) fail(`${name} image must use a lowercase immutable sha256 digest reference`);
    if (!image.startsWith(configuration.repositories[name])) {
      fail(`${channel} ${name} image must use the approved repository`);
    }
  }
  return { channel, images };
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.command !== "validate") fail(`unknown command: ${options.command}`);
  const result = validateChannelImages({
    channel: requireOption(options, "channel"),
    cnRepositoryPrefix: options.cn_repository_prefix,
    api: requireOption(options, "api_image"),
    web: requireOption(options, "web_image"),
    gateway: requireOption(options, "gateway_image"),
  });
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

if (process.argv[1] && pathEqualsCurrentModule(process.argv[1])) {
  try {
    main();
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}

function pathEqualsCurrentModule(candidate) {
  return fileURLToPath(import.meta.url) === candidate;
}
