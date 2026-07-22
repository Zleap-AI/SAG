import { describe, expect, it } from "vitest";

import {
  planUniversePreviewCards,
  selectUniverseViewpointCardIds,
  universeAccumulationCardViewScore,
  universeCardApertureBucket,
  type UniversePreviewNode,
} from "./universe-preview-plan";

const nodes: UniversePreviewNode[] = [
  { id: "event-a", kind: "event", sourceId: "source-a", active: true },
  { id: "event-b", kind: "event", sourceId: "source-a", active: true },
  { id: "event-c", kind: "event", sourceId: "source-a", active: true },
  { id: "entity-a", kind: "entity", sourceId: "source-a", active: true },
  { id: "entity-b", kind: "entity", sourceId: "source-a", active: true },
];

const adjacency = new Map<string, ReadonlySet<string>>([
  ["event-a", new Set(["entity-a"])],
  ["event-b", new Set(["entity-b"])],
  ["event-c", new Set()],
  ["entity-a", new Set(["event-a"])],
  ["entity-b", new Set(["event-b"])],
]);

function plan(
  overrides: Partial<Parameters<typeof planUniversePreviewCards>[0]> = {},
) {
  return planUniversePreviewCards({
    nodes,
    adjacency,
    sourceId: "source-a",
    focusId: null,
    cardsEnabled: true,
    eventPreviewCount: 2,
    entitySafetyMax: 24,
    ...overrides,
  });
}

describe("universe event card display limit", () => {
  it("limits event cards independently while preserving their related entities", () => {
    expect(plan()).toMatchObject({
      eventIds: ["event-a", "event-b"],
      entityIds: ["entity-a", "entity-b"],
    });
  });

  it("brings every resident entity out with its event card at rest", () => {
    const relatedEntities = Array.from({ length: 8 }, (_, index) => ({
      id: `entity-${index}`,
      kind: "entity" as const,
      sourceId: "source-a",
      active: true,
    }));
    const entityIds = relatedEntities.map((node) => node.id);

    expect(plan({
      nodes: [
        { id: "event-a", kind: "event", sourceId: "source-a", active: true },
        ...relatedEntities,
      ],
      adjacency: new Map([
        ["event-a", new Set(entityIds)],
      ]),
      eventPreviewCount: 1,
    })).toMatchObject({
      eventIds: ["event-a"],
      entityIds,
      hiddenRelatedEntityCount: 0,
    });
  });

  it("lets a focused one-hop network replace the resting card selection", () => {
    expect(plan({
      focusId: "event-c",
      eventPreviewCount: 1,
    })).toMatchObject({
      eventIds: ["event-c"],
      entityIds: [],
      focused: true,
    });
  });

  it("keeps the star network card-free at rest when cards are disabled", () => {
    expect(plan({ cardsEnabled: false }).ids).toEqual([]);
  });

  it("follows the current timeline position instead of pinning the first cards", () => {
    expect(plan({
      nodes: nodes.map((node, index) => ({
        ...node,
        viewDistance: node.kind === "event" ? Math.abs(index - 2) : undefined,
      })),
      eventPreviewCount: 1,
    }).eventIds).toEqual(["event-c"]);
  });

  it("refreshes the bounded aperture every three travelled events", () => {
    expect(universeCardApertureBucket(0, 60)).toBe(0);
    expect(universeCardApertureBucket(179, 60)).toBe(0);
    expect(universeCardApertureBucket(180, 60)).toBe(1);
  });
});

describe("accumulation card viewpoint", () => {
  const sample = {
    projectedZ: 0.2,
    screenX: 800,
    screenY: 450,
    viewportWidth: 1600,
    viewportHeight: 900,
    safeCenterX: 720,
    safeCenterY: 450,
    cameraDistance: 260,
  };

  it("rejects stars behind the camera, outside the reading field, or under UI", () => {
    expect(universeAccumulationCardViewScore({ ...sample, projectedZ: 1.1 })).toBeUndefined();
    expect(universeAccumulationCardViewScore({ ...sample, screenX: 24 })).toBeUndefined();
    expect(universeAccumulationCardViewScore({
      ...sample,
      blockedByOverlay: true,
    })).toBeUndefined();
    expect(universeAccumulationCardViewScore({
      ...sample,
      screenX: 1_450,
    })).toBeUndefined();
  });

  it("ranks the foreground layer ahead of a farther star", () => {
    const foreground = universeAccumulationCardViewScore(sample);
    const background = universeAccumulationCardViewScore({
      ...sample,
      screenX: sample.safeCenterX,
      cameraDistance: 520,
    });
    expect(foreground).toBeLessThan(background ?? Number.POSITIVE_INFINITY);
  });

  it("keeps the clearest cards separated in screen space", () => {
    expect(selectUniverseViewpointCardIds([
      { id: "center", score: 1, x: 800, y: 450 },
      { id: "overlap", score: 2, x: 860, y: 480 },
      { id: "clear", score: 3, x: 1_080, y: 470 },
      { id: "extra", score: 4, x: 500, y: 700 },
    ], {
      maxCount: 2,
      horizontalGap: 220,
      verticalGap: 108,
    })).toEqual(["center", "clear"]);
  });
});
