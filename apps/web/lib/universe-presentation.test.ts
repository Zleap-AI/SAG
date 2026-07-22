import { describe, expect, it } from "vitest";

import {
  resolveUniverseDetailSource,
  universeCardMorph,
  universeDeepLoadMilestone,
  universeNodeEmergence,
  universeVisualDetailProgress,
} from "./universe-presentation";

describe("universe presentation state", () => {
  it("enters detail only at the near threshold", () => {
    expect(resolveUniverseDetailSource({
      currentSourceId: null,
      currentRadiusPx: null,
      candidateSourceId: "source-a",
      candidateRadiusPx: 179,
      enterRadiusPx: 180,
      exitRadiusPx: 72,
    })).toBeNull();
    expect(resolveUniverseDetailSource({
      currentSourceId: null,
      currentRadiusPx: null,
      candidateSourceId: "source-a",
      candidateRadiusPx: 180,
      enterRadiusPx: 180,
      exitRadiusPx: 72,
    })).toBe("source-a");
  });

  it("keeps the activated source through orbit, pan, and ordinary zoom", () => {
    expect(resolveUniverseDetailSource({
      currentSourceId: "source-a",
      currentRadiusPx: 116,
      candidateSourceId: "source-b",
      candidateRadiusPx: 220,
      enterRadiusPx: 180,
      exitRadiusPx: 72,
    })).toBe("source-a");
    expect(resolveUniverseDetailSource({
      currentSourceId: "source-a",
      currentRadiusPx: 116,
      candidateSourceId: null,
      candidateRadiusPx: null,
      enterRadiusPx: 180,
      exitRadiusPx: 72,
    })).toBe("source-a");
  });

  it("returns the whole source to overview only at the exit threshold", () => {
    expect(resolveUniverseDetailSource({
      currentSourceId: "source-a",
      currentRadiusPx: 72,
      candidateSourceId: "source-a",
      candidateRadiusPx: 72,
      enterRadiusPx: 180,
      exitRadiusPx: 72,
    })).toBeNull();
  });

  it("lets an explicit node focus establish its source immediately", () => {
    expect(resolveUniverseDetailSource({
      currentSourceId: null,
      currentRadiusPx: null,
      candidateSourceId: null,
      candidateRadiusPx: null,
      explicitSourceId: "source-b",
      enterRadiusPx: 180,
      exitRadiusPx: 72,
    })).toBe("source-b");
  });

  it("lets an explicit source switch replace the previous detail latch", () => {
    expect(resolveUniverseDetailSource({
      currentSourceId: "source-a",
      currentRadiusPx: 240,
      candidateSourceId: "source-a",
      candidateRadiusPx: 240,
      explicitSourceId: "source-b",
      enterRadiusPx: 180,
      exitRadiusPx: 72,
    })).toBe("source-b");
  });

  it("uses coarse deep-zoom milestones instead of individual wheel gestures", () => {
    expect(universeDeepLoadMilestone(359, 360, 24)).toBe(0);
    expect(universeDeepLoadMilestone(360, 360, 24)).toBe(1);
    expect(universeDeepLoadMilestone(470, 360, 24)).toBe(1);
    expect(universeDeepLoadMilestone(500, 360, 24)).toBe(2);
  });

  it("morphs continuously from orbit stars through near cards to full cards", () => {
    const radii = [72, 108, 144, 180, 234, 288, 360];
    const progress = radii.map((radius) =>
      universeVisualDetailProgress(radius, 72, 180, 360));

    expect(progress[0]).toBe(0);
    expect(progress[3]).toBeCloseTo(0.5, 5);
    expect(progress[5]).toBe(1);
    expect(progress[6]).toBe(1);
    expect(progress.every((value, index) => index === 0 || value >= progress[index - 1])).toBe(true);
  });

  it("reveals and scales the complete card as one synchronized object", () => {
    const star = universeCardMorph(0);
    const compact = universeCardMorph(0.5);
    const full = universeCardMorph(1);

    expect(star).toEqual({ reveal: 0, scale: 0.72 });
    expect(compact.reveal).toBeGreaterThan(0.99);
    expect(compact.scale).toBeCloseTo(0.86, 5);
    expect(full).toEqual({ reveal: 1, scale: 1 });
  });

  it("condenses a particle into a star before resolving its card", () => {
    const particle = universeNodeEmergence(0, "event", 0);
    const formingStar = universeNodeEmergence(0.18, "event", 0);
    const settledStar = universeNodeEmergence(0.26, "event", 0);
    const resolvingCard = universeNodeEmergence(0.42, "event", 0);
    const resolved = universeNodeEmergence(1, "event", 0);

    expect(particle).toEqual({
      grain: 0,
      star: 0,
      card: 0,
      cloudScale: 0.22,
      starScale: 0.08,
      cardScale: 0.28,
      blur: 7,
    });
    expect(formingStar.grain).toBeGreaterThan(0);
    expect(formingStar.star).toBeGreaterThanOrEqual(0);
    expect(formingStar.card).toBe(0);
    expect(formingStar.cloudScale).toBeGreaterThan(0.22);
    expect(settledStar.star).toBeGreaterThan(0);
    expect(settledStar.card).toBe(0);
    expect(resolvingCard.star).toBe(1);
    expect(resolvingCard.card).toBeGreaterThan(0);
    expect(resolvingCard.cardScale).toBeGreaterThan(0.28);
    expect(resolvingCard.blur).toBeLessThan(7);
    expect(resolved).toMatchObject({
      star: 1,
      card: 1,
      cloudScale: 1,
      starScale: 1,
      cardScale: 1,
      blur: 0,
    });
    expect(resolved.grain).toBeCloseTo(0.28);
  });

  it("lets event stars lead entity stars while preserving per-node stagger", () => {
    const progress = 0.2;
    const event = universeNodeEmergence(progress, "event", 0);
    const staggeredEvent = universeNodeEmergence(progress, "event", 1);
    const entity = universeNodeEmergence(progress, "entity", 0);

    expect(event.star).toBeGreaterThan(entity.star);
    expect(event.star).toBeGreaterThan(staggeredEvent.star);
    expect(event.card).toBe(0);
    expect(entity.card).toBe(0);
  });

  it("keeps visibility and card resolution monotonic in both node phases", () => {
    const progress = Array.from({ length: 101 }, (_, index) => index / 100);

    (["event", "entity"] as const).forEach((kind) => {
      const states = progress.map((value) =>
        universeNodeEmergence(value, kind, 0.64));

      states.forEach((state, index) => {
        expect(state.card).toBeLessThanOrEqual(state.star);
        if (index === 0) return;
        expect(state.star).toBeGreaterThanOrEqual(states[index - 1].star);
        expect(state.card).toBeGreaterThanOrEqual(states[index - 1].card);
        expect(state.cardScale).toBeGreaterThanOrEqual(states[index - 1].cardScale);
        expect(state.blur).toBeLessThanOrEqual(states[index - 1].blur);
      });
    });
  });

  it("grows cloud, star, and card monotonically so reverse travel never pops", () => {
    const states = Array.from({ length: 101 }, (_, index) =>
      universeNodeEmergence(index / 100, "event", 0));
    const scales = Array.from({ length: 101 }, (_, index) =>
      universeNodeEmergence(index / 100, "event", 0).starScale);

    states.slice(1).forEach((state, index) => {
      expect(state.cloudScale).toBeGreaterThanOrEqual(states[index].cloudScale);
      expect(state.starScale).toBeGreaterThanOrEqual(states[index].starScale);
      expect(state.cardScale).toBeGreaterThanOrEqual(states[index].cardScale);
    });
    expect(Math.max(...scales)).toBe(1);
    expect(scales.at(-1)).toBe(1);
  });

  it("hands gathering grains to the star and keeps only a mature halo", () => {
    const gathering = universeNodeEmergence(0.18, "event", 0);
    const condensing = universeNodeEmergence(0.34, "event", 0);
    const mature = universeNodeEmergence(1, "event", 0);

    expect(gathering.grain).toBeGreaterThan(mature.grain);
    expect(condensing.star).toBeGreaterThan(0);
    expect(condensing.grain).toBeGreaterThan(mature.grain);
    expect(mature.grain).toBeCloseTo(0.28);
  });

  it("can write into a cached target without allocating a new state", () => {
    const target = universeNodeEmergence(0, "event", 0);
    const updated = universeNodeEmergence(0.72, "event", 0.4, target);

    expect(updated).toBe(target);
    expect(updated.star).toBe(1);
    expect(updated.card).toBeGreaterThan(0);
  });

  it("is reversible because presentation depends only on journey progress", () => {
    const progress = [0, 0.13, 0.31, 0.52, 0.79, 1];
    const forward = progress.map((value) =>
      universeNodeEmergence(value, "entity", 0.4));
    const backward = [...progress]
      .reverse()
      .map((value) => universeNodeEmergence(value, "entity", 0.4))
      .reverse();

    expect(backward).toEqual(forward);
  });

  it("clamps progress and stagger at their finite boundaries", () => {
    expect(universeNodeEmergence(-1, "event", -1)).toEqual(
      universeNodeEmergence(0, "event", 0),
    );
    expect(universeNodeEmergence(Number.NaN, "event", Number.NaN)).toEqual(
      universeNodeEmergence(0, "event", 0),
    );
    const infinite = universeNodeEmergence(
      Number.POSITIVE_INFINITY,
      "entity",
      4,
    );
    expect(infinite).toMatchObject({
      star: 1,
      card: 1,
      cloudScale: 1,
      starScale: 1,
      cardScale: 1,
      blur: 0,
    });
    expect(infinite.grain).toBeCloseTo(0.28);
    expect(universeNodeEmergence(0.5, "entity", 4)).toEqual(
      universeNodeEmergence(0.5, "entity", 1),
    );
  });
});
