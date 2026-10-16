/** @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

const searchResult = {
  query: "slow query", sections: [], events: [], entities: [], relations: [], source_hits: [],
  summary: "", exploration_id: null, stats: {},
};

beforeEach(() => {
  vi.useFakeTimers();
  // Native AbortSignal.timeout uses the browser clock; route that clock through
  // fake timers while keeping the production signal composition and abort path.
  vi.spyOn(AbortSignal, "timeout").mockImplementation(milliseconds => {
    const controller = new AbortController();
    setTimeout(() => controller.abort(new DOMException("Timed out", "TimeoutError")), milliseconds);
    return controller.signal;
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.clearAllTimers();
  vi.useRealTimers();
});

function slowFetch(milliseconds: number, body: unknown) {
  vi.stubGlobal("fetch", (_url: string, options: RequestInit) => new Promise<Response>((resolve, reject) => {
    const signal = options.signal;
    const abort = () => {
      clearTimeout(timer);
      reject(signal?.reason ?? new DOMException("Cancelled", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", abort);
      resolve(new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } }));
    }, milliseconds);
    if (signal?.aborted) abort();
    else signal?.addEventListener("abort", abort, { once: true });
  }));
}

function outcome<T>(request: Promise<T>) {
  return request.then(value => ({ value, error: undefined }), error => ({ value: undefined, error }));
}

describe("JEV request deadlines", () => {
  it("allows a 35-second draft connection test when its ranking limit is longer", async () => {
    slowFetch(35_000, { ok: true, message: "Connected" });
    const result = outcome(api.testJevModelConfig({ jev_ranking_timeout_seconds: 60 }));
    await vi.advanceTimersByTimeAsync(35_000);
    expect(await result).toEqual({ value: { ok: true, message: "Connected" }, error: undefined });
  });

  it("bounds the draft connection test by its ranking limit plus transport time", async () => {
    slowFetch(60_000, { ok: true });
    let settled = false;
    const result = outcome(api.testJevModelConfig({ jev_ranking_timeout_seconds: 40 })).then(value => { settled = true; return value; });
    await vi.advanceTimersByTimeAsync(44_999);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect((await result).error).toMatchObject({ code: "timeout" });
  });

  it("does not guess a short draft deadline when the server must resolve its saved setting", async () => {
    slowFetch(35_000, { ok: true, message: "Connected" });
    const result = outcome(api.testJevModelConfig({}));
    await vi.advanceTimersByTimeAsync(35_000);
    expect(await result).toEqual({ value: { ok: true, message: "Connected" }, error: undefined });
  });

  it("allows a 35-second synchronous JEV search", async () => {
    slowFetch(35_000, searchResult);
    const result = outcome(api.globalSearch({ query: "slow query", strategy: "multi_jev" }));
    await vi.advanceTimersByTimeAsync(35_000);
    expect(await result).toEqual({ value: searchResult, error: undefined });
  });

  it("preserves explicit cancellation of a synchronous JEV search", async () => {
    slowFetch(60_000, searchResult);
    const controller = new AbortController();
    const result = outcome(api.globalSearch({ query: "slow query", strategy: "multi_jev" }, controller.signal));
    await vi.advanceTimersByTimeAsync(35_000);
    controller.abort();
    expect((await result).error).toMatchObject({ code: "aborted" });
  });

  it("lets an unspecified search mode inherit a slow JEV default", async () => {
    slowFetch(35_000, searchResult);
    const result = outcome(api.globalSearch({ query: "slow query" }));
    await vi.advanceTimersByTimeAsync(35_000);
    expect(await result).toEqual({ value: searchResult, error: undefined });
  });

  it("preserves cancellation of a draft connection test", async () => {
    slowFetch(60_000, { ok: true });
    const controller = new AbortController();
    let settled = false;
    const result = outcome(api.testJevModelConfig({ jev_ranking_timeout_seconds: 60 }, controller.signal)).then(value => { settled = true; return value; });
    controller.abort();
    await vi.advanceTimersByTimeAsync(0);
    expect(settled).toBe(true);
    expect((await result).error).toMatchObject({ code: "aborted" });
  });

  it("keeps the ordinary search deadline at 30 seconds", async () => {
    slowFetch(35_000, searchResult);
    const result = outcome(api.globalSearch({ query: "slow query", strategy: "vector" }));
    await vi.advanceTimersByTimeAsync(30_000);
    expect((await result).error).toMatchObject({ code: "timeout" });
  });

  it("lets a comparison containing JEV finish after 35 seconds", async () => {
    const comparison = { query: "slow query", results: [], judges: [], judge_enabled: false, judge_reason: null };
    slowFetch(35_000, comparison);
    const result = outcome(api.evalCompare({ query: "slow query", strategies: ["vector", "multi_jev"] }));
    await vi.advanceTimersByTimeAsync(35_000);
    expect(await result).toEqual({ value: comparison, error: undefined });
  });

  it("keeps a comparison without JEV bounded by 30 seconds", async () => {
    slowFetch(35_000, {});
    const result = outcome(api.evalCompare({ query: "slow query", strategies: ["vector", "multi"] }));
    await vi.advanceTimersByTimeAsync(30_000);
    expect((await result).error).toMatchObject({ code: "timeout" });
  });
});

function delayedSearchStream(firstResultMs: number, { complete = true, pings = false } = {}) {
  vi.stubGlobal("fetch", (_url: string, options: RequestInit) => {
    const signal = options.signal;
    const encoder = new TextEncoder();
    let timer: ReturnType<typeof setTimeout>;
    let pingTimer: ReturnType<typeof setInterval> | undefined;
    let cleanup: () => void;
    return Promise.resolve(new Response(new ReadableStream({
      start(controller) {
        const abort = () => { cleanup(); controller.error(signal?.reason ?? new DOMException("Cancelled", "AbortError")); };
        cleanup = () => { clearTimeout(timer); clearInterval(pingTimer); signal?.removeEventListener("abort", abort); };
        timer = setTimeout(() => {
          controller.enqueue(encoder.encode(`event: result\ndata: ${JSON.stringify(searchResult)}\n\n`));
          if (complete) {
            controller.enqueue(encoder.encode(`event: completed\ndata: ${JSON.stringify(searchResult)}\n\n`));
            cleanup(); controller.close();
          }
        }, firstResultMs);
        if (pings) pingTimer = setInterval(() => controller.enqueue(encoder.encode(": ping\n\n")), 10_000);
        if (signal?.aborted) abort();
        else signal?.addEventListener("abort", abort, { once: true });
      },
      cancel() { cleanup(); },
    }), { headers: { "Content-Type": "text/event-stream" } }));
  });
}
const handlers = () => ({ onResult: vi.fn(), onSummaryDelta: vi.fn(), onCompleted: vi.fn() });

describe("JEV search stream deadlines", () => {
  it.each(["multi_jev", undefined] as const)("waits 35 seconds for a first result with mode %s", async strategy => {
    delayedSearchStream(35_000, { pings: true });
    const result = outcome(api.streamGlobalSearch({ query: "slow query", strategy }, handlers()));
    await vi.advanceTimersByTimeAsync(35_000);
    expect(await result).toEqual({ value: searchResult, error: undefined });
  });
  it("keeps the 30-second first-result limit for ordinary explicit modes", async () => {
    delayedSearchStream(35_000, { pings: true });
    const result = outcome(api.streamGlobalSearch({ query: "slow query", strategy: "vector" }, handlers()));
    await vi.advanceTimersByTimeAsync(30_000);
    expect((await result).error).toMatchObject({ code: "timeout" });
  });
  it("still cancels JEV before the first result when the caller aborts", async () => {
    delayedSearchStream(60_000);
    const controller = new AbortController();
    const result = outcome(api.streamGlobalSearch({ query: "slow query", strategy: "multi_jev" }, handlers(), controller.signal));
    await vi.advanceTimersByTimeAsync(35_000);
    controller.abort();
    expect((await result).error).toMatchObject({ code: "aborted" });
  });
  it("retains the generation idle limit after a JEV first result", async () => {
    delayedSearchStream(1_000, { complete: false });
    const result = outcome(api.streamGlobalSearch({ query: "slow query", strategy: "multi_jev" }, handlers()));
    await vi.advanceTimersByTimeAsync(46_000);
    expect((await result).error).toMatchObject({ code: "timeout" });
  });
});
