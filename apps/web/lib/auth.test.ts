import { afterEach, describe, expect, it, vi } from "vitest";

import { clearToken, setToken } from "./auth";

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubBrowser(protocol: "http:" | "https:") {
  const documentStub = { cookie: "" };
  vi.stubGlobal("document", documentStub);
  vi.stubGlobal("window", { location: { protocol } });
  return documentStub;
}

describe("authentication cookie transport attributes", () => {
  it("adds Secure when setting and clearing the token over HTTPS", () => {
    const documentStub = stubBrowser("https:");

    setToken("token-value");
    expect(documentStub.cookie).toContain("SameSite=Lax");
    expect(documentStub.cookie).toContain("Secure");

    clearToken();
    expect(documentStub.cookie).toContain("max-age=0");
    expect(documentStub.cookie).toContain("Secure");
  });

  it("keeps the current trusted-LAN HTTP flow usable without a Secure attribute", () => {
    const documentStub = stubBrowser("http:");

    setToken("token-value");
    expect(documentStub.cookie).toContain("SameSite=Lax");
    expect(documentStub.cookie).not.toContain("Secure");
  });
});
