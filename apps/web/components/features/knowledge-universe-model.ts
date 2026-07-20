import type {
  UniverseActivation,
  UniverseNodeKind,
  UniverseTimelineDirection,
} from "@/lib/types";
import {
  createUniverseTimelineWindow,
  type UniverseTimelineWindowState,
} from "@/lib/universe-timeline-window";
import {
  syncUniverseTimelineWindowToDeque,
  type UniverseTimelineDeque,
  type UniverseTimelineDequeAdmission,
} from "@/lib/universe-timeline-deque";
import { UNIVERSE_TEMPORAL_AXIS_UNITS_PER_EVENT } from "@/lib/universe-temporal-axis";
import {
  emptyUniverseWorkingSet,
  universeNodeKey,
  type UniverseWorkingSet,
} from "@/lib/universe-working-set";
import type {
  UniverseSceneData,
  UniverseSceneLink,
  UniverseSceneNode,
} from "@/components/features/universe-scene-contract";

export interface Universe3DNode extends UniverseSceneNode {
  root: boolean;
}

export type UniverseConcrete3DNode = Universe3DNode & {
  kind: "event" | "entity";
};

export type Universe3DLink = UniverseSceneLink;

export interface ActivationSummary {
  query: string;
  events: number;
  entities: number;
  relations: number;
}

export interface Position3D {
  x: number;
  y: number;
  z: number;
}

export interface SatelliteSlot {
  index: number;
  total: number;
  phaseKey: string;
}

export interface SourceTimelinePageState {
  deque: UniverseTimelineDeque | null;
  snapshotId: string | null;
  sourceRevision: string | null;
  asOf: string | null;
  /** Snapshot-stable event total: the counting axis' length. */
  totalEvents: number | null;
  /** Stable network page size for the lifetime of one source snapshot. */
  queryPageSize: number | null;
  preferredDirection: UniverseTimelineDirection;
  loading: boolean;
  pausedReason: "capacity" | null;
  window: UniverseTimelineWindowState;
}

export type SourceTimelineLoadCause = "source-entry" | "prefetch" | "journey";
export type SourceTimelineLoadResult = "blocked" | "loaded";

export interface SourceTimelineRequest {
  sourceId: string;
  cause: SourceTimelineLoadCause;
  direction: UniverseTimelineDirection;
  controller: AbortController;
  promise: Promise<SourceTimelineLoadResult>;
}

export interface SourceBrowseSession {
  sourceId: string;
  working: UniverseWorkingSet;
  timeline: SourceTimelinePageState;
}

export const PARTITION_RENDER_LIMIT = { desktop: 160, mobile: 64 } as const;
export const EVENT_ENTITY_PROJECTION_LIMIT = 8;
export const ENTITY_EXPANSION_EVENT_LIMIT = 4;

// Layout policy belongs to the projection model, not the React coordinator.
// The temporal axis already supplies a normalized spiral. Keep its world-space
// projection inside one readable corridor, then give each event a restrained
// local relation cluster. Perspective owns the centre-first transition, while
// this middle-width corridor keeps a surrounding outer layer in view: an
// approaching package forms near the source, grows through the reading field,
// then fans toward (but does not immediately stick to) the viewport edge.
export const TIMELINE_EVENT_LATERAL_SPREAD = 6.4;
export const LOCAL_ENTITY_SPREAD_MIN = 112;
export const LOCAL_ENTITY_SPREAD_RANGE = 88;
export const EMPTY_TIMELINE_BUNDLE_IDS: string[] = [];
export const TEMPORAL_AXIS_UNITS_PER_EVENT = UNIVERSE_TEMPORAL_AXIS_UNITS_PER_EVENT;

export function emptySourceTimelinePageState(
  eventWindowSize: number,
  cacheCapacity: number,
): SourceTimelinePageState {
  return {
    deque: null,
    snapshotId: null,
    sourceRevision: null,
    asOf: null,
    totalEvents: null,
    queryPageSize: null,
    preferredDirection: "older",
    loading: false,
    pausedReason: null,
    window: createUniverseTimelineWindow(
      eventWindowSize,
      cacheCapacity,
    ),
  };
}

