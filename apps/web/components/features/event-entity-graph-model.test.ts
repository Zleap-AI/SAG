import { describe, expect, it } from "vitest";

import { sliceEventEntityGraph } from "./event-entity-graph-model";
import type { SourceGraphResponse } from "../../lib/types";

function graphFixture(): SourceGraphResponse {
  return {
    documents: [],
    events: [
      {
        id: "event-linked",
        document_id: null,
        title: "关联事件",
        summary: "",
        category: "",
        rank: 0,
        parent_id: null,
        chunk_id: null,
        start_time: null,
      },
      {
        id: "event-isolated",
        document_id: null,
        title: "孤立事件",
        summary: "",
        category: "",
        rank: 0,
        parent_id: null,
        chunk_id: null,
        start_time: null,
      },
    ],
    entities: [
      { id: "entity-linked", name: "关联实体", type: "concept", description: "", heat: 1 },
      { id: "entity-isolated", name: "孤立实体", type: "concept", description: "", heat: 0 },
    ],
    relations: [
      {
        source_id: "entity-linked",
        source_kind: "entity",
        target_id: "event-linked",
        target_kind: "event",
        kind: "mentions",
        weight: 1,
        description: "",
      },
      {
        source_id: "event-linked",
        source_kind: "event",
        target_id: "entity-linked",
        target_kind: "entity",
        kind: "mentions",
        weight: 1,
        description: "重复方向",
      },
    ],
    counts: {
      documents: 0,
      events: 2,
      entities: 2,
      shown_documents: 0,
      shown_events: 2,
      shown_entities: 2,
      shown_relations: 2,
    },
    truncated: false,
  };
}

describe("sliceEventEntityGraph", () => {
  it("normalizes relation direction, deduplicates edges, and preserves all graph nodes", () => {
    const slice = sliceEventEntityGraph(graphFixture());

    expect(slice.relations).toEqual([
      {
        id: "mention:event-linked:entity-linked",
        eventId: "event-linked",
        entityId: "entity-linked",
      },
    ]);
    expect(slice.events.map((event) => event.id)).toEqual(["event-linked", "event-isolated"]);
    expect(slice.entities.map((entity) => entity.id)).toEqual([
      "entity-linked",
      "entity-isolated",
    ]);
  });

  it("preserves every returned event when only a few events have entity relations", () => {
    const graph = graphFixture();
    graph.events = Array.from({ length: 73 }, (_, index) => ({
      ...graph.events[index === 0 ? 0 : 1],
      id: `event-${index + 1}`,
      title: `事件 ${index + 1}`,
    }));
    graph.relations = [
      {
        source_id: "event-1",
        source_kind: "event",
        target_id: "entity-linked",
        target_kind: "entity",
        kind: "mentions",
        weight: 1,
        description: "",
      },
      {
        source_id: "event-2",
        source_kind: "event",
        target_id: "entity-linked",
        target_kind: "entity",
        kind: "mentions",
        weight: 1,
        description: "",
      },
      {
        source_id: "event-3",
        source_kind: "event",
        target_id: "entity-isolated",
        target_kind: "entity",
        kind: "mentions",
        weight: 1,
        description: "",
      },
    ];
    graph.counts.events = 73;
    graph.counts.shown_events = 73;

    const slice = sliceEventEntityGraph(graph);

    expect(slice.events).toHaveLength(73);
    expect(slice.events.map((event) => event.id)).toEqual(
      Array.from({ length: 73 }, (_, index) => `event-${index + 1}`),
    );
    expect(slice.relations).toHaveLength(3);
  });
});
