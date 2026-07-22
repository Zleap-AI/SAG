import { describe, expect, it } from "vitest";

import type { UniverseTimelineSlice } from "./types";
import {
  admitUniverseTimelineDequePage,
  requiredUniverseTimelineCacheBundles,
  resizeUniverseTimelineDeque,
  syncUniverseTimelineWindowToDeque,
} from "./universe-timeline-deque";

type TimelineBundle = UniverseTimelineSlice["bundles"][number];

function bundle(
  index: number,
  cursorBefore: string | null,
  cursorAfter: string | null,
): TimelineBundle {
  const eventId = `event-${index}`;
  return {
    bundle_id: `bundle-${index}`,
    // Fixtures number bundles newest-first from 1, so the counting-axis
    // ordinal is simply the zero-based position in that same order.
    ordinal: index - 1,
    event: {
      id: eventId,
      kind: "event",
      source_id: "source-a",
      label: eventId,
      description: "",
      category: "event",
      chunk_id: null,
      start_time: `2026-07-${String(20 - index).padStart(2, "0")}T00:00:00Z`,
      importance: 0.5,
      related_count: 0,
      state: "active",
    },
    nodes: [],
    relations: [],
    neighbor_page: {
      total_unique: 0,
      returned_unique: 0,
      complete: true,
      next_cursor: null,
    },
    cursor_before: cursorBefore,
    cursor_after: cursorAfter,
  };
}

function page(
  direction: "older" | "newer",
  requestCursor: string | null,
  bundles: TimelineBundle[],
): UniverseTimelineSlice {
  const newerCursor = bundles[0]?.cursor_before
    ?? (direction === "older" && bundles.length === 0 ? requestCursor : null);
  const olderCursor = bundles.at(-1)?.cursor_after
    ?? (direction === "newer" && bundles.length === 0 ? requestCursor : null);
  const nextCursor = direction === "older" ? olderCursor : newerCursor;
  return {
    schema_version: 3,
    epoch: 1,
    source_id: "source-a",
    source_revision: "revision-a",
    snapshot_id: "snapshot-a",
    request_direction: direction,
    request_cursor: requestCursor,
    page_id: `${direction}:${requestCursor ?? "root"}`,
    bundles,
    total_events: 40,
    page: {
      returned_bundles: bundles.length,
      returned_unique_nodes: bundles.length,
      returned_relations: 0,
      direction,
      has_newer: newerCursor !== null,
      newer_cursor: newerCursor,
      has_older: olderCursor !== null,
      older_cursor: olderCursor,
      has_more: nextCursor !== null,
      next_cursor: nextCursor,
    },
    as_of: "2026-07-15T00:00:00Z",
  };
}

