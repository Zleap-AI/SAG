import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "./api";
import type { SearchResponse } from "./types";

function result(summary = ""): SearchResponse {
  return {
    query: "测试问题",
    sections: [],
    events: [],
    entities: [],
    relations: [],
    source_hits: [],
    summary,
    exploration_id: null,
    stats: {},
  };
}

function streamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("global search stream", () => {
  it("parses fragmented result, delta and completed frames in order", async () => {
    const base = result();
    const completed = result("流式回答 [1]");
    const wire = [
      `event: result\ndata: ${JSON.stringify(base)}\n\n`,
      'event: summary.delta\ndata: {"delta":"流式"}\n\n',
      'event: summary.delta\ndata: {"delta":"回答 [1]"}\n\n',
      `event: completed\ndata: ${JSON.stringify(completed)}\n\n`,
    ].join("");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(streamResponse([wire.slice(0, 37), wire.slice(37, 91), wire.slice(91)])),
    );
    const received: string[] = [];

    const value = await api.streamGlobalSearch(
      { query: "测试问题" },
      {
        onResult: () => received.push("result"),
        onSummaryDelta: (delta) => received.push(delta),
        onCompleted: () => received.push("completed"),
      },
    );

    expect(value).toEqual(completed);
    expect(received).toEqual(["result", "流式", "回答 [1]", "completed"]);
  });

  it("turns a streamed error into an ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        streamResponse(['event: error\ndata: {"code":"generation_failed","message":"生成失败"}\n\n']),
      ),
    );

    await expect(
      api.streamGlobalSearch(
        { query: "测试问题" },
        { onResult: vi.fn(), onSummaryDelta: vi.fn(), onCompleted: vi.fn() },
      ),
    ).rejects.toMatchObject({ code: "generation_failed", message: "生成失败" } satisfies Partial<ApiError>);
  });

  it("returns immediately at completed without waiting for the server to close", async () => {
    const encoder = new TextEncoder();
    let cancelled = false;
    const base = result();
    const completed = result("完成 [1]");
    const response = new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(encoder.encode(
            `event: result\ndata: ${JSON.stringify(base)}\n\n`
            + `event: completed\ndata: ${JSON.stringify(completed)}\n\n`,
          ));
        },
        cancel() {
          cancelled = true;
        },
      }),
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));

    await expect(api.streamGlobalSearch(
      { query: "测试问题" },
      { onResult: vi.fn(), onSummaryDelta: vi.fn(), onCompleted: vi.fn() },
    )).resolves.toEqual(completed);
    expect(cancelled).toBe(true);
  });

  it("rejects out-of-order deltas and cancels the response body", async () => {
    let cancelled = false;
    const encoder = new TextEncoder();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(encoder.encode(
            'event: summary.delta\ndata: {"delta":"过早"}\n\n',
          ));
        },
        cancel() {
          cancelled = true;
        },
      }),
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    )));

    await expect(api.streamGlobalSearch(
      { query: "测试问题" },
      { onResult: vi.fn(), onSummaryDelta: vi.fn(), onCompleted: vi.fn() },
    )).rejects.toMatchObject({ code: "invalid_search_stream" });
    expect(cancelled).toBe(true);
  });
});
