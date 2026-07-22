import { describe, expect, it } from "vitest";

import {
  UNIVERSE_TIMELINE_AUTOPLAY_DELAY_MS,
  canAdvanceUniverseTimelinePlayback,
  planUniverseTimelinePlayback,
  toggleUniverseTimelinePlaybackOrder,
  universeTimelinePlaybackDirection,
  universeTimelinePlaybackSceneDirection,
  type UniverseTimelinePlaybackPlanInput,
} from "./universe-timeline-playback";

function input(
  overrides: Partial<UniverseTimelinePlaybackPlanInput> = {},
): UniverseTimelinePlaybackPlanInput {
  return {
    enabled: true,
    order: "reverse",
    hasOlder: true,
    hasNewer: false,
    documentHidden: false,
    reducedMotion: false,
    locked: false,
    loading: false,
    transitioning: false,
    ...overrides,
  };
}

describe("universe timeline playback", () => {
  it("plays reverse order from newest toward older through scene.next", () => {
    expect(universeTimelinePlaybackDirection("reverse")).toBe("older");
    expect(universeTimelinePlaybackSceneDirection("older")).toBe("next");
    expect(planUniverseTimelinePlayback(input())).toEqual({
      status: "ready",
      reason: null,
      direction: "older",
      sceneDirection: "next",
      delayMs: UNIVERSE_TIMELINE_AUTOPLAY_DELAY_MS,
    });
  });

  it("plays chronological order only from the current point toward newer", () => {
    expect(universeTimelinePlaybackDirection("chronological")).toBe("newer");
    expect(universeTimelinePlaybackSceneDirection("newer")).toBe("previous");
    expect(planUniverseTimelinePlayback(input({
      order: "chronological",
      hasOlder: true,
      hasNewer: true,
    }))).toMatchObject({
      status: "ready",
      direction: "newer",
      sceneDirection: "previous",
    });
  });

  it("does not fake an oldest starting point for chronological playback", () => {
    expect(planUniverseTimelinePlayback(input({
      order: "chronological",
      hasOlder: true,
      hasNewer: false,
    }))).toEqual({
      status: "paused",
      reason: "boundary",
      direction: "newer",
      sceneDirection: "previous",
      delayMs: null,
    });
  });

  it("stops reverse playback at the true older boundary", () => {
    expect(canAdvanceUniverseTimelinePlayback("older", {
      hasOlder: false,
      hasNewer: true,
    })).toBe(false);
    expect(planUniverseTimelinePlayback(input({
      hasOlder: false,
      hasNewer: true,
    }))).toMatchObject({ status: "paused", reason: "boundary" });
  });

  it.each([
    ["disabled", { enabled: false }],
    ["document-hidden", { documentHidden: true }],
    ["reduced-motion", { reducedMotion: true }],
    ["locked", { locked: true }],
    ["loading", { loading: true }],
    ["transitioning", { transitioning: true }],
  ] as const)("pauses for %s", (reason, overrides) => {
    expect(planUniverseTimelinePlayback(input(overrides))).toEqual({
      status: "paused",
      reason,
      direction: "older",
      sceneDirection: "next",
      delayMs: null,
    });
  });

  it("toggles order without mutating or reordering timeline data", () => {
    expect(toggleUniverseTimelinePlaybackOrder("reverse"))
      .toBe("chronological");
    expect(toggleUniverseTimelinePlaybackOrder("chronological"))
      .toBe("reverse");
  });
});
