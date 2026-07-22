import { describe, expect, it } from "vitest";

import type { UniverseActivation } from "@/lib/types";
import {
  LOCAL_ENTITY_SPREAD_MIN,
  LOCAL_ENTITY_SPREAD_RANGE,
  TIMELINE_EVENT_LATERAL_SPREAD,
  dominantSource,
  emptySourceBrowseSession,
  stableOffset,
  stableAccumulationEventOffset,
  stableRootEventOffset,
  stableSatelliteOffset,
} from "./knowledge-universe-model";

describe("knowledge universe model", () => {
  it("keeps approaching packages central before perspective fans them outward", () => {
    expect(TIMELINE_EVENT_LATERAL_SPREAD).toBeGreaterThanOrEqual(6);
    expect(TIMELINE_EVENT_LATERAL_SPREAD).toBeLessThanOrEqual(7);
    expect(LOCAL_ENTITY_SPREAD_MIN).toBeGreaterThanOrEqual(100);
    expect(LOCAL_ENTITY_SPREAD_MIN + LOCAL_ENTITY_SPREAD_RANGE)
      .toBeGreaterThanOrEqual(190);
    expect(LOCAL_ENTITY_SPREAD_MIN + LOCAL_ENTITY_SPREAD_RANGE)
      .toBeLessThanOrEqual(210);
  });

  it("builds isolated browse state with the requested window limits", () => {
    const session = emptySourceBrowseSession(7, "source-a", 12, 36);

    expect(session.sourceId).toBe("source-a");
    expect(session.working.epoch).toBe(7);
    expect(session.timeline.window.visibleLimit).toBe(12);
    expect(session.timeline.window.cacheLimit).toBe(36);
    expect(session.timeline.preferredDirection).toBe("older");
  });

  it("keeps deterministic projection offsets stable", () => {
    expect(stableOffset("entity-a", 120)).toEqual(stableOffset("entity-a", 120));
    expect(stableRootEventOffset("source-a", "event-a", 120, 3, 12)).toEqual(
      stableRootEventOffset("source-a", "event-a", 120, 3, 12),
    );
    expect(stableOffset("entity-a", 120)).not.toEqual(stableOffset("entity-b", 120));
  });

  it("keeps accumulated event coordinates stable as later answers arrive", () => {
    const first = stableAccumulationEventOffset("event:source-a:event-a");
    expect(first).toEqual(
      stableAccumulationEventOffset("event:source-a:event-a"),
    );
    expect(first).not.toEqual(
      stableAccumulationEventOffset("event:source-a:event-b"),
    );
    expect(Math.hypot(first.x, first.y, first.z)).toBeGreaterThan(45);
  });

  it("keeps entity satellites local to their event's temporal plane", () => {
    const offsets = Array.from({ length: 40 }, (_, index) =>
      stableSatelliteOffset(`entity-${index}`, 38));

    expect(Math.max(...offsets.map((offset) => Math.abs(offset.z))))
      .toBeLessThanOrEqual(10);
    expect(Math.min(...offsets.map((offset) => Math.hypot(offset.x, offset.y))))
      .toBeGreaterThan(20);
    expect(Math.max(...offsets.map((offset) => Math.hypot(
      offset.x,
      offset.y,
      offset.z,
    )))).toBeLessThanOrEqual(40);
  });

  it("uses stable radial slots so a focused relation network does not reflow", () => {
    const parent = { x: 84, y: -36, z: -300 };
    const offsets = Array.from({ length: 8 }, (_, index) =>
      stableSatelliteOffset(`entity-${index}`, 38, parent, {
        index,
        total: 8,
        phaseKey: "event-a",
      }));

    expect(offsets).toEqual(Array.from({ length: 8 }, (_, index) =>
      stableSatelliteOffset(`entity-${index}`, 38, parent, {
        index,
        total: 8,
        phaseKey: "event-a",
      })));
    expect(Math.abs(offsets.reduce((sum, offset) => sum + offset.x, 0) / offsets.length))
      .toBeLessThan(4);
    expect(Math.abs(offsets.reduce((sum, offset) => sum + offset.y, 0) / offsets.length))
      .toBeLessThan(4);
  });

  it("reserves the inner field for the latest non-temporal root", () => {
    const first = stableRootEventOffset("source-a", "event-0", 100, 0, 12);
    const latest = stableRootEventOffset("source-a", "event-11", 100, 11, 12);
    expect(Math.hypot(first.x, first.y)).toBeGreaterThan(
      Math.hypot(latest.x, latest.y),
    );
  });

  it("prefers explicit search evidence and otherwise finds the dominant event source", () => {
    const activation = {
      source_hits: [{ source_id: "explicit" }],
      nodes: [
        { kind: "event", source_id: "secondary" },
        { kind: "event", source_id: "secondary" },
      ],
    } as unknown as UniverseActivation;
    expect(dominantSource(activation)).toBe("explicit");

    expect(dominantSource({
      source_hits: [],
      nodes: [
        { kind: "event", source_id: "a" },
        { kind: "entity", source_id: "a" },
        { kind: "event", source_id: "b" },
        { kind: "event", source_id: "b" },
      ],
    } as unknown as UniverseActivation)).toBe("b");
  });
});
