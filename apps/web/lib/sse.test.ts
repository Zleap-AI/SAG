import { afterEach, describe, expect, it, vi } from "vitest";

import { streamAgentAsk } from "./sse";

function completedResponse(): Response {
  const event = {
    version: 1,
    type: "run.completed",
    run_id: "run-1",
    sequence: 1,
    timestamp: "2026-07-13T00:00:00Z",
    turn: 1,
    payload: {},
  };
  return new Response(`event: run.completed\ndata: ${JSON.stringify(event)}\n\n`, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("agent ask transport", () => {
  it.each([false, true])("sends web_enabled=%s on every round", async (webEnabled) => {
    const fetchMock = vi.fn().mockResolvedValue(completedResponse());
    vi.stubGlobal("fetch", fetchMock);

    await streamAgentAsk(
      "agent-1",
      "thread-1",
      { query: "测试", web_enabled: webEnabled },
      vi.fn(),
    );

    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(request.body))).toMatchObject({
      query: "测试",
      web_enabled: webEnabled,
    });
  });
});
