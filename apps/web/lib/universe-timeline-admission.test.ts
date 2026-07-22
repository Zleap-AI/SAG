import { describe, expect, it } from "vitest";

import type { UniverseTimelineSlice } from "./types";
import { admitUniverseTimelinePage } from "./universe-timeline-admission";
import { emptyUniverseWorkingSet } from "./universe-working-set";

function timelinePage(secondEntityCount = 1): UniverseTimelineSlice {
  const firstEntity = {
    id: "entity-shared",
    kind: "entity" as const,
    source_id: "source-a",
    label: "共享实体",
    description: "",
    category: "实体",
    chunk_id: null,
    start_time: null,
    importance: 0.5,
    related_count: 2,
    state: "active" as const,
  };
  const event = (id: string, relatedCount = 1) => ({
    id,
    kind: "event" as const,
    source_id: "source-a",
    label: id,
    description: "",
    category: "事件",
    chunk_id: null,
    start_time: "2026-07-14T00:00:00Z",
    importance: 0.8,
    related_count: relatedCount,
    state: "active" as const,
  });
  const secondEntities = Array.from({ length: secondEntityCount }, (_, index) => ({
    ...firstEntity,
    id: `entity-${index + 2}`,
    label: `实体 ${index + 2}`,
  }));
  const bundles: UniverseTimelineSlice["bundles"] = [
    {
      bundle_id: "bundle-1",
      ordinal: 0,
      event: event("event-1"),
      nodes: [firstEntity],
      relations: [{
        source_id: "source-a",
        from_id: "event-1",
        to_id: firstEntity.id,
        kind: "mentions",
        weight: 1,
        description: "",
      }],
      neighbor_page: {
        total_unique: 1,
        returned_unique: 1,
        complete: true,
        next_cursor: null,
      },
      cursor_before: null,
      cursor_after: "cursor-1",
    },
    {
      bundle_id: "bundle-2",
      ordinal: 1,
      event: event("event-2", secondEntities.length),
      nodes: secondEntities,
      relations: secondEntities.map((entity) => ({
        source_id: "source-a",
        from_id: "event-2",
        to_id: entity.id,
        kind: "mentions" as const,
        weight: 1,
        description: "",
      })),
      neighbor_page: {
        total_unique: secondEntities.length,
        returned_unique: secondEntities.length,
        complete: true,
        next_cursor: null,
      },
      cursor_before: "cursor-2",
      cursor_after: "cursor-2",
    },
  ];
  const uniqueNodes = new Set(
    bundles.flatMap((bundle) => [bundle.event, ...bundle.nodes])
      .map((node) => `${node.kind}:${node.id}`),
  ).size;
  return {
    schema_version: 3,
    epoch: 12,
    source_id: "source-a",
    source_revision: "revision-1",
    snapshot_id: "snapshot-1",
    request_direction: "older",
    request_cursor: null,
    page_id: "page-1",
    bundles,
    total_events: 12,
    page: {
      returned_bundles: bundles.length,
      returned_unique_nodes: uniqueNodes,
      returned_relations: bundles.reduce(
        (total, bundle) => total + bundle.relations.length,
        0,
      ),
      direction: "older",
      has_newer: false,
      newer_cursor: null,
      has_older: true,
      older_cursor: "cursor-2",
      has_more: true,
      next_cursor: "cursor-2",
    },
    as_of: "2026-07-14T00:00:00Z",
  };
}

