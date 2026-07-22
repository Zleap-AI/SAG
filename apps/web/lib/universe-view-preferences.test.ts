import { describe, expect, it } from "vitest";

import {
  replaceUniverseWorkingSet,
  type UniverseWorkingNode,
} from "./universe-working-set";
import {
  DEFAULT_UNIVERSE_VIEW_PREFERENCES,
  UNIVERSE_VIEW_LIMITS,
  UNIVERSE_VIEW_PREFERENCES_STORAGE_KEY,
  effectiveUniverseBudget,
  effectiveUniverseBundleWindow,
  loadUniverseEntityCategories,
  loadUniverseViewPreferences,
  minimumUniverseCacheCapacity,
  normalizeUniverseViewPreferences,
  projectUniverseWorkingSet,
  publishUniverseEntityCategories,
  type UniverseViewPreferences,
} from "./universe-view-preferences";

function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  };
}

function preferences(
  patch: Partial<UniverseViewPreferences> = {},
): UniverseViewPreferences {
  return normalizeUniverseViewPreferences({
    ...DEFAULT_UNIVERSE_VIEW_PREFERENCES,
    ...patch,
  });
}

function node(
  id: string,
  kind: "event" | "entity",
  category = "",
): UniverseWorkingNode {
  return {
    id,
    kind,
    source_id: "source-a",
    label: id,
    category,
    touched_at: 1,
    root: true,
  };
}

function workingSet() {
  return replaceUniverseWorkingSet({
    epoch: 7,
    query: "projection",
    nodes: [
      node("event-1", "event"),
      node("person-1", "entity", "Person"),
      node("event-2", "event"),
      node("org-1", "entity", "Organization"),
    ],
    relations: [
      {
        source_id: "source-a",
        from_id: "event-1",
        to_id: "person-1",
        kind: "mentions",
        weight: 1,
        description: "",
      },
      {
        source_id: "source-a",
        from_id: "event-2",
        to_id: "org-1",
        kind: "mentions",
        weight: 1,
        description: "",
      },
      {
        source_id: "source-a",
        from_id: "event-1",
        to_id: "event-2",
        kind: "subevent",
        weight: 1,
        description: "",
      },
    ],
  }, { nodes: 1_000, edges: 1_000 }, 1);
}

