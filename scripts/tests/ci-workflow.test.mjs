import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../..", import.meta.url));

test("CI installs the locked API environment before backend checks", async () => {
  const workflow = await readFile(path.join(root, ".github/workflows/ci.yml"), "utf8");
  assert.match(workflow, /astral-sh\/setup-uv@/);
  assert.match(workflow, /uv sync --frozen --extra dev/);
  assert.match(workflow, /uv run --extra dev pytest -q/);
});

test("CI release safety runs the fnOS NOFILE launcher regressions", async () => {
  const workflow = await readFile(path.join(root, ".github/workflows/ci.yml"), "utf8");
  const releaseSafety = workflow.match(/^  release-safety:[\s\S]*?(?=^  backend:)/m)?.[0];
  assert.ok(releaseSafety, "release-safety job must exist");
  const testCommands = [...releaseSafety.matchAll(/^\s+run: node --test (.+)$/gm)];
  assert.ok(testCommands.some(([, args]) => args.split(/\s+/).includes("scripts/tests/fnos-native-nofile.test.mjs")),
    "release-safety must execute fnos-native-nofile.test.mjs with node --test");
});