export function emptySourceBrowseSession(
  epoch: number,
  sourceId: string,
  eventWindowSize: number,
  cacheCapacity: number,
): SourceBrowseSession {
  return {
    sourceId,
    working: emptyUniverseWorkingSet(epoch),
    timeline: emptySourceTimelinePageState(
      eventWindowSize,
      cacheCapacity,
    ),
  };
}

export function synchronizeTimelineWindowWithDeque(
  current: UniverseTimelineWindowState,
  previousDeque: UniverseTimelineDeque | null,
  admission: UniverseTimelineDequeAdmission,
) {
  const activeBundleId = current.activeIndex >= 0
    ? current.cacheBundleIds[current.activeIndex] ?? null
    : null;
  const anchor = syncUniverseTimelineWindowToDeque(admission.deque, {
    activeBundleId,
    activeIndex: current.activeIndex,
    visibleLimit: current.visibleLimit,
  });
  if (!anchor) return null;

  const initial = previousDeque === null;
  const cacheStartOffset = initial
    ? 0
    : Math.max(
        0,
        current.cacheStartOffset
          + admission.evictedNewerBundleIds.length
          - admission.prependedBundleIds.length,
      );
  const rewindStartOffset = initial
    ? cacheStartOffset + anchor.activeIndex
    : current.rewindStartOffset;
  const networkExhausted = !admission.deque.hasOlder;
  const atOlderEdge = anchor.activeIndex === anchor.cacheBundleIds.length - 1;
  return {
    ...current,
    cacheBundleIds: anchor.cacheBundleIds,
    activeIndex: anchor.activeIndex,
    visibleBundleIds: anchor.visibleBundleIds,
    visitedCount: Math.max(
      current.visitedCount,
      cacheStartOffset + anchor.activeIndex + 1,
    ),
    queriedCount: Math.max(
      current.queriedCount,
      cacheStartOffset + anchor.cacheBundleIds.length,
    ),
    networkExhausted,
    phase: networkExhausted && atOlderEdge ? "complete" as const : current.phase,
    revision: current.revision + 1,
    cacheLimit: Math.max(current.visibleLimit, current.cacheLimit),
    cacheStartOffset,
    rewindStartOffset,
  };
}

export function universeExpansionCacheKey(
  epoch: number,
  sourceId: string,
  sourceRevision: string,
  snapshotId: string,
  kind: UniverseNodeKind,
  nodeId: string,
  cursor: string | null,
) {
  return [
    epoch,
    sourceId,
    sourceRevision,
    snapshotId,
    kind,
    nodeId,
    cursor ?? "root",
  ].join(":");
}

export function waitForAbortableDelay(duration: number, signal: AbortSignal) {
  if (signal.aborted) return Promise.resolve();
  return new Promise<void>((resolve) => {
    const finish = () => {
      globalThis.clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    };
    const timer = globalThis.setTimeout(finish, duration);
    signal.addEventListener("abort", finish, { once: true });
  });
}

export function stableUnit(value: string) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) / 0xffffffff;
}

export function stableOffset(key: string, radius: number): Position3D {
  const azimuth = stableUnit(`${key}:azimuth`) * Math.PI * 2;
  const vertical = stableUnit(`${key}:vertical`) * 2 - 1;
  const planar = Math.sqrt(Math.max(0, 1 - vertical * vertical));
  const distance = radius * (0.46 + stableUnit(`${key}:distance`) * 0.5);
  return {
    x: Math.cos(azimuth) * planar * distance,
    y: Math.sin(azimuth) * planar * distance,
    z: vertical * distance,
  };
}

/**
 * Entity satellites stay in their event's local screen plane. They may fan
 * around the event, but never jump several 60-unit temporal slices and create
 * long diagonal relations that visually break chronology.
 */
