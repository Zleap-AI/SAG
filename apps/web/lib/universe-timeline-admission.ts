import type { UniversePatchNode, UniverseTimelineSlice } from "./types";
import {
  admitUniverseBundles,
  type AdmitUniverseBundleOptions,
  type UniverseWorkingSet,
} from "./universe-working-set";

export interface UniverseTimelinePageAdmission {
  workingSet: UniverseWorkingSet;
  acknowledgedBundleIds: string[];
  committedBundleIds: string[];
  evictedBundleIds: string[];
  committedNodes: UniversePatchNode[];
  nextCursor: string | null;
  pageAcknowledged: boolean;
  done: boolean;
  rejectedBundleId: string | null;
  rejectionReason?: string;
}

function validOpaqueValue(value: unknown, maxLength: number) {
  return typeof value === "string" && value.length > 0 && value.length <= maxLength;
}

function validNullableCursor(value: unknown): value is string | null {
  return value === null || validOpaqueValue(value, 2048);
}

function validTimestamp(value: unknown) {
  return typeof value === "string"
    && value.length > 0
    && Number.isFinite(Date.parse(value));
}

function uniqueNodeCount(page: UniverseTimelineSlice) {
  const keys = new Set<string>();
  page.bundles.forEach((bundle) => {
    keys.add(`${bundle.event.kind}:${bundle.event.id}`);
    bundle.nodes.forEach((node) => keys.add(`${node.kind}:${node.id}`));
  });
  return keys.size;
}

function validBundleContract(
  bundle: UniverseTimelineSlice["bundles"][number],
  sourceId: string,
) {
  const entityIds = bundle.nodes.map((node) => node.id);
  const uniqueEntityIds = new Set(entityIds);
  const relationKeys = bundle.relations.map((relation) =>
    `${relation.kind}:${relation.from_id}:${relation.to_id}`);
  const relatedEntityIds = new Set(
    bundle.relations.map((relation) => relation.to_id),
  );
  const neighborPage = bundle.neighbor_page;
  return validOpaqueValue(bundle.bundle_id, 512)
    && validNullableCursor(bundle.cursor_before)
    && validNullableCursor(bundle.cursor_after)
    && validNullableCursor(neighborPage.next_cursor)
    && bundle.event.kind === "event"
    && bundle.event.source_id === sourceId
    && bundle.nodes.every((node) =>
      node.kind === "entity" && node.source_id === sourceId)
    && uniqueEntityIds.size === entityIds.length
    && bundle.relations.every((relation) =>
      relation.source_id === sourceId
      && relation.kind === "mentions"
      && relation.from_id === bundle.event.id
      && uniqueEntityIds.has(relation.to_id))
    && new Set(relationKeys).size === relationKeys.length
    && relatedEntityIds.size === uniqueEntityIds.size
    && [...uniqueEntityIds].every((id) => relatedEntityIds.has(id))
    && Number.isInteger(neighborPage.total_unique)
    && Number.isInteger(neighborPage.returned_unique)
    && neighborPage.total_unique >= 0
    && neighborPage.returned_unique === uniqueEntityIds.size
    && bundle.event.related_count === neighborPage.total_unique
    && neighborPage.total_unique >= neighborPage.returned_unique
    && neighborPage.complete === (
      neighborPage.returned_unique === neighborPage.total_unique
    )
    && neighborPage.complete === (neighborPage.next_cursor === null);
}

/**
 * Validates and atomically acknowledges the longest safely processed prefix
 * of one timeline page. Older acknowledged bundles may already have left the
 * FIFO window; the returned cursor never crosses the first rejected bundle.
 */
