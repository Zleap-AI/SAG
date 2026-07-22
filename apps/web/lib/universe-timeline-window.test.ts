import { describe, expect, it } from "vitest";

import {
  admitUniverseBundle,
  emptyUniverseWorkingSet,
  setUniversePinnedNetwork,
  universeNodeKey,
  universeRelationKey,
  type UniverseAdmissionBundle,
  type UniverseWorkingSet,
} from "./universe-working-set";
import {
  advanceUniverseTimelineWindow,
  applyUniverseTimelineBundleEvictions,
  appendUniverseTimelineBundles,
  createUniverseTimelineWindow,
  effectiveUniverseTimelineVisibleBundleIds,
  isUniverseTimelineWindowComplete,
  markUniverseTimelineNetworkExhausted,
  projectUniverseBundleWindow,
  projectUniverseBundleWindowWithinBudget,
  protectedUniverseTimelineBundleIds,
  queriedUniverseTimelineEventCount,
  reconfigureUniverseTimelineWindow,
  retainUniverseWorkingSetBundles,
  settleUniverseTimelineWindow,
  shouldPrefetchUniverseTimelineWindow,
  universeTimelinePageBundleLimit,
  universeTimelinePrefetchAheadTarget,
  universeTimelineRewindStartActiveIndex,
  universeTimelineStartActiveIndex,
} from "./universe-timeline-window";

