import { afterEach, describe, expect, it, vi } from "vitest";

const originalBasePath = process.env.NEXT_PUBLIC_APP_BASE_PATH;

afterEach(() => {
  if (originalBasePath === undefined) {
    delete process.env.NEXT_PUBLIC_APP_BASE_PATH;
  } else {
    process.env.NEXT_PUBLIC_APP_BASE_PATH = originalBasePath;
  }
  vi.resetModules();
});

describe("appPath", () => {
  it("prefixes fnOS routes exactly once", async () => {
    process.env.NEXT_PUBLIC_APP_BASE_PATH = "/app/sag/";
    const { APP_BASE_PATH, appPath } = await import("./deployment");

    expect(APP_BASE_PATH).toBe("/app/sag");
    expect(appPath("/login")).toBe("/app/sag/login");
    expect(appPath("/app/sag/chat")).toBe("/app/sag/chat");
  });

  it("keeps normal deployments at the origin root", async () => {
    delete process.env.NEXT_PUBLIC_APP_BASE_PATH;
    const { appPath } = await import("./deployment");

    expect(appPath("chat")).toBe("/chat");
  });
});
