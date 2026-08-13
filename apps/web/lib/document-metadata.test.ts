import { describe, expect, it } from "vitest";

import { hasRecordedTokenUsage } from "./document-metadata";

describe("document metadata", () => {
  it("hides missing or zero token usage and keeps recorded usage", () => {
    expect(hasRecordedTokenUsage(0)).toBe(false);
    expect(hasRecordedTokenUsage(-1)).toBe(false);
    expect(hasRecordedTokenUsage(477953)).toBe(true);
  });
});