describe("universe timeline raw deque", () => {
  it("keeps a fixed cache and preserves a checkpoint for the evicted newer edge", () => {
    const root = page("older", null, [
      bundle(1, null, "cursor-1"),
      bundle(2, "cursor-2", "cursor-2"),
      bundle(3, "cursor-3", "cursor-3"),
    ]);
    const initial = admitUniverseTimelineDequePage(null, root, 5);
    const older = page("older", "cursor-3", [
      bundle(4, "cursor-4", "cursor-4"),
      bundle(5, "cursor-5", "cursor-5"),
      bundle(6, "cursor-6", null),
    ]);
    const advanced = admitUniverseTimelineDequePage(initial.deque, older, 5);

    expect(advanced.deque.bundles.map((item) => item.bundle_id)).toEqual([
      "bundle-2",
      "bundle-3",
      "bundle-4",
      "bundle-5",
      "bundle-6",
    ]);
    expect(advanced.evictedBundleIds).toEqual(["bundle-1"]);
    expect(advanced.appendedBundleIds).toEqual([
      "bundle-4",
      "bundle-5",
      "bundle-6",
    ]);
    expect(advanced.prependedBundleIds).toEqual([]);
    expect(advanced.evictedNewerBundleIds).toEqual(["bundle-1"]);
    expect(advanced.evictedOlderBundleIds).toEqual([]);
    expect(advanced.deque).toMatchObject({
      hasNewer: true,
      newerCursor: "cursor-2",
      hasOlder: false,
      olderCursor: null,
    });
  });

  it("loads the newer edge back and retires only the oldest edge", () => {
    const root = admitUniverseTimelineDequePage(null, page("older", null, [
      bundle(2, "cursor-2", "cursor-2"),
      bundle(3, "cursor-3", "cursor-3"),
      bundle(4, "cursor-4", "cursor-4"),
      bundle(5, "cursor-5", "cursor-5"),
      bundle(6, "cursor-6", null),
    ]), 5);
    const newer = page("newer", "cursor-2", [
      bundle(1, null, "cursor-1"),
    ]);
    const restored = admitUniverseTimelineDequePage(root.deque, newer, 5);

    expect(restored.deque.bundles.map((item) => item.bundle_id)).toEqual([
      "bundle-1",
      "bundle-2",
      "bundle-3",
      "bundle-4",
      "bundle-5",
    ]);
    expect(restored.evictedBundleIds).toEqual(["bundle-6"]);
    expect(restored.prependedBundleIds).toEqual(["bundle-1"]);
    expect(restored.appendedBundleIds).toEqual([]);
    expect(restored.evictedNewerBundleIds).toEqual([]);
    expect(restored.evictedOlderBundleIds).toEqual(["bundle-6"]);
    expect(restored.deque.newerCursor).toBeNull();
    expect(restored.deque.olderCursor).toBe("cursor-5");
  });

  it("rejects a page from another snapshot or a non-adjacent edge", () => {
    const initial = admitUniverseTimelineDequePage(null, page("older", null, [
      bundle(1, null, "cursor-1"),
    ]), 4);
    const wrongSnapshot = page("older", "cursor-1", [
      bundle(2, "cursor-2", null),
    ]);
    wrongSnapshot.snapshot_id = "snapshot-b";
    expect(() => admitUniverseTimelineDequePage(
      initial.deque,
      wrongSnapshot,
      4,
    )).toThrow("active snapshot");

    expect(() => admitUniverseTimelineDequePage(
      initial.deque,
      page("older", "wrong-cursor", [bundle(2, "cursor-2", null)]),
      4,
    )).toThrow("not adjacent");
  });

  it("rejects a cursor-adjacent page whose ordinals overlap the cache", () => {
    const initial = admitUniverseTimelineDequePage(null, page("older", null, [
      bundle(1, null, "cursor-1"),
      bundle(2, "cursor-2", "cursor-2"),
    ]), 4);
    // Same cursor seam, but the page claims depths the cache already owns —
    // admitting it would fold two events onto one axis position.
    const overlapping = page("older", "cursor-2", [
      bundle(3, "cursor-3", null),
    ]);
    overlapping.bundles = [{ ...overlapping.bundles[0], ordinal: 1 }];
    expect(() => admitUniverseTimelineDequePage(
      initial.deque,
      overlapping,
      4,
    )).toThrow("ordinals overlap");
  });

  it("rejects a malformed directional edge before mutating the cache", () => {
    const malformed = page("older", null, [
      bundle(1, null, "cursor-1"),
    ]);
    malformed.page.older_cursor = "different-cursor";

    expect(() => admitUniverseTimelineDequePage(null, malformed, 4))
      .toThrow("invalid timeline deque page contract");
  });

  it("acknowledges an exact retry without rewinding either edge", () => {
    const rootPage = page("older", null, [
      bundle(1, null, "cursor-1"),
      bundle(2, "cursor-2", null),
    ]);
    const initial = admitUniverseTimelineDequePage(null, rootPage, 4);
    const retried = admitUniverseTimelineDequePage(initial.deque, rootPage, 4);

    expect(retried.deque).toBe(initial.deque);
    expect(retried.addedBundleIds).toEqual([]);
    expect(retried.duplicateBundleIds).toEqual(["bundle-1", "bundle-2"]);
  });

  it("derives cache capacity from visible, history, and ahead reserves", () => {
    expect(requiredUniverseTimelineCacheBundles(8, 6)).toBe(20);
    expect(requiredUniverseTimelineCacheBundles(8, 6, 1, 2)).toBe(26);
  });

  it("rebases the numerical index while preserving the active bundle identity", () => {
    const initial = admitUniverseTimelineDequePage(null, page("older", null, [
      bundle(1, null, "cursor-1"),
      bundle(2, "cursor-2", "cursor-2"),
      bundle(3, "cursor-3", "cursor-3"),
    ]), 4);
    const older = admitUniverseTimelineDequePage(
      initial.deque,
      page("older", "cursor-3", [
        bundle(4, "cursor-4", "cursor-4"),
        bundle(5, "cursor-5", null),
      ]),
      4,
    );
    const afterOlder = syncUniverseTimelineWindowToDeque(older.deque, {
      activeBundleId: "bundle-3",
      activeIndex: 2,
      visibleLimit: 2,
    });

    expect(afterOlder).toEqual({
      cacheBundleIds: ["bundle-2", "bundle-3", "bundle-4", "bundle-5"],
      activeBundleId: "bundle-3",
      activeIndex: 1,
      activeIndexDelta: -1,
      visibleBundleIds: ["bundle-2", "bundle-3"],
    });

    const newer = admitUniverseTimelineDequePage(
      older.deque,
      page("newer", "cursor-2", [bundle(1, null, "cursor-1")]),
      4,
    );
    const afterNewer = syncUniverseTimelineWindowToDeque(newer.deque, {
      activeBundleId: afterOlder?.activeBundleId ?? null,
      activeIndex: afterOlder?.activeIndex ?? -1,
      visibleLimit: 2,
    });

    expect(afterNewer).toEqual({
      cacheBundleIds: ["bundle-1", "bundle-2", "bundle-3", "bundle-4"],
      activeBundleId: "bundle-3",
      activeIndex: 2,
      activeIndexDelta: 1,
      visibleBundleIds: ["bundle-2", "bundle-3"],
    });
  });

  it("rejects a sync that would silently move focus after active eviction", () => {
    const initial = admitUniverseTimelineDequePage(null, page("older", null, [
      bundle(1, null, "cursor-1"),
      bundle(2, "cursor-2", "cursor-2"),
    ]), 2);
    const advanced = admitUniverseTimelineDequePage(
      initial.deque,
      page("older", "cursor-2", [bundle(3, "cursor-3", null)]),
      2,
    );

    expect(syncUniverseTimelineWindowToDeque(advanced.deque, {
      activeBundleId: "bundle-1",
      activeIndex: 0,
      visibleLimit: 2,
    })).toBeNull();
  });

  it("chooses the normal initial focus only when there is no prior anchor", () => {
    const initial = admitUniverseTimelineDequePage(null, page("older", null, [
      bundle(1, null, "cursor-1"),
      bundle(2, "cursor-2", "cursor-2"),
      bundle(3, "cursor-3", null),
    ]), 4);

    expect(syncUniverseTimelineWindowToDeque(initial.deque, {
      activeBundleId: null,
      activeIndex: -1,
      visibleLimit: 2,
    })).toMatchObject({
      activeBundleId: "bundle-2",
      activeIndex: 1,
      activeIndexDelta: 2,
      visibleBundleIds: ["bundle-1", "bundle-2"],
    });
  });

  it("shrinks around the active time while keeping visible history reloadable", () => {
    const initial = admitUniverseTimelineDequePage(null, page("older", null,
      Array.from({ length: 8 }, (_, index) => bundle(
        index + 1,
        index === 0 ? null : `cursor-${index + 1}`,
        index === 7 ? null : `cursor-${index + 1}`,
      )),
    ), 10);
    const resized = resizeUniverseTimelineDeque(
      initial.deque,
      5,
      "bundle-5",
      2,
      2,
    );

    expect(resized?.deque.bundles.map((item) => item.bundle_id)).toEqual([
      "bundle-2",
      "bundle-3",
      "bundle-4",
      "bundle-5",
      "bundle-6",
    ]);
    expect(resized?.evictedNewerBundleIds).toEqual(["bundle-1"]);
    expect(resized?.evictedOlderBundleIds).toEqual(["bundle-7", "bundle-8"]);
    expect(resized?.deque).toMatchObject({ hasNewer: true, hasOlder: true });
  });
});
