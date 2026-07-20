import type {
  UniverseTimelineDirection,
  UniverseTimelineSlice,
} from "./types";
import {
  effectiveUniverseTimelineVisibleBundleIds,
  universeTimelineStartActiveIndex,
} from "./universe-timeline-window";

export type UniverseTimelineBundle = UniverseTimelineSlice["bundles"][number];

/**
 * Raw timeline cache in canonical newest-to-oldest order.
 *
 * This cache is deliberately independent from the rendered working set. The
 * scene may project only a small visible window while this deque keeps enough
 * material on both sides for instant forward/backward travel.
 */
export interface UniverseTimelineDeque {
  sourceId: string;
  sourceRevision: string;
  snapshotId: string;
  bundles: UniverseTimelineBundle[];
  hasNewer: boolean;
  newerCursor: string | null;
  hasOlder: boolean;
  olderCursor: string | null;
}

export interface UniverseTimelineDequeAdmission {
  deque: UniverseTimelineDeque;
  addedBundleIds: string[];
  prependedBundleIds: string[];
  appendedBundleIds: string[];
  evictedBundleIds: string[];
  evictedNewerBundleIds: string[];
  evictedOlderBundleIds: string[];
  duplicateBundleIds: string[];
}

export interface UniverseTimelineDequeWindowAnchor {
  cacheBundleIds: string[];
  activeBundleId: string | null;
  activeIndex: number;
  activeIndexDelta: number;
  visibleBundleIds: string[];
}

export interface UniverseTimelineDequeResize {
  deque: UniverseTimelineDeque;
  evictedNewerBundleIds: string[];
  evictedOlderBundleIds: string[];
}

function positiveInteger(value: number, name: string) {
  if (!Number.isInteger(value) || value < 1) {
    throw new Error(`${name} must be a positive integer`);
  }
  return value;
}

/**
 * Reserve one or more complete pages at both edges of the rendered window.
 * This is a capacity calculation, not another user-facing tuning knob.
 */
export function requiredUniverseTimelineCacheBundles(
  visibleBundles: number,
  pageSize: number,
  historyReservePages = 1,
  aheadReservePages = 1,
) {
  positiveInteger(visibleBundles, "visibleBundles");
  positiveInteger(pageSize, "pageSize");
  if (!Number.isInteger(historyReservePages) || historyReservePages < 0) {
    throw new Error("historyReservePages must be a non-negative integer");
  }
  if (!Number.isInteger(aheadReservePages) || aheadReservePages < 0) {
    throw new Error("aheadReservePages must be a non-negative integer");
  }
  return visibleBundles + pageSize * (historyReservePages + aheadReservePages);
}

function edgeState(bundles: UniverseTimelineBundle[]) {
  const first = bundles[0];
  const last = bundles.at(-1);
  return {
    newerCursor: first?.cursor_before ?? null,
    hasNewer: Boolean(first?.cursor_before),
    olderCursor: last?.cursor_after ?? null,
    hasOlder: Boolean(last?.cursor_after),
  };
}

function expectedCursor(
  deque: UniverseTimelineDeque,
  direction: UniverseTimelineDirection,
) {
  return direction === "older" ? deque.olderCursor : deque.newerCursor;
}

function validPageEnvelope(page: UniverseTimelineSlice) {
  const ids = page.bundles.map((bundle) => bundle.bundle_id);
  const first = page.bundles[0];
  const last = page.bundles.at(-1);
  const directionalCursor = page.request_direction === "older"
    ? page.page.older_cursor
    : page.page.newer_cursor;
  return page.schema_version === 3
    && page.page.direction === page.request_direction
    && page.page.returned_bundles === page.bundles.length
    && new Set(ids).size === ids.length
    && page.page.has_newer === (page.page.newer_cursor !== null)
    && page.page.has_older === (page.page.older_cursor !== null)
    && page.page.has_more === (directionalCursor !== null)
    && page.page.next_cursor === directionalCursor
    && (page.request_direction !== "newer" || page.request_cursor !== null)
    && (page.request_cursor === null || page.request_cursor !== directionalCursor)
    && (page.bundles.length === 0 || first?.cursor_before === page.page.newer_cursor)
    && (page.bundles.length === 0 || last?.cursor_after === page.page.older_cursor)
    && Number.isInteger(page.total_events)
    && page.total_events >= 0
    && page.bundles.every((bundle, index) =>
      (index === 0 || bundle.cursor_before !== null)
      && (index === page.bundles.length - 1 || bundle.cursor_after !== null)
      // Ordinals are the counting axis: they must march strictly older within
      // the page and stay inside the snapshot's event total.
      && Number.isInteger(bundle.ordinal)
      && bundle.ordinal >= 0
      && bundle.ordinal < page.total_events
      && (index === 0 || bundle.ordinal > page.bundles[index - 1].ordinal));
}

