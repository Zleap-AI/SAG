import type {
  UniverseActivation,
  UniverseActivationNode,
  UniverseNodeKind,
  UniverseRelation,
} from "./types";

export const UNIVERSE_SCENE_BUDGET = {
  desktop: { nodes: 700, edges: 1_000 },
  mobile: { nodes: 520, edges: 720 },
} as const;

/**
 * Hard in-memory budget for browse sessions. Timeline pages outside the
 * visible window stay resident so their cursor can be revisited without a
 * network round-trip, but they are projected through `UNIVERSE_SCENE_BUDGET`
 * before reaching Three.js. Search/assistant activations deliberately keep
 * using the smaller scene budget because they have no virtual bundle window.
 */
export const UNIVERSE_RESIDENT_BUDGET = {
  desktop: { nodes: 10_000, edges: 12_000 },
  mobile: { nodes: 10_000, edges: 12_000 },
} as const;

export interface UniverseWorkingNode extends UniverseActivationNode {
  source_id: string;
  touched_at: number;
  root: boolean;
}

export interface UniverseWorkingSet {
  epoch: number;
  nodes: UniverseWorkingNode[];
  relations: UniverseRelation[];
  root_keys: string[];
  /** Oldest-to-newest resident node order. */
  node_order: string[];
  bundle_order: string[];
  bundles: Record<string, UniverseWorkingBundle>;
  node_owners: Record<string, string[]>;
  relation_owners: Record<string, string[]>;
  /** Persistent node pins. Transaction-only protection is supplied to admission calls. */
  pinned_keys: string[];
  /** Persistent factual-edge pins for a locked one-hop network. */
  pinned_relation_keys: string[];
}

export interface UniverseWorkingBundle {
  id: string;
  origin: "timeline" | "expansion" | "activation";
  /**
   * Snapshot-stable position on the source's counting axis (0 = newest end).
   * Present exactly for timeline bundles; the projection places the package at
   * ordinal × axis-unit, so it must ride the bundle through the working set.
   */
  ordinal?: number;
  /** Canonical expansion anchor; absent for timeline/activation bundles. */
  anchor_key?: string;
  /**
   * Stable timeline node that admitted this expansion branch. Unlike
   * `anchor_key`, this survives FIFO removal of intermediate expansion pages.
   */
  lineage_root_key?: string;
  request_cursor?: string | null;
  next_cursor?: string | null;
  /** Includes explicit nodes and existing endpoints referenced by this bundle. */
  node_keys: string[];
  relation_keys: string[];
  /** Canonical full payload used to reject same-id content collisions. */
  payload_fingerprint: string;
  admitted_at: number;
}

/**
 * A server event bundle (or a client activation bundle) is the smallest unit
 * that may be admitted or evicted. `nodes` may omit an endpoint only when that
 * endpoint is already resident; the bundle then becomes an additional owner.
 */
export interface UniverseAdmissionBundle {
  id: string;
  origin?: UniverseWorkingBundle["origin"];
  ordinal?: number;
  anchor_key?: string;
  lineage_root_key?: string;
  request_cursor?: string | null;
  next_cursor?: string | null;
  epoch?: number;
  source_id?: string;
  nodes: UniverseActivationNode[];
  relations: UniverseRelation[];
}

export interface AdmitUniverseBundleOptions {
  roots?: boolean;
  /** Protected only for this transaction. */
  protectedKeys?: Iterable<string>;
  /** Added to the persistent pin set if the transaction commits. */
  pinnedKeys?: Iterable<string>;
  /** Relation keys protected only for this transaction. */
  protectedRelationKeys?: Iterable<string>;
  /** Whole bundles protected only for this transaction. */
  protectedBundleIds?: Iterable<string>;
  /** Relation keys added to the persistent pin set if the transaction commits. */
  pinnedRelationKeys?: Iterable<string>;
}

export type UniverseBundleRejectionReason =
  | "epoch_mismatch"
  | "invalid_bundle"
  | "duplicate_bundle"
  | "over_budget"
  | "protected_capacity";

