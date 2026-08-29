import assert from "node:assert/strict";
import test from "node:test";

import {
  desktopApiEnvironment,
  localHttpOrigin,
  storageBootstrapPolicy,
} from "../src/runtime-policy.ts";

test("uses a fresh non-migrating workspace only for packaged Windows", () => {
  assert.equal(storageBootstrapPolicy("win32"), "windows_fresh");
  assert.equal(storageBootstrapPolicy("darwin"), "prompt");
  assert.equal(storageBootstrapPolicy("linux"), "prompt");
});

test("injects the actual desktop API address as the canonical DSH URL", () => {
  assert.deepEqual(desktopApiEnvironment("127.0.0.1", 18080), {
    SAG_DESKTOP_HOST: "127.0.0.1",
    SAG_DESKTOP_PORT: "18080",
    SAG_DSH_PUBLIC_URL: "http://127.0.0.1:18080",
  });
  assert.equal(localHttpOrigin("localhost", 8000), "http://localhost:8000");
  assert.equal(localHttpOrigin("::1", 8000), "http://[::1]:8000");
});
