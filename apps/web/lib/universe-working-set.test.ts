import { describe, expect, it } from "vitest";

import type { UniverseActivation } from "./types";
import {
  admitUniverseBundle,
  admitUniverseBundles,
  emptyUniverseWorkingSet,
  mergeUniverseWorkingSetActivation,
  replaceUniverseWorkingSet,
  setUniversePinnedKeys,
  setUniversePinnedNetwork,
  trimUniverseWorkingSet,
  universeAnchorProgress,
  universeNodeKey,
  universeRelationKey,
  type UniverseAdmissionBundle,
  type UniverseWorkingSet,
  UNIVERSE_RESIDENT_BUDGET,
  UNIVERSE_SCENE_BUDGET,
} from "./universe-working-set";

function activation(epoch: number, prefix: string, count = 4): UniverseActivation {
  const events = Array.from({ length: count }, (_, index) => ({
    id: `${prefix}-event-${index}`,
    kind: "event" as const,
    source_id: "source-a",
    label: `事件 ${index}`,
  }));
  const entities = Array.from({ length: count }, (_, index) => ({
    id: `${prefix}-entity-${index}`,
    kind: "entity" as const,
    source_id: "source-a",
    label: `实体 ${index}`,
  }));
  return {
    epoch,
    query: prefix,
    nodes: [...events, ...entities],
    relations: events.map((event, index) => ({
      source_id: "source-a",
      from_id: event.id,
      to_id: entities[index].id,
      kind: "mentions" as const,
      weight: 1,
      description: "",
    })),
  };
}

function eventBundle(
  id: string,
  eventId: string,
  entityId?: string,
  includeEntity = true,
): UniverseAdmissionBundle {
  return {
    id,
    epoch: 12,
    source_id: "source-a",
    nodes: [
      { id: eventId, kind: "event", source_id: "source-a", label: eventId },
      ...(entityId && includeEntity
        ? [{ id: entityId, kind: "entity" as const, source_id: "source-a", label: entityId }]
        : []),
    ],
    relations: entityId
      ? [{
          source_id: "source-a",
          from_id: eventId,
          to_id: entityId,
          kind: "mentions",
          weight: 1,
          description: "",
        }]
      : [],
  };
}

function denseEventBundle(id: string, index: number): UniverseAdmissionBundle {
  const eventId = `dense-event-${index}`;
  const entityIds = Array.from(
    { length: 8 },
    (_, entityIndex) => `dense-entity-${index}-${entityIndex}`,
  );
  return {
    id,
    epoch: 12,
    source_id: "source-a",
    nodes: [
      { id: eventId, kind: "event", source_id: "source-a", label: eventId },
      ...entityIds.map((entityId) => ({
        id: entityId,
        kind: "entity" as const,
        source_id: "source-a",
        label: entityId,
      })),
    ],
    relations: entityIds.map((entityId) => ({
      source_id: "source-a",
      from_id: eventId,
      to_id: entityId,
      kind: "mentions" as const,
      weight: 1,
      description: "",
    })),
  };
}

function expectBundleInvariants(
  current: UniverseWorkingSet,
  budget: { nodes: number; edges: number },
) {
  expect(current.nodes.length).toBeLessThanOrEqual(budget.nodes);
  expect(current.relations.length).toBeLessThanOrEqual(budget.edges);
  const nodeKeys = new Set(current.nodes.map((node) =>
    universeNodeKey(node.kind, node.id, node.source_id)));
  const relationKeys = new Set(current.relations.map((relation) =>
    universeRelationKey(relation)));
  const bundleIds = new Set(current.bundle_order);

  expect(Object.keys(current.node_owners).sort()).toEqual([...nodeKeys].sort());
  expect(Object.keys(current.relation_owners).sort()).toEqual([...relationKeys].sort());
  current.relations.forEach((relation) => {
    expect(nodeKeys.has(universeNodeKey("event", relation.from_id, relation.source_id))).toBe(true);
    expect(nodeKeys.has(universeNodeKey(
      relation.kind === "subevent" ? "event" : "entity",
      relation.to_id,
      relation.source_id,
    ))).toBe(true);
  });
  Object.entries(current.node_owners).forEach(([key, owners]) => {
    expect(nodeKeys.has(key)).toBe(true);
    expect(owners.length).toBeGreaterThan(0);
    expect(new Set(owners).size).toBe(owners.length);
    owners.forEach((owner) => expect(bundleIds.has(owner)).toBe(true));
  });
  Object.entries(current.relation_owners).forEach(([key, owners]) => {
    expect(relationKeys.has(key)).toBe(true);
    expect(owners.length).toBeGreaterThan(0);
    expect(new Set(owners).size).toBe(owners.length);
    owners.forEach((owner) => expect(bundleIds.has(owner)).toBe(true));
  });
  expect(new Set(current.pinned_keys).size).toBe(current.pinned_keys.length);
  expect(new Set(current.pinned_relation_keys).size).toBe(
    current.pinned_relation_keys.length,
  );
  current.pinned_keys.forEach((key) => expect(nodeKeys.has(key)).toBe(true));
  current.pinned_relation_keys.forEach((key) =>
    expect(relationKeys.has(key)).toBe(true));
}