export interface UniverseBundleAdmissionResult {
  accepted: boolean;
  /** False for an idempotent retry of an already committed bundle. */
  committed: boolean;
  workingSet: UniverseWorkingSet;
  evictedBundleIds: string[];
  reason?: UniverseBundleRejectionReason;
}

export interface UniverseBundleBatchAdmissionResult {
  workingSet: UniverseWorkingSet;
  committedBundleIds: string[];
  acknowledgedBundleIds: string[];
  evictedBundleIds: string[];
  rejectedBundleId: string | null;
  reason?: UniverseBundleRejectionReason;
}

export function universeNodeKey(
  kind: UniverseNodeKind,
  id: string,
  sourceId?: string | null,
) {
  return `${sourceId || "unknown"}:${kind}:${id}`;
}

export function emptyUniverseWorkingSet(epoch = 0): UniverseWorkingSet {
  return {
    epoch,
    nodes: [],
    relations: [],
    root_keys: [],
    node_order: [],
    bundle_order: [],
    bundles: {},
    node_owners: {},
    relation_owners: {},
    pinned_keys: [],
    pinned_relation_keys: [],
  };
}

export function universeRelationKey(relation: UniverseRelation) {
  return `${relation.source_id}:${relation.kind}:${relation.from_id}:${relation.to_id}`;
}

function nodeLimit(value: number) {
  return Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0;
}

function normalizedNodeOrder(value: UniverseWorkingSet) {
  const nodesByKey = new Map(
    value.nodes.map((node) => [universeNodeKey(node.kind, node.id, node.source_id), node]),
  );
  const seen = new Set<string>();
  const order: string[] = [];
  const append = (key: string) => {
    if (seen.has(key) || !nodesByKey.has(key)) return;
    seen.add(key);
    order.push(key);
  };
  value.node_order.forEach(append);
  value.nodes.forEach((node) => append(universeNodeKey(node.kind, node.id, node.source_id)));
  return order;
}

function dedupeRelations(relations: UniverseRelation[]) {
  const relationMap = new Map<string, UniverseRelation>();
  relations.forEach((relation) => {
    const key = universeRelationKey(relation);
    // Re-insertion records the latest occurrence at the end of the map.
    relationMap.delete(key);
    relationMap.set(key, relation);
  });
  return [...relationMap.values()];
}

function stableSerialize(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
  if (Array.isArray(value)) return `[${value.map(stableSerialize).join(",")}]`;
  const entries = Object.entries(value)
    .filter(([, item]) => item !== undefined)
    .sort(([left], [right]) => left.localeCompare(right));
  return `{${entries.map(([key, item]) =>
    `${JSON.stringify(key)}:${stableSerialize(item)}`).join(",")}}`;
}

function bundlePayloadFingerprint(
  nodes: UniverseActivationNode[],
  relations: UniverseRelation[],
  defaultSourceId = "",
) {
  const nodePayloads = new Map<string, UniverseActivationNode>();
  nodes.forEach((node) => {
    const payload = Object.fromEntries(
      Object.entries(node).filter(([key]) => key !== "touched_at" && key !== "root"),
    ) as unknown as UniverseActivationNode;
    const sourceId = node.source_id || defaultSourceId;
    nodePayloads.set(
      universeNodeKey(node.kind, node.id, sourceId),
      { ...payload, source_id: sourceId },
    );
  });
  const relationPayloads = new Map(dedupeRelations(relations).map((relation) => [
    universeRelationKey(relation),
    relation,
  ]));
  return stableSerialize({
    nodes: [...nodePayloads].sort(([left], [right]) => left.localeCompare(right)),
    relations: [...relationPayloads].sort(([left], [right]) => left.localeCompare(right)),
  });
}

interface NormalizedBundleState {
  order: string[];
  bundles: Map<string, UniverseWorkingBundle>;
}

function relationNodeKeys(relation: UniverseRelation) {
  return [
    universeNodeKey("event", relation.from_id, relation.source_id),
    universeNodeKey(
      relation.kind === "subevent" ? "event" : "entity",
      relation.to_id,
      relation.source_id,
    ),
  ] as const;
}

function uniqueValues(values: Iterable<string>) {
  return [...new Set(values)];
}

