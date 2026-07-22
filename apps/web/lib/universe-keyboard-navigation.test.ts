import { describe, expect, it } from "vitest";

import {
  nextUniverseKeyboardNodeId,
  orderUniverseKeyboardCandidates,
} from "./universe-keyboard-navigation";

describe("universe keyboard navigation", () => {
  const candidates = [
    { id: "entity-b", sourceId: "b", kind: "entity" as const, root: false, importance: 1 },
    { id: "event-low", sourceId: "a", kind: "event" as const, root: false, importance: 0.2 },
    { id: "source-b", sourceId: "b", kind: "source" as const, root: true, importance: 0.8 },
    { id: "event-root", sourceId: "a", kind: "event" as const, root: true, importance: 0.1 },
    { id: "source-a", sourceId: "a", kind: "source" as const, root: true, importance: 0.4 },
  ];

  it("orders a copy deterministically without mutating graph order", () => {
    const originalOrder = candidates.map((candidate) => candidate.id);
    const ordered = orderUniverseKeyboardCandidates(candidates);

    expect(ordered.map((candidate) => candidate.id)).toEqual([
      "source-a",
      "source-b",
      "event-root",
      "event-low",
      "entity-b",
    ]);
    expect(candidates.map((candidate) => candidate.id)).toEqual(originalOrder);
  });

  it("enters, wraps, and reverses within the bounded list", () => {
    const ids = ["a", "b", "c"];

    expect(nextUniverseKeyboardNodeId(ids, null, 1)).toBe("a");
    expect(nextUniverseKeyboardNodeId(ids, null, -1)).toBe("c");
    expect(nextUniverseKeyboardNodeId(ids, "c", 1)).toBe("a");
    expect(nextUniverseKeyboardNodeId(ids, "a", -1)).toBe("c");
    expect(nextUniverseKeyboardNodeId([], "a", 1)).toBeNull();
  });
});
