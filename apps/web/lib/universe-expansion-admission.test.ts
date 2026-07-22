import { describe, expect, it } from "vitest";

import type { UniverseGraphPatch, UniversePatchNode } from "./types";
import {
  admitUniverseExpansionPage,
  type UniverseExpansionRequestAnchor,
} from "./universe-expansion-admission";
import { emptyUniverseWorkingSet } from "./universe-working-set";

const sourceId = "source-a";

function node(kind: "event" | "entity", id: string): UniversePatchNode {
  return {
    id,
    kind,
    source_id: sourceId,
    label: id,
    description: "",
    category: kind,
    chunk_id: null,
    start_time: kind === "event" ? "2026-07-14T00:00:00Z" : null,
    importance: 0.7,
    related_count: 4,
    state: "active",
  };
}

function eventExpansion(cursor: string | null = null): UniverseGraphPatch {
  return {
    schema_version: 2,
    epoch: 4,
    source_id: sourceId,
    source_revision: "revision-1",
    snapshot_id: "snapshot-1",
    request_cursor: cursor,
    page_id: cursor ? "page-2" : "page-1",
    bundle_id: cursor ? "bundle-event-page-2" : "bundle-event-page-1",
    anchor: node("event", "event-1"),
    nodes: [node("entity", cursor ? "entity-2" : "entity-1")],
    relations: [{
      source_id: sourceId,
      from_id: "event-1",
      to_id: cursor ? "entity-2" : "entity-1",
      kind: "mentions",
      weight: 1,
      description: "",
    }],
    page: {
      returned: 1,
      has_more: cursor === null,
      next_cursor: cursor === null ? "cursor-1" : null,
    },
    as_of: "2026-07-14T00:00:00Z",
  };
}

function expected(
  overrides: Partial<UniverseExpansionRequestAnchor> = {},
): UniverseExpansionRequestAnchor {
  return {
    epoch: 4,
    sourceId,
    nodeKind: "event",
    nodeId: "event-1",
    lineageRootKey: "source-a:event:timeline-root",
    requestCursor: null,
    snapshotId: "snapshot-1",
    sourceRevision: "revision-1",
    asOf: "2026-07-14T00:00:00Z",
    ...overrides,
  };
}

