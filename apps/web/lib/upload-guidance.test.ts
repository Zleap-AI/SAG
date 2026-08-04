import { describe, expect, it } from "vitest";

import { uploadConcurrencyGuidance } from "./upload-guidance";

describe("uploadConcurrencyGuidance", () => {
  it("targets the knowledge processing settings", () => {
    expect(uploadConcurrencyGuidance(false)).toEqual({
      href: "/settings?tab=knowledge&section=document-processing",
      textClassName: "max-w-sm text-xs text-muted-foreground",
    });
  });

  it("keeps the settings target in the compact layout", () => {
    expect(uploadConcurrencyGuidance(true)).toEqual({
      href: "/settings?tab=knowledge&section=document-processing",
      textClassName: "max-w-sm text-[11px] leading-relaxed text-muted-foreground",
    });
  });
});
