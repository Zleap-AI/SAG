import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { resolveStableWebPort } from "../src/runtime-state.ts";

test("reuses the persisted web port when the preferred port becomes free later", async (t) => {
  const userDataDir = mkdtempSync(path.join(tmpdir(), "sag-runtime-state-"));
  t.after(() => rmSync(userDataDir, { recursive: true, force: true }));
  const stateFile = path.join(userDataDir, "desktop-runtime.json");
  const secretKey = "s".repeat(96);
  writeFileSync(stateFile, JSON.stringify({ secretKey }));

  const firstPort = await resolveStableWebPort(
    userDataDir,
    32100,
    async (port) => port === 32101,
  );
  assert.equal(firstPort, 32101);

  const checkedOnRestart: number[] = [];
  const secondPort = await resolveStableWebPort(
    userDataDir,
    32100,
    async (port) => {
      checkedOnRestart.push(port);
      return true;
    },
  );

  assert.equal(secondPort, 32101);
  assert.deepEqual(checkedOnRestart, [32101]);
  assert.deepEqual(JSON.parse(readFileSync(stateFile, "utf8")), {
    secretKey,
    webPort: 32101,
  });
});
