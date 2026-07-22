import { describe, expect, it } from "vitest";

import {
  APP_INITIALIZATION_DEFAULTS,
  APP_INITIALIZATION_STORAGE_KEYS,
  rememberThemeBeforeExplore,
  restoreThemeAfterExplore,
  dismissQuickModelSetup,
  persistPetCollapsed,
  persistPetPresence,
  persistAppMode,
  readInitialPetCollapsed,
  readInitialPetPresence,
  readInitialAppState,
  shouldShowPet,
  shouldShowQuickModelSetup,
  type InitializationStorage,
} from "./app-initialization";

function memoryStorage(initial: Record<string, string> = {}): InitializationStorage & {
  removeItem: (key: string) => void;
} {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
}

describe("app initialization", () => {
  it("opens the normal workspace with an always-available collapsed pet for new users", () => {
    const storage = memoryStorage();

    expect(readInitialAppState(storage)).toEqual({ mode: "normal", section: "answer" });
    expect(readInitialPetPresence(storage)).toBe("always");
    expect(readInitialPetCollapsed(storage)).toBe(true);
    expect(APP_INITIALIZATION_DEFAULTS).toMatchObject({
      appMode: "normal",
      workspaceSection: "answer",
      petPresence: "always",
      petCollapsed: true,
    });
  });

  it("preserves explicit existing workspace and pet choices", () => {
    const storage = memoryStorage({
      [APP_INITIALIZATION_STORAGE_KEYS.appMode]: "explore",
      [APP_INITIALIZATION_STORAGE_KEYS.workspaceSection]: "knowledge",
      [APP_INITIALIZATION_STORAGE_KEYS.petPresence]: "explore-only",
      [APP_INITIALIZATION_STORAGE_KEYS.petCollapsed]: "false",
    });

    expect(readInitialAppState(storage)).toEqual({ mode: "explore", section: "knowledge" });
    expect(readInitialPetPresence(storage)).toBe("explore-only");
    expect(readInitialPetCollapsed(storage)).toBe(false);
  });

  it("falls back from invalid values and migrates legacy workspace choices", () => {
    const invalid = memoryStorage({
      [APP_INITIALIZATION_STORAGE_KEYS.appMode]: "broken",
      [APP_INITIALIZATION_STORAGE_KEYS.workspaceSection]: "unknown",
      [APP_INITIALIZATION_STORAGE_KEYS.petCollapsed]: "unknown",
    });
    const legacy = memoryStorage({
      [APP_INITIALIZATION_STORAGE_KEYS.legacyWorkspacePanel]: "mini",
      [APP_INITIALIZATION_STORAGE_KEYS.legacyWorkspaceMini]: "search",
      [APP_INITIALIZATION_STORAGE_KEYS.legacyPetEnabled]: "off",
    });

    expect(readInitialAppState(invalid)).toEqual({ mode: "normal", section: "answer" });
    expect(readInitialPetCollapsed(invalid)).toBe(true);
    expect(readInitialAppState(legacy)).toEqual({ mode: "explore", section: "search" });
    expect(readInitialPetPresence(legacy)).toBe("explore-only");
  });

  it("maps the removed hidden workspace state back to normal mode", () => {
    const storage = memoryStorage({
      [APP_INITIALIZATION_STORAGE_KEYS.legacyWorkspacePanel]: "hidden",
    });

    expect(readInitialAppState(storage).mode).toBe("normal");
  });

  it("always shows the pet while exploring and otherwise follows its presence policy", () => {
    expect(shouldShowPet("explore", "always")).toBe(true);
    expect(shouldShowPet("explore", "explore-only")).toBe(true);
    expect(shouldShowPet("normal", "always")).toBe(true);
    expect(shouldShowPet("normal", "explore-only")).toBe(false);
  });

  it("uses friendly defaults when browser storage is unavailable", () => {
    const blockedStorage: InitializationStorage = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };

    expect(readInitialAppState(blockedStorage)).toEqual({
      mode: "normal",
      section: "answer",
    });
    expect(readInitialPetPresence(blockedStorage)).toBe("always");
    expect(readInitialPetCollapsed(blockedStorage)).toBe(true);
    expect(() => persistAppMode(blockedStorage, "normal")).not.toThrow();
  });

  it("persists later user choices", () => {
    const storage = memoryStorage();

    persistAppMode(storage, "explore", "knowledge");
    persistPetPresence(storage, "explore-only");
    persistPetCollapsed(storage, false);

    expect(readInitialAppState(storage)).toEqual({ mode: "explore", section: "knowledge" });
    expect(readInitialPetPresence(storage)).toBe("explore-only");
    expect(readInitialPetCollapsed(storage)).toBe(false);
  });

  it("restores only the theme captured before exploration", () => {
    const storage = memoryStorage();

    expect(rememberThemeBeforeExplore(storage, "light")).toBe("light");
    expect(rememberThemeBeforeExplore(storage, "dark")).toBe("light");
    expect(restoreThemeAfterExplore(storage)).toBe("light");
    expect(restoreThemeAfterExplore(storage)).toBeNull();

    expect(rememberThemeBeforeExplore(storage, "dark")).toBe("dark");
    expect(restoreThemeAfterExplore(storage)).toBe("dark");
  });

  it("shows model setup once and respects an explicit skip", () => {
    const storage = memoryStorage();

    expect(shouldShowQuickModelSetup(true, storage)).toBe(true);
    expect(shouldShowQuickModelSetup(false, storage)).toBe(false);

    dismissQuickModelSetup(storage);
    expect(shouldShowQuickModelSetup(true, storage)).toBe(false);
  });
});
