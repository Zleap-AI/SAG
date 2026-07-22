import { describe, expect, it } from "vitest";

import {
  DEFAULT_WINDOW_MODE,
  WINDOW_MODE_STORAGE_KEY,
  WINDOW_SIZE_STORAGE_KEY,
  clampWindowSize,
  persistWindowMode,
  persistWindowSize,
  readWindowMode,
  readWindowSize,
  resolveWindowScalingEnabled,
  type WindowLayoutStorage,
} from "./window-layout";

function memoryStorage(initial: Record<string, string> = {}): WindowLayoutStorage {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
}

describe("window layout", () => {
  it("enables browser window scaling by default and accepts explicit false values", () => {
    expect(resolveWindowScalingEnabled(undefined)).toBe(true);
    expect(resolveWindowScalingEnabled("")).toBe(true);
    expect(resolveWindowScalingEnabled("true")).toBe(true);
    expect(resolveWindowScalingEnabled("false")).toBe(false);
    expect(resolveWindowScalingEnabled("OFF")).toBe(false);
    expect(resolveWindowScalingEnabled("0")).toBe(false);
  });

  it("preserves the original window preference keys", () => {
    const storage = memoryStorage();

    expect(readWindowMode(storage)).toBe(DEFAULT_WINDOW_MODE);
    persistWindowMode(storage, "full");
    persistWindowSize(storage, { width: 1200, height: 720 });

    expect(storage.getItem(WINDOW_MODE_STORAGE_KEY)).toBe("full");
    expect(storage.getItem(WINDOW_SIZE_STORAGE_KEY)).toBe(
      JSON.stringify({ width: 1200, height: 720 }),
    );
  });

  it("restores and clamps a saved window to the current viewport", () => {
    const storage = memoryStorage({
      [WINDOW_SIZE_STORAGE_KEY]: JSON.stringify({ width: 1800, height: 1000 }),
    });

    expect(readWindowSize(storage, { width: 1440, height: 900 })).toEqual({
      width: 1408,
      height: 868,
    });
    expect(clampWindowSize({ width: 100, height: 100 }, { width: 1440, height: 900 }))
      .toEqual({ width: 900, height: 560 });
  });

  it("survives blocked storage", () => {
    const blocked: WindowLayoutStorage = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };

    expect(readWindowMode(blocked)).toBe(DEFAULT_WINDOW_MODE);
    expect(readWindowSize(blocked, { width: 1280, height: 800 })).toEqual({
      width: 1248,
      height: 768,
    });
  });
});