function eventBundle(
  id: string,
  eventId: string,
  entityIds: string[],
  includeEntities = true,
): UniverseAdmissionBundle {
  return {
    id,
    epoch: 7,
    source_id: "source-a",
    nodes: [
      { id: eventId, kind: "event", source_id: "source-a", label: eventId },
      ...(includeEntities ? entityIds.map((entityId) => ({
        id: entityId,
        kind: "entity" as const,
        source_id: "source-a",
        label: entityId,
      })) : []),
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

function workingSetWithSharedEntity() {
  const budget = { nodes: 20, edges: 20 };
  let working = emptyUniverseWorkingSet(7);
  working = admitUniverseBundle(
    working,
    eventBundle("bundle-1", "event-1", ["shared", "entity-1"]),
    budget,
    1,
    { roots: true },
  ).workingSet;
  working = admitUniverseBundle(
    working,
    eventBundle("bundle-2", "event-2", ["shared"], false),
    budget,
    2,
    { roots: true },
  ).workingSet;
  working = admitUniverseBundle(
    working,
    eventBundle("support", "event-support", ["entity-support"]),
    budget,
    3,
  ).workingSet;
  return working;
}

function expectClosedWorkingSet(working: UniverseWorkingSet) {
  const nodeKeys = new Set(working.nodes.map((node) =>
    universeNodeKey(node.kind, node.id, node.source_id)));
  const relationKeys = new Set(working.relations.map(universeRelationKey));
  working.relations.forEach((relation) => {
    expect(nodeKeys.has(universeNodeKey("event", relation.from_id, relation.source_id)))
      .toBe(true);
    expect(nodeKeys.has(universeNodeKey(
      relation.kind === "subevent" ? "event" : "entity",
      relation.to_id,
      relation.source_id,
    ))).toBe(true);
  });
  Object.entries(working.node_owners).forEach(([key, owners]) => {
    expect(nodeKeys.has(key)).toBe(true);
    owners.forEach((owner) => expect(working.bundle_order).toContain(owner));
  });
  Object.entries(working.relation_owners).forEach(([key, owners]) => {
    expect(relationKeys.has(key)).toBe(true);
    owners.forEach((owner) => expect(working.bundle_order).toContain(owner));
  });
}

function protectionForBundles(
  working: UniverseWorkingSet,
  bundleIds: readonly string[],
) {
  const nodeKeys = new Set<string>();
  const relationKeys = new Set<string>();
  bundleIds.forEach((id) => {
    working.bundles[id]?.node_keys.forEach((key) => nodeKeys.add(key));
    working.bundles[id]?.relation_keys.forEach((key) => relationKeys.add(key));
  });
  return { nodeKeys, relationKeys };
}

describe("universe timeline virtual window", () => {
  it("fills the first visible window and keeps later appends off-screen", () => {
    let state = createUniverseTimelineWindow(3, 9);
    expect(state.activeIndex).toBe(-1);
    expect(state.phase).toBe("idle");

    state = appendUniverseTimelineBundles(state, ["a", "b", "b", "c", "d"]);
    expect(state.cacheBundleIds).toEqual(["a", "b", "c", "d"]);
    expect(state.activeIndex).toBe(2);
    expect(state.visibleBundleIds).toEqual(["a", "b", "c"]);
    expect(state.visitedCount).toBe(3);

    const appended = appendUniverseTimelineBundles(state, ["d", "e", "f"]);
    expect(appended.activeIndex).toBe(2);
    expect(appended.visibleBundleIds).toEqual(["a", "b", "c"]);
    expect(appended.visitedCount).toBe(3);
    expect(appendUniverseTimelineBundles(appended, ["e"])).toBe(appended);
  });

  it("supports an explicit one-package stride and revisiting never inflates progress", () => {
    let state = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(3, 12),
      ["a", "b", "c", "d", "e", "f"],
    );
    state = advanceUniverseTimelineWindow(state, "next", 1);
    expect(state.activeIndex).toBe(3);
    expect(state.visibleBundleIds).toEqual(["b", "c", "d"]);
    expect(state.visitedCount).toBe(4);
    expect(state.phase).toBe("transitioning");

    state = settleUniverseTimelineWindow(state);
    expect(state.phase).toBe("idle");
    state = advanceUniverseTimelineWindow(state, "previous", 1);
    expect(state.visibleBundleIds).toEqual(["a", "b", "c"]);
    expect(state.visitedCount).toBe(4);
    expect(advanceUniverseTimelineWindow(state, "previous", 1)).toBe(state);
    state = advanceUniverseTimelineWindow(state, "next", 1);
    expect(state.visitedCount).toBe(4);
  });

  it("atomically exchanges one visual time page while preserving a fixed visible cap", () => {
    const ids = Array.from({ length: 15 }, (_, index) => `bundle-${index}`);
    const initial = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(6, 24),
      ids,
    );
    const initialRevision = initial.revision;
    expect(initial.cacheBundleIds[initial.activeIndex]).toBe("bundle-5");
    expect(initial.visibleBundleIds).toEqual(ids.slice(0, 6));

    const advanced = advanceUniverseTimelineWindow(initial, "next", 6);
    expect(advanced.revision).toBe(initialRevision + 1);
    expect(advanced.cacheBundleIds).toBe(initial.cacheBundleIds);
    expect(advanced.activeIndex).toBe(11);
    expect(advanced.cacheBundleIds[advanced.activeIndex]).toBe("bundle-11");
    expect(advanced.visibleBundleIds).toEqual(ids.slice(6, 12));
    expect(advanced.visibleBundleIds).toHaveLength(6);
    expect(advanced.visitedCount).toBe(12);
    expect(advanced.phase).toBe("transitioning");

    const rewound = advanceUniverseTimelineWindow(advanced, "previous", 6);
    expect(rewound.revision).toBe(advanced.revision + 1);
    expect(rewound.cacheBundleIds).toBe(initial.cacheBundleIds);
    expect(rewound.activeIndex).toBe(5);
    expect(rewound.cacheBundleIds[rewound.activeIndex]).toBe("bundle-5");
    expect(rewound.visibleBundleIds).toEqual(ids.slice(0, 6));
    expect(rewound.visitedCount).toBe(12);
    expect(advanceUniverseTimelineWindow(rewound, "previous", 6)).toBe(rewound);
  });

  it("never skips queried events when the visual window is smaller than a network page", () => {
    const ids = Array.from({ length: 12 }, (_, index) => `bundle-${index}`);
    let state = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(2, 24),
      ids,
    );
    expect(state.visibleBundleIds).toEqual(ids.slice(0, 2));

    for (let page = 1; page < 6; page += 1) {
      state = advanceUniverseTimelineWindow(state, "next", 2);
      expect(state.visibleBundleIds).toEqual(ids.slice(page * 2, page * 2 + 2));
      state = settleUniverseTimelineWindow(state);
    }
  });

  it("clamps a short final page to EOF in one atomic transition", () => {
    const ids = Array.from({ length: 15 }, (_, index) => `bundle-${index}`);
    let state = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(6, 24),
      ids,
    );
    state = advanceUniverseTimelineWindow(state, "next", 6);
    state = markUniverseTimelineNetworkExhausted(state);
    const beforeFinalRevision = state.revision;

    const finalPage = advanceUniverseTimelineWindow(state, "next", 6);
    expect(finalPage.revision).toBe(beforeFinalRevision + 1);
    expect(finalPage.cacheBundleIds).toBe(state.cacheBundleIds);
    expect(finalPage.activeIndex).toBe(14);
    expect(finalPage.cacheBundleIds[finalPage.activeIndex]).toBe("bundle-14");
    expect(finalPage.visibleBundleIds).toEqual(ids.slice(9, 15));
    expect(finalPage.visibleBundleIds).toHaveLength(6);
    expect(finalPage.visitedCount).toBe(15);
    expect(finalPage.phase).toBe("complete");
    expect(advanceUniverseTimelineWindow(finalPage, "next", 6)).toBe(finalPage);

    const previousPage = advanceUniverseTimelineWindow(finalPage, "previous", 6);
    expect(previousPage.activeIndex).toBe(11);
    expect(previousPage.visibleBundleIds).toEqual(ids.slice(6, 12));
    const firstPage = advanceUniverseTimelineWindow(previousPage, "previous", 6);
    expect(firstPage.activeIndex).toBe(5);
    expect(firstPage.visibleBundleIds).toEqual(ids.slice(0, 6));
  });

  it("requires an explicit positive integral page stride", () => {
    const state = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(3, 12),
      ["a", "b", "c", "d"],
    );
    expect(() => advanceUniverseTimelineWindow(state, "next", 0))
      .toThrow("positive integer");
    expect(() => advanceUniverseTimelineWindow(state, "next", 1.5))
      .toThrow("positive integer");
  });

  it("keeps a full window at the shallow boundary and then hands outward travel to the camera", () => {
    const state = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(4, 24),
      Array.from({ length: 12 }, (_, index) => `bundle-${index}`),
    );
    expect(universeTimelineStartActiveIndex(12, 4)).toBe(3);
    expect(state.activeIndex).toBe(3);
    expect(state.visibleBundleIds).toEqual([
      "bundle-0",
      "bundle-1",
      "bundle-2",
      "bundle-3",
    ]);
    expect(advanceUniverseTimelineWindow(state, "previous", 1)).toBe(state);
  });

  it("does not skip packages after increasing the visible limit at the shallow boundary", () => {
    const initial = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(3, 24),
      Array.from({ length: 12 }, (_, index) => `bundle-${index}`),
    );
    const expanded = reconfigureUniverseTimelineWindow(initial, 6, 24);
    expect(expanded.activeIndex).toBe(2);
    expect(advanceUniverseTimelineWindow(expanded, "previous", 1)).toBe(expanded);
    const next = advanceUniverseTimelineWindow(expanded, "next", 1);
    expect(next.activeIndex).toBe(3);
    expect(next.visibleBundleIds).toEqual([
      "bundle-0",
      "bundle-1",
      "bundle-2",
      "bundle-3",
    ]);
  });

  it("keeps the first-page rewind floor stable when prefetch grows a wide window", () => {
    let state = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(18, 96),
      Array.from({ length: 6 }, (_, index) => `bundle-${index}`),
    );
    expect(state.activeIndex).toBe(5);
    expect(state.rewindStartOffset).toBe(5);
    expect(universeTimelineRewindStartActiveIndex(state)).toBe(5);

    state = appendUniverseTimelineBundles(
      state,
      Array.from({ length: 12 }, (_, index) => `bundle-${index + 6}`),
    );
    const cacheIdentity = state.cacheBundleIds;
    expect(state.activeIndex).toBe(5);
    expect(universeTimelineRewindStartActiveIndex(state)).toBe(5);

    const advanced = advanceUniverseTimelineWindow(state, "next", 1);
    expect(advanced.activeIndex).toBe(6);
    const rewound = advanceUniverseTimelineWindow(advanced, "previous", 1);
    expect(rewound.activeIndex).toBe(5);
    expect(rewound.visibleBundleIds).toEqual(
      Array.from({ length: 6 }, (_, index) => `bundle-${index}`),
    );
    expect(rewound.cacheBundleIds).toBe(cacheIdentity);
    expect(advanceUniverseTimelineWindow(rewound, "previous", 1)).toBe(rewound);
  });

  it("translates the absolute rewind floor after FIFO prefix eviction", () => {
    let state = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(3, 12),
      Array.from({ length: 8 }, (_, index) => `bundle-${index}`),
    );
    state = advanceUniverseTimelineWindow(state, "next", 1);
    state = advanceUniverseTimelineWindow(state, "next", 1);
    expect(state.activeIndex).toBe(4);
    expect(state.rewindStartOffset).toBe(2);

    const evicted = applyUniverseTimelineBundleEvictions(
      state,
      ["bundle-0", "bundle-1"],
    );
    expect(evicted).not.toBeNull();
    expect(evicted?.cacheStartOffset).toBe(2);
    expect(evicted?.activeIndex).toBe(2);
    expect(universeTimelineRewindStartActiveIndex(evicted!)).toBe(0);
    const previous = advanceUniverseTimelineWindow(evicted!, "previous", 1);
    expect(previous.activeIndex).toBe(1);
    expect(advanceUniverseTimelineWindow(
      advanceUniverseTimelineWindow(previous, "previous", 1),
      "previous",
      1,
    ).activeIndex).toBe(0);
  });

  it("distinguishes network EOF from reaching the final cached bundle", () => {
    let state = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(2, 8),
      ["a", "b", "c", "d"],
    );
    state = markUniverseTimelineNetworkExhausted(state);
    expect(state.networkExhausted).toBe(true);
    expect(state.phase).toBe("idle");
    expect(isUniverseTimelineWindowComplete(state)).toBe(false);

    state = advanceUniverseTimelineWindow(state, "next", 1);
    expect(state.phase).toBe("transitioning");
    expect(isUniverseTimelineWindowComplete(state)).toBe(false);
    state = advanceUniverseTimelineWindow(state, "next", 1);
    expect(state.activeIndex).toBe(3);
    expect(state.phase).toBe("complete");
    expect(isUniverseTimelineWindowComplete(state)).toBe(true);
    expect(advanceUniverseTimelineWindow(state, "next", 1)).toBe(state);

    state = advanceUniverseTimelineWindow(state, "previous", 1);
    expect(state.phase).toBe("transitioning");
    expect(isUniverseTimelineWindowComplete(state)).toBe(false);
  });

  it("keeps the visible window while trimming a long FIFO cache", () => {
    let state = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(4, 12),
      Array.from({ length: 12 }, (_, index) => `bundle-${index}`),
    );
    for (let index = 0; index < 6; index += 1) {
      state = advanceUniverseTimelineWindow(state, "next", 1);
    }
    expect(state.visibleBundleIds).toEqual([
      "bundle-6",
      "bundle-7",
      "bundle-8",
      "bundle-9",
    ]);
    const visibleBeforeTrim = state.visibleBundleIds;
    state = appendUniverseTimelineBundles(
      state,
      Array.from({ length: 6 }, (_, index) => `bundle-${index + 12}`),
    );
    expect(state.cacheBundleIds).toHaveLength(12);
    expect(state.cacheBundleIds.slice(0, 4)).toEqual(visibleBeforeTrim);
    expect(state.visibleBundleIds).toEqual(visibleBeforeTrim);
    expect(state.activeIndex).toBe(3);
    expect(state.cacheStartOffset).toBe(6);
    expect(state.visitedCount).toBe(10);
    expect(queriedUniverseTimelineEventCount(state)).toBe(18);
  });

  it("temporarily lifts the cache limit instead of dropping unseen admitted bundles", () => {
    let state = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(3, 3),
      ["a", "b", "c", "d", "e", "f"],
    );
    expect(state.cacheBundleIds).toEqual(["a", "b", "c", "d", "e", "f"]);
    expect(state.visibleBundleIds).toEqual(["a", "b", "c"]);
    expect(state.cacheLimit).toBe(6);
    expect(queriedUniverseTimelineEventCount(state)).toBe(6);

    state = advanceUniverseTimelineWindow(state, "next", 1);
    state = advanceUniverseTimelineWindow(state, "next", 1);
    state = advanceUniverseTimelineWindow(state, "next", 1);
    state = reconfigureUniverseTimelineWindow(state, 3, 3);
    expect(state.cacheBundleIds).toEqual(["d", "e", "f"]);
    expect(state.visibleBundleIds).toEqual(["d", "e", "f"]);
    expect(state.cacheLimit).toBe(3);
    expect(queriedUniverseTimelineEventCount(state)).toBe(6);
  });

  it("remains bounded and monotonic across thousands of event packages", () => {
    let state = createUniverseTimelineWindow(6, 18);
    let nextId = 0;
    for (let page = 0; page < 200; page += 1) {
      state = appendUniverseTimelineBundles(
        state,
        Array.from({ length: 6 }, () => `bundle-${nextId++}`),
      );
      while (
        state.activeIndex >= 0
        && state.activeIndex < state.cacheBundleIds.length - 7
      ) {
        state = advanceUniverseTimelineWindow(state, "next", 1);
      }
      expect(state.cacheBundleIds.length).toBeLessThanOrEqual(18);
      expect(state.visibleBundleIds.length).toBeLessThanOrEqual(6);
      expect(new Set(state.cacheBundleIds).size).toBe(state.cacheBundleIds.length);
    }
    expect(state.visitedCount).toBeGreaterThan(1_100);
  });

  it("normalizes invalid limits and computes a bounded effective slice", () => {
    const state = createUniverseTimelineWindow(Number.NaN, 0);
    expect(state.visibleLimit).toBe(1);
    expect(state.cacheLimit).toBe(1);
    expect(effectiveUniverseTimelineVisibleBundleIds(["a", "b"], 99, 1))
      .toEqual(["b"]);
  });

  it("reconfigures around the same active bundle without advancing progress", () => {
    let state = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(4, 12),
      Array.from({ length: 12 }, (_, index) => `bundle-${index}`),
    );
    const activeId = state.cacheBundleIds[state.activeIndex];
    state = reconfigureUniverseTimelineWindow(state, 6, 18);

    expect(state.cacheBundleIds[state.activeIndex]).toBe(activeId);
    expect(state.activeIndex).toBe(3);
    expect(state.visibleBundleIds).toEqual([
      "bundle-0",
      "bundle-1",
      "bundle-2",
      "bundle-3",
    ]);
    expect(state.visitedCount).toBe(4);
    expect(state.visibleLimit).toBe(6);
    expect(state.cacheLimit).toBe(18);
  });

  it("shrinks only safe history and defers the remainder without losing future", () => {
    let state = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(4, 12),
      Array.from({ length: 12 }, (_, index) => `bundle-${index}`),
    );
    for (let index = 0; index < 4; index += 1) {
      state = advanceUniverseTimelineWindow(state, "next", 1);
    }
    const activeId = state.cacheBundleIds[state.activeIndex];
    const futureIds = state.cacheBundleIds.slice(state.activeIndex + 1);
    state = reconfigureUniverseTimelineWindow(state, 4, 6);

    expect(state.cacheBundleIds[state.activeIndex]).toBe(activeId);
    expect(state.cacheBundleIds.slice(state.activeIndex + 1)).toEqual(futureIds);
    expect(state.cacheBundleIds).toEqual(
      Array.from({ length: 8 }, (_, index) => `bundle-${index + 4}`),
    );
    expect(state.activeIndex).toBe(3);
    expect(state.cacheStartOffset).toBe(4);
    // Four visible bundles plus four unseen future bundles cannot safely fit
    // the requested six yet, so the effective limit converges later.
    expect(state.cacheLimit).toBe(8);
    expect(queriedUniverseTimelineEventCount(state)).toBe(12);
    expect(shouldPrefetchUniverseTimelineWindow(state, 4, 6)).toBe(false);

    state = advanceUniverseTimelineWindow(state, "next", 1);
    state = reconfigureUniverseTimelineWindow(state, 4, 6);
    expect(state.cacheBundleIds[0]).toBe("bundle-5");
    expect(state.cacheLimit).toBe(7);
    expect(state.cacheBundleIds[state.activeIndex]).toBe("bundle-8");
  });

  it("protects visible and unseen future while atomically applying safe prefix eviction", () => {
    let state = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(4, 12),
      Array.from({ length: 12 }, (_, index) => `bundle-${index}`),
    );
    for (let index = 0; index < 4; index += 1) {
      state = advanceUniverseTimelineWindow(state, "next", 1);
    }
    expect(state.activeIndex).toBe(7);
    expect(state.visibleBundleIds).toEqual([
      "bundle-4",
      "bundle-5",
      "bundle-6",
      "bundle-7",
    ]);
    expect(protectedUniverseTimelineBundleIds(state)).toEqual(
      Array.from({ length: 8 }, (_, index) => `bundle-${index + 4}`),
    );
    expect(protectedUniverseTimelineBundleIds(state, "active-bundle")).toEqual(
      Array.from({ length: 5 }, (_, index) => `bundle-${index + 7}`),
    );

    const synchronized = applyUniverseTimelineBundleEvictions(
      state,
      ["support-old", "bundle-0", "bundle-1"],
    );
    expect(synchronized).not.toBeNull();
    expect(synchronized?.cacheBundleIds).toEqual(
      Array.from({ length: 10 }, (_, index) => `bundle-${index + 2}`),
    );
    expect(synchronized?.activeIndex).toBe(5);
    expect(synchronized?.visibleBundleIds).toEqual(state.visibleBundleIds);
    expect(synchronized?.cacheStartOffset).toBe(2);
    expect(synchronized && queriedUniverseTimelineEventCount(synchronized)).toBe(12);

    expect(applyUniverseTimelineBundleEvictions(state, ["bundle-1"]))
      .toBeNull();
    expect(applyUniverseTimelineBundleEvictions(
      state,
      ["bundle-0", "bundle-1", "bundle-2", "bundle-3", "bundle-4"],
    )).toBeNull();

    const recovery = applyUniverseTimelineBundleEvictions(
      state,
      Array.from({ length: 7 }, (_, index) => `bundle-${index}`),
      "active-bundle",
    );
    expect(recovery?.cacheBundleIds).toEqual([
      "bundle-7",
      "bundle-8",
      "bundle-9",
      "bundle-10",
      "bundle-11",
    ]);
    expect(recovery?.activeIndex).toBe(0);
    expect(recovery?.visibleBundleIds).toEqual(["bundle-7"]);
    expect(recovery && queriedUniverseTimelineEventCount(recovery)).toBe(12);
    expect(applyUniverseTimelineBundleEvictions(
      state,
      Array.from({ length: 8 }, (_, index) => `bundle-${index}`),
      "active-bundle",
    )).toBeNull();
  });

  it("continues one bundle at a time under a 40-node capacity pause", () => {
    const budget = { nodes: 40, edges: 40 };
    expect(universeTimelinePageBundleLimit(6, 8, budget)).toBe(4);
    expect(universeTimelinePageBundleLimit(6, 8, budget, true)).toBe(1);
    expect(universeTimelinePageBundleLimit(
      6,
      8,
      { nodes: 240, edges: 360 },
    )).toBe(6);

    let working = emptyUniverseWorkingSet(7);
    let state = createUniverseTimelineWindow(4, 4);
    const initialBundleIds: string[] = [];
    for (let index = 0; index < 4; index += 1) {
      const id = `bundle-${index}`;
      initialBundleIds.push(id);
      const admitted = admitUniverseBundle(
        working,
        eventBundle(
          id,
          `event-${index}`,
          Array.from({ length: 8 }, (_, entityIndex) =>
            `entity-${index}-${entityIndex}`),
        ),
        budget,
        index + 1,
      );
      expect(admitted.accepted).toBe(true);
      working = admitted.workingSet;
    }
    state = appendUniverseTimelineBundles(state, initialBundleIds);
    expect(state.activeIndex).toBe(3);
    expect(working.nodes).toHaveLength(36);

    const normalProtection = protectionForBundles(
      working,
      protectedUniverseTimelineBundleIds(state),
    );
    const normallyRejected = admitUniverseBundle(
      working,
      eventBundle(
        "bundle-4",
        "event-4",
        Array.from({ length: 8 }, (_, index) => `entity-4-${index}`),
      ),
      budget,
      5,
      {
        protectedKeys: normalProtection.nodeKeys,
        protectedRelationKeys: normalProtection.relationKeys,
      },
    );
    expect(normallyRejected.accepted).toBe(false);
    expect(normallyRejected.reason).toBe("protected_capacity");

    for (let index = 4; index < 6; index += 1) {
      const id = `bundle-${index}`;
      const recoveryProtection = protectionForBundles(
        working,
        protectedUniverseTimelineBundleIds(state, "active-bundle"),
      );
      const admitted = admitUniverseBundle(
        working,
        eventBundle(
          id,
          `event-${index}`,
          Array.from({ length: 8 }, (_, entityIndex) =>
            `entity-${index}-${entityIndex}`),
        ),
        budget,
        index + 1,
        {
          protectedKeys: recoveryProtection.nodeKeys,
          protectedRelationKeys: recoveryProtection.relationKeys,
        },
      );
      expect(admitted.accepted).toBe(true);
      expect(admitted.evictedBundleIds).toEqual([state.cacheBundleIds[0]]);
      const synchronized = applyUniverseTimelineBundleEvictions(
        state,
        admitted.evictedBundleIds,
        "active-bundle",
      );
      expect(synchronized).not.toBeNull();
      state = appendUniverseTimelineBundles(synchronized!, [id]);
      working = admitted.workingSet;
      state = advanceUniverseTimelineWindow(state, "next", 1);
      expect(state.cacheBundleIds[state.activeIndex]).toBe(id);
      expect(queriedUniverseTimelineEventCount(state)).toBe(index + 1);
      expect(working.nodes).toHaveLength(36);
    }
  });

  it("preserves terminal semantics while changing limits", () => {
    let state = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(2, 8),
      ["a", "b", "c", "d"],
    );
    state = markUniverseTimelineNetworkExhausted(state);
    expect(isUniverseTimelineWindowComplete(state)).toBe(false);
    const activeId = state.cacheBundleIds[state.activeIndex];
    state = reconfigureUniverseTimelineWindow(state, 4, 8);
    expect(state.cacheBundleIds[state.activeIndex]).toBe(activeId);
    expect(isUniverseTimelineWindowComplete(state)).toBe(false);

    state = advanceUniverseTimelineWindow(state, "next", 1);
    state = advanceUniverseTimelineWindow(state, "next", 1);
    expect(isUniverseTimelineWindowComplete(state)).toBe(true);
    state = reconfigureUniverseTimelineWindow(state, 2, 4);
    expect(isUniverseTimelineWindowComplete(state)).toBe(true);
    expect(state.phase).toBe("complete");
  });

  it("prefetches only when low-water demand fits safely", () => {
    let state = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(4, 12),
      ["a", "b", "c", "d"],
    );
    expect(shouldPrefetchUniverseTimelineWindow(state, 4, 12)).toBe(true);

    state = appendUniverseTimelineBundles(state, ["e", "f", "g", "h", "i"]);
    expect(state.cacheBundleIds.length - state.activeIndex - 1).toBe(5);
    expect(shouldPrefetchUniverseTimelineWindow(state, 4, 12)).toBe(false);

    state = advanceUniverseTimelineWindow(state, "next", 1);
    expect(shouldPrefetchUniverseTimelineWindow(state, 4, 12)).toBe(true);

    for (let index = 0; index < 3; index += 1) {
      state = advanceUniverseTimelineWindow(state, "next", 1);
    }
    state = appendUniverseTimelineBundles(state, ["j", "k", "l"]);
    // Ahead is now at the low-water threshold, but a six-bundle page would
    // overflow beyond the four safely evictable historical bundles.
    expect(shouldPrefetchUniverseTimelineWindow(state, 6, 12)).toBe(false);
  });

  it("fills only the ahead-water target instead of eagerly filling capacity", () => {
    let state = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(6, 96),
      Array.from({ length: 6 }, (_, index) => `bundle-${index}`),
    );
    expect(universeTimelinePrefetchAheadTarget(state, 6, 96)).toBe(12);
    let nextId = 6;
    for (let page = 0; page < 2; page += 1) {
      expect(shouldPrefetchUniverseTimelineWindow(state, 6, 96)).toBe(true);
      state = appendUniverseTimelineBundles(
        state,
        Array.from({ length: 6 }, () => `bundle-${nextId++}`),
      );
    }
    expect(state.cacheBundleIds).toHaveLength(18);
    expect(state.activeIndex).toBe(5);
    expect(state.cacheBundleIds.length - state.activeIndex - 1).toBe(12);
    expect(shouldPrefetchUniverseTimelineWindow(state, 6, 96)).toBe(false);
  });
});

