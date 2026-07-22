import { describe, expect, it } from "vitest";

import {
  createUniverseTemporalAxis,
  projectUniverseTemporalAxis,
  resolveUniverseTemporalAxisPolicy,
  universeTemporalAxisAgeProgress,
  universeTemporalAxisDepth,
} from "./universe-temporal-axis";

/** Ten events in exploration order; the shape every source now shares. */
const TEN = createUniverseTemporalAxis(10);

describe("universe temporal axis", () => {
  it("spends the same depth on every event, whatever its clock time was", () => {
    // ordinal → age is linear: the axis is the exploration order itself. An
    // imported book (every event at one instant) gets exactly the same axis as
    // a two-year diary with the same event count.
    expect(universeTemporalAxisAgeProgress(TEN, 0)).toBe(0);
    expect(universeTemporalAxisAgeProgress(TEN, 9)).toBe(1);
    expect(universeTemporalAxisAgeProgress(TEN, 3)).toBeCloseTo(3 / 9, 10);
  });

  it("clamps ordinals outside the snapshot's own range", () => {
    expect(universeTemporalAxisAgeProgress(TEN, -5)).toBe(0);
    expect(universeTemporalAxisAgeProgress(TEN, 50)).toBe(1);
    expect(universeTemporalAxisAgeProgress(TEN, Number.NaN)).toBe(0);
  });

  it("gives every event the same slice of the axis", () => {
    // The visible window is at most 18 packages, so a per-event length is what
    // decides whether depth is legible — not the source's size or time span.
    expect(universeTemporalAxisDepth(TEN, 60)).toBe(540);
    expect(universeTemporalAxisDepth(createUniverseTemporalAxis(2), 60)).toBe(60);
    expect(universeTemporalAxisDepth(null, 60)).toBe(0);
  });

  it("places an event at exactly ordinal × unit along the axis", () => {
    // depth = age × axisDepth must land on the counting grid, or the flight's
    // per-event margins and the window follow thresholds drift apart.
    const axisDepth = universeTemporalAxisDepth(TEN, 60);
    const [projection] = projectUniverseTemporalAxis(
      [{ bundleId: "x", ordinal: 4 }],
      TEN,
    );
    expect(projection.normalizedOffset.z * axisDepth).toBeCloseTo(-240, 10);
    expect(projection.normalizedOffset.z).toBeCloseTo(-4 / 9, 10);
  });

  it("refuses to build an axis only when there is nothing to explore", () => {
    expect(createUniverseTemporalAxis(0)).toBeNull();
    expect(createUniverseTemporalAxis(-3)).toBeNull();
    expect(createUniverseTemporalAxis(Number.NaN)).toBeNull();
    // A single event still carries an axis: depth 0, nowhere to fly, but the
    // wheel keeps its in-source meaning instead of falling back to zoom.
    expect(createUniverseTemporalAxis(1)).toEqual({ total: 1 });
    expect(universeTemporalAxisAgeProgress(createUniverseTemporalAxis(1), 0)).toBe(0);
    expect(universeTemporalAxisAgeProgress(null, 5)).toBe(0);
  });

  it("keeps a package's projection identical no matter what else is in the batch", () => {
    const alone = projectUniverseTemporalAxis(
      [{ bundleId: "x", ordinal: 7 }],
      TEN,
    );
    const crowded = projectUniverseTemporalAxis([
      { bundleId: "a", ordinal: 1 },
      { bundleId: "x", ordinal: 7 },
      { bundleId: "b", ordinal: 9 },
    ], TEN);

    expect(crowded.find((projection) => projection.bundleId === "x"))
      .toEqual(alone[0]);
  });

  it("separates packages without letting them leave the axis line's spine", () => {
    const [a, b] = projectUniverseTemporalAxis([
      { bundleId: "a", ordinal: 5 },
      { bundleId: "b", ordinal: 5 },
    ], TEN);

    expect(a.normalizedOffset.z).toBe(b.normalizedOffset.z);
    expect(a.normalizedOffset).not.toEqual(b.normalizedOffset);
    // The near end must keep a lateral radius. Collapsing it to a point is only
    // safe when exactly one package can ever sit there, which a real axis cannot
    // promise.
    expect(Math.hypot(a.normalizedOffset.x, a.normalizedOffset.y)).toBeGreaterThan(0);
    expect(Math.hypot(b.normalizedOffset.x, b.normalizedOffset.y)).toBeGreaterThan(0);
  });

  it("keeps package lanes deterministic while ordinal advances the spiral", () => {
    const [near] = projectUniverseTemporalAxis(
      [{ bundleId: "x", ordinal: 1 }],
      TEN,
    );
    const [nearAgain] = projectUniverseTemporalAxis(
      [{ bundleId: "x", ordinal: 1 }],
      TEN,
    );
    const [far] = projectUniverseTemporalAxis(
      [{ bundleId: "x", ordinal: 9 }],
      TEN,
    );

    expect(nearAgain).toEqual(near);
    expect(far.normalizedOffset.z).toBeLessThan(near.normalizedOffset.z);
    expect({ x: far.normalizedOffset.x, y: far.normalizedOffset.y })
      .not.toEqual({ x: near.normalizedOffset.x, y: near.normalizedOffset.y });
  });

  it("keeps every page in the same narrow lateral corridor", () => {
    const largeAxis = createUniverseTemporalAxis(586);
    const ordinals = [0, 1, 3, 7, 35, 292, 585];
    const projections = projectUniverseTemporalAxis(
      ordinals.map((ordinal) => ({ bundleId: `event-${ordinal}`, ordinal })),
      largeAxis,
      { angularPhase: 0 },
    );
    const radii = projections.map(({ normalizedOffset }) => Math.hypot(
      normalizedOffset.x,
      normalizedOffset.y / 0.74,
    ));

    radii.forEach((radius) => expect(radius).toBeCloseTo(0.42, 8));
    // Stable lateral lanes must never compromise the canonical chronology.
    projections.forEach((projection, index) => {
      expect(projection.normalizedOffset.z).toBeCloseTo(
        -ordinals[index] / 585,
        10,
      );
    });
  });

  it("fans the first visible batch around the core instead of one vertical lane", () => {
    const largeAxis = createUniverseTemporalAxis(586);
    const bundles = Array.from({ length: 8 }, (_, ordinal) => ({
      bundleId: `event-${ordinal}`,
      ordinal,
    }));
    const projections = projectUniverseTemporalAxis(
      bundles,
      largeAxis,
      { angularPhase: 0 },
    );
    const rotated = projectUniverseTemporalAxis(
      bundles,
      largeAxis,
      { angularPhase: 0.83 },
    );
    const circularGaps = (items: typeof projections) => {
      const angles = items.map(({ normalizedOffset }) => {
        const angle = Math.atan2(normalizedOffset.y / 0.74, normalizedOffset.x);
        return angle < 0 ? angle + Math.PI * 2 : angle;
      }).sort((left, right) => left - right);
      return angles.map((angle, index) => {
        const next = angles[(index + 1) % angles.length]
          + (index === angles.length - 1 ? Math.PI * 2 : 0);
        return next - angle;
      }).sort((left, right) => left - right);
    };
    const gaps = circularGaps(projections);
    const rotatedGaps = circularGaps(rotated);
    const quadrants = new Set(projections.map(({ normalizedOffset }) =>
      `${normalizedOffset.x >= 0 ? "+" : "-"}${normalizedOffset.y >= 0 ? "+" : "-"}`,
    ));

    expect(quadrants.size).toBe(4);
    expect(Math.max(...gaps)).toBeLessThan(75 * Math.PI / 180);
    gaps.forEach((gap, index) => {
      expect(rotatedGaps[index]).toBeCloseTo(gap, 10);
    });
  });

  it("keeps travel even by default and leaves the curve as a knob", () => {
    // An exponent bends depth away from the count it is supposed to track, so
    // even spacing is the default the axis ships with.
    expect(resolveUniverseTemporalAxisPolicy().ageExponent).toBe(1);
    const [half] = projectUniverseTemporalAxis(
      [{ bundleId: "x", ordinal: 4.5 }],
      TEN,
    );
    expect(half.normalizedOffset.z).toBeCloseTo(-0.5, 10);
  });

  it("keeps overrides monotonic along the axis", () => {
    const policy = resolveUniverseTemporalAxisPolicy({
      farLateralSpread: 0,
      ageExponent: 0,
    });

    expect(policy.farLateralSpread).toBe(policy.nearLateralSpread);
    expect(policy.ageExponent).toBeGreaterThan(0);
  });
});
