"use client";

import * as React from "react";

import {
  UNIVERSE_SCENE_BUDGET,
  universeNodeKey,
  type UniverseWorkingSet,
} from "./universe-working-set";

export const UNIVERSE_VIEW_PREFERENCES_VERSION = 7;
export const UNIVERSE_VIEW_PREFERENCES_STORAGE_KEY =
  "sag:universe-view-preferences:v7";
export const UNIVERSE_ENTITY_CATEGORIES_STORAGE_KEY =
  "sag:universe-entity-categories";

const UNIVERSE_VIEW_PREFERENCES_CHANGE_EVENT =
  "sag:universe-view-preferences-change";
const UNIVERSE_ENTITY_CATEGORIES_CHANGE_EVENT =
  "sag:universe-entity-categories-change";
const MAX_REMEMBERED_ENTITY_CATEGORIES = 64;

export interface UniverseViewPreferences {
  version: typeof UNIVERSE_VIEW_PREFERENCES_VERSION;
  cacheCapacity: number;
  eventWindowSize: number;
  cardsEnabled: boolean;
  eventCardPreviewCount: number;
  temporalPageSize: number;
  temporalPrefetchPages: number;
  /** `null` means every discovered entity type is selected. */
  entityTypes: string[] | null;
  /** `null` means every document in the active source is selected. */
  documentIds: string[] | null;
}

/** Product limits shared by normalization, controls and the scene contract. */
export const UNIVERSE_VIEW_LIMITS = {
  cacheCapacity: {
    min: 200,
    max: 5_000,
    step: 100,
    default: 1_000,
  },
  eventWindowSize: {
    min: 20,
    max: 100,
    step: 5,
    default: 50,
  },
  eventCardPreviewCount: {
    min: 1,
    max: 20,
    step: 1,
    default: 13,
  },
  temporalPageSize: {
    min: 10,
    max: 50,
    step: 5,
    default: 20,
  },
  temporalPrefetchPages: {
    min: 0,
    max: 3,
    step: 1,
    default: 3,
  },
  /**
   * Internal DOM safety cap, sized for every resident entity belonging to the
   * maximum 20-event card aperture (20 × the server's 8-entity page).
   */
  entityCardSafetyMax: 160,
} as const;

export const DEFAULT_UNIVERSE_VIEW_PREFERENCES: Readonly<UniverseViewPreferences> =
  Object.freeze({
    version: UNIVERSE_VIEW_PREFERENCES_VERSION,
    cacheCapacity: UNIVERSE_VIEW_LIMITS.cacheCapacity.default,
    eventWindowSize: UNIVERSE_VIEW_LIMITS.eventWindowSize.default,
    cardsEnabled: true,
    eventCardPreviewCount:
      UNIVERSE_VIEW_LIMITS.eventCardPreviewCount.default,
    temporalPageSize: UNIVERSE_VIEW_LIMITS.temporalPageSize.default,
    temporalPrefetchPages:
      UNIVERSE_VIEW_LIMITS.temporalPrefetchPages.default,
    entityTypes: null,
    documentIds: null,
  });

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function finiteInteger(value: unknown, fallback: number) {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.floor(value)
    : fallback;
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function roundToStep(value: number, step: number) {
  return Math.round(value / step) * step;
}

function cloneDefaultPreferences(): UniverseViewPreferences {
  return {
    ...DEFAULT_UNIVERSE_VIEW_PREFERENCES,
    entityTypes: null,
    documentIds: null,
  };
}

function normalizeStringSelection(value: unknown, limit: number): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value
    .filter((item): item is string => typeof item === "string")
    .map((item) => item.trim())
    .filter(Boolean))]
    .sort((left, right) => left.localeCompare(right, "zh-CN"))
    .slice(0, limit);
}

/**
 * The cache must hold the visible window and configured prefetch runways on
 * both sides. Its normal default remains far above this functional minimum.
 */
