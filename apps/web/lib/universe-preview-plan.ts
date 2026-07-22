export interface UniversePreviewNode {
  id: string;
  kind: "event" | "entity";
  sourceId: string;
  active: boolean;
  /** Distance from the current timeline camera position, in event units. */
  viewDistance?: number;
}

export interface UniversePreviewPlan {
  ids: readonly string[];
  eventIds: readonly string[];
  entityIds: readonly string[];
  focused: boolean;
  hiddenRelatedEntityCount: number;
}

export interface UniversePreviewPlanInput {
  /** Nodes arrive in the scene's stable reading order. */
  nodes: readonly UniversePreviewNode[];
  adjacency: ReadonlyMap<string, ReadonlySet<string>>;
  sourceId: string | null;
  /** Accumulation may contain evidence from more than one source. */
  includeAllSources?: boolean;
  focusId: string | null;
  cardsEnabled: boolean;
  eventPreviewCount: number;
  entitySafetyMax: number;
}

export interface UniverseAccumulationCardViewSample {
  projectedZ: number;
  screenX: number;
  screenY: number;
  viewportWidth: number;
  viewportHeight: number;
  safeCenterX: number;
  safeCenterY: number;
  cameraDistance: number;
  blockedByOverlay?: boolean;
}

export interface UniverseViewpointCardCandidate {
  id: string;
  score: number;
  x: number;
  y: number;
}

export interface UniverseViewpointCardSelection {
  maxCount: number;
  horizontalGap: number;
  verticalGap: number;
}

/**
 * Ranks only stars that are actually facing the current reading viewport.
 * The reading centre leads the order; camera distance breaks ties toward the
 * foreground without flattening the surrounding 3D graph.
 */
export function universeAccumulationCardViewScore(
  sample: UniverseAccumulationCardViewSample,
) {
  const values = [
    sample.projectedZ,
    sample.screenX,
    sample.screenY,
    sample.viewportWidth,
    sample.viewportHeight,
    sample.safeCenterX,
    sample.safeCenterY,
    sample.cameraDistance,
  ];
  if (
    sample.blockedByOverlay
    || values.some((value) => !Number.isFinite(value))
    || sample.projectedZ <= -1
    || sample.projectedZ >= 1
    || sample.viewportWidth <= 0
    || sample.viewportHeight <= 0
    || sample.screenX < 56
    || sample.screenX > sample.viewportWidth - 56
    || sample.screenY < 64
    || sample.screenY > sample.viewportHeight - 52
  ) return undefined;

  const horizontal = (sample.screenX - sample.safeCenterX)
    / Math.max(1, sample.viewportWidth / 2);
  const vertical = (sample.screenY - sample.safeCenterY)
    / Math.max(1, sample.viewportHeight / 2);
  const centerDistance = Math.hypot(horizontal, vertical);
  if (centerDistance > 0.68) return undefined;
  return centerDistance * 720 + Math.max(0, sample.cameraDistance) * 0.35;
}

/** Greedily opens the clearest cards while preserving screen-space breathing room. */
export function selectUniverseViewpointCardIds(
  candidates: readonly UniverseViewpointCardCandidate[],
  selection: UniverseViewpointCardSelection,
) {
  const limit = boundedInteger(selection.maxCount, 0, candidates.length);
  const horizontalGap = Number.isFinite(selection.horizontalGap)
    ? Math.max(0, selection.horizontalGap)
    : 0;
  const verticalGap = Number.isFinite(selection.verticalGap)
    ? Math.max(0, selection.verticalGap)
    : 0;
  const selected: UniverseViewpointCardCandidate[] = [];
  const seen = new Set<string>();
  for (const candidate of [...candidates].sort((left, right) =>
    left.score - right.score || left.id.localeCompare(right.id))) {
    if (
      selected.length >= limit
      || seen.has(candidate.id)
      || !Number.isFinite(candidate.score)
      || !Number.isFinite(candidate.x)
      || !Number.isFinite(candidate.y)
      || selected.some((item) =>
        Math.abs(item.x - candidate.x) < horizontalGap
        && Math.abs(item.y - candidate.y) < verticalGap)
    ) continue;
    selected.push(candidate);
    seen.add(candidate.id);
  }
  return selected.map((candidate) => candidate.id);
}

function boundedInteger(value: number, min: number, max: number) {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, Math.floor(value)));
}

function normalizedViewDistance(node: UniversePreviewNode) {
  return typeof node.viewDistance === "number"
    && Number.isFinite(node.viewDistance)
    ? Math.max(0, node.viewDistance)
    : Number.POSITIVE_INFINITY;
}