function sameStringSet(left: string[], right: string[]) {
  if (left.length !== right.length) return false;
  const rightSet = new Set(right);
  return left.every((value) => rightSet.has(value));
}

function relationMapFor(value: UniverseWorkingSet) {
  return new Map(dedupeRelations(value.relations).map((relation) => [
    universeRelationKey(relation),
    relation,
  ]));
}

function bundleStateFor(value: UniverseWorkingSet): NormalizedBundleState {
  const order = uniqueValues(value.bundle_order);
  return {
    order,
    bundles: new Map(order.flatMap((id) => {
      const bundle = value.bundles[id];
      return bundle ? [[id, bundle] as const] : [];
    })),
  };
}

function ownerMapsFor(state: NormalizedBundleState) {
  const nodeOwners = new Map<string, string[]>();
  const relationOwners = new Map<string, string[]>();
  for (const id of state.order) {
    const bundle = state.bundles.get(id);
    if (!bundle) continue;
    for (const key of bundle.node_keys) {
      const owners = nodeOwners.get(key) ?? [];
      owners.push(id);
      nodeOwners.set(key, owners);
    }
    for (const key of bundle.relation_keys) {
      const owners = relationOwners.get(key) ?? [];
      owners.push(id);
      relationOwners.set(key, owners);
    }
  }
  return { nodeOwners, relationOwners };
}

/** Builds complete event-bundle ownership for atomic result replacement. */
function withDerivedBundleMetadata(
  value: UniverseWorkingSet,
  now: number,
): UniverseWorkingSet {
  const nodeOrder = normalizedNodeOrder(value);
  const nodeIndex = new Map(nodeOrder.map((key, index) => [key, index]));
  const nodesByKey = new Map(value.nodes.map((node) => [
    universeNodeKey(node.kind, node.id, node.source_id),
    node,
  ]));
  const relationsByEvent = new Map<string, UniverseRelation[]>();
  const validRelations: UniverseRelation[] = [];
  for (const relation of dedupeRelations(value.relations)) {
    const eventKey = universeNodeKey("event", relation.from_id, relation.source_id);
    const endpoints = relationNodeKeys(relation);
    if (!endpoints.every((key) => nodesByKey.has(key))) continue;
    validRelations.push(relation);
    const relations = relationsByEvent.get(eventKey) ?? [];
    relations.push(relation);
    relationsByEvent.set(eventKey, relations);
  }

  const ownedNodes = new Set<string>();
  const entries: Array<{ position: number; bundle: UniverseWorkingBundle }> = [];
  for (const key of nodeOrder) {
    const node = nodesByKey.get(key);
    if (!node || node.kind !== "event") continue;
    const relations = relationsByEvent.get(key) ?? [];
    const nodeKeys = [key];
    relations.forEach((relation) => {
      relationNodeKeys(relation).forEach((endpoint) => {
        if (!nodeKeys.includes(endpoint)) nodeKeys.push(endpoint);
      });
    });
    nodeKeys.forEach((nodeKey) => ownedNodes.add(nodeKey));
    entries.push({
      position: nodeIndex.get(key) ?? Number.MAX_SAFE_INTEGER,
      bundle: {
        id: `__graph__:${value.epoch}:event:${key}`,
        origin: "activation",
        node_keys: nodeKeys,
        relation_keys: relations.map(universeRelationKey),
        payload_fingerprint: bundlePayloadFingerprint(
          nodeKeys.map((nodeKey) => nodesByKey.get(nodeKey))
            .filter((node): node is UniverseWorkingNode => Boolean(node)),
          relations,
        ),
        admitted_at: now,
      },
    });
  }
  for (const key of nodeOrder) {
    if (ownedNodes.has(key)) continue;
    ownedNodes.add(key);
    entries.push({
      position: nodeIndex.get(key) ?? Number.MAX_SAFE_INTEGER,
      bundle: {
        id: `__graph__:${value.epoch}:node:${key}`,
        origin: "activation",
        node_keys: [key],
        relation_keys: [],
        payload_fingerprint: bundlePayloadFingerprint(
          [nodesByKey.get(key)].filter(
            (node): node is UniverseWorkingNode => Boolean(node),
          ),
          [],
        ),
        admitted_at: now,
      },
    });
  }
  entries.sort((left, right) => left.position - right.position
    || left.bundle.id.localeCompare(right.bundle.id));
  const state = {
    order: entries.map(({ bundle }) => bundle.id),
    bundles: new Map(entries.map(({ bundle }) => [bundle.id, bundle])),
  };
  const owners = ownerMapsFor(state);
  const resident = new Set(nodeOrder);
  const residentRelations = new Set(validRelations.map(universeRelationKey));
  return {
    ...value,
    relations: validRelations,
    bundle_order: state.order,
    bundles: Object.fromEntries(state.bundles),
    node_owners: Object.fromEntries(owners.nodeOwners),
    relation_owners: Object.fromEntries(owners.relationOwners),
    pinned_keys: value.pinned_keys.filter((key) => resident.has(key)),
    pinned_relation_keys: value.pinned_relation_keys.filter((key) =>
      residentRelations.has(key)),
  };
}

