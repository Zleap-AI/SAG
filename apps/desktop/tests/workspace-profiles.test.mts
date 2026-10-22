import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { WorkspaceProfiles } from "../src/workspace-profiles.ts";
import { WorkspaceController, trustedFrame } from "../src/workspace-controller.ts";

function fixture(t: test.TestContext) {
  const dir = mkdtempSync(path.join(tmpdir(), "sag-workspaces-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return dir;
}

test("first install and existing runtime default to local without choosing a connection", t => {
  const dir = fixture(t);
  assert.equal(new WorkspaceProfiles(dir).read().activeId, "local");
  writeFileSync(path.join(dir, "desktop-runtime.json"), JSON.stringify({ secretKey: "private" }));
  const upgraded = new WorkspaceProfiles(dir).read();
  assert.equal(upgraded.activeId, "local");
  assert.equal(upgraded.profiles[0].kind, "local-managed");
});

test("profile persistence validates addresses and isolates sessions after endpoint edits", t => {
  const dir = fixture(t); const store = new WorkspaceProfiles(dir);
  const first = store.save({ name: "NAS", webUrl: "https://nas.example/app/sag/", deployment: "fnos" });
  assert.equal(first.webOrigin, "https://nas.example");
  assert.equal(first.appPrefix, "/app/sag");
  store.activate(first.id);
  assert.equal(new WorkspaceProfiles(dir).read().activeId, first.id);
  const second = store.save({ id: first.id, name: "NAS renamed", webUrl: "https://nas.example/app/sag", deployment: "fnos" });
  assert.equal(second.partition, first.partition);
  const edited = store.save({ id: first.id, name: "NAS", webUrl: "https://nas.example/other/sag", deployment: "fnos" });
  assert.notEqual(edited.partition, first.partition);
  const peer = store.save({ name: "Other identity", webUrl: "https://nas.example/other/sag", deployment: "fnos" });
  assert.notEqual(edited.partition, peer.partition);
  for (const webUrl of ["file:///etc/passwd", "https://u:secret@nas.example/app", "https://nas.example/app?token=x", "https://nas.example/app#x", "https://nas.example/a/%2e%2e/app", "javascript:alert(1)"]) {
    assert.throws(() => store.save({ name: "bad", webUrl, deployment: "fnos" }));
  }
  assert.doesNotMatch(readFileSync(path.join(dir, "workspace-profiles.json"), "utf8"), /secretKey|private/);
  assert.throws(() => store.remove("local"));
});

test("unknown schemas and corrupted profiles fail visibly without overwriting", t => {
  const dir = fixture(t); const file = path.join(dir, "workspace-profiles.json");
  writeFileSync(file, '{"schemaVersion":99,"profiles":[]}');
  assert.throws(() => new WorkspaceProfiles(dir).read());
  assert.match(readFileSync(file, "utf8"), /99/);
});

test("remote mode never starts or stops server; local switch confirms activity", async t => {
  const store = new WorkspaceProfiles(fixture(t));
  const remote = store.save({ name: "NAS", webUrl: "https://nas.example/app/sag", deployment: "fnos" });
  const calls: string[] = []; let approve = false;
  const controller = new WorkspaceController(store, {
    startLocal: async () => { calls.push("start"); return "http://127.0.0.1:3000"; },
    stopLocal: async () => { calls.push("stop"); },
    inspectLocal: async () => { calls.push("inspect"); return true; },
    confirmSwitch: async () => { calls.push("confirm"); return approve; },
  });
  assert.equal((await controller.open(remote.id))!.url, "https://nas.example/app/sag/chat");
  await controller.close(); assert.deepEqual(calls, []);
  await controller.open("local");
  assert.equal(await controller.open(remote.id), null);
  assert.equal(controller.active!.kind, "local-managed");
  approve = true; await controller.open(remote.id); await controller.close();
  assert.deepEqual(calls, ["start", "inspect", "confirm", "inspect", "confirm", "stop"]);
  assert.equal(controller.active, undefined);
});

test("privileged IPC requires exact window and main frame, not just an origin", () => {
  const frame = {}; const contents = { mainFrame: frame };
  assert.equal(trustedFrame({ sender: contents, senderFrame: frame }, contents), true);
  assert.equal(trustedFrame({ sender: contents, senderFrame: {} }, contents), false);
  assert.equal(trustedFrame({ sender: { mainFrame: frame }, senderFrame: frame }, contents), false);
});

test("remote popup deferral is bound to the live opener, profile, partition and session", async () => {
  const { RemoteWebBoundary } = await import("../src/remote-web-boundary.ts");
  const window = {}; const session = {};
  const context = { window, session, profileId: "nas-one", partition: "persist:nas-one" };
  const boundary = new RemoteWebBoundary("https://nas.example", context);
  assert.equal(boundary.popupDisposition(context), "unsupported");
  for (const changed of [{ ...context, window: {} }, { ...context, session: {} },
    { ...context, profileId: "nas-two" }, { ...context, partition: "persist:changed-endpoint" }]) {
    assert.equal(boundary.popupDisposition(changed), "stale");
    assert.equal(boundary.navigationAllowed("https://nas.example/login", changed), false);
  }
  assert.equal(boundary.navigationAllowed("https://nas.example/login", context), true);
  assert.equal(boundary.navigationAllowed("https://other.example/login", context), false);
});

test("fnOS host login targets the exact saved origin and uses normal local task guards", async t => {
  const store = new WorkspaceProfiles(fixture(t));
  const nas = store.save({ name: "NAS", webUrl: "https://nas.example:5667/app/sag", deployment: "fnos" });
  const standard = store.save({ name: "Web", webUrl: "https://web.example/sag", deployment: "standard" });
  const calls: string[] = []; let approve = false;
  const controller = new WorkspaceController(store, {
    startLocal: async () => { calls.push("start"); return "http://127.0.0.1:3000"; },
    stopLocal: async () => { calls.push("stop"); },
    inspectLocal: async () => { calls.push("inspect"); return true; },
    confirmSwitch: async () => { calls.push("confirm"); return approve; },
  });
  await controller.open("local");
  assert.equal(await controller.open(nas.id, "host-login"), null);
  assert.equal(controller.active!.kind, "local-managed");
  approve = true;
  const login = (await controller.open(nas.id, "host-login"))!;
  assert.equal(login.url, "https://nas.example:5667/");
  assert.equal(login.profile.partition, nas.partition);
  assert.equal((await controller.open(nas.id))!.url, "https://nas.example:5667/app/sag/chat");
  assert.equal(controller.active!.partition, nas.partition);
  assert.deepEqual(calls, ["start", "inspect", "confirm", "inspect", "confirm", "stop"]);
  await assert.rejects(controller.open(standard.id, "host-login"), /fnOS/);
  await assert.rejects(controller.open("local", "host-login"), /fnOS/);
  assert.equal(controller.active!.id, nas.id);
});

test("editing a target while local shutdown waits cannot open its superseded endpoint", async t => {
  const store = new WorkspaceProfiles(fixture(t));
  const nas = store.save({ name: "NAS", webUrl: "https://old.example/app/sag", deployment: "fnos" });
  let release: () => void = () => {};
  let stopping: () => void = () => {};
  const stopped = new Promise<void>(resolve => { release = resolve; });
  const entered = new Promise<void>(resolve => { stopping = resolve; });
  const controller = new WorkspaceController(store, {
    startLocal: async () => "http://127.0.0.1:3000",
    stopLocal: async () => { stopping(); await stopped; },
    inspectLocal: async () => false, confirmSwitch: async () => true,
  });
  await controller.open("local");
  const pending = controller.open(nas.id);
  await entered;
  const updated = store.save({ id: nas.id, name: "NAS", webUrl: "https://new.example/app/sag", deployment: "fnos" });
  release();
  await assert.rejects(pending, /连接已更改/);
  assert.equal(controller.active, undefined);
  assert.equal(store.read().profiles.find(profile => profile.id === nas.id)?.partition, updated.partition);
});
