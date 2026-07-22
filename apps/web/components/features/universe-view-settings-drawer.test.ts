import { describe, expect, it } from "vitest";

import en from "../../messages/en-US.json";
import zh from "../../messages/zh-CN.json";
import {
  DEFAULT_UNIVERSE_VIEW_PREFERENCES,
  UNIVERSE_VIEW_LIMITS,
  minimumUniverseCacheCapacity,
  normalizeUniverseViewPreferences,
} from "../../lib/universe-view-preferences";

describe("knowledge universe settings contract", () => {
  it("exposes one configuration shared by exploration and accumulation", () => {
    expect(DEFAULT_UNIVERSE_VIEW_PREFERENCES).toEqual({
      version: 7,
      cacheCapacity: 1_000,
      eventWindowSize: 50,
      cardsEnabled: true,
      eventCardPreviewCount: 13,
      temporalPageSize: 20,
      temporalPrefetchPages: 3,
      entityTypes: null,
      documentIds: null,
    });
  });

  it("keeps cache, scene window and network page capacities distinct", () => {
    const normalized = normalizeUniverseViewPreferences({
      ...DEFAULT_UNIVERSE_VIEW_PREFERENCES,
      cacheCapacity: 1_000,
      eventWindowSize: 50,
      temporalPageSize: 20,
      temporalPrefetchPages: 3,
    });

    expect(normalized.cacheCapacity).toBe(1_000);
    expect(normalized.eventWindowSize).toBe(50);
    expect(normalized.temporalPageSize).toBe(20);
    expect(normalized.temporalPrefetchPages).toBe(3);
    expect(minimumUniverseCacheCapacity(50, 20, 3)).toBe(200);
  });

  it("supports production-sized ranges without device-specific silent caps", () => {
    expect(UNIVERSE_VIEW_LIMITS.cacheCapacity).toMatchObject({
      min: 200,
      max: 5_000,
      default: 1_000,
    });
    expect(UNIVERSE_VIEW_LIMITS.eventWindowSize).toMatchObject({
      min: 20,
      max: 100,
      default: 50,
    });
    expect(UNIVERSE_VIEW_LIMITS.eventCardPreviewCount).toMatchObject({
      min: 1,
      max: 20,
      default: 13,
    });
  });

  it("repairs filters while preserving all-documents and all-types semantics", () => {
    expect(normalizeUniverseViewPreferences({
      ...DEFAULT_UNIVERSE_VIEW_PREFERENCES,
      entityTypes: [" Person ", "Person", "", "Concept"],
      documentIds: ["doc-b", " doc-a ", "doc-a", ""],
    })).toMatchObject({
      entityTypes: ["Concept", "Person"],
      documentIds: ["doc-a", "doc-b"],
    });
    expect(normalizeUniverseViewPreferences({
      ...DEFAULT_UNIVERSE_VIEW_PREFERENCES,
      entityTypes: null,
      documentIds: null,
    })).toMatchObject({
      entityTypes: null,
      documentIds: null,
    });
  });

  it("keeps user-facing copy aligned in both locales", () => {
    for (const messages of [zh, en]) {
      expect(messages.AppShell.graphSettings).toBeTruthy();
      expect(messages.GraphSettings.drawer.title).toBeTruthy();
      expect(messages.GraphSettings.cards.enabled.title).toBeTruthy();
      expect(messages.GraphSettings.cards.preview.title).toBeTruthy();
      expect(messages.GraphSettings.eventWindow.title).toBeTruthy();
      expect(messages.GraphSettings.cacheCapacity.title).toBeTruthy();
      expect(messages.GraphSettings.temporal.page.title).toBeTruthy();
      expect(messages.GraphSettings.temporal.prefetch.title).toBeTruthy();
      expect(messages.GraphSettings.entityTypes.title).toBeTruthy();
    }
    expect(zh.GraphSettings.entityTypes.description)
      .toContain("实体及其事项连线一起隐藏");
    expect(en.GraphSettings.entityTypes.description)
      .toContain("entities and their event links hide together");
    expect(JSON.stringify(zh.GraphSettings)).not.toContain("事件包");
    expect(JSON.stringify(en.GraphSettings).toLowerCase())
      .not.toContain("event bundle");
  });
});