function cloneBundleState(state: NormalizedBundleState): NormalizedBundleState {
  return {
    order: [...state.order],
    bundles: new Map(state.bundles),
  };
}

function planBundleEvictions(
  source: NormalizedBundleState,
  budget: { nodes: number; edges: number },
  protectedKeys: Set<string>,
  protectedRelationKeys: Set<string>,
  nonEvictableBundleIds: Set<string> = new Set(),
) {
  const state = cloneBundleState(source);
  const evictedBundleIds: string[] = [];
  let blockedByProtection = false;
  const initialOwners = ownerMapsFor(state);
  const nodeOwnerCounts = new Map(
    [...initialOwners.nodeOwners].map(([key, owners]) => [key, owners.length]),
  );
  const relationOwnerCounts = new Map(
    [...initialOwners.relationOwners].map(([key, owners]) => [key, owners.length]),
  );
  // Expansion support is opportunistic: release it before timeline history,
  // while retaining FIFO order inside each origin class. This order is stable
  // for the whole transaction, so consume it once instead of sorting the
  // shrinking bundle list on every eviction.
  const evictionOrder = [
    ...state.order.filter((id) => state.bundles.get(id)?.origin === "expansion"),
    ...state.order.filter((id) => state.bundles.get(id)?.origin !== "expansion"),
  ];
  let evictionIndex = 0;

  while (
    nodeOwnerCounts.size > nodeLimit(budget.nodes)
    || relationOwnerCounts.size > nodeLimit(budget.edges)
  ) {

    let candidate: string | null = null;
    while (evictionIndex < evictionOrder.length) {
      const id = evictionOrder[evictionIndex];
      evictionIndex += 1;
      if (nonEvictableBundleIds.has(id)) {
        blockedByProtection = true;
        continue;
      }
      const bundle = state.bundles.get(id);
      if (!bundle) continue;
      const removesProtectedNode = bundle.node_keys.some((key) =>
        protectedKeys.has(key) && nodeOwnerCounts.get(key) === 1);
      const removesProtectedRelation = bundle.relation_keys.some((key) =>
        protectedRelationKeys.has(key) && relationOwnerCounts.get(key) === 1);
      if (removesProtectedNode || removesProtectedRelation) {
        blockedByProtection = true;
        continue;
      }
      candidate = id;
      break;
    }
    if (!candidate) return { state: null, evictedBundleIds: [], blockedByProtection };
    const evicted = state.bundles.get(candidate);
    if (!evicted) continue;
    for (const key of evicted.node_keys) {
      const nextCount = (nodeOwnerCounts.get(key) ?? 0) - 1;
      if (nextCount <= 0) nodeOwnerCounts.delete(key);
      else nodeOwnerCounts.set(key, nextCount);
    }
    for (const key of evicted.relation_keys) {
      const nextCount = (relationOwnerCounts.get(key) ?? 0) - 1;
      if (nextCount <= 0) relationOwnerCounts.delete(key);
      else relationOwnerCounts.set(key, nextCount);
    }
    state.bundles.delete(candidate);
    state.order = state.order.filter((id) => id !== candidate);
    evictedBundleIds.push(candidate);
  }
  return { state, evictedBundleIds, blockedByProtection };
}