/** Coarse bucket used to refresh a bounded card aperture as time moves. */
export function universeCardApertureBucket(
  depth: number,
  unitsPerEvent: number,
  eventsPerBucket = 3,
) {
  const safeDepth = Number.isFinite(depth) ? Math.max(0, depth) : 0;
  const safeUnit = Number.isFinite(unitsPerEvent)
    ? Math.max(1, unitsPerEvent)
    : 1;
  const safeSpan = Number.isFinite(eventsPerBucket)
    ? Math.max(1, Math.floor(eventsPerBucket))
    : 1;
  return Math.floor(safeDepth / safeUnit / safeSpan);
}

function uniqueNodes(
  nodes: readonly UniversePreviewNode[],
  sourceId: string | null,
) {
  const result: UniversePreviewNode[] = [];
  const seen = new Set<string>();
  for (const node of nodes) {
    if ((sourceId && node.sourceId !== sourceId) || seen.has(node.id)) continue;
    seen.add(node.id);
    result.push(node);
  }
  return result;
}

function relatedEntityIds(
  eventIds: readonly string[],
  adjacency: ReadonlyMap<string, ReadonlySet<string>>,
) {
  const result = new Set<string>();
  for (const eventId of eventIds) {
    for (const relatedId of adjacency.get(eventId) ?? []) {
      result.add(relatedId);
    }
  }
  return result;
}

/**
 * Bounds only the DOM reading cards. The scene window still owns every event,
 * entity and factual relation, so hidden cards remain visible as connected
 * stars and can be focused on demand.
 */
export function planUniversePreviewCards(
  input: UniversePreviewPlanInput,
): UniversePreviewPlan {
  const empty = {
    ids: [],
    eventIds: [],
    entityIds: [],
    focused: false,
    hiddenRelatedEntityCount: 0,
  } satisfies UniversePreviewPlan;
  const allNodes = uniqueNodes(input.nodes, null);
  const allById = new Map(allNodes.map((node) => [node.id, node]));
  const focusNode = input.focusId ? allById.get(input.focusId) : undefined;
  const focused = Boolean(focusNode);
  const sourceScope = focused
    ? focusNode?.sourceId ?? null
    : input.includeAllSources
      ? null
      : input.sourceId;
  if (!sourceScope && !input.includeAllSources) return empty;
  const nodes = uniqueNodes(allNodes, sourceScope);
  if (!input.cardsEnabled && !focused) return empty;

  const configuredEventLimit = boundedInteger(
    input.eventPreviewCount,
    1,
    nodes.length,
  );
  const networkIds = focused && input.focusId
    ? new Set([input.focusId, ...(input.adjacency.get(input.focusId) ?? [])])
    : null;
  const eligible = nodes.filter((node) => (
    networkIds ? networkIds.has(node.id) : node.active
  ));

  const eventCandidates = eligible
    .filter((node) => node.kind === "event")
    .map((node, stableIndex) => ({ node, stableIndex }));
  if (!focused) {
    eventCandidates.sort((left, right) =>
      normalizedViewDistance(left.node) - normalizedViewDistance(right.node)
      || left.stableIndex - right.stableIndex);
  }

  const eventIds: string[] = [];
  if (focusNode?.kind === "event") eventIds.push(focusNode.id);
  for (const { node } of eventCandidates) {
    if (
      eventIds.includes(node.id)
      || eventIds.length >= configuredEventLimit
    ) continue;
    eventIds.push(node.id);
  }

  const relatedIds = networkIds ?? relatedEntityIds(
    eventIds,
    input.adjacency,
  );
  const entityCandidates: string[] = [];
  if (focusNode?.kind === "entity") entityCandidates.push(focusNode.id);
  for (const node of eligible) {
    if (
      node.kind !== "entity"
      || !relatedIds.has(node.id)
      || entityCandidates.includes(node.id)
    ) continue;
    entityCandidates.push(node.id);
  }

  const entitySafetyLimit = boundedInteger(
    input.entitySafetyMax,
    0,
    nodes.length,
  );
  // Event + entities is one visual knowledge package. Once an event receives
  // a reading card, every related entity already resident in the scene must
  // receive its compact label too; a separate "two entities per event" cap
  // made most of the package appear only after focus and looked like data loss.
  const entityIds = entityCandidates.slice(0, entitySafetyLimit);

  return {
    ids: [...eventIds, ...entityIds],
    eventIds,
    entityIds,
    focused,
    hiddenRelatedEntityCount: Math.max(
      0,
      entityCandidates.length - entityIds.length,
    ),
  };
}