describe("universe bundle window projection", () => {
  it("projects complete selected bundles and deduplicates a shared entity", () => {
    const projected = projectUniverseBundleWindow(
      workingSetWithSharedEntity(),
      ["bundle-1", "bundle-2", "bundle-2", "missing"],
    );

    expect(projected.bundle_order).toEqual(["bundle-1", "bundle-2"]);
    expect(projected.nodes.map((node) => node.id)).toEqual([
      "event-1",
      "shared",
      "entity-1",
      "event-2",
    ]);
    expect(projected.nodes.filter((node) => node.id === "shared")).toHaveLength(1);
    expect(projected.relations).toHaveLength(3);
    expect(projected.node_owners["source-a:entity:shared"]).toEqual([
      "bundle-1",
      "bundle-2",
    ]);
    expect(projected.root_keys).toEqual(projected.node_order);
    expectClosedWorkingSet(projected);
  });

  it("drops relations whose endpoint is absent instead of producing a dangling edge", () => {
    const working = workingSetWithSharedEntity();
    working.bundles["bundle-1"] = {
      ...working.bundles["bundle-1"],
      node_keys: working.bundles["bundle-1"].node_keys.filter((key) =>
        key !== "source-a:entity:entity-1"),
    };
    const projected = projectUniverseBundleWindow(working, ["bundle-1"]);

    expect(projected.nodes.some((node) => node.id === "entity-1")).toBe(false);
    expect(projected.relations.some((relation) => relation.to_id === "entity-1"))
      .toBe(false);
    expectClosedWorkingSet(projected);
  });

  it("keeps visible bundles before pinned and caller-ranked support within scene budget", () => {
    const residentBudget = { nodes: 40, edges: 40 };
    let working = emptyUniverseWorkingSet(7);
    [
      eventBundle("visible-1", "visible-event-1", ["visible-entity-1"]),
      eventBundle("visible-2", "visible-event-2", ["visible-entity-2"]),
      eventBundle("support-old", "support-event-old", ["support-entity-old"]),
      eventBundle("support-new", "support-event-new", ["support-entity-new"]),
    ].forEach((bundle, index) => {
      working = admitUniverseBundle(
        working,
        bundle,
        residentBudget,
        index + 1,
        { roots: bundle.id.startsWith("visible") },
      ).workingSet;
    });
    working = setUniversePinnedNetwork(
      working,
      [universeNodeKey("event", "support-event-old", "source-a")],
      [],
    );

    const pinnedProjection = projectUniverseBundleWindowWithinBudget(
      working,
      ["visible-1", "visible-2"],
      ["support-old", "support-new"],
      { nodes: 6, edges: 3 },
    );
    expect(pinnedProjection.bundle_order).toEqual([
      "visible-1",
      "visible-2",
      "support-old",
    ]);
    expectClosedWorkingSet(pinnedProjection);

    const unpinnedProjection = projectUniverseBundleWindowWithinBudget(
      setUniversePinnedNetwork(working, [], []),
      ["visible-1", "visible-2"],
      ["support-old", "support-new"],
      { nodes: 6, edges: 3 },
    );
    expect(unpinnedProjection.bundle_order).toEqual([
      "visible-1",
      "visible-2",
      "support-old",
    ]);
    expectClosedWorkingSet(unpinnedProjection);
  });

  it("caps unique visible events while recent explicit expansion replaces edge history", () => {
    const residentBudget = { nodes: 80, edges: 80 };
    let working = emptyUniverseWorkingSet(7);
    const visibleIds = Array.from({ length: 6 }, (_, index) => `visible-${index}`);
    visibleIds.forEach((id, index) => {
      working = admitUniverseBundle(
        working,
        {
          ...eventBundle(id, `timeline-event-${index}`, [`timeline-entity-${index}`]),
          origin: "timeline",
        },
        residentBudget,
        index + 1,
        { roots: true },
      ).workingSet;
    });
    working = admitUniverseBundle(
      working,
      {
        id: "support-new",
        origin: "expansion",
        anchor_key: universeNodeKey("entity", "timeline-entity-5", "source-a"),
        epoch: 7,
        source_id: "source-a",
        nodes: [
          {
            id: "timeline-entity-5",
            kind: "entity",
            source_id: "source-a",
            label: "timeline-entity-5",
          },
          ...Array.from({ length: 4 }, (_, index) => ({
            id: `support-event-${index}`,
            kind: "event" as const,
            source_id: "source-a",
            label: `support-event-${index}`,
          })),
        ],
        relations: Array.from({ length: 4 }, (_, index) => ({
          source_id: "source-a",
          from_id: `support-event-${index}`,
          to_id: "timeline-entity-5",
          kind: "mentions" as const,
          weight: 1,
          description: "",
        })),
      },
      residentBudget,
      20,
    ).workingSet;

    const projected = projectUniverseBundleWindowWithinBudget(
      working,
      visibleIds,
      ["support-new"],
      { nodes: 40, edges: 40 },
      6,
    );
    const eventIds = projected.nodes
      .filter((node) => node.kind === "event")
      .map((node) => node.id);
    expect(eventIds).toHaveLength(6);
    expect(eventIds).toEqual(expect.arrayContaining([
      "timeline-event-4",
      "timeline-event-5",
      "support-event-0",
      "support-event-1",
      "support-event-2",
      "support-event-3",
    ]));
    expect(eventIds).not.toContain("timeline-event-0");
    expect(projected.nodes.filter((node) => node.id === "timeline-entity-5"))
      .toHaveLength(1);
    expectClosedWorkingSet(projected);
  });

  it("prioritizes recent support unless an older support network is pinned", () => {
    const residentBudget = { nodes: 30, edges: 30 };
    let working = emptyUniverseWorkingSet(7);
    const supportBundle = (id: string, eventId: string): UniverseAdmissionBundle => ({
      id,
      origin: "expansion",
      anchor_key: universeNodeKey("entity", "timeline-entity", "source-a"),
      epoch: 7,
      source_id: "source-a",
      nodes: [
        { id: eventId, kind: "event", source_id: "source-a", label: eventId },
        {
          id: "timeline-entity",
          kind: "entity",
          source_id: "source-a",
          label: "timeline-entity",
        },
      ],
      relations: [{
        source_id: "source-a",
        from_id: eventId,
        to_id: "timeline-entity",
        kind: "mentions",
        weight: 1,
        description: "",
      }],
    });
    [
      { ...eventBundle("visible", "timeline-active", ["timeline-entity"]), origin: "timeline" as const },
      supportBundle("support-old", "support-old-event"),
      supportBundle("support-new", "support-new-event"),
    ].forEach((bundle, index) => {
      working = admitUniverseBundle(
        working,
        bundle,
        residentBudget,
        index + 1,
        { roots: bundle.id === "visible" },
      ).workingSet;
    });
    const recent = projectUniverseBundleWindowWithinBudget(
      working,
      ["visible"],
      ["support-new", "support-old"],
      { nodes: 10, edges: 10 },
      2,
    );
    expect(recent.nodes.some((node) => node.id === "support-new-event")).toBe(true);
    expect(recent.nodes.some((node) => node.id === "support-old-event")).toBe(false);
    expect(recent.nodes.some((node) => node.id === "timeline-active")).toBe(true);

    const pinned = projectUniverseBundleWindowWithinBudget(
      setUniversePinnedNetwork(
        working,
        [universeNodeKey("event", "support-old-event", "source-a")],
        [],
      ),
      ["visible"],
      ["support-new", "support-old"],
      { nodes: 10, edges: 10 },
      2,
    );
    expect(pinned.nodes.some((node) => node.id === "support-old-event")).toBe(true);
    expect(pinned.nodes.some((node) => node.id === "support-new-event")).toBe(true);
    expect(pinned.nodes.some((node) => node.id === "timeline-active")).toBe(false);
    expectClosedWorkingSet(pinned);
  });

  it("keeps the newest connected exploration suffix when a deep path fills the window", () => {
    const residentBudget = { nodes: 30, edges: 30 };
    let working = emptyUniverseWorkingSet(7);
    const bundles: UniverseAdmissionBundle[] = [
      {
        ...eventBundle("visible", "timeline-event", ["anchor-x"]),
        origin: "timeline",
      },
      {
        id: "expand-x",
        origin: "expansion",
        anchor_key: universeNodeKey("entity", "anchor-x", "source-a"),
        lineage_root_key: universeNodeKey("entity", "anchor-x", "source-a"),
        epoch: 7,
        source_id: "source-a",
        nodes: [
          { id: "anchor-x", kind: "entity", source_id: "source-a", label: "X" },
          { id: "event-1", kind: "event", source_id: "source-a", label: "E1" },
        ],
        relations: [{
          source_id: "source-a",
          from_id: "event-1",
          to_id: "anchor-x",
          kind: "mentions",
          weight: 1,
          description: "",
        }],
      },
      {
        id: "expand-event-1",
        origin: "expansion",
        anchor_key: universeNodeKey("event", "event-1", "source-a"),
        lineage_root_key: universeNodeKey("entity", "anchor-x", "source-a"),
        epoch: 7,
        source_id: "source-a",
        nodes: [
          { id: "event-1", kind: "event", source_id: "source-a", label: "E1" },
          { id: "anchor-y", kind: "entity", source_id: "source-a", label: "Y" },
        ],
        relations: [{
          source_id: "source-a",
          from_id: "event-1",
          to_id: "anchor-y",
          kind: "mentions",
          weight: 1,
          description: "",
        }],
      },
      {
        id: "expand-y",
        origin: "expansion",
        anchor_key: universeNodeKey("entity", "anchor-y", "source-a"),
        lineage_root_key: universeNodeKey("entity", "anchor-x", "source-a"),
        epoch: 7,
        source_id: "source-a",
        nodes: [
          { id: "anchor-y", kind: "entity", source_id: "source-a", label: "Y" },
          { id: "event-2", kind: "event", source_id: "source-a", label: "E2" },
        ],
        relations: [{
          source_id: "source-a",
          from_id: "event-2",
          to_id: "anchor-y",
          kind: "mentions",
          weight: 1,
          description: "",
        }],
      },
      {
        id: "expand-event-2",
        origin: "expansion",
        anchor_key: universeNodeKey("event", "event-2", "source-a"),
        lineage_root_key: universeNodeKey("entity", "anchor-x", "source-a"),
        epoch: 7,
        source_id: "source-a",
        nodes: [
          { id: "event-2", kind: "event", source_id: "source-a", label: "E2" },
          { id: "anchor-z", kind: "entity", source_id: "source-a", label: "Z" },
        ],
        relations: [{
          source_id: "source-a",
          from_id: "event-2",
          to_id: "anchor-z",
          kind: "mentions",
          weight: 1,
          description: "",
        }],
      },
      {
        id: "expand-z",
        origin: "expansion",
        anchor_key: universeNodeKey("entity", "anchor-z", "source-a"),
        lineage_root_key: universeNodeKey("entity", "anchor-x", "source-a"),
        epoch: 7,
        source_id: "source-a",
        nodes: [
          { id: "anchor-z", kind: "entity", source_id: "source-a", label: "Z" },
          { id: "event-3", kind: "event", source_id: "source-a", label: "E3" },
        ],
        relations: [{
          source_id: "source-a",
          from_id: "event-3",
          to_id: "anchor-z",
          kind: "mentions",
          weight: 1,
          description: "",
        }],
      },
    ];
    bundles.forEach((bundle, index) => {
      working = admitUniverseBundle(
        working,
        bundle,
        residentBudget,
        index + 1,
        { roots: bundle.origin === "timeline" },
      ).workingSet;
    });

    const supportIds = [
      "expand-z",
      "expand-event-2",
      "expand-y",
      "expand-event-1",
      "expand-x",
    ];
    const projected = projectUniverseBundleWindowWithinBudget(
      working,
      ["visible"],
      supportIds,
      { nodes: 20, edges: 20 },
      2,
    );
    const events = projected.nodes
      .filter((node) => node.kind === "event")
      .map((node) => node.id);
    expect(events).toEqual(["timeline-event", "event-3"]);
    expect(events).toContain("timeline-event");
    expect(events).not.toContain("event-1");
    expect(projected.relations.some((relation) =>
      relation.from_id === "event-3" && relation.to_id === "anchor-z"))
      .toBe(true);
    expectClosedWorkingSet(projected);

    const retainedSuffix = retainUniverseWorkingSetBundles(
      working,
      ["visible", "expand-event-2", "expand-z"],
    );
    const rebasedProjection = projectUniverseBundleWindowWithinBudget(
      retainedSuffix,
      ["visible"],
      ["expand-z", "expand-event-2"],
      { nodes: 20, edges: 20 },
      2,
    );
    expect(rebasedProjection.nodes
      .filter((node) => node.kind === "event")
      .map((node) => node.id)).toEqual(["timeline-event", "event-3"]);
    expect(rebasedProjection.relations.some((relation) =>
      relation.from_id === "event-3" && relation.to_id === "anchor-z"))
      .toBe(true);
    expectClosedWorkingSet(rebasedProjection);

    const eventThreeRelationKey = universeRelationKey(
      working.relations.find((relation) => relation.from_id === "event-3")!,
    );
    const pinnedProjection = projectUniverseBundleWindowWithinBudget(
      setUniversePinnedNetwork(
        working,
        [universeNodeKey("event", "event-3", "source-a")],
        [eventThreeRelationKey],
      ),
      ["visible"],
      supportIds,
      { nodes: 20, edges: 20 },
      2,
    );
    expect(pinnedProjection.nodes
      .filter((node) => node.kind === "event")
      .map((node) => node.id)).toEqual(["event-2", "event-3"]);
    expect(pinnedProjection.relations.some((relation) =>
      relation.from_id === "event-2" && relation.to_id === "anchor-z"))
      .toBe(true);
    expect(pinnedProjection.relations.some((relation) =>
      relation.from_id === "event-3" && relation.to_id === "anchor-z"))
      .toBe(true);
    expectClosedWorkingSet(pinnedProjection);
  });

  it("keeps the exact pinned event and pinned edge ahead of bundle siblings", () => {
    const residentBudget = { nodes: 30, edges: 30 };
    let working = admitUniverseBundle(
      emptyUniverseWorkingSet(7),
      {
        ...eventBundle("visible", "timeline-active", ["timeline-entity"]),
        origin: "timeline",
      },
      residentBudget,
      1,
      { roots: true },
    ).workingSet;
    working = admitUniverseBundle(
      working,
      {
        id: "dense-support",
        origin: "expansion",
        anchor_key: universeNodeKey("entity", "timeline-entity", "source-a"),
        epoch: 7,
        source_id: "source-a",
        nodes: [
          { id: "support-0", kind: "event", source_id: "source-a", label: "0" },
          { id: "support-1", kind: "event", source_id: "source-a", label: "1" },
          { id: "support-2", kind: "event", source_id: "source-a", label: "2" },
          { id: "entity-a", kind: "entity", source_id: "source-a", label: "A" },
          { id: "entity-b", kind: "entity", source_id: "source-a", label: "B" },
        ],
        relations: [
          {
            source_id: "source-a",
            from_id: "support-2",
            to_id: "entity-a",
            kind: "mentions",
            weight: 1,
            description: "",
          },
          {
            source_id: "source-a",
            from_id: "support-2",
            to_id: "entity-b",
            kind: "mentions",
            weight: 1,
            description: "",
          },
        ],
      },
      residentBudget,
      2,
    ).workingSet;
    const pinnedRelation = working.relations.find((relation) =>
      relation.from_id === "support-2" && relation.to_id === "entity-b")!;
    working = setUniversePinnedNetwork(
      working,
      [universeNodeKey("event", "support-2", "source-a")],
      [universeRelationKey(pinnedRelation)],
    );

    const projected = projectUniverseBundleWindowWithinBudget(
      working,
      ["visible"],
      ["dense-support"],
      { nodes: 10, edges: 10 },
      2,
      1,
    );
    const eventIds = projected.nodes
      .filter((node) => node.kind === "event")
      .map((node) => node.id);
    expect(eventIds).toContain("support-2");
    expect(eventIds).toHaveLength(2);
    expect(projected.relations.filter((relation) =>
      relation.from_id === "support-2")).toEqual([
      expect.objectContaining({ to_id: "entity-b" }),
    ]);
    expect(projected.pinned_keys).toContain(
      universeNodeKey("event", "support-2", "source-a"),
    );
    expect(projected.pinned_relation_keys).toEqual([
      universeRelationKey(pinnedRelation),
    ]);
    expectClosedWorkingSet(projected);
  });

  it("skips an unrelated deep leaf when its support suffix cannot fit atomically", () => {
    const residentBudget = { nodes: 40, edges: 40 };
    let working = admitUniverseBundle(
      emptyUniverseWorkingSet(7),
      {
        ...eventBundle("visible", "timeline-active", ["anchor-x"]),
        origin: "timeline",
      },
      residentBudget,
      1,
      { roots: true },
    ).workingSet;
    const bundles: UniverseAdmissionBundle[] = [
      {
        ...eventBundle("old-parent", "old-parent-event", ["anchor-x"]),
        origin: "expansion",
        anchor_key: universeNodeKey("entity", "anchor-x", "source-a"),
      },
      {
        ...eventBundle("old-bridge", "old-parent-event", ["anchor-y"]),
        origin: "expansion",
        anchor_key: universeNodeKey("event", "old-parent-event", "source-a"),
      },
      {
        ...eventBundle("old-leaf", "old-leaf-event", ["anchor-y"]),
        origin: "expansion",
        anchor_key: universeNodeKey("entity", "anchor-y", "source-a"),
      },
      {
        id: "latest-leaf",
        origin: "expansion",
        anchor_key: universeNodeKey("entity", "anchor-x", "source-a"),
        epoch: 7,
        source_id: "source-a",
        nodes: [
          { id: "latest-0", kind: "event", source_id: "source-a", label: "L0" },
          { id: "latest-1", kind: "event", source_id: "source-a", label: "L1" },
          { id: "anchor-x", kind: "entity", source_id: "source-a", label: "X" },
        ],
        relations: ["latest-0", "latest-1"].map((eventId) => ({
          source_id: "source-a",
          from_id: eventId,
          to_id: "anchor-x",
          kind: "mentions" as const,
          weight: 1,
          description: "",
        })),
      },
    ];
    bundles.forEach((bundle, index) => {
      working = admitUniverseBundle(
        working,
        bundle,
        residentBudget,
        index + 2,
      ).workingSet;
    });

    const projected = projectUniverseBundleWindowWithinBudget(
      working,
      ["visible"],
      bundles.map((bundle) => bundle.id).reverse(),
      { nodes: 20, edges: 20 },
      4,
    );
    const eventIds = projected.nodes
      .filter((node) => node.kind === "event")
      .map((node) => node.id);
    expect(eventIds).toEqual([
      "timeline-active",
      "latest-0",
      "latest-1",
    ]);
    expect(eventIds).not.toContain("old-leaf-event");
    expect(eventIds).not.toContain("old-parent-event");
    expectClosedWorkingSet(projected);
  });

  it("keeps a dense resident cache separate from the bounded render projection", () => {
    const residentBudget = { nodes: 1_152, edges: 1_152 };
    let working = emptyUniverseWorkingSet(7);
    const timelineIds: string[] = [];
    const supportIds: string[] = [];
    for (let index = 0; index < 38; index += 1) {
      const id = index < 18 ? `timeline-${index}` : `support-${index}`;
      const admitted = admitUniverseBundle(
        working,
        eventBundle(
          id,
          `event-${index}`,
          Array.from({ length: 8 }, (_, entityIndex) =>
            `entity-${index}-${entityIndex}`),
        ),
        residentBudget,
        index + 1,
        { roots: index < 18 },
      );
      expect(admitted.accepted).toBe(true);
      working = admitted.workingSet;
      (index < 18 ? timelineIds : supportIds).push(id);
    }
    expect(working.nodes).toHaveLength(342);
    expect(working.relations).toHaveLength(304);

    const projected = projectUniverseBundleWindowWithinBudget(
      working,
      timelineIds,
      supportIds,
      { nodes: 240, edges: 360 },
    );
    timelineIds.forEach((id) => expect(projected.bundle_order).toContain(id));
    expect(projected.nodes).toHaveLength(234);
    expect(projected.relations).toHaveLength(208);
    expect(projected.bundle_order).toHaveLength(26);
    expectClosedWorkingSet(projected);
  });

  it("physically retains cache and support bundles while rebuilding ownership", () => {
    const working = workingSetWithSharedEntity();
    working.pinned_keys = [
      "source-a:event:event-1",
      "source-a:event:event-support",
    ];
    working.pinned_relation_keys = working.relations.map(universeRelationKey);

    const retained = retainUniverseWorkingSetBundles(
      working,
      ["bundle-2", "support", "missing"],
    );
    expect(retained.bundle_order).toEqual(["bundle-2", "support"]);
    expect(retained.nodes.map((node) => node.id)).toEqual([
      "event-2",
      "shared",
      "event-support",
      "entity-support",
    ]);
    expect(retained.node_owners["source-a:entity:shared"]).toEqual(["bundle-2"]);
    expect(retained.pinned_keys).toEqual(["source-a:event:event-support"]);
    expect(retained.pinned_relation_keys).toHaveLength(2);
    expectClosedWorkingSet(retained);
  });

  it("returns an invariant empty working set when no bundle is selected", () => {
    const projected = projectUniverseBundleWindow(workingSetWithSharedEntity(), []);
    expect(projected.nodes).toEqual([]);
    expect(projected.relations).toEqual([]);
    expect(projected.bundle_order).toEqual([]);
    expect(projected.bundles).toEqual({});
    expect(projected.node_owners).toEqual({});
    expect(projected.relation_owners).toEqual({});
    expect(projected.root_keys).toEqual([]);
  });
});