function materializeBundleState(
  value: UniverseWorkingSet,
  state: NormalizedBundleState,
  nodesByKey: Map<string, UniverseWorkingNode>,
  relationsByKey: Map<string, UniverseRelation>,
  pinnedKeys: Iterable<string>,
  pinnedRelationKeys: Iterable<string>,
): UniverseWorkingSet {
  const { nodeOwners, relationOwners } = ownerMapsFor(state);
  const preferredOrder = normalizedNodeOrder({
    ...value,
    nodes: [...nodesByKey.values()],
  });
  const nodeOrder = preferredOrder.filter((key) => nodeOwners.has(key) && nodesByKey.has(key));
  const keptKeys = new Set(nodeOrder);
  const nodes = nodeOrder
    .map((key) => nodesByKey.get(key))
    .filter((node): node is UniverseWorkingNode => Boolean(node));
  const relations = [...relationsByKey]
    .filter(([key, relation]) => {
      if (!relationOwners.has(key)) return false;
      const endpoints = relationNodeKeys(relation);
      return endpoints.every((endpoint) => keptKeys.has(endpoint));
    })
    .map(([, relation]) => relation);
  const validRelationKeys = new Set(relations.map(universeRelationKey));
  const normalizedBundles = state.order
    .map((id) => state.bundles.get(id))
    .filter((bundle): bundle is UniverseWorkingBundle => Boolean(bundle))
    .map((bundle) => ({
      ...bundle,
      node_keys: bundle.node_keys.filter((key) => keptKeys.has(key)),
      relation_keys: bundle.relation_keys.filter((key) => validRelationKeys.has(key)),
    }));
  const finalState = {
    order: normalizedBundles.map((bundle) => bundle.id),
    bundles: new Map(normalizedBundles.map((bundle) => [bundle.id, bundle])),
  };
  const finalOwners = ownerMapsFor(finalState);
  const rootSet = new Set([
    ...value.root_keys,
    ...nodes.filter((node) => node.root).map((node) =>
      universeNodeKey(node.kind, node.id, node.source_id)),
  ]);
  return {
    ...value,
    nodes,
    relations,
    root_keys: nodeOrder.filter((key) => rootSet.has(key)),
    node_order: nodeOrder,
    bundle_order: finalState.order,
    bundles: Object.fromEntries(finalState.bundles),
    node_owners: Object.fromEntries(finalOwners.nodeOwners),
    relation_owners: Object.fromEntries(finalOwners.relationOwners),
    pinned_keys: uniqueValues(pinnedKeys).filter((key) => keptKeys.has(key)),
    pinned_relation_keys: uniqueValues(pinnedRelationKeys).filter((key) =>
      validRelationKeys.has(key)),
  };
}

export function trimUniverseWorkingSet(
  current: UniverseWorkingSet,
  budget: { nodes: number; edges: number },
  protectedKeys: Iterable<string> = [],
  protectedRelationKeys: Iterable<string> = [],
): UniverseWorkingSet {
  const protectedSet = new Set([
    ...current.pinned_keys,
    ...protectedKeys,
  ]);
  const protectedRelationSet = new Set([
    ...current.pinned_relation_keys,
    ...protectedRelationKeys,
  ]);
  const planned = planBundleEvictions(
    bundleStateFor(current),
    budget,
    protectedSet,
    protectedRelationSet,
  );
  // Lowering a budget must not tear apart a protected bundle. The caller can
  // retry after unlocking instead of receiving a corrupt partial projection.
  if (!planned.state) return current;
  const nodesByKey = new Map(current.nodes.map((node) => [
    universeNodeKey(node.kind, node.id, node.source_id),
    node,
  ]));
  return materializeBundleState(
    current,
    planned.state,
    nodesByKey,
    relationMapFor(current),
    current.pinned_keys,
    current.pinned_relation_keys,
  );
}