describe("timeline bundle admission", () => {
  it("commits a complete page and advances to its declared cursor", () => {
    const result = admitUniverseTimelinePage(
      emptyUniverseWorkingSet(12),
      timelinePage(),
      { nodes: 8, edges: 8 },
      1,
    );

    expect(result.pageAcknowledged).toBe(true);
    expect(result.acknowledgedBundleIds).toEqual(["bundle-1", "bundle-2"]);
    expect(result.nextCursor).toBe("cursor-2");
    expect(result.done).toBe(false);
    expect(result.workingSet.bundles["bundle-1"]).toMatchObject({
      id: "bundle-1",
      origin: "timeline",
    });
    expect(result.workingSet.bundles["bundle-2"]).toMatchObject({
      id: "bundle-2",
      origin: "timeline",
    });
  });

  it("never advances past the first bundle that cannot fit atomically", () => {
    const result = admitUniverseTimelinePage(
      emptyUniverseWorkingSet(12),
      timelinePage(3),
      { nodes: 3, edges: 3 },
      1,
    );

    expect(result.pageAcknowledged).toBe(false);
    expect(result.acknowledgedBundleIds).toEqual(["bundle-1"]);
    expect(result.rejectedBundleId).toBe("bundle-2");
    expect(result.nextCursor).toBe("cursor-1");
    expect(result.workingSet.nodes.map((node) => node.id).sort()).toEqual([
      "entity-shared",
      "event-1",
    ]);
  });

  it("acknowledges an idempotent retry without duplicating residents", () => {
    const page = timelinePage();
    const first = admitUniverseTimelinePage(
      emptyUniverseWorkingSet(12),
      page,
      { nodes: 8, edges: 8 },
      1,
    );
    const retry = admitUniverseTimelinePage(
      first.workingSet,
      page,
      { nodes: 8, edges: 8 },
      2,
    );

    expect(retry.pageAcknowledged).toBe(true);
    expect(retry.committedBundleIds).toEqual([]);
    expect(retry.nextCursor).toBe("cursor-2");
    expect(retry.workingSet.nodes).toHaveLength(first.workingSet.nodes.length);
  });

  it("rejects a malformed count contract before mutating the graph", () => {
    const page = timelinePage();
    page.page.returned_relations += 1;

    expect(() => admitUniverseTimelinePage(
      emptyUniverseWorkingSet(12),
      page,
      { nodes: 8, edges: 8 },
      1,
    )).toThrow("invalid timeline bundle contract");
  });

  it("rejects a bundle whose factual edge escapes its event neighborhood", () => {
    const page = timelinePage();
    page.bundles[0].relations[0].to_id = "entity-not-returned";

    expect(() => admitUniverseTimelinePage(
      emptyUniverseWorkingSet(12),
      page,
      { nodes: 8, edges: 8 },
      1,
    )).toThrow("invalid timeline bundle contract");
  });

  it("rejects inconsistent neighbor completeness", () => {
    const page = timelinePage();
    page.bundles[0].neighbor_page.total_unique = 2;

    expect(() => admitUniverseTimelinePage(
      emptyUniverseWorkingSet(12),
      page,
      { nodes: 8, edges: 8 },
      1,
    )).toThrow("invalid timeline bundle contract");
  });

  it("rejects a cursor reused by two different bundles", () => {
    const page = timelinePage();
    page.bundles[1].cursor_after = "cursor-1";
    page.page.next_cursor = "cursor-1";

    expect(() => admitUniverseTimelinePage(
      emptyUniverseWorkingSet(12),
      page,
      { nodes: 8, edges: 8 },
      1,
    )).toThrow("invalid timeline bundle contract");
  });

  it("rejects a continuation page whose cursor does not advance", () => {
    const page = timelinePage();
    page.request_cursor = "cursor-2";

    expect(() => admitUniverseTimelinePage(
      emptyUniverseWorkingSet(12),
      page,
      { nodes: 8, edges: 8 },
      1,
    )).toThrow("invalid timeline bundle contract");
  });

  it("requires a continuation cursor for an incomplete event neighborhood", () => {
    const page = timelinePage();
    page.bundles[0].neighbor_page.total_unique = 2;
    page.bundles[0].event.related_count = 2;
    page.bundles[0].neighbor_page.complete = false;

    expect(() => admitUniverseTimelinePage(
      emptyUniverseWorkingSet(12),
      page,
      { nodes: 8, edges: 8 },
      1,
    )).toThrow("invalid timeline bundle contract");
  });

  it.each([undefined, ""])(
    "rejects a missing or empty cursor for an incomplete event neighborhood: %s",
    (invalidCursor) => {
      const page = timelinePage();
      page.bundles[0].neighbor_page.total_unique = 2;
      page.bundles[0].event.related_count = 2;
      page.bundles[0].neighbor_page.complete = false;
      page.bundles[0].neighbor_page.next_cursor = invalidCursor as unknown as string;

      expect(() => admitUniverseTimelinePage(
        emptyUniverseWorkingSet(12),
        page,
        { nodes: 8, edges: 8 },
        1,
      )).toThrow("invalid timeline bundle contract");
    },
  );
});
