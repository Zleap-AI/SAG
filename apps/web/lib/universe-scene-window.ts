import type { UniverseActivationNode, UniverseRelation } from "./types";
import {
  universeEventBundleKey,
  universeEventRelationKey,
  type UniverseEventBundle,
} from "./universe-event-cache";

export type UniverseWindowDirection = "forward" | "backward" | "append";
export type UniverseWindowPhase = "stable" | "transitioning";

export interface UniverseSceneWindow {
  limit: number;
  transitionCapacity: number;
  activeBundles: readonly UniverseEventBundle[];
  incomingBundles: readonly UniverseEventBundle[];
  outgoingBundles: readonly UniverseEventBundle[];
  pendingActiveBundles: readonly UniverseEventBundle[];
  phase: UniverseWindowPhase;
  direction: UniverseWindowDirection;
  revision: number;
}

export interface UniverseWindowAppendResult {
  window: UniverseSceneWindow;
  admittedBundles: readonly UniverseEventBundle[];
  deferredBundles: readonly UniverseEventBundle[];
}

export interface UniverseWindowProjection {
  bundles: readonly UniverseEventBundle[];
  events: readonly (UniverseActivationNode & { kind: "event" })[];
  entities: readonly (UniverseActivationNode & { kind: "entity" })[];
  relations: readonly UniverseRelation[];
  entityReferenceCounts: ReadonlyMap<string, number>;
}

function positiveInteger(value: number, fallback: number) {
  return Number.isFinite(value) ? Math.max(1, Math.floor(value)) : fallback;
}

function uniqueBundles(bundles: readonly UniverseEventBundle[]) {
  const byKey = new Map<string, UniverseEventBundle>();
  for (const bundle of bundles) {
    const key = universeEventBundleKey(bundle);
    if (byKey.has(key)) byKey.delete(key);
    byKey.set(key, bundle);
  }
  return [...byKey.values()];
}

function bundleKeys(bundles: readonly UniverseEventBundle[]) {
  return new Set(bundles.map(universeEventBundleKey));
}

export function createUniverseSceneWindow(
  limit = 50,
  transitionCapacity = 20,
): UniverseSceneWindow {
  return {
    limit: positiveInteger(limit, 50),
    transitionCapacity: positiveInteger(transitionCapacity, 20),
    activeBundles: [],
    incomingBundles: [],
    outgoingBundles: [],
    pendingActiveBundles: [],
    phase: "stable",
    direction: "forward",
    revision: 0,
  };
}

export function configureUniverseSceneWindow(
  current: UniverseSceneWindow,
  limit: number,
  transitionCapacity: number,
) {
  const nextLimit = positiveInteger(limit, current.limit);
  const nextTransitionCapacity = positiveInteger(
    transitionCapacity,
    current.transitionCapacity,
  );
  const base = current.phase === "transitioning"
    ? current.pendingActiveBundles
    : current.activeBundles;
  const activeBundles = base.slice(-nextLimit);
  return {
    ...current,
    limit: nextLimit,
    transitionCapacity: nextTransitionCapacity,
    activeBundles,
    incomingBundles: [],
    outgoingBundles: [],
    pendingActiveBundles: [],
    phase: "stable" as const,
    revision: current.revision + 1,
  };
}

export function beginUniverseWindowTransition(
  current: UniverseSceneWindow,
  requestedBundles: readonly UniverseEventBundle[],
  direction: UniverseWindowDirection,
): UniverseSceneWindow {
  if (current.phase !== "stable") {
    throw new Error("universe window transition is already active");
  }
  const nextActive = uniqueBundles(requestedBundles).slice(-current.limit);
  const currentKeys = bundleKeys(current.activeBundles);
  const nextKeys = bundleKeys(nextActive);
  const incomingBundles = nextActive.filter(
    (bundle) => !currentKeys.has(universeEventBundleKey(bundle)),
  );
  if (incomingBundles.length > current.transitionCapacity) {
    throw new Error("universe window transition exceeds its capacity");
  }
  const outgoingBundles = current.activeBundles.filter(
    (bundle) => !nextKeys.has(universeEventBundleKey(bundle)),
  );
  const unchanged = incomingBundles.length === 0
    && outgoingBundles.length === 0
    && nextActive.length === current.activeBundles.length;
  if (unchanged) return current;

  return {
    ...current,
    incomingBundles,
    outgoingBundles,
    pendingActiveBundles: nextActive,
    phase: "transitioning",
    direction,
    revision: current.revision + 1,
  };
}

