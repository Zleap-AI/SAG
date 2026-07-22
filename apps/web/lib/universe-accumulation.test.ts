import { describe, expect, it } from "vitest";

import {
  createUniverseEventCache,
  type UniverseEventBundle,
} from "./universe-event-cache";
import {
  advanceUniverseEvidenceTransition,
  appendUniverseEvidence,
  createUniverseAccumulationState,
} from "./universe-accumulation";
import {
  createUniverseSceneWindow,
  settleUniverseSceneWindow,
} from "./universe-scene-window";

function bundle(id: string, origin: UniverseEventBundle["origin"]): UniverseEventBundle {
  return {
    origin,
    sourceId: "source-a",
    event: { id, kind: "event", label: id },
    entities: [],
    relations: [],
  };
}

function enrichedBundle(
  id: string,
  origin: UniverseEventBundle["origin"],
): UniverseEventBundle {
  return {
    ...bundle(id, origin),
    entities: [{
      id: "entity-1",
      kind: "entity",
      label: "Entity 1",
      source_id: "source-a",
    }],
    relations: [{
      source_id: "source-a",
      from_id: id,
      to_id: "entity-1",
      kind: "mentions",
      weight: 1,
      description: "",
    }],
  };
}

describe("universe evidence accumulation", () => {
  it("dedupes the same fact across search, answer and expansion", () => {
    const initial = createUniverseAccumulationState(
      createUniverseSceneWindow(50, 20),
    );
    const first = appendUniverseEvidence(
      initial,
      createUniverseEventCache(1_000),
      [bundle("event-1", "search")],
    );
    const stable = {
      ...first.state,
      window: settleUniverseSceneWindow(first.state.window),
    };
    const second = appendUniverseEvidence(
      stable,
      first.cache,
      [
        bundle("event-1", "assistant"),
        bundle("event-2", "expansion"),
      ],
    );

    expect(second.newBundleKeys).toEqual(["source-a:event:event-2"]);
    expect(second.updatedBundleKeys).toEqual([]);
    expect(second.state.window.incomingBundles.map((item) => item.event.id))
      .toEqual(["event-2"]);
  });

  it("queues batches while a visual transition is active", () => {
    const initial = createUniverseAccumulationState(
      createUniverseSceneWindow(2, 1),
    );
    const first = appendUniverseEvidence(
      initial,
      createUniverseEventCache(1_000),
      [bundle("event-1", "search"), bundle("event-2", "search")],
    );

    expect(first.state.window.incomingBundles).toHaveLength(1);
    expect(first.state.pendingBundles).toHaveLength(1);

    const second = advanceUniverseEvidenceTransition(first.state);
    expect(second.window.incomingBundles.map((item) => item.event.id))
      .toEqual(["event-2"]);
    expect(second.pendingBundles).toHaveLength(0);
  });

  it("reports an all-duplicate batch without starting camera work", () => {
    const initial = createUniverseAccumulationState(
      createUniverseSceneWindow(50, 20),
    );
    const first = appendUniverseEvidence(
      initial,
      createUniverseEventCache(1_000),
      [bundle("event-1", "search")],
    );
    const stable = {
      ...first.state,
      window: settleUniverseSceneWindow(first.state.window),
    };
    const duplicate = appendUniverseEvidence(
      stable,
      first.cache,
      [bundle("event-1", "assistant")],
    );

    expect(duplicate.hasNewEvidence).toBe(false);
    expect(duplicate.state.window.phase).toBe("stable");
    expect(duplicate.state.appendRevision).toBe(1);
  });

  it("enriches a duplicate fact in place without changing window order", () => {
    const initial = createUniverseAccumulationState(
      createUniverseSceneWindow(50, 20),
    );
    const first = appendUniverseEvidence(
      initial,
      createUniverseEventCache(1_000),
      [bundle("event-1", "search"), bundle("event-2", "search")],
    );
    const stable = {
      ...first.state,
      window: settleUniverseSceneWindow(first.state.window),
    };
    const duplicate = appendUniverseEvidence(
      stable,
      first.cache,
      [enrichedBundle("event-1", "assistant")],
    );

    expect(duplicate.hasNewEvidence).toBe(false);
    expect(duplicate.state.window.phase).toBe("stable");
    expect(duplicate.state.window.activeBundles.map((item) => item.event.id))
      .toEqual(["event-1", "event-2"]);
    expect(duplicate.state.window.activeBundles[0]?.entities.map(
      (entity) => entity.id,
    )).toEqual(["entity-1"]);
    expect(duplicate.state.window.activeBundles[0]?.relations).toHaveLength(1);
  });
});
