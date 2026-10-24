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

  it("sends the retrieval mode only when the caller pinned one", async () => {
    const fetchMock = vi.fn().mockResolvedValue(completedResponse());
    vi.stubGlobal("fetch", fetchMock);

    await streamAgentAsk("agent-1", "thread-1", { query: "问", web_enabled: false, strategy: "sag2" }, vi.fn());
    fetchMock.mockResolvedValue(completedResponse());
    await streamAgentAsk("agent-1", "thread-1", { query: "问", web_enabled: false }, vi.fn());

    const bodies = fetchMock.mock.calls.map((call) => JSON.parse(String((call[1] as RequestInit).body)));
    expect(bodies[0]).toMatchObject({ strategy: "sag2" });
    expect(bodies[1]).not.toHaveProperty("strategy");
  });
});
