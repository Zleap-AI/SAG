import { describe, expect, it } from "vitest";

import { buildSingleUserSetupRequest } from "./login";

describe("buildSingleUserSetupRequest", () => {
  it("treats the username only as a trimmed display name", () => {
    expect(buildSingleUserSetupRequest("  Ada  ")).toEqual({ name: "Ada" });
  });
});