/**
 * Admit one adjacent page into a fixed-capacity raw cache.
 *
 * Older pages append and retire the newest edge. Newer pages prepend and
 * retire the oldest edge. Every retained boundary bundle carries the opaque
 * checkpoint needed to fetch the evicted side again.
 */
export function admitUniverseTimelineDequePage(
  current: UniverseTimelineDeque | null,
  page: UniverseTimelineSlice,
  maxBundles: number,
): UniverseTimelineDequeAdmission {
  positiveInteger(maxBundles, "maxBundles");
  if (!validPageEnvelope(page)) {
    throw new Error("invalid timeline deque page contract");
  }

  if (current === null) {
    if (page.request_direction !== "older" || page.request_cursor !== null) {
      throw new Error("timeline deque must start from the root page");
    }
  } else {
    if (
      current.sourceId !== page.source_id
      || current.sourceRevision !== page.source_revision
      || current.snapshotId !== page.snapshot_id
    ) {
      throw new Error("timeline page does not belong to the active snapshot");
    }
  }

  const existingIds = new Set(current?.bundles.map((bundle) => bundle.bundle_id));
  const duplicateBundleIds = page.bundles
    .filter((bundle) => existingIds.has(bundle.bundle_id))
    .map((bundle) => bundle.bundle_id);
  const incoming = page.bundles.filter((bundle) => !existingIds.has(bundle.bundle_id));

  // An exact retry is idempotent even after another prefetch has advanced an
  // edge; it must never rewind the current checkpoints.
  if (current && incoming.length === 0 && page.bundles.length > 0) {
    return {
      deque: current,
      addedBundleIds: [],
      prependedBundleIds: [],
      appendedBundleIds: [],
      evictedBundleIds: [],
      evictedNewerBundleIds: [],
      evictedOlderBundleIds: [],
      duplicateBundleIds,
    };
  }

  if (current && page.request_cursor !== expectedCursor(current, page.request_direction)) {
    throw new Error("timeline page is not adjacent to the requested cache edge");
  }

  // A page that is cursor-adjacent must also be ordinal-adjacent, or the
  // counting axis would place its events at depths already owned by the cache.
  if (current && incoming.length > 0) {
    const currentFirst = current.bundles[0]?.ordinal;
    const currentLast = current.bundles.at(-1)?.ordinal;
    const incomingFirst = incoming[0].ordinal;
    const incomingLast = incoming.at(-1)?.ordinal ?? incomingFirst;
    const adjacent = page.request_direction === "older"
      ? currentLast === undefined || incomingFirst > currentLast
      : currentFirst === undefined || incomingLast < currentFirst;
    if (!adjacent) {
      throw new Error("timeline page ordinals overlap the cached window");
    }
  }

  let merged = current
    ? page.request_direction === "older"
      ? [...current.bundles, ...incoming]
      : [...incoming, ...current.bundles]
    : [...incoming];

  // An empty terminal page closes the requested edge without discarding the
  // opposite checkpoint.
  if (current && incoming.length === 0 && page.bundles.length === 0) {
    merged = current.bundles.map((bundle, index) => {
      if (page.request_direction === "older" && index === current.bundles.length - 1) {
        return { ...bundle, cursor_after: null };
      }
      if (page.request_direction === "newer" && index === 0) {
        return { ...bundle, cursor_before: null };
      }
      return bundle;
    });
  }

  const overflow = Math.max(0, merged.length - maxBundles);
  const evicted = overflow === 0
    ? []
    : current === null || page.request_direction === "newer"
      ? merged.slice(merged.length - overflow)
      : merged.slice(0, overflow);
  if (overflow > 0) {
    merged = current === null || page.request_direction === "newer"
      ? merged.slice(0, merged.length - overflow)
      : merged.slice(overflow);
  }

  const edges = edgeState(merged);
  const retainedIds = new Set(merged.map((bundle) => bundle.bundle_id));
  const addedBundleIds = incoming
    .filter((bundle) => retainedIds.has(bundle.bundle_id))
    .map((bundle) => bundle.bundle_id);
  const evictedBundleIds = evicted.map((bundle) => bundle.bundle_id);
  const prependedBundleIds = page.request_direction === "newer"
    ? addedBundleIds
    : [];
  const appendedBundleIds = page.request_direction === "older"
    ? addedBundleIds
    : [];
  const evictedNewerBundleIds = current !== null
    && page.request_direction === "older"
    ? evictedBundleIds
    : [];
  const evictedOlderBundleIds = evictedBundleIds.filter(
    (id) => !evictedNewerBundleIds.includes(id),
  );
  return {
    deque: {
      sourceId: page.source_id,
      sourceRevision: page.source_revision,
      snapshotId: page.snapshot_id,
      bundles: merged,
      ...edges,
    },
    addedBundleIds,
    prependedBundleIds,
    appendedBundleIds,
    evictedBundleIds,
    evictedNewerBundleIds,
    evictedOlderBundleIds,
    duplicateBundleIds,
  };
}