describe("universe working set", () => {
  it("uses bounded production fallbacks when the manifest policy is unavailable", () => {
    expect(UNIVERSE_SCENE_BUDGET).toEqual({
      desktop: { nodes: 700, edges: 1_000 },
      mobile: { nodes: 520, edges: 720 },
    });
    expect(UNIVERSE_RESIDENT_BUDGET).toEqual({
      desktop: { nodes: 10_000, edges: 12_000 },
      mobile: { nodes: 10_000, edges: 12_000 },
    });
  });

  it("holds dense desktop and mobile cache capacities in resident memory only", () => {
    const cases = [
      {
        count: 96,
        budget: UNIVERSE_RESIDENT_BUDGET.desktop,
        expectedNodes: 864,
        expectedEdges: 768,
      },
      {
        count: 36,
        budget: UNIVERSE_RESIDENT_BUDGET.mobile,
        expectedNodes: 324,
        expectedEdges: 288,
      },
    ];
    cases.forEach(({ count, budget, expectedNodes, expectedEdges }) => {
      let current = emptyUniverseWorkingSet(12);
      for (let index = 0; index < count; index += 1) {
        const admission = admitUniverseBundle(
          current,
          denseEventBundle(`dense-bundle-${index}`, index),
          budget,
          index + 1,
          { roots: true },
        );
        expect(admission.accepted).toBe(true);
        expect(admission.evictedBundleIds).toEqual([]);
        current = admission.workingSet;
      }
      expect(current.bundle_order).toHaveLength(count);
      expect(current.nodes).toHaveLength(expectedNodes);
      expect(current.relations).toHaveLength(expectedEdges);
      expectBundleInvariants(current, budget);
    });
  });

  it("replaces results with complete event-bundle ownership", () => {
    const first = replaceUniverseWorkingSet(activation(1, "first"), {
      nodes: 20,
      edges: 20,
    });
    const second = replaceUniverseWorkingSet(activation(2, "second"), {
      nodes: 20,
      edges: 20,
    });

    expect(first.nodes.some((node) => node.id.startsWith("first"))).toBe(true);
    expect(second.nodes.every((node) => node.id.startsWith("second"))).toBe(true);
    expect(second.bundle_order).toHaveLength(4);
    expect(second.epoch).toBe(2);
    expectBundleInvariants(second, { nodes: 20, edges: 20 });
  });

  it("accumulates multi-turn answer evidence with stable identity and epoch", () => {
    const budget = { nodes: 20, edges: 20 };
    const first = replaceUniverseWorkingSet({
      epoch: 9,
      origin: "assistant",
      query: "第一轮",
      nodes: [
        { id: "event-a", kind: "event", source_id: "source-a", label: "事件 A" },
        { id: "shared", kind: "entity", source_id: "source-a", label: "共享线索" },
      ],
      relations: [{
        source_id: "source-a",
        from_id: "event-a",
        to_id: "shared",
        kind: "mentions",
        weight: 1,
        description: "",
      }],
    }, budget, 1);
    const accumulated = mergeUniverseWorkingSetActivation(first, {
      epoch: 12,
      origin: "assistant",
      query: "第二轮",
      nodes: [
        { id: "event-b", kind: "event", source_id: "source-a", label: "事件 B" },
        { id: "shared", kind: "entity", source_id: "source-a", label: "共享线索（更新）" },
      ],
      relations: [{
        source_id: "source-a",
        from_id: "event-b",
        to_id: "shared",
        kind: "mentions",
        weight: 0.9,
        description: "",
      }],
    }, budget, 2);

    expect(accumulated.epoch).toBe(9);
    expect(accumulated.nodes.filter((node) => node.id === "shared")).toHaveLength(1);
    expect(accumulated.nodes.find((node) => node.id === "shared")?.label)
      .toBe("共享线索（更新）");
    expect(accumulated.nodes.filter((node) => node.kind === "event").map((node) => node.id))
      .toEqual(["event-a", "event-b"]);
    expect(accumulated.relations).toHaveLength(2);
    expectBundleInvariants(accumulated, budget);
  });

  it("keeps card exploration evidence when the next answer turn arrives", () => {
    const budget = { nodes: 20, edges: 20 };
    const firstAnswer = replaceUniverseWorkingSet({
      epoch: 21,
      origin: "assistant",
      query: "第一轮",
      nodes: [
        { id: "event-a", kind: "event", source_id: "source-a", label: "事件 A" },
        { id: "entity-a", kind: "entity", source_id: "source-a", label: "实体 A" },
      ],
      relations: [{
        source_id: "source-a",
        from_id: "event-a",
        to_id: "entity-a",
        kind: "mentions",
        weight: 1,
        description: "",
      }],
    }, budget, 1);
    const explored = admitUniverseBundle(firstAnswer, {
      id: "explore-more:event-a:1",
      epoch: firstAnswer.epoch,
      origin: "expansion",
      anchor_key: universeNodeKey("event", "event-a", "source-a"),
      nodes: [
        { id: "event-expanded", kind: "event", source_id: "source-a", label: "扩展事件" },
        { id: "entity-a", kind: "entity", source_id: "source-a", label: "实体 A" },
      ],
      relations: [{
        source_id: "source-a",
        from_id: "event-expanded",
        to_id: "entity-a",
        kind: "mentions",
        weight: 0.8,
        description: "",
      }],
    }, budget, 2).workingSet;
    const nextAnswer = mergeUniverseWorkingSetActivation(explored, {
      epoch: 24,
      origin: "assistant",
      query: "第二轮",
      nodes: [
        { id: "event-b", kind: "event", source_id: "source-a", label: "事件 B" },
        { id: "entity-a", kind: "entity", source_id: "source-a", label: "实体 A" },
      ],
      relations: [{
        source_id: "source-a",
        from_id: "event-b",
        to_id: "entity-a",
        kind: "mentions",
        weight: 0.9,
        description: "",
      }],
    }, budget, 3);

    expect(nextAnswer.nodes.filter((node) => node.kind === "event").map((node) => node.id))
      .toEqual(["event-a", "event-expanded", "event-b"]);
    expect(nextAnswer.nodes.filter((node) => node.id === "entity-a")).toHaveLength(1);
    expect(nextAnswer.relations).toHaveLength(3);
    expectBundleInvariants(nextAnswer, budget);
  });

  it("trims replacement results only at event-bundle boundaries", () => {
    const current = replaceUniverseWorkingSet(activation(3, "bounded"), {
      nodes: 4,
      edges: 4,
    });

    expect(current.nodes.map((node) => node.id)).toEqual([
      "bounded-event-2",
      "bounded-event-3",
      "bounded-entity-2",
      "bounded-entity-3",
    ]);
    expect(current.relations).toHaveLength(2);
    expect(current.bundle_order).toHaveLength(2);
    expectBundleInvariants(current, { nodes: 4, edges: 4 });
  });

  it("rejects an oversized replacement bundle instead of slicing it", () => {
    const current = replaceUniverseWorkingSet({
      epoch: 4,
      query: "oversized",
      nodes: [
        { id: "event", kind: "event", source_id: "source-a", label: "event" },
        ...["a", "b", "c"].map((id) => ({
          id,
          kind: "entity" as const,
          source_id: "source-a",
          label: id,
        })),
      ],
      relations: ["a", "b", "c"].map((id) => ({
        source_id: "source-a",
        from_id: "event",
        to_id: id,
        kind: "mentions" as const,
        weight: 1,
        description: "",
      })),
    }, { nodes: 3, edges: 3 });

    expect(current.nodes).toEqual([]);
    expect(current.relations).toEqual([]);
    expect(current.bundle_order).toEqual([]);
  });

  it("keeps identical raw ids isolated by source and drops dangling input relations", () => {
    const current = replaceUniverseWorkingSet({
      epoch: 5,
      query: "multi-source",
      nodes: ["source-a", "source-b"].flatMap((sourceId) => [
        { id: "same-event", kind: "event" as const, source_id: sourceId, label: sourceId },
        { id: "same-entity", kind: "entity" as const, source_id: sourceId, label: sourceId },
      ]),
      relations: [
        ...["source-a", "source-b"].map((sourceId) => ({
          source_id: sourceId,
          from_id: "same-event",
          to_id: "same-entity",
          kind: "mentions" as const,
          weight: 1,
          description: "",
        })),
        {
          source_id: "source-a",
          from_id: "same-event",
          to_id: "missing",
          kind: "mentions" as const,
          weight: 1,
          description: "",
        },
      ],
    }, { nodes: 10, edges: 10 });

    expect(current.nodes).toHaveLength(4);
    expect(current.relations).toHaveLength(2);
    expectBundleInvariants(current, { nodes: 10, edges: 10 });
  });

  it("counts committed neighbors by source and anchor kind", () => {
    const current = replaceUniverseWorkingSet(activation(6, "count", 3), {
      nodes: 12,
      edges: 12,
    });

    expect(universeAnchorProgress(current, "event", "count-event-1", "source-a")).toBe(1);
    expect(universeAnchorProgress(current, "entity", "count-entity-1", "source-a")).toBe(1);
    expect(universeAnchorProgress(current, "entity", "count-entity-1", "source-b")).toBe(0);
  });
});

