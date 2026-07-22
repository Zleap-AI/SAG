import { describe, expect, it } from "vitest";

import {
  universeCardMorph,
  universeNodeEmergence,
} from "@/lib/universe-presentation";
import { planUniverseSceneDelta } from "@/lib/universe-scene-transition";
import {
  advanceUniverseSourceExitGate,
  applyUniverseTemporalFlightWheel,
  armUniverseSourceExitGate,
  createUniverseTemporalFlightState,
  planUniverseTemporalFlightFollow,
  stepUniverseTemporalFlight,
  universeTemporalFlightPresence,
} from "@/lib/universe-temporal-flight";

describe("knowledge universe scene behavior", () => {
  it("retains overlapping object identities across an incremental window change", () => {
    expect(planUniverseSceneDelta(
      ["event-a", "shared", "event-b"],
      ["shared", "event-b", "event-c"],
    )).toEqual({
      retainedIds: ["shared", "event-b"],
      enteringIds: ["event-c"],
      exitingIds: ["event-a"],
      topologyChanged: true,
    });
  });

  it("uses one reversible particle, star and whole-card lifecycle", () => {
    const progress = [0, 0.2, 0.45, 0.7, 1];
    const forward = progress.map((value) =>
      universeNodeEmergence(value, "event", 0.35));
    const reverse = [...progress].reverse().map((value) =>
      universeNodeEmergence(value, "event", 0.35)).reverse();

    expect(reverse).toEqual(forward);
    expect(forward[0]).toMatchObject({ star: 0, card: 0, blur: 7 });
    expect(forward.at(-1)).toMatchObject({
      star: 1,
      card: 1,
      cardScale: 1,
      blur: 0,
    });
    forward.slice(1).forEach((state, index) => {
      expect(state.star).toBeGreaterThanOrEqual(forward[index].star);
      expect(state.card).toBeGreaterThanOrEqual(forward[index].card);
      expect(state.cardScale).toBeGreaterThanOrEqual(
        forward[index].cardScale,
      );
    });
  });

  it("scales metadata, title and summary as one card object", () => {
    expect(universeCardMorph(0)).toEqual({ reveal: 0, scale: 0.72 });
    expect(universeCardMorph(1)).toEqual({ reveal: 1, scale: 1 });
    expect(universeCardMorph(0.6).scale).toBeGreaterThan(
      universeCardMorph(0.3).scale,
    );
  });

  it("uses wheel travel for time in exploration and preserves native zoom elsewhere", () => {
    const initial = createUniverseTemporalFlightState(120);
    const deeper = applyUniverseTemporalFlightWheel(initial, {
      deltaY: -120,
      deltaMode: 0,
      viewportHeight: 900,
      reducedMotion: true,
    });
    const newer = applyUniverseTemporalFlightWheel(initial, {
      deltaY: 120,
      deltaMode: 0,
      viewportHeight: 900,
      reducedMotion: true,
    });

    expect(deeper.depth).toBeGreaterThan(initial.depth);
    expect(newer.depth).toBeLessThan(initial.depth);
    expect(deeper.targetDepth).toBeNull();
  });

  it("prefetches the visual window before temporal flight reaches its edge", () => {
    expect(planUniverseTemporalFlightFollow({
      depth: 930,
      windowNearDepth: 500,
      windowFarDepth: 1_000,
      marginUnits: 80,
      velocity: 240,
      busy: false,
      hasNext: true,
      hasPrevious: true,
    })).toBe("next");
    expect(planUniverseTemporalFlightFollow({
      depth: 930,
      windowNearDepth: 500,
      windowFarDepth: 1_000,
      marginUnits: 80,
      velocity: 240,
      busy: true,
      hasNext: true,
      hasPrevious: true,
    })).toBeNull();
  });

  it("settles inertial flight and keeps reached facts fully legible", () => {
    const moving = applyUniverseTemporalFlightWheel(
      createUniverseTemporalFlightState(120),
      {
        deltaY: -80,
        deltaMode: 0,
        viewportHeight: 900,
      },
    );
    const stepped = stepUniverseTemporalFlight(moving, {
      elapsedMs: 16,
      maxDepth: 2_000,
    });
    expect(stepped.state.depth).toBeGreaterThan(120);
    expect(universeTemporalFlightPresence(0, 60)).toEqual({
      scale: 1,
      opacity: 1,
      card: 1,
    });
    expect(universeTemporalFlightPresence(-300, 60).opacity).toBeLessThan(1);
    expect(universeTemporalFlightPresence(900, 60).scale).toBeLessThan(1);
  });

  it("requires a deliberate second outward gesture before leaving a source", () => {
    const armed = armUniverseSourceExitGate(1_000);
    const inertia = advanceUniverseSourceExitGate(armed, {
      now: 1_100,
      deltaY: 120,
      deltaMode: 0,
      viewportHeight: 900,
    });
    expect(inertia.exitRequested).toBe(false);

    const deliberate = advanceUniverseSourceExitGate(inertia.gate, {
      now: 1_700,
      deltaY: 120,
      deltaMode: 0,
      viewportHeight: 900,
    });
    expect(deliberate.exitRequested).toBe(true);
  });
});
