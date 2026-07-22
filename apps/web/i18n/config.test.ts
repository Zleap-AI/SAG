import { describe, expect, it } from "vitest";

import {
  defaultLocale,
  isAppLocale,
  localeDocumentTag,
  localeFromAcceptLanguage,
} from "./config";

describe("locale configuration", () => {
  it("validates only supported locales", () => {
    expect(isAppLocale("zh-CN")).toBe(true);
    expect(isAppLocale("en-US")).toBe(true);
    expect(isAppLocale("fr-FR")).toBe(false);
    expect(isAppLocale(undefined)).toBe(false);
  });

  it.each([
    ["en-US,en;q=0.9,zh-CN;q=0.2", "en-US"],
    ["zh-Hans-CN,zh;q=0.9,en;q=0.5", "zh-CN"],
    ["fr-FR, en;q=0.8, zh;q=0.9", "zh-CN"],
    ["zh;q=0, en;q=0.7", "en-US"],
    ["de-DE,fr;q=0.8", defaultLocale],
    ["*;q=0.5", defaultLocale],
    [null, defaultLocale],
  ])("selects a supported locale from %s", (header, expected) => {
    expect(localeFromAcceptLanguage(header)).toBe(expected);
  });

  it("uses a valid locale as the document language", () => {
    expect(localeDocumentTag("zh-CN")).toBe("zh-CN");
    expect(localeDocumentTag("en-US")).toBe("en-US");
  });
});
