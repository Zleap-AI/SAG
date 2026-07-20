import {
  admitUniverseEventBundles,
  readUniverseEventBundles,
  universeEventBundleKey,
  type UniverseEventBundle,
  type UniverseEventCache,
} from "./universe-event-cache";
import {
  appendUniverseWindowBundles,
  settleUniverseSceneWindow,
  type UniverseSceneWindow,
} from "./universe-scene-window";

export interface UniverseAccumulationState {
  window: UniverseSceneWindow;
  seenBundleKeys: ReadonlySet<string>;
  pendingBundles: readonly UniverseEventBundle[];
  appendRevision: number;
}

export interface UniverseEvidenceAppendResult {
  state: UniverseAccumulationState;
  cache: UniverseEventCache;
  newBundleKeys: readonly string[];
  updatedBundleKeys: readonly string[];
  evictedCacheKeys: readonly string[];
  hasNewEvidence: boolean;
}

export function createUniverseAccumulationState(
  window: UniverseSceneWindow,
): UniverseAccumulationState {
  return {
    window,
    seenBundleKeys: new Set(),
    pendingBundles: [],
    appendRevision: 0,
  };
}

function uniqueUnseenBundles(
  bundles: readonly UniverseEventBundle[],
  seen: ReadonlySet<string>,
) {
  const result = new Map<string, UniverseEventBundle>();
  for (const bundle of bundles) {
    const key = universeEventBundleKey(bundle);
    if (seen.has(key)) continue;
    result.set(key, bundle);
  }
  return [...result.values()];
}

function refreshCachedBundles(
  bundles: readonly UniverseEventBundle[],
  cache: UniverseEventCache,
) {
  return bundles.map((bundle) =>
    cache.recordsByKey.get(universeEventBundleKey(bundle)) ?? bundle);
}

/**
 * Repeated evidence enriches the existing fact in place. Refreshing every
 * resident phase from the cache preserves the window order and transition
 * identity while making newly discovered entities and relations readable.
 */
function refreshAccumulationFromCache(
  state: UniverseAccumulationState,
  cache: UniverseEventCache,
): UniverseAccumulationState {
  return {
    ...state,
    window: {
      ...state.window,
      activeBundles: refreshCachedBundles(
        state.window.activeBundles,
        cache,
      ),
      incomingBundles: refreshCachedBundles(
        state.window.incomingBundles,
        cache,
      ),
      outgoingBundles: refreshCachedBundles(
        state.window.outgoingBundles,
        cache,
      ),
      pendingActiveBundles: refreshCachedBundles(
        state.window.pendingActiveBundles,
        cache,
      ),
    },
    pendingBundles: refreshCachedBundles(state.pendingBundles, cache),
  };
}

function stagePendingBundles(
  state: UniverseAccumulationState,
): UniverseAccumulationState {
  if (
    state.window.phase !== "stable"
    || state.pendingBundles.length === 0
  ) {
    return state;
  }
  const admission = appendUniverseWindowBundles(
    state.window,
    state.pendingBundles,
  );
  return {
    ...state,
    window: admission.window,
    pendingBundles: admission.deferredBundles,
  };
}

export function appendUniverseEvidence(
  state: UniverseAccumulationState,
  cache: UniverseEventCache,
  bundles: readonly UniverseEventBundle[],
): UniverseEvidenceAppendResult {
  const cacheAdmission = admitUniverseEventBundles(cache, bundles);
  const refreshedState = cacheAdmission.updatedKeys.length > 0
    ? refreshAccumulationFromCache(state, cacheAdmission.cache)
    : state;
  const unseenInput = uniqueUnseenBundles(bundles, state.seenBundleKeys);
  const newBundleKeys = unseenInput.map(universeEventBundleKey);
  const seenBundleKeys = new Set(state.seenBundleKeys);
  newBundleKeys.forEach((key) => seenBundleKeys.add(key));
  const mergedNewBundles = readUniverseEventBundles(
    cacheAdmission.cache,
    newBundleKeys,
  );
  const pendingBundles = [
    ...refreshedState.pendingBundles,
    ...mergedNewBundles,
  ];
  const next = stagePendingBundles({
    ...refreshedState,
    seenBundleKeys,
    pendingBundles,
    appendRevision: newBundleKeys.length > 0
      ? state.appendRevision + 1
      : state.appendRevision,
  });

  return {
    state: next,
    cache: cacheAdmission.cache,
    newBundleKeys,
    updatedBundleKeys: cacheAdmission.updatedKeys,
    evictedCacheKeys: cacheAdmission.evictedKeys,
    hasNewEvidence: newBundleKeys.length > 0,
  };
}

export function advanceUniverseEvidenceTransition(
  state: UniverseAccumulationState,
) {
  return stagePendingBundles({
    ...state,
    window: settleUniverseSceneWindow(state.window),
  });
}
