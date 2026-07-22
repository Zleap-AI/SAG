import { describe, expect, it } from "vitest";

import {
  DEFAULT_PET_FACE_PRESETS,
  PET_APPEARANCE_LIMITS,
  migrateLegacyPetAppearancePreferences,
  normalizePetAppearancePreferences,
  resolvePetFace,
} from "./pet-appearance-preferences";

describe("pet appearance preferences", () => {
  it("normalizes untrusted values and keeps a bounded unique expression library", () => {
    const preferences = normalizePetAppearancePreferences({
      faceMode: "custom",
      face: "  @_@  ",
      facePresets: [" ^_^ ", "^_^", "", "o_o"],
      size: 99,
      floatStrength: -2,
      actionRate: "1.4",
      expressionDelay: Number.NaN,
      reduceMotion: true,
    });

    expect(preferences).toMatchObject({
      version: 1,
      faceMode: "custom",
      face: "@_@",
      facePresets: ["^_^", "o_o"],
      size: PET_APPEARANCE_LIMITS.size.max,
      floatStrength: PET_APPEARANCE_LIMITS.floatStrength.min,
      actionRate: 1.4,
      expressionDelay: PET_APPEARANCE_LIMITS.expressionDelay.default,
      reduceMotion: true,
    });
  });

  it("migrates the former per-field records without losing user choices", () => {
    const values = new Map<string, string>([
      ["sag:pet-face", "Z_Z"],
      ["sag:pet-size", "1.2"],
      ["sag:pet-action-rate", "0"],
      ["sag:pet-face-presets", JSON.stringify(["Z_Z", "^_^"])],
      ["sag:pet-reduce-motion", "true"],
    ]);
    const migrated = migrateLegacyPetAppearancePreferences(
      (key) => values.get(key) ?? null,
    );

    expect(migrated.faceMode).toBe("custom");
    expect(migrated.face).toBe("Z_Z");
    expect(migrated.facePresets).toEqual(["Z_Z", "^_^"]);
    expect(migrated.size).toBe(1.2);
    expect(migrated.actionRate).toBe(0);
    expect(migrated.reduceMotion).toBe(true);
  });

  it("uses the assistant avatar in agent mode and permits a blank custom visor", () => {
    expect(resolvePetFace({ faceMode: "agent", face: "" }, " AI ")).toBe("AI");
    expect(resolvePetFace({ faceMode: "custom", face: "" }, "AI")).toBe("");
    expect(DEFAULT_PET_FACE_PRESETS.length).toBeGreaterThan(5);
  });
});