export function minimumUniverseCacheCapacity(
  eventWindowSize: number,
  temporalPageSize: number = UNIVERSE_VIEW_LIMITS.temporalPageSize.default,
  temporalPrefetchPages: number =
    UNIVERSE_VIEW_LIMITS.temporalPrefetchPages.default,
) {
  const windowSize = clamp(
    finiteInteger(eventWindowSize, UNIVERSE_VIEW_LIMITS.eventWindowSize.default),
    UNIVERSE_VIEW_LIMITS.eventWindowSize.min,
    UNIVERSE_VIEW_LIMITS.eventWindowSize.max,
  );
  const pageSize = clamp(
    finiteInteger(temporalPageSize, UNIVERSE_VIEW_LIMITS.temporalPageSize.default),
    UNIVERSE_VIEW_LIMITS.temporalPageSize.min,
    UNIVERSE_VIEW_LIMITS.temporalPageSize.max,
  );
  const prefetchPages = clamp(
    finiteInteger(
      temporalPrefetchPages,
      UNIVERSE_VIEW_LIMITS.temporalPrefetchPages.default,
    ),
    UNIVERSE_VIEW_LIMITS.temporalPrefetchPages.min,
    UNIVERSE_VIEW_LIMITS.temporalPrefetchPages.max,
  );
  return clamp(
    Math.ceil(
      (
        windowSize
        + pageSize * prefetchPages * 2
      ) / UNIVERSE_VIEW_LIMITS.cacheCapacity.step,
    ) * UNIVERSE_VIEW_LIMITS.cacheCapacity.step,
    UNIVERSE_VIEW_LIMITS.cacheCapacity.min,
    UNIVERSE_VIEW_LIMITS.cacheCapacity.max,
  );
}

function normalizedNumber(
  value: unknown,
  limits: { min: number; max: number; step: number; default: number },
) {
  return clamp(
    roundToStep(
      finiteInteger(value, limits.default),
      limits.step,
    ),
    limits.min,
    limits.max,
  );
}

/** Converts persisted or partially trusted data into the strict current schema. */
export function normalizeUniverseViewPreferences(
  value: unknown,
): UniverseViewPreferences {
  const input = isRecord(value) && value.version === UNIVERSE_VIEW_PREFERENCES_VERSION
    ? value
    : {};
  const eventWindowSize = normalizedNumber(
    input.eventWindowSize,
    UNIVERSE_VIEW_LIMITS.eventWindowSize,
  );
  const temporalPageSize = normalizedNumber(
    input.temporalPageSize,
    UNIVERSE_VIEW_LIMITS.temporalPageSize,
  );
  const temporalPrefetchPages = normalizedNumber(
    input.temporalPrefetchPages,
    UNIVERSE_VIEW_LIMITS.temporalPrefetchPages,
  );
  const cacheCapacity = clamp(
    Math.max(
      normalizedNumber(
        input.cacheCapacity,
        UNIVERSE_VIEW_LIMITS.cacheCapacity,
      ),
      minimumUniverseCacheCapacity(
        eventWindowSize,
        temporalPageSize,
        temporalPrefetchPages,
      ),
    ),
    UNIVERSE_VIEW_LIMITS.cacheCapacity.min,
    UNIVERSE_VIEW_LIMITS.cacheCapacity.max,
  );
  const eventCardPreviewCount = Math.min(
    eventWindowSize,
    normalizedNumber(
      input.eventCardPreviewCount,
      UNIVERSE_VIEW_LIMITS.eventCardPreviewCount,
    ),
  );
  const selectedEntityTypes = normalizeStringSelection(
    input.entityTypes,
    MAX_REMEMBERED_ENTITY_CATEGORIES,
  );
  const selectedDocumentIds = normalizeStringSelection(input.documentIds, 1_000);

  return {
    version: UNIVERSE_VIEW_PREFERENCES_VERSION,
    cacheCapacity,
    eventWindowSize,
    cardsEnabled: typeof input.cardsEnabled === "boolean"
      ? input.cardsEnabled
      : DEFAULT_UNIVERSE_VIEW_PREFERENCES.cardsEnabled,
    eventCardPreviewCount,
    temporalPageSize,
    temporalPrefetchPages,
    entityTypes: selectedEntityTypes.length > 0
      ? selectedEntityTypes
      : null,
    documentIds: selectedDocumentIds.length > 0
      ? selectedDocumentIds
      : null,
  };
}

export interface UniverseBundleWindow {
  eventWindowSize: number;
  cacheCapacity: number;
}

