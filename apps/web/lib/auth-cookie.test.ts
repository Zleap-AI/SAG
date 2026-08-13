import { describe, expect, it } from "vitest";

import { tokenCookieName } from "@/lib/auth-cookie";

describe("SAG authentication cookie isolation", () => {
  it("keeps the production cookie name when the host has no port", () => {
    expect(tokenCookieName("sag.example.com")).toBe("sag_token");
  });

  it("isolates local SAG instances by Web port", () => {
    expect(tokenCookieName("localhost:3000")).toBe("sag_token_3000");
    expect(tokenCookieName("localhost:3100")).toBe("sag_token_3100");
    expect(tokenCookieName("127.0.0.1:3100")).toBe("sag_token_3100");
  });
});
