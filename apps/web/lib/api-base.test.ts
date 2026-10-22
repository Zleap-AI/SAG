// @vitest-environment jsdom
import { afterEach, expect, test, vi } from "vitest";
import { resolveApiBase } from "./api-base";

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); delete window.sagDesktop; });

test("Desktop requests follow the selected runtime API port instead of the build-time port", () => {
  Object.defineProperty(window, "sagDesktop", { configurable: true, value: {
    isDesktop: true, apiBase: "http://127.0.0.1:49152",
  } });
  expect(resolveApiBase("http://localhost:8000")).toBe("http://127.0.0.1:49152");
});

test("ordinary Web and older Desktop builds retain their configured address", () => {
  expect(resolveApiBase("https://api.example")).toBe("https://api.example");
  Object.defineProperty(window, "sagDesktop", { configurable: true, value: { isDesktop: true } });
  expect(resolveApiBase("http://localhost:8000")).toBe("http://localhost:8000");
});

test("LAN Web keeps the existing same-host API fallback", () => {
  vi.stubGlobal("window", { location: { protocol: "http:", hostname: "192.168.1.9" } });
  expect(resolveApiBase("http://localhost:8000")).toBe("http://192.168.1.9:8000");
  expect(resolveApiBase("https://api.example/prefix")).toBe("https://api.example/prefix");
});

test("server rendering retains the configured API without a Desktop bridge", () => {
  vi.stubGlobal("window", undefined);
  expect(resolveApiBase("https://api.example")).toBe("https://api.example");
});

// Exercise the real API client so extracting the resolver cannot leave it unwired.
test("the API client sends requests to the Desktop runtime address", async () => {
  Object.defineProperty(window, "sagDesktop", { configurable: true, value: {
    isDesktop: true, apiBase: "http://127.0.0.1:49153",
  } });
  const fetcher = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ auth_mode: "local" }), { status: 200 }));
  vi.resetModules();
  const { api } = await import("./api");
  await api.authStatus();
  expect(fetcher.mock.calls[0][0]).toBe("http://127.0.0.1:49153/api/v1/auth/status");
});