export function admitUniverseTimelinePage(
  current: UniverseWorkingSet,
  page: UniverseTimelineSlice,
  budget: { nodes: number; edges: number },
  now = Date.now(),
  options: AdmitUniverseBundleOptions = {},
): UniverseTimelinePageAdmission {
  const bundleIds = page.bundles.map((bundle) => bundle.bundle_id);
  const relationCount = page.bundles.reduce(
    (total, bundle) => total + bundle.relations.length,
    0,
  );
  const lastBundle = page.bundles.at(-1);
  const firstBundle = page.bundles[0];
  const eventIds = page.bundles.map((bundle) => bundle.event.id);
  const afterCursors = page.bundles
    .map((bundle) => bundle.cursor_after)
    .filter((cursor): cursor is string => cursor !== null);
  const beforeCursors = page.bundles
    .map((bundle) => bundle.cursor_before)
    .filter((cursor): cursor is string => cursor !== null);
  const directionalCursor = page.request_direction === "older"
    ? page.page.older_cursor
    : page.page.newer_cursor;
  const invalidContract = page.schema_version !== 3
    || !validOpaqueValue(page.source_id, 64)
    || !validOpaqueValue(page.source_revision, 128)
    || !validOpaqueValue(page.snapshot_id, 2048)
    || !validOpaqueValue(page.page_id, 128)
    || !validTimestamp(page.as_of)
    || !Number.isInteger(page.total_events)
    || page.total_events < 0
    || page.bundles.some((bundle, index) =>
      !Number.isInteger(bundle.ordinal)
      || bundle.ordinal < 0
      || bundle.ordinal >= page.total_events
      || (index > 0 && bundle.ordinal <= page.bundles[index - 1].ordinal))
    || !["older", "newer"].includes(page.request_direction)
    || page.page.direction !== page.request_direction
    || !validNullableCursor(page.request_cursor)
    || !validNullableCursor(page.page.newer_cursor)
    || !validNullableCursor(page.page.older_cursor)
    || !validNullableCursor(page.page.next_cursor)
    || page.page.returned_bundles !== page.bundles.length
    || page.page.returned_unique_nodes !== uniqueNodeCount(page)
    || page.page.returned_relations !== relationCount
    || new Set(bundleIds).size !== bundleIds.length
    || new Set(eventIds).size !== eventIds.length
    || new Set(afterCursors).size !== afterCursors.length
    || new Set(beforeCursors).size !== beforeCursors.length
    || (page.request_direction === "newer" && page.request_cursor === null)
    || (page.bundles.length === 0 && page.page.has_more)
    || (page.bundles.length > 0
      && firstBundle?.cursor_before !== page.page.newer_cursor)
    || (page.bundles.length > 0
      && lastBundle?.cursor_after !== page.page.older_cursor)
    || page.page.has_newer !== (page.page.newer_cursor !== null)
    || page.page.has_older !== (page.page.older_cursor !== null)
    || page.page.has_more !== (directionalCursor !== null)
    || page.page.next_cursor !== directionalCursor
    || (page.request_cursor !== null && page.request_cursor === directionalCursor)
    || page.bundles.some((bundle, index) =>
      (!validBundleContract(bundle, page.source_id)
        || (index > 0 && !bundle.cursor_before)
        || (index < page.bundles.length - 1 && !bundle.cursor_after)));
  if (invalidContract) {
    throw new Error("invalid timeline bundle contract");
  }

  const admission = admitUniverseBundles(
    current,
    page.bundles.map((bundle) => ({
      id: bundle.bundle_id,
      origin: "timeline" as const,
      ordinal: bundle.ordinal,
      epoch: page.epoch,
      source_id: page.source_id,
      nodes: [bundle.event, ...bundle.nodes],
      relations: bundle.relations,
    })),
    budget,
    now,
    options,
  );
  const acknowledgedCount = admission.acknowledgedBundleIds.length;
  const acknowledgedBundles = page.bundles.slice(0, acknowledgedCount);
  const pageAcknowledged = admission.rejectedBundleId === null
    && acknowledgedCount === page.bundles.length;
  return {
    workingSet: admission.workingSet,
    acknowledgedBundleIds: admission.acknowledgedBundleIds,
    committedBundleIds: admission.committedBundleIds,
    evictedBundleIds: admission.evictedBundleIds,
    committedNodes: acknowledgedBundles.flatMap((bundle) => [
      bundle.event,
      ...bundle.nodes,
    ]),
    nextCursor: pageAcknowledged
      ? page.page.next_cursor
      : acknowledgedBundles.at(-1)?.cursor_after ?? page.request_cursor,
    pageAcknowledged,
    done: pageAcknowledged && !page.page.has_more,
    rejectedBundleId: admission.rejectedBundleId,
    rejectionReason: admission.reason,
  };
}