/** Resolves the one shared window configuration used by both scene states. */
export function effectiveUniverseBundleWindow(
  preferences: UniverseViewPreferences,
  _isMobile: boolean,
): UniverseBundleWindow {
  // Mobile and desktop intentionally share the same product-level window.
  // Renderer safety budgets are enforced independently below.
  void _isMobile;
  const normalized = normalizeUniverseViewPreferences(preferences);
  return {
    eventWindowSize: normalized.eventWindowSize,
    cacheCapacity: normalized.cacheCapacity,
  };
}

export interface UniverseSceneBudget {
  nodes: number;
  edges: number;
}

/** Applies the renderer's hard safety budget independently from view settings. */
export function effectiveUniverseBudget(
  policyBudget: UniverseSceneBudget,
): UniverseSceneBudget {
  const policyNodes = Math.min(
    UNIVERSE_SCENE_BUDGET.desktop.nodes,
    Math.max(0, finiteInteger(policyBudget.nodes, 0)),
  );
  const policyEdges = Math.min(
    UNIVERSE_SCENE_BUDGET.desktop.edges,
    Math.max(0, finiteInteger(policyBudget.edges, 0)),
  );
  return {
    nodes: policyNodes,
    edges: policyEdges,
  };
}

/**
 * Applies the explicit entity-category filter without ever removing events.
 * Card visibility remains a scene-only concern and does not enter this path.
 */
export function projectUniverseWorkingSet(
  current: UniverseWorkingSet,
  entityTypes: string[] | null,
): UniverseWorkingSet {
  if (entityTypes === null) return current;
  const selectedCategories = new Set(
    normalizeStringSelection(entityTypes, MAX_REMEMBERED_ENTITY_CATEGORIES),
  );
  if (selectedCategories.size === 0) return current;
  const nodes = current.nodes.filter((node) =>
    node.kind === "event"
    || selectedCategories.has((node.category ?? "").trim()));
  const keptKeys = new Set(nodes.map((node) =>
    universeNodeKey(node.kind, node.id, node.source_id)));
  const relations = current.relations.filter((relation) => {
    const targetKind = relation.kind === "subevent" ? "event" : "entity";
    return keptKeys.has(universeNodeKey("event", relation.from_id, relation.source_id))
      && keptKeys.has(universeNodeKey(targetKind, relation.to_id, relation.source_id));
  });

  return {
    ...current,
    nodes,
    relations,
    root_keys: current.root_keys.filter((key) => keptKeys.has(key)),
    node_order: current.node_order.filter((key) => keptKeys.has(key)),
  };
}

type UniverseViewStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

function defaultStorage(): UniverseViewStorage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function dispatchBrowserEvent(name: string) {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(name));
}

export function loadUniverseViewPreferences(
  storage: UniverseViewStorage | null = defaultStorage(),
): UniverseViewPreferences {
  if (!storage) return cloneDefaultPreferences();
  try {
    const raw = storage.getItem(UNIVERSE_VIEW_PREFERENCES_STORAGE_KEY);
    return raw ? normalizeUniverseViewPreferences(JSON.parse(raw)) : cloneDefaultPreferences();
  } catch {
    return cloneDefaultPreferences();
  }
}

export function saveUniverseViewPreferences(
  preferences: UniverseViewPreferences,
  storage: UniverseViewStorage | null = defaultStorage(),
): UniverseViewPreferences {
  const normalized = normalizeUniverseViewPreferences(preferences);
  if (!storage) return normalized;
  try {
    storage.setItem(
      UNIVERSE_VIEW_PREFERENCES_STORAGE_KEY,
      JSON.stringify(normalized),
    );
  } catch {
    // Storage can be unavailable (private mode, quota, or embedded contexts).
  }
  return normalized;
}

export type UniverseViewPreferencesUpdate =
  | Partial<UniverseViewPreferences>
  | ((current: UniverseViewPreferences) => Partial<UniverseViewPreferences>);

export function updateStoredUniverseViewPreferences(
  update: UniverseViewPreferencesUpdate,
  storage: UniverseViewStorage | null = defaultStorage(),
) {
  const current = loadUniverseViewPreferences(storage);
  const patch = typeof update === "function" ? update(current) : update;
  return saveUniverseViewPreferences({ ...current, ...patch }, storage);
}