export function stableSatelliteOffset(
  key: string,
  radius: number,
  _parentRadial?: Position3D,
  slot?: SatelliteSlot,
): Position3D {
  const total = Math.max(1, Math.floor(slot?.total ?? 1));
  const index = Math.max(0, Math.floor(slot?.index ?? 0)) % total;
  const phaseKey = slot?.phaseKey ?? key;
  const phase = stableUnit(`${phaseKey}:satellite-phase`) * Math.PI * 2;
  const angle = slot
    ? phase + index * (Math.PI * 2 / total)
    : stableUnit(`${key}:satellite-angle`) * Math.PI * 2;
  const distance = radius * (0.78 + stableUnit(`${key}:satellite-distance`) * 0.22);
  return {
    // A package is one small, deterministic radial graph. Slots are stable
    // across pagination, evenly distributed, and independent of camera state;
    // hover therefore only reveals the graph and never causes it to reflow.
    x: Math.cos(angle) * distance,
    y: Math.sin(angle) * distance,
    z: (stableUnit(`${key}:satellite-depth`) - 0.5) * 20,
  };
}

export function stableRootEventOffset(
  sourceId: string,
  key: string,
  radius: number,
  index: number,
  total: number,
): Position3D {
  const count = Math.max(1, total);
  const progress = (index + 0.65) / count;
  // Existing roots occupy the outer field; the latest admitted root is born
  // nearer the luminous centre. This keeps the centre available for continued
  // exploration without making settled content jump on every frame.
  const distance = radius * (0.72 + Math.sqrt(1 - progress) * 1.28);
  const goldenAngle = Math.PI * (3 - Math.sqrt(5));
  const phase = stableUnit(`${sourceId}:root-event-phase`) * Math.PI * 2;
  const angle = phase + index * goldenAngle;
  return {
    x: Math.cos(angle) * distance,
    y: Math.sin(angle) * distance * 0.82,
    z: (stableUnit(`${key}:root-event-depth`) - 0.5) * radius * 0.18,
  };
}

/**
 * Places accumulated evidence in one stable 3D field. The coordinate depends
 * only on the canonical node key, so adding another answer never reorders or
 * shifts the clues the user has already seen.
 */
export function stableAccumulationEventOffset(key: string): Position3D {
  const azimuth = stableUnit(`${key}:accumulation-azimuth`) * Math.PI * 2;
  const vertical = stableUnit(`${key}:accumulation-vertical`) * 1.5 - 0.75;
  const planar = Math.sqrt(Math.max(0, 1 - vertical * vertical));
  const distance = 76 + stableUnit(`${key}:accumulation-radius`) * 112;
  return {
    x: Math.cos(azimuth) * planar * distance,
    y: Math.sin(azimuth) * planar * distance * 0.86,
    z: vertical * distance * 0.72,
  };
}

function lineageQualifiedExpansionBundleIds(
  current: UniverseWorkingSet,
  seedNodeKeys: Iterable<string>,
) {
  const roots = new Set(seedNodeKeys);
  const supportIds = new Set<string>();
  current.bundle_order.forEach((id) => {
    const bundle = current.bundles[id];
    if (
      bundle?.origin === "expansion"
      && bundle.lineage_root_key
      && roots.has(bundle.lineage_root_key)
    ) supportIds.add(id);
  });
  return supportIds;
}

export function timelineProjectionBundleIds(
  current: UniverseWorkingSet,
  timelineBundleIds: readonly string[],
  visibleTimelineBundleIds: readonly string[],
) {
  const timelineIds = new Set(timelineBundleIds);
  const visibleIds = new Set(visibleTimelineBundleIds);
  const availableNodeKeys = new Set(current.nodes.map((node) =>
    universeNodeKey(node.kind, node.id, node.source_id)));
  const visibleNodeKeys = new Set<string>(
    visibleTimelineBundleIds
      .flatMap((id) => current.bundles[id]?.node_keys ?? [])
      .filter((key) => availableNodeKeys.has(key)),
  );
  const supportIds = lineageQualifiedExpansionBundleIds(current, visibleNodeKeys);
  return current.bundle_order.filter((id) => {
    if (visibleIds.has(id)) return true;
    if (timelineIds.has(id)) return false;
    return supportIds.has(id);
  });
}

