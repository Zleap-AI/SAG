import type {
  SearchSourceHit,
  UniverseActivation,
  UniverseActivationNode,
  UniverseGraphPatch,
  UniverseRelation,
  UniverseTimelineSlice,
} from "./types";
import {
  mergeUniverseEventBundles,
  universeEventBundleKey,
  type UniverseEventBundle,
  type UniverseEventBundleOrigin,
} from "./universe-event-cache";

export interface UniverseBundleFilters {
  entityTypes?: readonly string[] | null;
  documentIds?: readonly string[] | null;
}

function relationKey(relation: UniverseRelation) {
  return [
    relation.source_id,
    relation.kind,
    relation.from_id,
    relation.to_id,
  ].join(":");
}

function selected(value: string, selection: ReadonlySet<string> | null) {
  return selection === null || selection.has(value);
}

function normalizeSelection(values: readonly string[] | null | undefined) {
  if (!values?.length) return null;
  return new Set(values.map((value) => value.trim()).filter(Boolean));
}

function sourceIdForNode(
  node: UniverseActivationNode,
  fallbackSourceId: string,
) {
  return node.source_id?.trim() || fallbackSourceId;
}

/**
 * Converts a graph-shaped response into atomic event bundles. Entities and
 * relations enter only through the event they qualify, so filtering cannot
 * leave dangling relation objects behind.
 */
export function universeEventBundlesFromGraph(
  nodes: readonly UniverseActivationNode[],
  relations: readonly UniverseRelation[],
  origin: UniverseEventBundleOrigin,
  fallbackSourceId = "",
  filters: UniverseBundleFilters = {},
): UniverseEventBundle[] {
  const entityTypes = normalizeSelection(filters.entityTypes);
  // Document scope is enforced by the query contract. Activation nodes expose
  // chunk ids, not document ids, so treating one as the other would silently
  // drop valid evidence.
  const events = nodes.filter(
    (node): node is UniverseActivationNode & { kind: "event" } =>
      node.kind === "event"
      && Boolean(node.id.trim()),
  );
  const entities = nodes.filter(
    (node): node is UniverseActivationNode & { kind: "entity" } =>
      node.kind === "entity"
      && Boolean(node.id.trim())
      && selected(node.category ?? "", entityTypes),
  );
  const entitiesByIdentity = new Map<string, typeof entities[number]>();
  for (const entity of entities) {
    const sourceId = sourceIdForNode(entity, fallbackSourceId);
    entitiesByIdentity.set(`${sourceId}:${entity.id}`, {
      ...entity,
      source_id: sourceId,
    });
  }

  const bundles = new Map<string, UniverseEventBundle>();
  for (const event of events) {
    const sourceId = sourceIdForNode(
      event,
      relations.find((relation) =>
        relation.from_id === event.id)?.source_id || fallbackSourceId,
    );
    if (!sourceId) continue;
    const eventRelations = new Map<string, UniverseRelation>();
    const eventEntities = new Map<string, typeof entities[number]>();
    for (const relation of relations) {
      const relationSourceId = relation.source_id || sourceId;
      if (relationSourceId !== sourceId || relation.from_id !== event.id) continue;
      if (relation.kind === "subevent") {
        eventRelations.set(relationKey(relation), {
          ...relation,
          source_id: sourceId,
        });
        continue;
      }
      const entity = entitiesByIdentity.get(`${sourceId}:${relation.to_id}`);
      if (!entity) continue;
      eventEntities.set(entity.id, entity);
      eventRelations.set(relationKey(relation), {
        ...relation,
        source_id: sourceId,
      });
    }
    const bundle: UniverseEventBundle = {
      origin,
      sourceId,
      event: {
        ...event,
        source_id: sourceId,
      },
      entities: [...eventEntities.values()],
      relations: [...eventRelations.values()],
      temporalKey: event.start_time ?? undefined,
      documentId: event.chunk_id ?? null,
    };
    const key = universeEventBundleKey(bundle);
    const current = bundles.get(key);
    bundles.set(
      key,
      current ? mergeUniverseEventBundles(current, bundle) : bundle,
    );
  }
  return [...bundles.values()];
}

export function universeEventBundlesFromActivation(
  activation: UniverseActivation,
  filters: UniverseBundleFilters = {},
) {
  return universeEventBundlesFromGraph(
    activation.nodes,
    activation.relations,
    activation.origin === "search"
      ? "search"
      : activation.origin === "assistant"
        ? "assistant"
        : "expansion",
    "",
    filters,
  );
}

export function universeEventBundlesFromTimeline(
  page: UniverseTimelineSlice,
  filters: UniverseBundleFilters = {},
) {
  return page.bundles.flatMap((bundle) => universeEventBundlesFromGraph(
    [bundle.event, ...bundle.nodes],
    bundle.relations,
    "timeline",
    page.source_id,
    filters,
  ).map((record) => ({
    ...record,
    ordinal: bundle.ordinal,
  })));
}

export function universeEventBundlesFromPatch(
  patch: UniverseGraphPatch,
  filters: UniverseBundleFilters = {},
) {
  return universeEventBundlesFromGraph(
    [patch.anchor, ...patch.nodes],
    patch.relations,
    "expansion",
    patch.source_id,
    filters,
  );
}

export function universeActivationFromEventBundles({
  bundles,
  epoch,
  query,
  origin,
  sourceHits,
}: {
  bundles: readonly UniverseEventBundle[];
  epoch: number;
  query: string;
  origin: UniverseActivation["origin"];
  sourceHits?: SearchSourceHit[];
}): UniverseActivation {
  const nodes = new Map<string, UniverseActivationNode>();
  const relations = new Map<string, UniverseRelation>();
  for (const bundle of bundles) {
    nodes.set(`${bundle.sourceId}:event:${bundle.event.id}`, bundle.event);
    for (const entity of bundle.entities) {
      nodes.set(`${bundle.sourceId}:entity:${entity.id}`, entity);
    }
    for (const relation of bundle.relations) {
      relations.set(relationKey(relation), relation);
    }
  }
  return {
    epoch,
    origin,
    query,
    nodes: [...nodes.values()],
    relations: [...relations.values()],
    source_hits: sourceHits,
  };
}
