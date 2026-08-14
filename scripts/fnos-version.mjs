#!/usr/bin/env node

import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const CURRENT_VERSION = /^(\d+)\.(\d+)\.(\d+)-fnos$/;
const STABLE_VERSION = /^(\d+)\.(\d+)\.(\d+)$/;
const RELEASE_TAG = /^fnos-v(\d+)\.(\d+)\.(\d+)-fnos(?:\.[1-9]\d*)?$/;

export function isCurrentFnOSVersion(value) {
  return typeof value === "string" && CURRENT_VERSION.test(value);
}

export function assertCurrentFnOSVersion(value) {
  if (!isCurrentFnOSVersion(value)) {
    throw new Error(`fnOS version '${String(value)}' must match x.y.z-fnos`);
  }
  return value;
}

export function resolveNextFnOSVersion(tags, fallbackBase) {
  const fallback = STABLE_VERSION.exec(String(fallbackBase));
  if (!fallback) throw new Error("fallback base must match x.y.z");
  const versions = [];
  for (const tag of tags) {
    const match = RELEASE_TAG.exec(String(tag).trim());
    if (match) versions.push(match.slice(1, 4).map(Number));
  }
  if (!versions.length) return `${fallbackBase}-fnos`;
  versions.sort((left, right) => {
    for (let index = 0; index < 3; index += 1) {
      if (left[index] !== right[index]) return right[index] - left[index];
    }
    return 0;
  });
  const [major, minor, patch] = versions[0];
  return `${major}.${minor}.${patch + 1}-fnos`;
}

function cli() {
  const [flag, fallbackBase, ...extra] = process.argv.slice(2);
  if (flag !== "--fallback-base" || !fallbackBase || extra.length) {
    throw new Error("usage: fnos-version.mjs --fallback-base x.y.z");
  }
  const tags = readFileSync(0, "utf8").split(/\r?\n/).filter(Boolean);
  process.stdout.write(`${resolveNextFnOSVersion(tags, fallbackBase)}\n`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    cli();
  } catch (error) {
    process.stderr.write(`fnos-version: ${error.message}\n`);
    process.exitCode = 1;
  }
}
