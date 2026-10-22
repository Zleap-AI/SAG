import assert from "node:assert/strict";
import { createServer, type Server } from "node:net";
import test from "node:test";

import { resolveApiPort } from "../src/runtime-ports.ts";

async function listen(port = 0): Promise<Server> {
  const server = createServer();
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });
  return server;
}
function port(server: Server): number {
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  return address.port;
}
async function close(server: Server): Promise<void> {
  await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
}

test("an occupied API port falls back to a bindable port without stopping its owner", async t => {
  const occupied = await listen();
  t.after(() => close(occupied));
  const selected = await resolveApiPort("127.0.0.1", port(occupied), 32100);
  assert.notEqual(selected, port(occupied));
  assert.notEqual(selected, 32100);
  assert.equal(occupied.listening, true);
  const backend = await listen(selected);
  await close(backend);
});

test("an available preferred API port is preserved", async () => {
  const candidate = await listen();
  const preferred = port(candidate);
  await close(candidate);
  assert.equal(await resolveApiPort("127.0.0.1", preferred, 32100), preferred);
});

test("the persisted Web port is never chosen for the API", async () => {
  const candidate = await listen();
  const webPort = port(candidate);
  await close(candidate);
  const selected = await resolveApiPort("127.0.0.1", webPort, webPort);
  assert.notEqual(selected, webPort);
  const backend = await listen(selected);
  await close(backend);
});
