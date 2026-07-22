import { describe, expect, it } from "vitest";

import { clampPetPosition, resolveExplorePetPosition } from "./pet-placement";

describe("clampPetPosition", () => {
  it("preserves an in-bounds position when the pet changes form", () => {
    expect(clampPetPosition(
      { x: 460, y: 310 },
      { width: 1200, height: 800 },
      { width: 94, height: 118 },
    )).toEqual({ x: 460, y: 310 });
  });

  it("only adjusts the axes that would leave the viewport", () => {
    expect(clampPetPosition(
      { x: 1100, y: 310 },
      { width: 1200, height: 800 },
      { width: 94, height: 118 },
    )).toEqual({ x: 1082, y: 310 });
  });
});

describe("resolveExplorePetPosition", () => {
  it("uses a lower-right safe inset instead of hugging the viewport edge", () => {
    expect(resolveExplorePetPosition({
      viewport: { width: 1200, height: 800 },
      pet: { width: 100, height: 120 },
    })).toEqual({ x: 1004, y: 584 });
  });

  it("moves away from measured graph controls", () => {
    const position = resolveExplorePetPosition({
      viewport: { width: 1000, height: 800 },
      pet: { width: 100, height: 120 },
      avoidRects: [{ x: 780, y: 560, width: 120, height: 220 }],
    });

    const clearsHorizontally = position.x + 100 <= 780;
    const clearsVertically = position.y + 120 <= 560;
    expect(clearsHorizontally || clearsVertically).toBe(true);
  });

  it("keeps the pet inside compact viewports", () => {
    const position = resolveExplorePetPosition({
      viewport: { width: 320, height: 400 },
      pet: { width: 94, height: 118 },
    });

    expect(position.x).toBeGreaterThanOrEqual(24);
    expect(position.y).toBeGreaterThanOrEqual(24);
    expect(position.x + 94).toBeLessThanOrEqual(296);
    expect(position.y + 118).toBeLessThanOrEqual(376);
  });
});
