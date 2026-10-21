import assert from "node:assert/strict";
import test from "node:test";
import { FnOsHostContext } from "../src/fnos-host-context.ts";
import type { WorkspaceProfile } from "../src/workspace-profiles.ts";

const profile: WorkspaceProfile = { id: "nas-one", name: "NAS", kind: "remote-web", deployment: "fnos",
  webOrigin: "https://nas.example", appPrefix: "/app/sag", partition: "persist:nas-one" };
function fixture(probe: (profile: WorkspaceProfile, signal: AbortSignal) => Promise<boolean>) {
  const windows: Array<{ visible: boolean; destroyed: boolean; loads: number }> = [];
  const context = new FnOsHostContext({ create: () => {
    const window = { visible: false, destroyed: false, loads: 0 }; windows.push(window);
    return { window, load: () => { window.loads++; }, show: () => { window.visible = true; },
      hide: () => { window.visible = false; }, destroy: () => { window.destroyed = true; }, isDestroyed: () => window.destroyed };
  }, probe }, { attempts: 2, pollMs: 0, timeoutMs: 100 });
  return { context, windows };
}

test("fnOS knowledge readiness keeps its normal host page alive in the same profile", async () => {
  const { context, windows } = fixture(async () => true);
  assert.equal(await context.ready(profile), true);
  context.hide(profile);
  assert.equal(windows.length, 1);
  assert.equal(windows[0].destroyed, false);
  assert.equal(windows[0].visible, false);
  assert.equal(context.isCurrent(windows[0], profile), true);
  context.show(profile);
  assert.equal(windows[0].loads, 1, "showing host login must reuse its live page");
  context.close(); assert.equal(windows[0].destroyed, true);
});

test("an unready host stays visible and cannot authorize a knowledge window", async () => {
  const { context, windows } = fixture(async () => false);
  assert.equal(await context.ready(profile), false);
  assert.equal(windows[0].visible, true);
  assert.equal(windows[0].destroyed, false);
  context.close();
});

test("switching partition destroys the old host and rejects its late ready response", async () => {
  let release: (value: boolean) => void = () => {};
  const { context, windows } = fixture(() => new Promise(resolve => { release = resolve; }));
  const pending = context.ready(profile);
  const peer = { ...profile, id: "nas-two", partition: "persist:nas-two" };
  context.show(peer);
  assert.equal(windows[0].destroyed, true);
  assert.equal(context.isCurrent(windows[0], profile), false);
  assert.equal(context.isCurrent(windows[1], peer), true);
  release(true);
  assert.equal(await pending, false);
  context.close();
});

test("closing host context cancels bootstrap and prevents stale callbacks from reopening it", async () => {
  const { context, windows } = fixture(async (_profile, signal) => new Promise(resolve => {
    signal.addEventListener("abort", () => resolve(true), { once: true });
  }));
  const pending = context.ready(profile);
  context.close();
  assert.equal(await pending, false);
  assert.equal(windows.length, 1);
  assert.equal(windows[0].destroyed, true);
});

test("host readiness is bounded and only fnOS profiles may create host contexts", async () => {
  const { context, windows } = fixture(async (_profile, signal) => new Promise(resolve => {
    signal.addEventListener("abort", () => resolve(false), { once: true });
  }));
  assert.equal(await context.ready(profile), false);
  assert.equal(windows[0].visible, true);
  context.close();
  assert.throws(() => context.show({ ...profile, deployment: "standard" }), /fnOS/);
  assert.equal(windows.length, 1);
});

test("clearing another profile does not destroy this profile's live host", async () => {
  const { context, windows } = fixture(async () => true);
  await context.ready(profile);
  context.closeProfile("other-profile");
  assert.equal(windows[0].destroyed, false);
  context.closeProfile(profile.id);
  assert.equal(windows[0].destroyed, true);
});
