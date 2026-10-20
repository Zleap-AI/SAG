import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "./api";

const result = { query: "question", sections: [], events: [], entities: [], relations: [], source_hits: [], summary: "", exploration_id: null, stats: {} };
const handlers = { onResult: vi.fn(), onSummaryDelta: vi.fn(), onCompleted: vi.fn() };
beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(AbortSignal, "timeout").mockImplementation(ms => {
    const controller = new AbortController();
    setTimeout(() => controller.abort(new DOMException("Timed out", "TimeoutError")), ms);
    return controller.signal;
  });
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function delayedFetch(stream: boolean, delay = 31_000) {
  vi.stubGlobal("fetch", vi.fn((_url, options: RequestInit) => new Promise<Response>((resolve, reject) => {
    options.signal?.addEventListener("abort", () => reject(options.signal?.reason), { once: true });
    setTimeout(() => resolve(stream
      ? new Response(`event: result\ndata: ${JSON.stringify(result)}\n\nevent: completed\ndata: ${JSON.stringify(result)}\n\n`, { headers: { "Content-Type": "text/event-stream" } })
      : Response.json(result)), delay);
  })));
}

it.each([undefined, "multi_jev"] as const)("lets the backend bound %s search beyond 30 seconds", async strategy => {
  delayedFetch(false);
  const pending = api.globalSearch({ query: "question", strategy }).catch(error => error);
  await vi.advanceTimersByTimeAsync(31_000);
  expect(await pending).toMatchObject(result);
});

it.each([undefined, "multi_jev"] as const)("lets the backend bound %s first stream result beyond 30 seconds", async strategy => {
  delayedFetch(true);
  const pending = api.streamGlobalSearch({ query: "question", strategy }, handlers).catch(error => error);
  await vi.advanceTimersByTimeAsync(31_000);
  expect(await pending).toMatchObject(result);
});

it("retains the client deadline for explicit vector search", async () => {
  delayedFetch(false);
  const pending = api.globalSearch({ query: "question", strategy: "vector" }).catch(error => error);
  await vi.advanceTimersByTimeAsync(30_000);
  expect(await pending).toMatchObject({ code: "timeout" });
});

it("preserves caller cancellation while inheriting JEV", async () => {
  delayedFetch(false);
  const controller = new AbortController();
  const pending = api.globalSearch({ query: "question" }, controller.signal).catch(error => error);
  controller.abort();
  expect(await pending).toMatchObject({ code: "aborted" });
});

it("keeps the 45 second idle deadline after the first JEV stream result", async () => {
  let wire: ReadableStreamDefaultController<Uint8Array>;
  const encoder = new TextEncoder();
  vi.stubGlobal("fetch", vi.fn(async (_url, options: RequestInit) => {
    options.signal?.addEventListener("abort", () => wire.error(options.signal?.reason), { once: true });
    return new Response(new ReadableStream<Uint8Array>({ start(controller) {
      wire = controller; controller.enqueue(encoder.encode(`event: result\ndata: ${JSON.stringify(result)}\n\n`));
    } }));
  }));
  const pending = api.streamGlobalSearch({ query: "question", strategy: "multi_jev" }, handlers).catch(error => error);
  await vi.advanceTimersByTimeAsync(45_000);
  expect(await pending).toMatchObject({ code: "timeout" });
});

it("gives a draft probe its ranking budget plus five seconds", async () => {
  delayedFetch(false, 64_000);
  const pending = api.testJevModelConfig({ jev_ranking_timeout_seconds: 60 }).catch(error => error);
  await vi.advanceTimersByTimeAsync(64_000);
  expect(await pending).toMatchObject(result);
  expect(vi.mocked(AbortSignal.timeout)).toHaveBeenCalledWith(65_000);
});

it("leaves an inherited probe budget to the backend", async () => {
  delayedFetch(false, 40_000);
  const pending = api.testJevModelConfig({}).catch(error => error);
  await vi.advanceTimersByTimeAsync(40_000);
  expect(await pending).toMatchObject(result);
  expect(vi.mocked(AbortSignal.timeout)).not.toHaveBeenCalled();
});

it("does not retry a draft probe when the service is unavailable", async () => {
  vi.stubGlobal("window", { setTimeout: (callback: () => void) => { callback(); return 0; } });
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ error: { code: "jev_unavailable", message: "Unavailable" } }, { status: 503, headers: { "retry-after": "0" } })));
  await expect(api.testJevModelConfig({ jev_model: "draft-model" })).rejects.toMatchObject({ status: 503 });
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/system/model-config/jev/test"), expect.objectContaining({ method: "POST", body: '{"jev_model":"draft-model"}' }));
});
