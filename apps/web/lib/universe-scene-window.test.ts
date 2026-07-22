import { describe, expect, it } from "vitest";

import type { UniverseEventBundle } from "./universe-event-cache";
import {
  appendUniverseWindowBundles,
  beginUniverseWindowTransition,
  createUniverseSceneWindow,
  projectUniverseSceneWindow,
  residentUniverseWindowBundles,
  settleUniverseSceneWindow,
} from "./universe-scene-window";

function bundle(id: string, entityId = `entity-${id}`): UniverseEventBundle {
  return {
    origin: "timeline",
    sourceId: "source-a",
    event: { id, kind: "event", label: id },
    entities: [{ id: entityId, kind: "entity", label: entityId }],
    relations: [{
      source_id: "source-a",
      from_id: id,
      to_id: entityId,
      kind: "mentions",
      weight: 1,
      description: "",
    }],
  };
}

describe("universe scene window", () => {
  it("keeps a stable 50-event window and at most one 20-event transition page", () => {
    const initial = Array.from({ length: 50 }, (_, index) =>
      bundle(`event-${index}`));
    const window = {
      ...createUniverseSceneWindow(50, 20),
      activeBundles: initial,
    };
    const incoming = Array.from({ length: 20 }, (_, index) =>
      bundle(`next-${index}`));
    const append = appendUniverseWindowBundles(window, incoming);

    expect(append.window.phase).toBe("transitioning");
    expect(append.window.incomingBundles).toHaveLength(20);
    expect(append.window.outgoingBundles).toHaveLength(20);
    expect(residentUniverseWindowBundles(append.window)).toHaveLength(70);

    const settled = settleUniverseSceneWindow(append.window);
    expect(settled.activeBundles).toHaveLength(50);
    expect(settled.activeBundles[0].event.id).toBe("event-20");
  });

  it("refuses an overlapping transition instead of rebuilding the scene", () => {
    const current = beginUniverseWindowTransition(
      createUniverseSceneWindow(50, 20),
      [bundle("event-1")],
      "forward",
    );

    expect(() => beginUniverseWindowTransition(
      current,
      [bundle("event-2")],
      "forward",
    )).toThrow("already active");
  });

  it("keeps a shared entity until its final event leaves the window", () => {
    const shared = "shared-entity";
    const current = {
      ...createUniverseSceneWindow(2, 1),
      activeBundles: [bundle("event-1", shared), bundle("event-2", shared)],
    };
    const firstProjection = projectUniverseSceneWindow(current);
    expect(firstProjection.entities).toHaveLength(1);
    expect(firstProjection.entityReferenceCounts.get(
      "source-a:entity:shared-entity",
    )).toBe(2);

    const append = appendUniverseWindowBundles(
      current,
      [bundle("event-3", "other")],
    );
    const settled = settleUniverseSceneWindow(append.window);
    const nextProjection = projectUniverseSceneWindow(settled);
    expect(nextProjection.entities.map((entity) => entity.id)).toEqual([
      shared,
      "other",
    ]);
    expect(nextProjection.entityReferenceCounts.get(
      "source-a:entity:shared-entity",
    )).toBe(1);
  });
});
