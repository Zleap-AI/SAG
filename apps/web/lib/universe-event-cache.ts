import type {
  UniverseActivationNode,
  UniverseRelation,
} from "./types";

export type UniverseEventBundleOrigin =
  | "timeline"
  | "search"
  | "assistant"
  | "expansion";

export interface UniverseEventBundle {
  origin: UniverseEventBundleOrigin;
  sourceId: string;
  event: UniverseActivationNode & { kind: "event" };
  entities: readonly (UniverseActivationNode & { kind: "entity" })[];
  relations: readonly UniverseRelation[];
  ordinal?: number;
  temporalKey?: string;
  documentId?: string | null;
}

export interface UniverseEventCache {
  capacity: number;
  recordsByKey: ReadonlyMap<string, UniverseEventBundle>;
  admissionOrder: readonly string[];
}

export interface UniverseEventCacheAdmission {
  cache: UniverseEventCache;
  addedKeys: readonly string[];
  updatedKeys: readonly string[];
  evictedKeys: readonly string[];
}

function positiveInteger(value: number, fallback: number) {
  return Number.isFinite(value) ? Math.max(1, Math.floor(value)) : fallback;
}

function normalizedSourceId(value: string) {
  const sourceId = value.trim();
  if (!sourceId) throw new Error("universe event bundle requires a source id");
  return sourceId;
}

export function universeEventBundleKey(
  bundle: Pick<UniverseEventBundle, "sourceId" | "event">,
) {
  return `${normalizedSourceId(bundle.sourceId)}:event:${bundle.event.id}`;
}

function universeEntityKey(
  sourceId: string,
  node: Pick<UniverseActivationNode, "id">,
) {
  return `${sourceId}:entity:${node.id}`;
}

export function universeEventRelationKey(relation: UniverseRelation) {
  return [
    relation.source_id,
    relation.kind,
    relation.from_id,
    relation.to_id,
  ].join(":");
}

function mergeNode<T extends UniverseActivationNode>(current: T, next: T): T {
  return {
    ...current,
    ...next,
    id: current.id,
    kind: current.kind,
    source_id: current.source_id ?? next.source_id,
  };
}

function normalizeBundle(bundle: UniverseEventBundle): UniverseEventBundle {
  const sourceId = normalizedSourceId(bundle.sourceId);
  if (!bundle.event.id.trim() || bundle.event.kind !== "event") {
    throw new Error("universe event bundle requires an event");
  }

  const entities = new Map<
    string,
    UniverseActivationNode & { kind: "entity" }
  >();
  for (const entity of bundle.entities) {
    if (!entity.id.trim() || entity.kind !== "entity") continue;
    const normalized = {
      ...entity,
      source_id: sourceId,
    } as UniverseActivationNode & { kind: "entity" };
    const key = universeEntityKey(sourceId, normalized);
    const current = entities.get(key);
    entities.set(key, current ? mergeNode(current, normalized) : normalized);
  }

  const relations = new Map<string, UniverseRelation>();
  for (const relation of bundle.relations) {
    if (!relation.from_id.trim() || !relation.to_id.trim()) continue;
    const normalized = {
      ...relation,
      source_id: sourceId,
    };
    relations.set(universeEventRelationKey(normalized), normalized);
  }

  return {
    ...bundle,
    sourceId,
    event: {
      ...bundle.event,
      source_id: sourceId,
    },
    entities: [...entities.values()],
    relations: [...relations.values()],
  };
}

export function mergeUniverseEventBundles(
  current: UniverseEventBundle,
  incoming: UniverseEventBundle,
): UniverseEventBundle {
  const left = normalizeBundle(current);
  const right = normalizeBundle(incoming);
  if (universeEventBundleKey(left) !== universeEventBundleKey(right)) {
    throw new Error("cannot merge different universe event bundles");
  }

  const entities = new Map(
    left.entities.map((entity) => [
      universeEntityKey(left.sourceId, entity),
      entity,
    ]),
  );
  for (const entity of right.entities) {
    const key = universeEntityKey(right.sourceId, entity);
    const existing = entities.get(key);
    entities.set(key, existing ? mergeNode(existing, entity) : entity);
  }

  const relations = new Map(
    left.relations.map((relation) => [
      universeEventRelationKey(relation),
      relation,
    ]),
  );
  for (const relation of right.relations) {
    relations.set(universeEventRelationKey(relation), relation);
  }

  return {
    ...left,
    sourceId: left.sourceId,
    event: mergeNode(left.event, right.event),
    entities: [...entities.values()],
    relations: [...relations.values()],
    // Origin describes the first admission into this FIFO. A later search,
    // answer or expansion may enrich the same fact, but changing this field
    // alone is not a factual update and must not rebuild the stable window.
    origin: left.origin,
    ordinal: left.ordinal ?? right.ordinal,
    temporalKey: left.temporalKey ?? right.temporalKey,
    documentId: left.documentId ?? right.documentId,
  };
}

function sameUniverseEventBundle(
  left: UniverseEventBundle,
  right: UniverseEventBundle,
) {
  return JSON.stringify(left) === JSON.stringify(right);
}

export function createUniverseEventCache(capacity = 1_000): UniverseEventCache {
  return {
    capacity: positiveInteger(capacity, 1_000),
    recordsByKey: new Map(),
    admissionOrder: [],
  };
}

export function resizeUniverseEventCache(
  current: UniverseEventCache,
  capacity: number,
): UniverseEventCacheAdmission {
  const normalizedCapacity = positiveInteger(capacity, current.capacity);
  const recordsByKey = new Map(current.recordsByKey);
  const admissionOrder = [...current.admissionOrder];
  const evictedKeys: string[] = [];

  while (admissionOrder.length > normalizedCapacity) {
    const key = admissionOrder.shift();
    if (!key) break;
    recordsByKey.delete(key);
    evictedKeys.push(key);
  }

  return {
    cache: {
      capacity: normalizedCapacity,
      recordsByKey,
      admissionOrder,
    },
    addedKeys: [],
    updatedKeys: [],
    evictedKeys,
  };
}

export function admitUniverseEventBundles(
  current: UniverseEventCache,
  bundles: readonly UniverseEventBundle[],
): UniverseEventCacheAdmission {
  const recordsByKey = new Map(current.recordsByKey);
  const admissionOrder = [...current.admissionOrder];
  const addedKeys: string[] = [];
  const updatedKeys: string[] = [];
  const evictedKeys: string[] = [];

  for (const candidate of bundles) {
    const normalized = normalizeBundle(candidate);
    const key = universeEventBundleKey(normalized);
    const existing = recordsByKey.get(key);
    if (existing) {
      const merged = mergeUniverseEventBundles(existing, normalized);
      if (sameUniverseEventBundle(existing, merged)) continue;
      recordsByKey.set(key, merged);
      updatedKeys.push(key);
      continue;
    }
    recordsByKey.set(key, normalized);
    admissionOrder.push(key);
    addedKeys.push(key);
  }

  while (admissionOrder.length > current.capacity) {
    const key = admissionOrder.shift();
    if (!key) break;
    recordsByKey.delete(key);
    evictedKeys.push(key);
  }

  return {
    cache: {
      capacity: current.capacity,
      recordsByKey,
      admissionOrder,
    },
    addedKeys,
    updatedKeys,
    evictedKeys,
  };
}

export function readUniverseEventBundles(
  cache: UniverseEventCache,
  keys: Iterable<string>,
) {
  const records: UniverseEventBundle[] = [];
  for (const key of keys) {
    const record = cache.recordsByKey.get(key);
    if (record) records.push(record);
  }
  return records;
}
