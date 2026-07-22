import { describe, expect, it } from "vitest";

import type { SearchEvent } from "./types";
import { sortSearchEvents } from "./search-result-sort";

function event(id: string, startTime: string | null): SearchEvent {
  return {
    id,
    document_id: null,
    source_id: "source-1",
    source_name: "小说",
    title: id,
    summary: "",
    category: "",
    rank: 0,
    parent_id: null,
    chunk_id: null,
    start_time: startTime,
    score: 0.8,
  };
}

describe("search result event sorting", () => {
  it("keeps the API relevance order by default", () => {
    const events = [
      event("relevance-first", "2020-01-01"),
      event("relevance-second", "2025-01-01"),
    ];

    expect(sortSearchEvents(events, "relevance")).toBe(events);
    expect(sortSearchEvents(events, "relevance").map((item) => item.id)).toEqual([
      "relevance-first",
      "relevance-second",
    ]);
  });

  it("sorts newest event time first and keeps undated events last", () => {
    const events = [
      event("old", "2020-01-01"),
      event("missing", null),
      event("new", "2025-01-01"),
    ];

    expect(sortSearchEvents(events, "time").map((item) => item.id)).toEqual([
      "new",
      "old",
      "missing",
    ]);
  });

  it("preserves relevance order when event times tie", () => {
    const events = [event("first", "2025-01-01"), event("second", "2025-01-01")];

    expect(sortSearchEvents(events, "time").map((item) => item.id)).toEqual([
      "first",
      "second",
    ]);
  });
});