export function appendUniverseWindowBundles(
  current: UniverseSceneWindow,
  bundles: readonly UniverseEventBundle[],
): UniverseWindowAppendResult {
  if (current.phase !== "stable") {
    return {
      window: current,
      admittedBundles: [],
      deferredBundles: uniqueBundles(bundles),
    };
  }
  const activeKeys = bundleKeys(current.activeBundles);
  const candidates = uniqueBundles(bundles).filter(
    (bundle) => !activeKeys.has(universeEventBundleKey(bundle)),
  );
  const admittedBundles = candidates.slice(0, current.transitionCapacity);
  const deferredBundles = candidates.slice(current.transitionCapacity);
  if (admittedBundles.length === 0) {
    return { window: current, admittedBundles, deferredBundles };
  }
  const nextActive = [
    ...current.activeBundles,
    ...admittedBundles,
  ].slice(-current.limit);
  return {
    window: beginUniverseWindowTransition(current, nextActive, "append"),
    admittedBundles,
    deferredBundles,
  };
}

export function settleUniverseSceneWindow(
  current: UniverseSceneWindow,
): UniverseSceneWindow {
  if (current.phase === "stable") return current;
  return {
    ...current,
    activeBundles: current.pendingActiveBundles,
    incomingBundles: [],
    outgoingBundles: [],
    pendingActiveBundles: [],
    phase: "stable",
    revision: current.revision + 1,
  };
}

export function residentUniverseWindowBundles(current: UniverseSceneWindow) {
  if (current.phase === "stable") return current.activeBundles;
  const result = [...current.activeBundles];
  const existing = bundleKeys(result);
  for (const bundle of current.incomingBundles) {
    const key = universeEventBundleKey(bundle);
    if (existing.has(key)) continue;
    existing.add(key);
    result.push(bundle);
  }
  return result;
}

export function projectUniverseSceneWindow(
  current: UniverseSceneWindow,
): UniverseWindowProjection {
  const bundles = residentUniverseWindowBundles(current);
  const events = bundles.map((bundle) => bundle.event);
  const entitiesByKey = new Map<
    string,
    UniverseActivationNode & { kind: "entity" }
  >();
  const entityReferenceCounts = new Map<string, number>();
  const relationsByKey = new Map<string, UniverseRelation>();
  const eventIds = new Set(events.map((event) => event.id));

  for (const bundle of bundles) {
    for (const entity of bundle.entities) {
      const key = `${bundle.sourceId}:entity:${entity.id}`;
      entitiesByKey.set(key, entity);
      entityReferenceCounts.set(
        key,
        (entityReferenceCounts.get(key) ?? 0) + 1,
      );
    }
  }
  const entityIds = new Set(
    [...entitiesByKey.values()].map((entity) => entity.id),
  );
  for (const bundle of bundles) {
    for (const relation of bundle.relations) {
      const hasSource = eventIds.has(relation.from_id);
      const hasTarget = relation.kind === "subevent"
        ? eventIds.has(relation.to_id)
        : entityIds.has(relation.to_id);
      if (!hasSource || !hasTarget) continue;
      relationsByKey.set(universeEventRelationKey(relation), relation);
    }
  }

  return {
    bundles,
    events,
    entities: [...entitiesByKey.values()],
    relations: [...relationsByKey.values()],
    entityReferenceCounts,
  };
}