export function expansionLineageRootKey(
  current: UniverseWorkingSet,
  timelineBundleIds: readonly string[],
  anchorKey: string,
) {
  const timelineNodeKeys = new Set(
    timelineBundleIds.flatMap((id) => current.bundles[id]?.node_keys ?? []),
  );
  if (timelineNodeKeys.has(anchorKey)) return anchorKey;
  for (let index = current.bundle_order.length - 1; index >= 0; index -= 1) {
    const bundle = current.bundles[current.bundle_order[index]];
    if (
      bundle?.origin === "expansion"
      && bundle.node_keys.includes(anchorKey)
      && bundle.lineage_root_key
      && timelineNodeKeys.has(bundle.lineage_root_key)
    ) return bundle.lineage_root_key;
  }
  return null;
}

export function timelineRetentionBundleIds(
  current: UniverseWorkingSet,
  timelineBundleIds: readonly string[],
) {
  const timelineIds = new Set(timelineBundleIds);
  const timelineNodeKeys = new Set(
    timelineBundleIds.flatMap((id) => current.bundles[id]?.node_keys ?? []),
  );
  const supportIds = lineageQualifiedExpansionBundleIds(current, timelineNodeKeys);
  const anchoredSupportIds = current.bundle_order.filter((id) =>
    !timelineIds.has(id) && supportIds.has(id));
  return [...new Set([...timelineBundleIds, ...anchoredSupportIds])];
}

export function universeBundleWindowProtection(
  current: UniverseWorkingSet,
  bundleIds: readonly string[],
) {
  const nodeKeys = new Set<string>();
  const relationKeys = new Set<string>();
  bundleIds.forEach((id) => {
    const bundle = current.bundles[id];
    bundle?.node_keys.forEach((key) => nodeKeys.add(key));
    bundle?.relation_keys.forEach((key) => relationKeys.add(key));
  });
  return { nodeKeys: [...nodeKeys], relationKeys: [...relationKeys] };
}

export function universeSceneDataSignature(data: UniverseSceneData) {
  return JSON.stringify({
    epoch: data.epoch,
    windowRevision: data.windowRevision ?? 0,
    windowChangeCause: data.windowChangeCause ?? "synchronization",
    windowDirection: data.windowDirection ?? null,
    temporalFlight: data.temporalFlight ?? null,
    nodes: data.nodes,
    links: data.links,
  });
}

export function isConcreteUniverseNode(
  node: Universe3DNode | null,
): node is UniverseConcrete3DNode {
  return Boolean(node && node.kind !== "source");
}

export function visualNebulaRadius(
  eventCount: number,
  entityCount: number,
  sourceCount: number,
) {
  const total = Math.max(1, eventCount + entityCount * 0.72);
  const dataScale = 54 + Math.min(62, Math.log2(total + 1) * 9.2);
  const crowdScale = Math.min(
    1.04,
    Math.max(0.52, 1.18 - Math.log2(Math.max(2, sourceCount + 1)) * 0.09),
  );
  return Math.max(38, dataScale * crowdScale);
}

export function dominantSource(activation: UniverseActivation) {
  if (activation.source_hits?.[0]?.source_id) return activation.source_hits[0].source_id;
  const counts = new Map<string, number>();
  let dominantId: string | null = null;
  let dominantCount = 0;
  activation.nodes.forEach((node) => {
    if (!node.source_id || node.kind !== "event") return;
    const count = (counts.get(node.source_id) ?? 0) + 1;
    counts.set(node.source_id, count);
    if (count > dominantCount) {
      dominantId = node.source_id;
      dominantCount = count;
    }
  });
  return dominantId;
}