/**
 * Rebase a virtual window onto a changed raw deque without changing focus.
 * The bundle identity is authoritative; a stale numerical index is never used
 * to select another event. ``null`` means the admission evicted the active
 * anchor and must be rejected or retried with a larger/protected cache.
 */
export function syncUniverseTimelineWindowToDeque(
  deque: UniverseTimelineDeque,
  previous: {
    activeBundleId: string | null;
    activeIndex: number;
    visibleLimit: number;
  },
): UniverseTimelineDequeWindowAnchor | null {
  const cacheBundleIds = deque.bundles.map((bundle) => bundle.bundle_id);
  if (cacheBundleIds.length === 0) {
    return {
      cacheBundleIds,
      activeBundleId: null,
      activeIndex: -1,
      activeIndexDelta: previous.activeIndex < 0 ? 0 : -previous.activeIndex - 1,
      visibleBundleIds: [],
    };
  }

  let activeIndex: number;
  if (previous.activeBundleId === null) {
    activeIndex = universeTimelineStartActiveIndex(
      cacheBundleIds.length,
      previous.visibleLimit,
    );
  } else {
    activeIndex = cacheBundleIds.indexOf(previous.activeBundleId);
    if (activeIndex < 0) return null;
  }
  const activeBundleId = cacheBundleIds[activeIndex] ?? null;
  return {
    cacheBundleIds,
    activeBundleId,
    activeIndex,
    activeIndexDelta: activeIndex - previous.activeIndex,
    visibleBundleIds: effectiveUniverseTimelineVisibleBundleIds(
      cacheBundleIds,
      activeIndex,
      previous.visibleLimit,
    ),
  };
}

/**
 * Shrinks a raw deque around its active identity while preserving the visible
 * time window and a caller-defined history runway. Boundary bundle cursors
 * make either retired side reloadable later.
 */
export function resizeUniverseTimelineDeque(
  deque: UniverseTimelineDeque,
  maxBundles: number,
  activeBundleId: string,
  visibleLimit: number,
  historyReserveBundles: number,
): UniverseTimelineDequeResize | null {
  positiveInteger(maxBundles, "maxBundles");
  positiveInteger(visibleLimit, "visibleLimit");
  if (!Number.isInteger(historyReserveBundles) || historyReserveBundles < 0) {
    throw new Error("historyReserveBundles must be a non-negative integer");
  }
  if (deque.bundles.length <= maxBundles) {
    return {
      deque,
      evictedNewerBundleIds: [],
      evictedOlderBundleIds: [],
    };
  }
  const activeIndex = deque.bundles.findIndex(
    (bundle) => bundle.bundle_id === activeBundleId,
  );
  if (activeIndex < 0) return null;

  const historyTarget = Math.max(0, visibleLimit - 1 + historyReserveBundles);
  const maxStart = deque.bundles.length - maxBundles;
  const start = Math.min(maxStart, Math.max(0, activeIndex - historyTarget));
  const end = start + maxBundles;
  if (activeIndex < start || activeIndex >= end) return null;
  const bundles = deque.bundles.slice(start, end);
  return {
    deque: {
      ...deque,
      bundles,
      ...edgeState(bundles),
    },
    evictedNewerBundleIds: deque.bundles
      .slice(0, start)
      .map((bundle) => bundle.bundle_id),
    evictedOlderBundleIds: deque.bundles
      .slice(end)
      .map((bundle) => bundle.bundle_id),
  };
}