export function replaceUniverseWorkingSet(
  activation: UniverseActivation,
  budget: { nodes: number; edges: number },
  now = Date.now(),
): UniverseWorkingSet {
  const nodesByKey = new Map<string, UniverseWorkingNode>();
  for (const node of activation.nodes) {
    const sourceId = node.source_id || "";
    const normalized: UniverseWorkingNode = {
      ...node,
      source_id: sourceId,
      touched_at: now,
      root: true,
    };
    nodesByKey.set(universeNodeKey(node.kind, node.id, sourceId), normalized);
  }
  const relationMap = new Map<string, UniverseRelation>();
  activation.relations.forEach((relation) =>
    relationMap.set(universeRelationKey(relation), relation));
  const rootKeys = [...nodesByKey.keys()];
  const epoch = activation.epoch ?? 0;
  const replacement = withDerivedBundleMetadata({
    ...emptyUniverseWorkingSet(epoch),
    nodes: [...nodesByKey.values()],
    relations: [...relationMap.values()],
    root_keys: rootKeys,
    node_order: [...nodesByKey.keys()],
  }, now);
  return trimUniverseWorkingSet(replacement, budget);
}

/**
 * Adds one contextual result to an existing graph without changing the graph
 * epoch. Stable node identities let the renderer keep every existing node in
 * place while newly discovered events and entities animate into the network.
 *
 * A later result wins when the same factual node/relation is returned again;
 * accumulated facts remain event-bundle-aware and are evicted oldest-first
 * only when the hard scene budget is reached.
 */
export function mergeUniverseWorkingSetActivation(
  current: UniverseWorkingSet,
  activation: UniverseActivation,
  budget: { nodes: number; edges: number },
  now = Date.now(),
): UniverseWorkingSet {
  const nodes = new Map(current.nodes.map((node) => [
    universeNodeKey(node.kind, node.id, node.source_id),
    node as UniverseActivationNode,
  ]));
  activation.nodes.forEach((node) => {
    nodes.set(
      universeNodeKey(node.kind, node.id, node.source_id),
      node,
    );
  });
  const relations = new Map(current.relations.map((relation) => [
    universeRelationKey(relation),
    relation,
  ]));
  activation.relations.forEach((relation) => {
    relations.set(universeRelationKey(relation), relation);
  });
  return replaceUniverseWorkingSet({
    ...activation,
    epoch: current.epoch,
    nodes: [...nodes.values()],
    relations: [...relations.values()],
  }, budget, now);
}

export function universeAnchorProgress(
  current: UniverseWorkingSet,
  kind: UniverseNodeKind,
  id: string,
  sourceId: string,
) {
  const neighborIds = new Set<string>();
  current.relations.forEach((relation) => {
    if (relation.source_id !== sourceId) return;
    if (kind === "event" && relation.from_id === id) {
      neighborIds.add(`${relation.kind}:${relation.to_id}`);
    }
    if (kind === "entity" && relation.kind === "mentions" && relation.to_id === id) {
      neighborIds.add(relation.from_id);
    }
  });
  return neighborIds.size;
}

/**
 * Atomically admits one event/activation bundle. Rejection returns the exact
 * input object so callers cannot accidentally publish a partially mutated
 * graph or advance a cursor past data that was never committed.
 */