export function resetStoredUniverseViewPreferences(
  storage: UniverseViewStorage | null = defaultStorage(),
) {
  try {
    storage?.removeItem(UNIVERSE_VIEW_PREFERENCES_STORAGE_KEY);
  } catch {
    // Reset still succeeds in memory if persistent storage is unavailable.
  }
  return cloneDefaultPreferences();
}

export interface UseUniverseViewPreferencesResult {
  preferences: UniverseViewPreferences;
  updatePreferences: (update: UniverseViewPreferencesUpdate) => void;
  resetPreferences: () => void;
}

export function useUniverseViewPreferences(): UseUniverseViewPreferencesResult {
  const [preferences, setPreferences] = React.useState<UniverseViewPreferences>(
    cloneDefaultPreferences,
  );

  const updatePreferences = React.useCallback((update: UniverseViewPreferencesUpdate) => {
    const next = updateStoredUniverseViewPreferences(update);
    setPreferences(next);
    dispatchBrowserEvent(UNIVERSE_VIEW_PREFERENCES_CHANGE_EVENT);
  }, []);

  const resetPreferences = React.useCallback(() => {
    setPreferences(resetStoredUniverseViewPreferences());
    dispatchBrowserEvent(UNIVERSE_VIEW_PREFERENCES_CHANGE_EVENT);
  }, []);

  React.useEffect(() => {
    // Keep the server snapshot deterministic, then hydrate browser preferences.
    setPreferences(loadUniverseViewPreferences());
    const onStorage = (event: StorageEvent) => {
      if (
        event.key === UNIVERSE_VIEW_PREFERENCES_STORAGE_KEY
        || event.key === null
      ) {
        setPreferences(loadUniverseViewPreferences());
      }
    };
    const onPreferenceChange = () => {
      setPreferences(loadUniverseViewPreferences());
    };
    window.addEventListener("storage", onStorage);
    window.addEventListener(
      UNIVERSE_VIEW_PREFERENCES_CHANGE_EVENT,
      onPreferenceChange,
    );
    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener(
        UNIVERSE_VIEW_PREFERENCES_CHANGE_EVENT,
        onPreferenceChange,
      );
    };
  }, []);

  return { preferences, updatePreferences, resetPreferences };
}

/** Keeps discovered categories available while the graph renderer is asleep. */
export function loadUniverseEntityCategories(
  storage: UniverseViewStorage | null = defaultStorage(),
): string[] {
  if (!storage) return [];
  try {
    const raw = storage.getItem(UNIVERSE_ENTITY_CATEGORIES_STORAGE_KEY);
    return raw
      ? normalizeStringSelection(
          JSON.parse(raw),
          MAX_REMEMBERED_ENTITY_CATEGORIES,
        )
      : [];
  } catch {
    return [];
  }
}

export function publishUniverseEntityCategories(
  categories: string[],
  storage: UniverseViewStorage | null = defaultStorage(),
) {
  const current = loadUniverseEntityCategories(storage);
  const next = normalizeStringSelection(
    [...current, ...categories],
    MAX_REMEMBERED_ENTITY_CATEGORIES,
  );
  if (next.join("\u0000") === current.join("\u0000")) return current;
  try {
    storage?.setItem(
      UNIVERSE_ENTITY_CATEGORIES_STORAGE_KEY,
      JSON.stringify(next),
    );
  } catch {
    // The current graph still supplies its categories when storage is unavailable.
  }
  dispatchBrowserEvent(UNIVERSE_ENTITY_CATEGORIES_CHANGE_EVENT);
  return next;
}

export function useUniverseEntityCategories() {
  const [categories, setCategories] = React.useState<string[]>([]);

  React.useEffect(() => {
    const refresh = () => setCategories(loadUniverseEntityCategories());
    const onStorage = (event: StorageEvent) => {
      if (
        event.key === UNIVERSE_ENTITY_CATEGORIES_STORAGE_KEY
        || event.key === null
      ) {
        refresh();
      }
    };
    refresh();
    window.addEventListener("storage", onStorage);
    window.addEventListener(UNIVERSE_ENTITY_CATEGORIES_CHANGE_EVENT, refresh);
    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener(UNIVERSE_ENTITY_CATEGORIES_CHANGE_EVENT, refresh);
    };
  }, []);

  return categories;
}
