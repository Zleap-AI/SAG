import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("OCTX document export API", () => {
  it("reuses one transfer id when retrying a lost create response", async () => {
    vi.useFakeTimers();
    const body = {
      id: "b".repeat(32),
      direction: "export",
      status: "queued",
      progress: 0,
      created_at: "2026-08-14T00:00:00Z",
      updated_at: "2026-08-14T00:00:00Z",
    };
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error("response lost"))
      .mockResolvedValueOnce(
        new Response(JSON.stringify(body), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" });

    const pending = api.startOctxDocumentExport("source-1", "document-1");
    await vi.advanceTimersByTimeAsync(500);
    await pending;

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const first = fetchMock.mock.calls[0][1] as RequestInit;
    const second = fetchMock.mock.calls[1][1] as RequestInit;
    expect((first.headers as Record<string, string>)["X-OCTX-Transfer-ID"]).toBe(
      "bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb",
    );
    expect((second.headers as Record<string, string>)["X-OCTX-Transfer-ID"]).toBe(
      (first.headers as Record<string, string>)["X-OCTX-Transfer-ID"],
    );
  });
});