describe("universe v7 view preferences", () => {
  it("uses the one production configuration by default", () => {
    expect(DEFAULT_UNIVERSE_VIEW_PREFERENCES).toEqual({
      version: 7,
      cacheCapacity: 1_000,
      eventWindowSize: 50,
      cardsEnabled: true,
      eventCardPreviewCount: 13,
      temporalPageSize: 20,
      temporalPrefetchPages: 3,
      entityTypes: null,
      documentIds: null,
    });
  });

  it("repairs untrusted values inside the strict v7 schema", () => {
    expect(normalizeUniverseViewPreferences({
      version: 7,
      cacheCapacity: -100,
      eventWindowSize: 999,
      cardsEnabled: "yes",
      eventCardPreviewCount: 999,
      temporalPageSize: 27,
      temporalPrefetchPages: 99,
      entityTypes: [" Person ", "", "Person", 42],
      documentIds: [" doc-2 ", "doc-1", "doc-1"],
    })).toEqual({
      version: 7,
      cacheCapacity: 300,
      eventWindowSize: 100,
      cardsEnabled: true,
      eventCardPreviewCount: 20,
      temporalPageSize: 25,
      temporalPrefetchPages: 3,
      entityTypes: ["Person"],
      documentIds: ["doc-1", "doc-2"],
    });
  });

  it("exposes ranges that can support the default 50-event scene", () => {
    expect(UNIVERSE_VIEW_LIMITS.eventWindowSize).toMatchObject({
      min: 20,
      max: 100,
      default: 50,
    });
    expect(UNIVERSE_VIEW_LIMITS.cacheCapacity).toMatchObject({
      min: 200,
      max: 5_000,
      default: 1_000,
    });
    expect(UNIVERSE_VIEW_LIMITS.eventCardPreviewCount).toMatchObject({
      min: 1,
      max: 20,
      default: 13,
    });
    expect(UNIVERSE_VIEW_LIMITS.temporalPageSize.default).toBe(20);
    expect(UNIVERSE_VIEW_LIMITS.temporalPrefetchPages.default).toBe(3);
  });

  it("keeps enough cache for the window and both prefetch runways", () => {
    expect(minimumUniverseCacheCapacity(50, 20, 3)).toBe(200);
    expect(preferences({
      cacheCapacity: 200,
      eventWindowSize: 100,
      temporalPageSize: 50,
      temporalPrefetchPages: 3,
    }).cacheCapacity).toBe(400);
  });

  it("reads only v7 and deliberately ignores all old preference keys", () => {
    const storage = memoryStorage({
      "sag:universe-view-preferences:v6": JSON.stringify({
        version: 6,
        visibleEventBundles: 2,
      }),
    });
    expect(loadUniverseViewPreferences(storage)).toEqual(
      DEFAULT_UNIVERSE_VIEW_PREFERENCES,
    );

    storage.setItem(UNIVERSE_VIEW_PREFERENCES_STORAGE_KEY, JSON.stringify({
      ...DEFAULT_UNIVERSE_VIEW_PREFERENCES,
      cardsEnabled: false,
    }));
    expect(loadUniverseViewPreferences(storage).cardsEnabled).toBe(false);
  });

  it("uses the same configured window on desktop and mobile", () => {
    expect(effectiveUniverseBundleWindow(
      DEFAULT_UNIVERSE_VIEW_PREFERENCES,
      false,
    )).toEqual({ eventWindowSize: 50, cacheCapacity: 1_000 });
    expect(effectiveUniverseBundleWindow(
      DEFAULT_UNIVERSE_VIEW_PREFERENCES,
      true,
    )).toEqual({ eventWindowSize: 50, cacheCapacity: 1_000 });
  });
});

describe("universe rendering budgets", () => {
  it("keeps the factual budget independent from presentation settings", () => {
    expect(effectiveUniverseBudget({ nodes: 800, edges: 1_500 })).toEqual({
      nodes: 700,
      edges: 1_000,
    });
  });

  it("never exceeds a smaller renderer policy", () => {
    expect(effectiveUniverseBudget({ nodes: 24, edges: 30 }))
      .toEqual({ nodes: 24, edges: 30 });
  });
});

describe("entity-type projection", () => {
  it("always keeps events while filtering entities and dangling mentions", () => {
    const current = workingSet();
    const projected = projectUniverseWorkingSet(current, ["Person"]);

    expect(projected.nodes.map((item) => item.id)).toEqual([
      "event-1",
      "person-1",
      "event-2",
    ]);
    expect(projected.relations.map((relation) => relation.kind)).toEqual([
      "mentions",
      "subevent",
    ]);
    expect(projected.nodes.some((item) => item.id === "org-1")).toBe(false);
    expect(projected.relations.some((relation) => relation.to_id === "org-1"))
      .toBe(false);
    expect(current.nodes).toHaveLength(4);
  });

  it("returns the full network for all or an empty untrusted selection", () => {
    const current = workingSet();
    expect(projectUniverseWorkingSet(current, null)).toBe(current);
    expect(projectUniverseWorkingSet(current, [])).toBe(current);
  });
});

describe("universe entity type registry", () => {
  it("merges, trims and sorts discovered entity types", () => {
    const storage = memoryStorage();

    publishUniverseEntityCategories([" organization ", "concept"], storage);
    publishUniverseEntityCategories(["location", "concept", ""], storage);

    expect(loadUniverseEntityCategories(storage)).toEqual([
      "concept",
      "location",
      "organization",
    ]);
  });

  it("recovers safely from malformed persisted data", () => {
    const storage = memoryStorage({
      "sag:universe-entity-categories": "not-json",
    });
    expect(loadUniverseEntityCategories(storage)).toEqual([]);
  });
});