export function admitUniverseBundle(
  current: UniverseWorkingSet,
  bundle: UniverseAdmissionBundle,
  budget: { nodes: number; edges: number },
  now = Date.now(),
  options: AdmitUniverseBundleOptions = {},
): UniverseBundleAdmissionResult {
  if ((bundle.epoch ?? current.epoch) !== current.epoch) {
    return {
      accepted: false,
      committed: false,
      workingSet: current,
      evictedBundleIds: [],
      reason: "epoch_mismatch",
    };
  }
  if (!bundle.id || !bundle.id.trim() || (bundle.nodes.length === 0 && bundle.relations.length === 0)) {
    return {
      accepted: false,
      committed: false,
      workingSet: current,
      evictedBundleIds: [],
      reason: "invalid_bundle",
    };
  }

  const state = bundleStateFor(current);
  const nodesByKey = new Map(current.nodes.map((node) => [
    universeNodeKey(node.kind, node.id, node.source_id),
    node,
  ]));
  const bundleNodeKeys: string[] = [];
  for (const node of bundle.nodes) {
    const sourceId = node.source_id || bundle.source_id || "";
    const key = universeNodeKey(node.kind, node.id, sourceId);
    const existing = nodesByKey.get(key);
    if (!bundleNodeKeys.includes(key)) bundleNodeKeys.push(key);
    nodesByKey.set(key, {
      ...existing,
      ...node,
      source_id: sourceId,
      touched_at: now,
      root: Boolean(existing?.root || options.roots),
    });
  }

  const relationsByKey = relationMapFor(current);
  const bundleRelationKeys: string[] = [];
  for (const relation of dedupeRelations(bundle.relations)) {
    const endpoints = relationNodeKeys(relation);
    if (!endpoints.every((key) => nodesByKey.has(key))) {
      return {
        accepted: false,
        committed: false,
        workingSet: current,
        evictedBundleIds: [],
        reason: "invalid_bundle",
      };
    }
    endpoints.forEach((key) => {
      if (!bundleNodeKeys.includes(key)) bundleNodeKeys.push(key);
    });
    const key = universeRelationKey(relation);
    bundleRelationKeys.push(key);
    // Keep the latest payload and deterministic relation insertion order.
    relationsByKey.delete(key);
    relationsByKey.set(key, relation);
  }
  if (options.roots) {
    bundleNodeKeys.forEach((key) => {
      const node = nodesByKey.get(key);
      if (node && !node.root) nodesByKey.set(key, { ...node, root: true });
    });
  }
  const payloadFingerprint = bundlePayloadFingerprint(
    bundle.nodes,
    bundle.relations,
    bundle.source_id,
  );

  const existingBundle = state.bundles.get(bundle.id);
  if (existingBundle) {
    const identical = sameStringSet(existingBundle.node_keys, bundleNodeKeys)
      && sameStringSet(existingBundle.relation_keys, bundleRelationKeys)
      && existingBundle.payload_fingerprint === payloadFingerprint
      && existingBundle.origin === (bundle.origin ?? "activation")
      && existingBundle.ordinal === bundle.ordinal
      && existingBundle.anchor_key === bundle.anchor_key
      && existingBundle.lineage_root_key === bundle.lineage_root_key
      && existingBundle.request_cursor === bundle.request_cursor
      && existingBundle.next_cursor === bundle.next_cursor;
    return identical
      ? {
          accepted: true,
          committed: false,
          workingSet: current,
          evictedBundleIds: [],
        }
      : {
          accepted: false,
          committed: false,
          workingSet: current,
          evictedBundleIds: [],
          reason: "duplicate_bundle",
        };
  }

  if (
    bundleNodeKeys.length > nodeLimit(budget.nodes)
    || bundleRelationKeys.length > nodeLimit(budget.edges)
  ) {
    return {
      accepted: false,
      committed: false,
      workingSet: current,
      evictedBundleIds: [],
      reason: "over_budget",
    };
  }

  const candidate = cloneBundleState(state);
  candidate.order.push(bundle.id);
  candidate.bundles.set(bundle.id, {
    id: bundle.id,
    origin: bundle.origin ?? "activation",
    ordinal: bundle.ordinal,
    anchor_key: bundle.anchor_key,
    lineage_root_key: bundle.lineage_root_key,
    request_cursor: bundle.request_cursor,
    next_cursor: bundle.next_cursor,
    node_keys: bundleNodeKeys,
    relation_keys: bundleRelationKeys,
    payload_fingerprint: payloadFingerprint,
    admitted_at: now,
  });
  const persistentPins = uniqueValues([
    ...current.pinned_keys,
    ...(options.pinnedKeys ?? []),
  ]);
  const persistentRelationPins = uniqueValues([
    ...current.pinned_relation_keys,
    ...(options.pinnedRelationKeys ?? []),
  ]);
  const protectedSet = new Set([
    ...persistentPins,
    ...(options.protectedKeys ?? []),
  ]);
  const protectedRelationSet = new Set([
    ...persistentRelationPins,
    ...(options.protectedRelationKeys ?? []),
  ]);
  const planned = planBundleEvictions(
    candidate,
    budget,
    protectedSet,
    protectedRelationSet,
    new Set([bundle.id, ...(options.protectedBundleIds ?? [])]),
  );
  if (!planned.state) {
    return {
      accepted: false,
      committed: false,
      workingSet: current,
      evictedBundleIds: [],
      reason: planned.blockedByProtection ? "protected_capacity" : "over_budget",
    };
  }

  const rootKeys = options.roots
    ? uniqueValues([...current.root_keys, ...bundleNodeKeys])
    : current.root_keys;
  const workingSet = materializeBundleState(
    { ...current, root_keys: rootKeys },
    planned.state,
    nodesByKey,
    relationsByKey,
    persistentPins,
    persistentRelationPins,
  );
  return {
    accepted: true,
    committed: true,
    workingSet,
    evictedBundleIds: planned.evictedBundleIds,
  };
}

