import { describe, expect, it } from "vitest";

import {
  universeActivationFromEventBundles,
  universeEventBundlesFromActivation,
} from "./universe-event-bundle-adapter";
import type { UniverseActivation } from "./types";

function activation(): UniverseActivation {
  return {
    epoch: 4,
    origin: "assistant",
    query: "why",
    nodes: [
      {
        id: "event-1",
        kind: "event",
        source_id: "source-1",
        label: "Event",
      },
      {
        id: "person-1",
        kind: "entity",
        source_id: "source-1",
        label: "Person",
        category: "person",
      },
      {
        id: "place-1",
        kind: "entity",
        source_id: "source-1",
        label: "Place",
        category: "location",
      },
    ],
    relations: [
      {
        source_id: "source-1",
        from_id: "event-1",
        to_id: "person-1",
        kind: "mentions",
        weight: 1,
        description: "",
      },
      {
        source_id: "source-1",
        from_id: "event-1",
        to_id: "place-1",
        kind: "mentions",
        weight: 1,
        description: "",
      },
    ],
  };
}

describe("universe event bundle adapter", () => {
  it("groups one event with its entities and relations atomically", () => {
    const [bundle] = universeEventBundlesFromActivation(activation());
    expect(bundle.event.id).toBe("event-1");
    expect(bundle.entities.map((entity) => entity.id)).toEqual([
      "person-1",
      "place-1",
    ]);
    expect(bundle.relations).toHaveLength(2);
  });

  it("removes filtered entities and their relations together", () => {
    const [bundle] = universeEventBundlesFromActivation(activation(), {
      entityTypes: ["person"],
    });
    expect(bundle.entities.map((entity) => entity.id)).toEqual(["person-1"]);
    expect(bundle.relations.map((relation) => relation.to_id)).toEqual([
      "person-1",
    ]);
  });

  it("round-trips the window projection without duplicate identities", () => {
    const bundles = [
      ...universeEventBundlesFromActivation(activation()),
      ...universeEventBundlesFromActivation(activation()),
    ];
    const projected = universeActivationFromEventBundles({
      bundles,
      epoch: 9,
      query: "result",
      origin: "assistant",
    });
    expect(projected.nodes).toHaveLength(3);
    expect(projected.relations).toHaveLength(2);
  });
});