describe("expansion page admission", () => {
  it("admits a source- and snapshot-bound event page atomically", () => {
    const result = admitUniverseExpansionPage(
      emptyUniverseWorkingSet(4),
      eventExpansion(),
      expected(),
      { nodes: 8, edges: 8 },
      1,
    );

    expect(result.accepted).toBe(true);
    expect(result.committed).toBe(true);
    expect(result.nextCursor).toBe("cursor-1");
    expect(result.workingSet.bundle_order).toEqual(["bundle-event-page-1"]);
    expect(result.workingSet.bundles["bundle-event-page-1"]).toMatchObject({
      origin: "expansion",
      anchor_key: "source-a:event:event-1",
      lineage_root_key: "source-a:event:timeline-root",
      request_cursor: null,
      next_cursor: "cursor-1",
    });
  });

  it("counts only primary events for an entity expansion", () => {
    const page = eventExpansion();
    page.anchor = node("entity", "entity-anchor");
    page.nodes = [
      node("event", "event-1"),
      node("event", "event-2"),
      node("entity", "entity-context"),
    ];
    page.relations = [
      {
        source_id: sourceId,
        from_id: "event-1",
        to_id: "entity-anchor",
        kind: "mentions",
        weight: 1,
        description: "",
      },
      {
        source_id: sourceId,
        from_id: "event-1",
        to_id: "entity-context",
        kind: "mentions",
        weight: 0.5,
        description: "",
      },
      {
        source_id: sourceId,
        from_id: "event-2",
        to_id: "entity-anchor",
        kind: "mentions",
        weight: 1,
        description: "",
      },
    ];
    page.page.returned = 2;
    page.page.has_more = false;
    page.page.next_cursor = null;
    page.bundle_id = "bundle-entity-page-1";

    const result = admitUniverseExpansionPage(
      emptyUniverseWorkingSet(4),
      page,
      expected({ nodeKind: "entity", nodeId: "entity-anchor" }),
      { nodes: 8, edges: 8 },
      1,
    );

    expect(result.accepted).toBe(true);
    expect(result.done).toBe(true);
    expect(result.workingSet.nodes).toHaveLength(4);
    expect(result.workingSet.bundles["bundle-entity-page-1"]).toMatchObject({
      origin: "expansion",
      anchor_key: "source-a:entity:entity-anchor",
      lineage_root_key: "source-a:event:timeline-root",
      request_cursor: null,
      next_cursor: null,
    });
  });

  it("records continuation cursors as replay-safe bundle metadata", () => {
    const page = eventExpansion("cursor-1");
    const result = admitUniverseExpansionPage(
      emptyUniverseWorkingSet(4),
      page,
      expected({ requestCursor: "cursor-1" }),
      { nodes: 8, edges: 8 },
      1,
    );

    expect(result.workingSet.bundles["bundle-event-page-2"]).toMatchObject({
      origin: "expansion",
      anchor_key: "source-a:event:event-1",
      lineage_root_key: "source-a:event:timeline-root",
      request_cursor: "cursor-1",
      next_cursor: null,
    });
  });

  it("rejects a response from another snapshot before admission", () => {
    const page = eventExpansion();
    page.snapshot_id = "snapshot-other";

    expect(() => admitUniverseExpansionPage(
      emptyUniverseWorkingSet(4),
      page,
      expected(),
      { nodes: 8, edges: 8 },
    )).toThrow("invalid expansion bundle contract");
  });

  it("rejects a response whose as-of boundary changed inside one snapshot", () => {
    const page = eventExpansion();
    page.as_of = "2026-07-15T00:00:00Z";

    expect(() => admitUniverseExpansionPage(
      emptyUniverseWorkingSet(4),
      page,
      expected(),
      { nodes: 8, edges: 8 },
    )).toThrow("invalid expansion bundle contract");
  });

  it("rejects a continuation cursor that does not advance", () => {
    const page = eventExpansion("cursor-1");
    page.page.has_more = true;
    page.page.next_cursor = "cursor-1";

    expect(() => admitUniverseExpansionPage(
      emptyUniverseWorkingSet(4),
      page,
      expected({ requestCursor: "cursor-1" }),
      { nodes: 8, edges: 8 },
    )).toThrow("invalid expansion bundle contract");
  });

  it.each([undefined, ""])(
    "rejects a missing or empty next cursor when expansion declares more pages: %s",
    (invalidCursor) => {
      const page = eventExpansion();
      page.page.next_cursor = invalidCursor as unknown as string;

      expect(() => admitUniverseExpansionPage(
        emptyUniverseWorkingSet(4),
        page,
        expected(),
        { nodes: 8, edges: 8 },
      )).toThrow("invalid expansion bundle contract");
    },
  );

  it("rejects a node that is not connected by a factual relation", () => {
    const page = eventExpansion();
    page.nodes.push(node("entity", "entity-dangling"));
    page.page.returned = 2;

    expect(() => admitUniverseExpansionPage(
      emptyUniverseWorkingSet(4),
      page,
      expected(),
      { nodes: 8, edges: 8 },
    )).toThrow("invalid expansion bundle contract");
  });

  it("rejects a relation whose endpoint escapes the returned closure", () => {
    const page = eventExpansion();
    page.relations[0].to_id = "entity-other";

    expect(() => admitUniverseExpansionPage(
      emptyUniverseWorkingSet(4),
      page,
      expected(),
      { nodes: 8, edges: 8 },
    )).toThrow("invalid expansion bundle contract");
  });

  it("acknowledges an exact retry but rejects a bundle-id content collision", () => {
    const page = eventExpansion();
    const first = admitUniverseExpansionPage(
      emptyUniverseWorkingSet(4),
      page,
      expected(),
      { nodes: 8, edges: 8 },
      1,
    );
    const retry = admitUniverseExpansionPage(
      first.workingSet,
      page,
      expected(),
      { nodes: 8, edges: 8 },
      2,
    );
    const collision = structuredClone(page);
    collision.nodes[0].label = "changed";
    const rejected = admitUniverseExpansionPage(
      first.workingSet,
      collision,
      expected(),
      { nodes: 8, edges: 8 },
      3,
    );

    expect(retry.accepted).toBe(true);
    expect(retry.committed).toBe(false);
    expect(rejected.accepted).toBe(false);
    expect(rejected.reason).toBe("duplicate_bundle");
  });
});