/**
 * Commits the longest valid prefix of a page. A cursor may advance only to the
 * `cursor_after` associated with the final acknowledged id. The rejected
 * bundle and every bundle after it remain uncommitted.
 */
export function admitUniverseBundles(
  current: UniverseWorkingSet,
  bundles: UniverseAdmissionBundle[],
  budget: { nodes: number; edges: number },
  now = Date.now(),
  options: AdmitUniverseBundleOptions = {},
): UniverseBundleBatchAdmissionResult {
  let workingSet = current;
  const committedBundleIds: string[] = [];
  const acknowledgedBundleIds: string[] = [];
  const evictedBundleIds: string[] = [];
  for (const bundle of bundles) {
    const result = admitUniverseBundle(workingSet, bundle, budget, now, options);
    if (!result.accepted) {
      return {
        workingSet,
        committedBundleIds,
        acknowledgedBundleIds,
        evictedBundleIds: uniqueValues(evictedBundleIds),
        rejectedBundleId: bundle.id,
        reason: result.reason,
      };
    }
    workingSet = result.workingSet;
    acknowledgedBundleIds.push(bundle.id);
    if (result.committed) committedBundleIds.push(bundle.id);
    evictedBundleIds.push(...result.evictedBundleIds);
  }
  return {
    workingSet,
    committedBundleIds,
    acknowledgedBundleIds,
    evictedBundleIds: uniqueValues(evictedBundleIds),
    rejectedBundleId: null,
  };
}

/** Replaces persistent one-hop network pins without mutating graph content. */
export function setUniversePinnedNetwork(
  current: UniverseWorkingSet,
  nodeKeys: Iterable<string>,
  relationKeys: Iterable<string>,
): UniverseWorkingSet {
  const residentNodes = new Set(current.nodes.map((node) =>
    universeNodeKey(node.kind, node.id, node.source_id)));
  const residentRelations = new Set(current.relations
    .filter((relation) => relationNodeKeys(relation).every((key) => residentNodes.has(key)))
    .map(universeRelationKey));
  const nextPinnedKeys = uniqueValues(nodeKeys).filter((key) => residentNodes.has(key));
  const nextPinnedRelationKeys = uniqueValues(relationKeys).filter((key) =>
    residentRelations.has(key));
  const samePinnedKeys = nextPinnedKeys.length === current.pinned_keys.length
    && nextPinnedKeys.every((key, index) => current.pinned_keys[index] === key);
  const samePinnedRelations =
    nextPinnedRelationKeys.length === current.pinned_relation_keys.length
    && nextPinnedRelationKeys.every(
      (key, index) => current.pinned_relation_keys[index] === key,
    );
  if (samePinnedKeys && samePinnedRelations) return current;
  return {
    ...current,
    pinned_keys: nextPinnedKeys,
    pinned_relation_keys: nextPinnedRelationKeys,
  };
}

/** Replaces node pins while retaining the currently pinned factual edges. */
export function setUniversePinnedKeys(
  current: UniverseWorkingSet,
  pinnedKeys: Iterable<string>,
): UniverseWorkingSet {
  return setUniversePinnedNetwork(
    current,
    pinnedKeys,
    current.pinned_relation_keys,
  );
}
