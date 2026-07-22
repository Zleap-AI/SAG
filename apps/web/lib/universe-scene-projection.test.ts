import { describe, expect, it } from "vitest";

import { projectUniverseAccumulationTopology } from "./universe-scene-projection";

const nodes = [
  { id: "source", kind: "source" as const },
  { id: "event-a", kind: "event" as const },
  { id: "event-b", kind: "event" as const },
  { id: "entity-linked", kind: "entity" as const },
  { id: "entity-reversed", kind: "entity" as const },
  { id: "entity-virtual", kind: "entity" as const },
  { id: "entity-orphan", kind: "entity" as const },
];

const links = [
  { id: "linked", source: "event-a", target: "entity-linked", virtual: false },
  { id: "reversed", source: "entity-reversed", target: "event-b", virtual: false },
  { id: "virtual", source: "event-a", target: "entity-virtual", virtual: true },
  { id: "orphan-chain", source: "entity-orphan", target: "entity-linked", virtual: false },
];

describe("accumulation topology projection", () => {
  it("keeps only entities with a visible factual relation to an event", () => {
    const projection = projectUniverseAccumulationTopology(nodes, links);

    expect(projection.nodes.map((node) => node.id)).toEqual([
      "source",
      "event-a",
      "event-b",
      "entity-linked",
      "entity-reversed",
    ]);
    expect(projection.links.map((link) => link.id)).toEqual(["linked", "reversed"]);
    expect(projection.orphanEntityIds).toEqual(["entity-virtual", "entity-orphan"]);
  });
});
