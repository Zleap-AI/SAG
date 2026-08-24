import assert from "node:assert/strict";
import test from "node:test";

import { storageBootstrapPolicy } from "../src/runtime-policy.ts";

test("uses a fresh non-migrating workspace only for packaged Windows", () => {
  assert.equal(storageBootstrapPolicy("win32"), "windows_fresh");
  assert.equal(storageBootstrapPolicy("darwin"), "prompt");
  assert.equal(storageBootstrapPolicy("linux"), "prompt");
});
