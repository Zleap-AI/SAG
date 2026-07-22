export interface UniverseProjectionNode {
  id: string;
  kind: "source" | "event" | "entity";
}

export interface UniverseProjectionLink {
  source: string;
  target: string;
  virtual: boolean;
}

/**
 * Accumulation is event-led: an entity may enter the visual topology only
 * when a factual, currently projected relation connects it to an event.
 */
export function projectUniverseAccumulationTopology<
  TNode extends UniverseProjectionNode,
  TLink extends UniverseProjectionLink,
>(nodes: readonly TNode[], links: readonly TLink[]) {
  const eventIds = new Set(
    nodes.filter((node) => node.kind === "event").map((node) => node.id),
  );
  const entityIds = new Set(
    nodes.filter((node) => node.kind === "entity").map((node) => node.id),
  );
  const connectedEntityIds = new Set<string>();
  links.forEach((link) => {
    if (link.virtual) return;
    if (eventIds.has(link.source) && entityIds.has(link.target)) {
      connectedEntityIds.add(link.target);
    }
    if (eventIds.has(link.target) && entityIds.has(link.source)) {
      connectedEntityIds.add(link.source);
    }
  });

  const retainedNodes = nodes.filter((node) =>
    node.kind !== "entity" || connectedEntityIds.has(node.id));
  const retainedIds = new Set(retainedNodes.map((node) => node.id));
  return {
    nodes: retainedNodes,
    links: links.filter((link) =>
      retainedIds.has(link.source) && retainedIds.has(link.target)),
    orphanEntityIds: nodes
      .filter((node) => node.kind === "entity" && !connectedEntityIds.has(node.id))
      .map((node) => node.id),
  };
}
