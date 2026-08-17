import { describe, expect, it } from "vitest";

import { mineruProviderBaseUrl } from "./mineru-config";

describe("mineruProviderBaseUrl", () => {
  it("switches known provider defaults", () => {
    expect(mineruProviderBaseUrl("https://api.302ai.cn", "302", "official")).toBe(
      "https://mineru.net/api/v4",
    );
    expect(mineruProviderBaseUrl("https://mineru.net", "official", "302")).toBe(
      "https://api.302ai.cn",
    );
  });

  it("resets a custom gateway when the provider changes", () => {
    expect(
      mineruProviderBaseUrl(
        "https://mineru.net/api/v4/extract/task",
        "official",
        "302",
      ),
    ).toBe("https://api.302ai.cn");
    expect(
      mineruProviderBaseUrl(
        "https://proxy.example.test/mineru",
        "302",
        "official",
      ),
    ).toBe("https://mineru.net/api/v4");
  });

  it("resets an invalid official endpoint when switching to 302", () => {
    expect(mineruProviderBaseUrl("not-a-url", "official", "302")).toBe(
      "https://api.302ai.cn",
    );
  });
});
