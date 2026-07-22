import { describe, expect, it } from "vitest";

import type { UniverseActivationNode } from "./types";
import {
  admitUniverseEventBundles,
  createUniverseEventCache,
  resizeUniverseEventCache,
  universeEventBundleKey,
  type UniverseEventBundle,
} from "./universe-event-cache";

function bundle(
  id: string,
  entityIds: string[] = [],
  description = id,
): UniverseEventBundle {
  return {
    origin: "timeline",
    sourceId: "source-a",
    event: {
      id,
      kind: "event",
      label: id,
      description,
    },
    entities: entityIds.map((entityId) => ({
      id: entityId,
      kind: "entity",
      label: entityId,
      category: "concept",
    } satisfies UniverseActivationNode & { kind: "entity" })),
    relations: entityIds.map((entityId) => ({
      source_id: "source-a",
      from_id: id,
      to_id: entityId,
      kind: "mentions",
      weight: 1,
      description: "",
    })),
  };
}

describe("universe event cache", () => {
  it("admits immutable event bundles and evicts by simple FIFO", () => {
    const first = admitUniverseEventBundles(
      createUniverseEventCache(3),
      [bundle("event-1"), bundle("event-2"), bundle("event-3")],
    );
    const second = admitUniverseEventBundles(
      first.cache,
      [bundle("event-4")],
    );

    expect(second.evictedKeys).toEqual(["source-a:event:event-1"]);
    expect(second.cache.admissionOrder).toEqual([
      "source-a:event:event-2",
      "source-a:event:event-3",
      "source-a:event:event-4",
    ]);
  });

  it("merges repeated facts without moving them to the queue tail", () => {
    const first = admitUniverseEventBundles(
      createUniverseEventCache(3),
      [bundle("event-1", ["entity-1"]), bundle("event-2")],
    );
    const second = admitUniverseEventBundles(
      first.cache,
      [bundle("event-1", ["entity-2"], "updated")],
    );
    const key = universeEventBundleKey(bundle("event-1"));
    const record = second.cache.recordsByKey.get(key);

    expect(second.addedKeys).toEqual([]);
    expect(second.updatedKeys).toEqual([key]);
    expect(second.cache.admissionOrder).toEqual([
      "source-a:event:event-1",
      "source-a:event:event-2",
    ]);
    expect(record?.event.description).toBe("updated");
    expect(record?.entities.map((entity) => entity.id)).toEqual([
      "entity-1",
      "entity-2",
    ]);
  });

  it("ignores a provenance-only duplicate without replacing the record", () => {
    const first = admitUniverseEventBundles(
      createUniverseEventCache(3),
      [bundle("event-1", ["entity-1"])],
    );
    const key = universeEventBundleKey(bundle("event-1"));
    const record = first.cache.recordsByKey.get(key);
    const duplicate = admitUniverseEventBundles(
      first.cache,
      [{
        ...bundle("event-1", ["entity-1"]),
        origin: "assistant",
      }],
    );

    expect(duplicate.updatedKeys).toEqual([]);
    expect(duplicate.cache.recordsByKey.get(key)).toBe(record);
  });

  it("shrinks capacity from the oldest admitted edge", () => {
    const admitted = admitUniverseEventBundles(
      createUniverseEventCache(4),
      [bundle("event-1"), bundle("event-2"), bundle("event-3")],
    );
    const resized = resizeUniverseEventCache(admitted.cache, 2);

    expect(resized.evictedKeys).toEqual(["source-a:event:event-1"]);
    expect(resized.cache.admissionOrder).toEqual([
      "source-a:event:event-2",
      "source-a:event:event-3",
    ]);
  });
});
