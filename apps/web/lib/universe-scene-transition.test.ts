import { describe, expect, it } from "vitest";

import {
  planUniverseSceneDelivery,
  planUniverseSceneDelta,
  universeTimelineFanProgress,
} from "./universe-scene-transition";
import {
  advanceUniverseTimelineWindow,
  appendUniverseTimelineBundles,
  createUniverseTimelineWindow,
  settleUniverseTimelineWindow,
} from "./universe-timeline-window";

describe("universe scene incremental transition", () => {
  it("restores a retained exploration without replaying scene entrance", () => {
    expect(planUniverseSceneDelivery({
      strategyBoundary: true,
      restoringExploration: true,
      windowChanged: true,
      timelineJourneyEnabled: true,
    })).toEqual({
      stableRestore: true,
      animateTimelineWindow: false,
      animateEntrants: false,
      autoFocus: false,
    });
  });

  it("keeps ordinary timeline and accumulation deliveries animated", () => {
    expect(planUniverseSceneDelivery({
      strategyBoundary: false,
      restoringExploration: false,
      windowChanged: true,
      timelineJourneyEnabled: true,
    })).toMatchObject({
      stableRestore: false,
      animateTimelineWindow: true,
      animateEntrants: true,
      autoFocus: true,
    });
  });

  it("keeps the overlapping network retained during a time-page step", () => {
    expect(planUniverseSceneDelta(
      ["source", "event-a", "shared", "event-b"],
      ["source", "shared", "event-b", "event-c"],
    )).toEqual({
      retainedIds: ["source", "shared", "event-b"],
      enteringIds: ["event-c"],
      exitingIds: ["event-a"],
      topologyChanged: true,
    });
  });

  it("recognizes metadata-only window changes without topology churn", () => {
    expect(planUniverseSceneDelta(
      ["source", "event-a", "entity-a"],
      ["source", "event-a", "entity-a"],
    )).toMatchObject({
      retainedIds: ["source", "event-a", "entity-a"],
      enteringIds: [],
      exitingIds: [],
      topologyChanged: false,
    });
  });

  it("moves every age through a bounded continuous fan", () => {
    const samples = Array.from({ length: 18 }, (_, age) =>
      universeTimelineFanProgress(age, 18));
    expect(samples[0]).toBe(0);
    expect(samples.at(-1)).toBe(1);
    samples.slice(1).forEach((value, index) => {
      expect(value).toBeGreaterThan(samples[index]);
      expect(value).toBeLessThanOrEqual(1);
    });
  });

  it("keeps cache and rendered window separate while moving forward and backward", () => {
    const ids = Array.from({ length: 24 }, (_, index) => `bundle-${index}`);
    let window = appendUniverseTimelineBundles(
      createUniverseTimelineWindow(4, 24),
      ids,
    );
    for (let step = 0; step < 12; step += 1) {
      window = settleUniverseTimelineWindow(
        advanceUniverseTimelineWindow(window, "next", 1),
      );
    }
    expect(window.activeIndex).toBe(15);
    expect(window.visitedCount).toBe(16);
    expect(window.cacheBundleIds).toHaveLength(24);
    expect(window.visibleBundleIds).toHaveLength(4);
    const forwardVisible = [...window.visibleBundleIds];

    for (let step = 0; step < 5; step += 1) {
      window = settleUniverseTimelineWindow(
        advanceUniverseTimelineWindow(window, "previous", 1),
      );
    }
    expect(window.visibleBundleIds).toHaveLength(4);
    expect(window.cacheBundleIds).toEqual(ids);
    expect(window.visitedCount).toBe(16);

    for (let step = 0; step < 5; step += 1) {
      window = settleUniverseTimelineWindow(
        advanceUniverseTimelineWindow(window, "next", 1),
      );
    }
    expect(window.visibleBundleIds).toEqual(forwardVisible);
    expect(window.visibleBundleIds).toHaveLength(4);
  });
});
