import { afterEach, describe, expect, it, vi } from "vitest";

import { UuidCapabilityError, generateUuidV4 } from "./uuid";

describe("generateUuidV4", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("prefers crypto.randomUUID when available", () => {
    vi.stubGlobal("crypto", {
      randomUUID: () => "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    });

    expect(generateUuidV4()).toBe("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");
  });

  it("builds an RFC 4122 v4 UUID with getRandomValues", () => {
    vi.stubGlobal("crypto", {
      getRandomValues: (value: Uint8Array) => {
        value.fill(0);
        return value;
      },
    });

    expect(generateUuidV4()).toBe("00000000-0000-4000-8000-000000000000");
  });

  it("fails explicitly when Web Crypto is unavailable", () => {
    vi.stubGlobal("crypto", undefined);

    expect(() => generateUuidV4()).toThrow(UuidCapabilityError);
  });
});
