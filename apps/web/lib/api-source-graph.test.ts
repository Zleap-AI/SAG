import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import type { SourceGraphResponse } from "./types";

const emptyGraph: SourceGraphResponse = {
  documents: [],
  events: [],
  entities: [],
  relations: [],
  counts: {
    documents: 0,
    events: 0,
    entities: 0,
    shown_documents: 0,
    shown_events: 0,
    shown_entities: 0,
    shown_relations: 0,
  },
  truncated: false,
};

function graphResponse() {
  return new Response(JSON.stringify(emptyGraph), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("source graph API document scope", () => {
  it("omits document_ids for the all-documents scope", async () => {
    const fetchMock = vi.fn().mockResolvedValue(graphResponse());
    vi.stubGlobal("fetch", fetchMock);

    await api.getSourceGraph("source-a", { limit: 300 });

    const url = new URL(String(fetchMock.mock.calls[0][0]), "http://localhost");
    expect(url.searchParams.get("event_limit")).toBe("300");
    expect(url.searchParams.has("document_ids")).toBe(false);
  });

  it("sends repeated, deduplicated document_ids for a multi-selection", async () => {
    const fetchMock = vi.fn().mockResolvedValue(graphResponse());
    vi.stubGlobal("fetch", fetchMock);

    await api.getSourceGraph("source-a", {
      documentIds: ["doc-a", "doc-b", "doc-b"],
    });

    const url = new URL(String(fetchMock.mock.calls[0][0]), "http://localhost");
    expect(url.searchParams.getAll("document_ids")).toEqual(["doc-a", "doc-b"]);
  });

  it("keeps an explicit empty scope distinct from all documents", async () => {
    const fetchMock = vi.fn().mockResolvedValue(graphResponse());
    vi.stubGlobal("fetch", fetchMock);

    await api.getSourceGraph("source-a", { documentIds: [] });

    const url = new URL(String(fetchMock.mock.calls[0][0]), "http://localhost");
    expect(url.searchParams.has("document_ids")).toBe(true);
    expect(url.searchParams.getAll("document_ids")).toEqual([""]);
  });
});

describe("activity API source scope", () => {
  it("omits source_ids when activity is not scoped", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("[]", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.getActivity();

    const url = new URL(String(fetchMock.mock.calls[0][0]), "http://localhost");
    expect(url.searchParams.has("source_ids")).toBe(false);
  });

  it("sends repeated, deduplicated source_ids for scoped activity", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("[]", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.getActivity(["source-a", "source-b", "source-a"]);

    const url = new URL(String(fetchMock.mock.calls[0][0]), "http://localhost");
    expect(url.searchParams.getAll("source_ids")).toEqual([
      "source-a",
      "source-b",
    ]);
  });
});