describe("transactional universe bundle admission", () => {
  it("deduplicates an expanded network when a later timeline bundle owns it", () => {
    const budget = { nodes: 8, edges: 8 };
    const expanded = eventBundle("expansion:entity-1", "event-1", "entity-1");
    const timeline = eventBundle("timeline:event-1", "event-1", "entity-1");
    const first = admitUniverseBundle(
      emptyUniverseWorkingSet(12),
      expanded,
      budget,
      1,
    );
    const second = admitUniverseBundle(first.workingSet, timeline, budget, 2);

    expect(second.accepted).toBe(true);
    expect(second.workingSet.nodes.map((node) => node.id).sort()).toEqual([
      "entity-1",
      "event-1",
    ]);
    expect(second.workingSet.relations).toHaveLength(1);
    expect(second.workingSet.node_owners["source-a:event:event-1"]).toEqual([
      "expansion:entity-1",
      "timeline:event-1",
    ]);
    expect(second.workingSet.node_owners["source-a:entity:entity-1"]).toEqual([
      "expansion:entity-1",
      "timeline:event-1",
    ]);
    expect(second.workingSet.relation_owners[
      "source-a:mentions:event-1:entity-1"
    ]).toEqual([
      "expansion:entity-1",
      "timeline:event-1",
    ]);
    expectBundleInvariants(second.workingSet, budget);
  });

  it("keeps a shared entity resident when its oldest owner bundle is evicted", () => {
    const budget = { nodes: 4, edges: 4 };
    let current = admitUniverseBundle(
      emptyUniverseWorkingSet(12),
      eventBundle("bundle-1", "event-1", "shared-entity"),
      budget,
      1,
    ).workingSet;
    const second = admitUniverseBundle(
      current,
      eventBundle("bundle-2", "event-2", "shared-entity", false),
      budget,
      2,
    );
    expect(second.accepted).toBe(true);
    current = second.workingSet;
    expect(current.node_owners["source-a:entity:shared-entity"]).toEqual([
      "bundle-1",
      "bundle-2",
    ]);

    const third = admitUniverseBundle(
      current,
      eventBundle("bundle-3", "event-3", "entity-3"),
      budget,
      3,
    );
    expect(third.accepted).toBe(true);
    expect(third.evictedBundleIds).toEqual(["bundle-1"]);
    expect(third.workingSet.nodes.some((node) => node.id === "event-1")).toBe(false);
    expect(third.workingSet.nodes.some((node) => node.id === "shared-entity")).toBe(true);
    expect(third.workingSet.relations.some((relation) =>
      relation.from_id === "event-2" && relation.to_id === "shared-entity")).toBe(true);
    expect(third.workingSet.node_owners["source-a:entity:shared-entity"]).toEqual([
      "bundle-2",
    ]);
    expectBundleInvariants(third.workingSet, budget);
  });

  it("rejects an incomplete or oversized bundle without committing a partial graph", () => {
    const budget = { nodes: 2, edges: 2 };
    const initial = admitUniverseBundle(
      emptyUniverseWorkingSet(12),
      eventBundle("base", "base-event"),
      budget,
      1,
    ).workingSet;
    const incomplete = admitUniverseBundle(
      initial,
      eventBundle("incomplete", "event-2", "missing-entity", false),
      budget,
      2,
    );
    expect(incomplete.accepted).toBe(false);
    expect(incomplete.reason).toBe("invalid_bundle");
    expect(incomplete.workingSet).toBe(initial);

    const oversized = admitUniverseBundle(initial, {
      id: "oversized",
      epoch: 12,
      source_id: "source-a",
      nodes: ["a", "b", "c"].map((id) => ({
        id,
        kind: "entity" as const,
        source_id: "source-a",
        label: id,
      })),
      relations: [],
    }, budget, 3);
    expect(oversized.accepted).toBe(false);
    expect(oversized.reason).toBe("over_budget");
    expect(oversized.workingSet).toBe(initial);
  });

  it("evicts deterministically around transient protection and persistent pins", () => {
    const budget = { nodes: 2, edges: 0 };
    let current = emptyUniverseWorkingSet(12);
    for (const id of ["a", "b"]) {
      current = admitUniverseBundle(
        current,
        eventBundle(id, `event-${id}`),
        budget,
        1,
      ).workingSet;
    }
    const protectedAdmission = admitUniverseBundle(
      current,
      eventBundle("c", "event-c"),
      budget,
      2,
      { protectedKeys: ["source-a:event:event-a"] },
    );
    expect(protectedAdmission.accepted).toBe(true);
    expect(protectedAdmission.evictedBundleIds).toEqual(["b"]);
    expect(protectedAdmission.workingSet.bundle_order).toEqual(["a", "c"]);

    current = setUniversePinnedKeys(protectedAdmission.workingSet, [
      "source-a:event:event-a",
      "source-a:event:event-c",
    ]);
    const blocked = admitUniverseBundle(
      current,
      eventBundle("d", "event-d"),
      budget,
      3,
    );
    expect(blocked.accepted).toBe(false);
    expect(blocked.reason).toBe("protected_capacity");
    expect(blocked.workingSet).toBe(current);
  });

  it("protects complete cache bundles even when their nodes have other owners", () => {
    const budget = { nodes: 2, edges: 0 };
    let current = admitUniverseBundle(
      emptyUniverseWorkingSet(12),
      {
        id: "cached-timeline",
        origin: "timeline",
        epoch: 12,
        source_id: "source-a",
        nodes: [{
          id: "shared",
          kind: "entity",
          source_id: "source-a",
          label: "shared",
        }],
        relations: [],
      },
      budget,
      1,
    ).workingSet;
    current = admitUniverseBundle(
      current,
      {
        id: "other-owner",
        origin: "activation",
        epoch: 12,
        source_id: "source-a",
        nodes: [
          {
            id: "shared",
            kind: "entity",
            source_id: "source-a",
            label: "shared",
          },
          {
            id: "old-event",
            kind: "event",
            source_id: "source-a",
            label: "old-event",
          },
        ],
        relations: [],
      },
      budget,
      2,
    ).workingSet;

    const admitted = admitUniverseBundle(
      current,
      {
        ...eventBundle("new-timeline", "new-event"),
        origin: "timeline",
      },
      budget,
      3,
      { protectedBundleIds: ["cached-timeline"] },
    );

    expect(admitted.accepted).toBe(true);
    expect(admitted.evictedBundleIds).toEqual(["other-owner"]);
    expect(admitted.workingSet.bundle_order).toEqual([
      "cached-timeline",
      "new-timeline",
    ]);
    expect(admitted.workingSet.nodes.map((node) => node.id).sort()).toEqual([
      "new-event",
      "shared",
    ]);
    expectBundleInvariants(admitted.workingSet, budget);
  });

  it("releases expansion support before older timeline history", () => {
    const budget = { nodes: 2, edges: 0 };
    let current = admitUniverseBundle(
      emptyUniverseWorkingSet(12),
      {
        ...eventBundle("timeline-old", "event-old"),
        origin: "timeline",
      },
      budget,
      1,
    ).workingSet;
    current = admitUniverseBundle(
      current,
      {
        ...eventBundle("expansion-newer", "event-expanded"),
        origin: "expansion",
        anchor_key: "source-a:entity:anchor",
        lineage_root_key: "source-a:event:timeline-root",
        request_cursor: null,
        next_cursor: null,
      },
      budget,
      2,
    ).workingSet;

    const admitted = admitUniverseBundle(
      current,
      {
        ...eventBundle("timeline-new", "event-new"),
        origin: "timeline",
      },
      budget,
      3,
    );

    expect(admitted.accepted).toBe(true);
    expect(admitted.evictedBundleIds).toEqual(["expansion-newer"]);
    expect(admitted.workingSet.bundle_order).toEqual([
      "timeline-old",
      "timeline-new",
    ]);
    expect(admitted.workingSet.bundles["timeline-old"].origin).toBe("timeline");
    expectBundleInvariants(admitted.workingSet, budget);
  });

  it("protects a locked factual edge until its one-hop network is unpinned", () => {
    const budget = { nodes: 4, edges: 1 };
    const lockedBundle = eventBundle(
      "locked-fact",
      "locked-event",
      "shared-entity",
    );
    const lockedRelationKey = universeRelationKey(lockedBundle.relations[0]);
    let current = admitUniverseBundle(
      emptyUniverseWorkingSet(12),
      lockedBundle,
      budget,
      1,
      {
        pinnedRelationKeys: [lockedRelationKey, lockedRelationKey, "not-resident"],
      },
    ).workingSet;
    expect(current.pinned_relation_keys).toEqual([lockedRelationKey]);
    current = setUniversePinnedNetwork(current, [], []);

    current = admitUniverseBundle(current, {
      id: "event-owner",
      epoch: 12,
      nodes: [{
        id: "locked-event",
        kind: "event",
        source_id: "source-a",
        label: "locked-event",
      }],
      relations: [],
    }, budget, 2).workingSet;
    current = admitUniverseBundle(current, {
      id: "entity-owner",
      epoch: 12,
      nodes: [{
        id: "shared-entity",
        kind: "entity",
        source_id: "source-a",
        label: "shared-entity",
      }],
      relations: [],
    }, budget, 3).workingSet;

    const replacement = eventBundle("replacement", "new-event", "new-entity");
    const transientlyBlocked = admitUniverseBundle(
      current,
      replacement,
      budget,
      4,
      { protectedRelationKeys: [lockedRelationKey] },
    );
    expect(transientlyBlocked.accepted).toBe(false);
    expect(transientlyBlocked.reason).toBe("protected_capacity");
    expect(transientlyBlocked.workingSet).toBe(current);
    expectBundleInvariants(current, budget);

    const eventKey = universeNodeKey("event", "locked-event", "source-a");
    const entityKey = universeNodeKey("entity", "shared-entity", "source-a");
    current = setUniversePinnedNetwork(
      current,
      [eventKey, entityKey, eventKey, "not-resident"],
      [lockedRelationKey, lockedRelationKey, "not-resident"],
    );
    expect(current.pinned_keys).toEqual([eventKey, entityKey]);
    expect(current.pinned_relation_keys).toEqual([lockedRelationKey]);
    expect(setUniversePinnedNetwork(
      current,
      [eventKey, entityKey, eventKey],
      [lockedRelationKey, lockedRelationKey],
    )).toBe(current);
    current = setUniversePinnedKeys(current, [entityKey, entityKey]);
    expect(current.pinned_keys).toEqual([entityKey]);
    expect(current.pinned_relation_keys).toEqual([lockedRelationKey]);

    const persistentlyBlocked = admitUniverseBundle(
      current,
      replacement,
      budget,
      5,
    );
    expect(persistentlyBlocked.accepted).toBe(false);
    expect(persistentlyBlocked.reason).toBe("protected_capacity");
    expect(persistentlyBlocked.workingSet).toBe(current);

    current = setUniversePinnedNetwork(current, [], []);
    const admitted = admitUniverseBundle(current, replacement, budget, 6);
    expect(admitted.accepted).toBe(true);
    expect(admitted.evictedBundleIds).toEqual(["locked-fact"]);
    expect(admitted.workingSet.nodes.some((node) => node.id === "locked-event")).toBe(true);
    expect(admitted.workingSet.nodes.some((node) => node.id === "shared-entity")).toBe(true);
    expect(admitted.workingSet.relations.some((relation) =>
      universeRelationKey(relation) === lockedRelationKey)).toBe(false);
    expect(admitted.workingSet.pinned_keys).toEqual([]);
    expect(admitted.workingSet.pinned_relation_keys).toEqual([]);
    expectBundleInvariants(admitted.workingSet, budget);
  });

  it("keeps an already-clear pin set referentially stable", () => {
    const current = emptyUniverseWorkingSet(20);
    expect(setUniversePinnedNetwork(current, [], [])).toBe(current);
  });

  it("acknowledges exact idempotent retries and rejects changed payloads for the same id", () => {
    const budget = { nodes: 2, edges: 1 };
    const bundle = eventBundle("bundle-1", "event-1", "entity-1");
    const first = admitUniverseBundle(emptyUniverseWorkingSet(12), bundle, budget, 1);
    const retry = admitUniverseBundle(first.workingSet, bundle, budget, 2);
    expect(retry.accepted).toBe(true);
    expect(retry.committed).toBe(false);
    expect(retry.workingSet).toBe(first.workingSet);

    const changed = eventBundle("bundle-1", "event-1", "entity-1");
    changed.nodes[0] = { ...changed.nodes[0], label: "changed payload" };
    const collision = admitUniverseBundle(first.workingSet, changed, budget, 3);
    expect(collision.accepted).toBe(false);
    expect(collision.reason).toBe("duplicate_bundle");
    expect(collision.workingSet).toBe(first.workingSet);
  });

  it("stops a page at the first rejected bundle", () => {
    const budget = { nodes: 2, edges: 0 };
    const batch = admitUniverseBundles(
      emptyUniverseWorkingSet(12),
      [
        eventBundle("bundle-1", "event-1"),
        {
          id: "oversized",
          epoch: 12,
          source_id: "source-a",
          nodes: ["a", "b", "c"].map((id) => ({
            id,
            kind: "entity" as const,
            source_id: "source-a",
            label: id,
          })),
          relations: [],
        },
        eventBundle("never-attempted", "event-3"),
      ],
      budget,
      3,
    );
    expect(batch.acknowledgedBundleIds).toEqual(["bundle-1"]);
    expect(batch.committedBundleIds).toEqual(["bundle-1"]);
    expect(batch.rejectedBundleId).toBe("oversized");
    expect(batch.reason).toBe("over_budget");
    expect(batch.workingSet.bundle_order).toEqual(["bundle-1"]);
  });

  it("rejects a late bundle from an older epoch", () => {
    const current = emptyUniverseWorkingSet(12);
    const late = admitUniverseBundle(
      current,
      { ...eventBundle("late", "event-late"), epoch: 11 },
      { nodes: 2, edges: 0 },
    );
    expect(late.accepted).toBe(false);
    expect(late.reason).toBe("epoch_mismatch");
    expect(late.workingSet).toBe(current);
  });

  it("preserves hard budgets, ownership, and relation endpoints over a long FIFO sequence", () => {
    const budget = { nodes: 12, edges: 8 };
    const run = () => {
      let current = emptyUniverseWorkingSet(12);
      for (let index = 0; index < 300; index += 1) {
        const admitted = admitUniverseBundle(
          current,
          eventBundle(`bundle-${index}`, `event-${index}`, `shared-${index % 5}`),
          budget,
          index,
        );
        expect(admitted.accepted).toBe(true);
        current = admitted.workingSet;
        expectBundleInvariants(current, budget);
      }
      return current;
    };

    const first = run();
    const second = run();
    expect(first.node_order).toEqual(second.node_order);
    expect(first.bundle_order).toEqual(second.bundle_order);
    expect(first.node_owners).toEqual(second.node_owners);
    expect(first.relations).toEqual(second.relations);
  });

  it("trims a bundle-aware set only at bundle boundaries", () => {
    let current = emptyUniverseWorkingSet(12);
    current = admitUniverseBundle(
      current,
      eventBundle("old", "old-event", "old-entity"),
      { nodes: 4, edges: 2 },
      1,
    ).workingSet;
    current = admitUniverseBundle(
      current,
      eventBundle("new", "new-event", "new-entity"),
      { nodes: 4, edges: 2 },
      2,
    ).workingSet;

    const trimmed = trimUniverseWorkingSet(current, { nodes: 3, edges: 1 });
    expect(trimmed.bundle_order).toEqual(["new"]);
    expect(trimmed.nodes.map((node) => node.id)).toEqual(["new-event", "new-entity"]);
    expect(trimmed.relations).toHaveLength(1);
    expectBundleInvariants(trimmed, { nodes: 3, edges: 1 });
  });
});
