import { describe, expect, it } from "vitest";

import {
  advanceUniverseEvidenceTransition,
  appendUniverseEvidence,
  createUniverseAccumulationState,
} from "@/lib/universe-accumulation";
import {
  admitUniverseEventBundles,
  createUniverseEventCache,
  type UniverseEventBundle,
} from "@/lib/universe-event-cache";
import { planUniversePrefetch } from "@/lib/universe-prefetch-controller";
import {
  createUniverseSceneWindow,
  projectUniverseSceneWindow,
  settleUniverseSceneWindow,
} from "@/lib/universe-scene-window";
import {
  createUniverseSessionState,
  reduceUniverseSession,
} from "@/lib/universe-session-state";

function bundle(index: number, origin: UniverseEventBundle["origin"] = "timeline"):
UniverseEventBundle {
  const eventId = `event-${index}`;
  const entityId = `entity-${index % 7}`;
  return {
    origin,
    sourceId: "source-a",
    event: {
      id: eventId,
      kind: "event",
      label: eventId,
      description: `fact ${index}`,
    },
    entities: [{
      id: entityId,
      kind: "entity",
      label: entityId,
      category: "concept",
    }],
    relations: [{
      source_id: "source-a",
      from_id: eventId,
      to_id: entityId,
      kind: "mentions",
      weight: 1,
      description: "",
    }],
    ordinal: index,
    temporalKey: `${index}`,
  };
}

function range(start: number, count: number, origin?: UniverseEventBundle["origin"]) {
  return Array.from({ length: count }, (_, offset) =>
    bundle(start + offset, origin));
}

describe("knowledge universe session integration", () => {
  it("keeps pagination, cache, scene window and cards as separate capacities", () => {
    const preferences = {
      pageSize: 20,
      prefetchPages: 3,
      windowSize: 50,
      previewCards: 10,
      cacheCapacity: 1_000,
    };
    const firstPage = range(0, preferences.pageSize);
    const admission = admitUniverseEventBundles(
      createUniverseEventCache(preferences.cacheCapacity),
      firstPage,
    );

    expect(admission.cache.admissionOrder).toHaveLength(20);
    expect(preferences.windowSize).toBe(50);
    expect(preferences.previewCards).toBe(10);
    expect(planUniversePrefetch({
      newerEvents: 0,
      olderEvents: 20,
      pageSize: preferences.pageSize,
      pagesPerSide: preferences.prefetchPages,
      hasNewer: false,
      hasOlder: true,
      preferredDirection: "older",
      inFlight: false,
    })).toMatchObject({
      direction: "older",
      targetEventsPerSide: 60,
      olderDeficit: 40,
    });
  });

  it("moves explicitly from exploration to cumulative evidence and back", () => {
    let session = reduceUniverseSession(createUniverseSessionState(), {
      type: "ENTER_EXPLORATION",
      sourceId: "source-a",
    });
    expect(session.mode).toBe("exploration");

    session = reduceUniverseSession(session, {
      type: "ENTER_ACCUMULATION",
      origin: "assistant",
      snapshotAvailable: true,
    });
    expect(session).toMatchObject({
      mode: "accumulation",
      sourceId: "source-a",
      explorationSnapshotAvailable: true,
    });

    session = reduceUniverseSession(session, {
      type: "APPEND_EVIDENCE",
      origin: "assistant",
      addedEvents: 8,
    });
    session = reduceUniverseSession(session, {
      type: "APPEND_EVIDENCE",
      origin: "browse",
      addedEvents: 3,
    });
    expect(session.evidenceBatchCount).toBe(2);
    expect(session.evidenceOrigin).toBe("browse");

    session = reduceUniverseSession(session, {
      type: "RETURN_TO_EXPLORATION",
    });
    expect(session).toMatchObject({
      mode: "exploration",
      sourceId: "source-a",
      evidenceBatchCount: 0,
    });
  });

  it("accumulates whole batches, deduplicates facts and settles to FIFO 50", () => {
    let cache = createUniverseEventCache(1_000);
    let state = createUniverseAccumulationState(
      createUniverseSceneWindow(50, 20),
    );

    const append = (bundles: readonly UniverseEventBundle[]) => {
      const result = appendUniverseEvidence(state, cache, bundles);
      state = result.state;
      cache = result.cache;
      return result;
    };
    const settle = () => {
      state = advanceUniverseEvidenceTransition(state);
    };

    expect(append(range(0, 20, "assistant")).newBundleKeys).toHaveLength(20);
    settle();
    expect(append([
      bundle(0, "assistant"),
      ...range(20, 19, "search"),
    ]).newBundleKeys).toHaveLength(19);
    settle();
    expect(append(range(39, 20, "expansion")).newBundleKeys).toHaveLength(20);

    const transitioning = projectUniverseSceneWindow(state.window);
    expect(transitioning.events).toHaveLength(59);
    expect(state.window.phase).toBe("transitioning");
    expect(state.window.outgoingBundles).toHaveLength(9);

    settle();
    const stable = projectUniverseSceneWindow(state.window);
    expect(stable.events).toHaveLength(50);
    expect(stable.events[0]?.id).toBe("event-9");
    expect(stable.events.at(-1)?.id).toBe("event-58");
    expect(stable.entities.length).toBeLessThanOrEqual(7);
  });

  it("does not move a stable accumulation window for an all-duplicate batch", () => {
    let cache = createUniverseEventCache(100);
    let state = createUniverseAccumulationState(
      settleUniverseSceneWindow(createUniverseSceneWindow(50, 20)),
    );
    const first = appendUniverseEvidence(
      state,
      cache,
      range(0, 5, "assistant"),
    );
    state = advanceUniverseEvidenceTransition(first.state);
    cache = first.cache;
    const before = state.window;

    const duplicate = appendUniverseEvidence(
      state,
      cache,
      range(0, 5, "search"),
    );
    expect(duplicate.hasNewEvidence).toBe(false);
    expect(duplicate.state.window).toBe(before);
  });

  it("clears focus without changing the current workspace mode", () => {
    let session = reduceUniverseSession(createUniverseSessionState(), {
      type: "ENTER_EXPLORATION",
      sourceId: "source-a",
    });
    session = reduceUniverseSession(session, {
      type: "TOGGLE_LOCK",
      key: "source-a:event:event-1",
    });
    session = reduceUniverseSession(session, {
      type: "OPEN_DETAIL",
      key: "source-a:event:event-1",
    });

    const cleared = reduceUniverseSession(session, { type: "CLEAR_FOCUS" });
    expect(cleared).toMatchObject({
      mode: "exploration",
      sourceId: "source-a",
      selectedKey: null,
      lockedKey: null,
      detailKey: null,
    });
  });
});
