import { describe, expect, it, vi } from "vitest";

import {
  createFnOSFolderAuthorizer,
  parseFnOSAuthCallback,
  type FnOSAppRuntime,
  type FnOSSDK,
} from "./fnos-app";

function directSDK(response: unknown): FnOSSDK {
  return {
    isStandaloneWeb: false,
    ready: vi.fn().mockResolvedValue(undefined),
    pickSharedFile: vi.fn().mockResolvedValue(response),
    openAppAuth: vi.fn(),
    parseAppAuthCallback: vi.fn(),
  };
}

function runtime(): FnOSAppRuntime & { emit(data: unknown, origin?: string): void } {
  const listeners = new Set<(event: MessageEvent) => void>();
  const values = new Map<string, string>();
  return {
    origin: "https://nas.local",
    sessionStorage: {
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
      removeItem: (key) => values.delete(key),
    },
    addMessageListener: (listener) => listeners.add(listener),
    removeMessageListener: (listener) => listeners.delete(listener),
    setTimer: (callback, delay) => setTimeout(callback, delay),
    clearTimer: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
    emit(data, origin = "https://nas.local") {
      for (const listener of listeners) listener({ data, origin } as MessageEvent);
    },
  };
}

describe("fnOS application adapter", () => {
  it("authorizes directly without retaining returned paths", async () => {
    const sdk = directSDK({ code: 0, msg: "ok", data: ["/private/team"] });
    const authorizer = createFnOSFolderAuthorizer({
      createSDK: async () => sdk,
      runtime: runtime(),
    });

    await expect(
      authorizer.authorizeDirectory({ callbackUrl: "/callback", state: "state-1" }),
    ).resolves.toBe("authorized");
    expect(sdk.pickSharedFile).toHaveBeenCalledWith(
      expect.objectContaining({ title: expect.any(String), okText: expect.any(String) }),
    );
  });

  it("maps picker cancellation", async () => {
    const sdk = directSDK(undefined);
    const authorizer = createFnOSFolderAuthorizer({
      createSDK: async () => sdk,
      runtime: runtime(),
    });
    await expect(
      authorizer.authorizeDirectory({ callbackUrl: "/callback", state: "state-1" }),
    ).resolves.toBe("cancelled");
  });

  it("uses a same-origin, state-bound callback in standalone mode", async () => {
    const appRuntime = runtime();
    const sdk: FnOSSDK = {
      ...directSDK(undefined),
      isStandaloneWeb: true,
      openAppAuth: vi.fn().mockResolvedValue("https://nas.local/app-auth"),
    };
    const authorizer = createFnOSFolderAuthorizer({
      createSDK: async () => sdk,
      runtime: appRuntime,
      timeoutMs: 100,
    });
    const pending = authorizer.authorizeDirectory({
      callbackUrl: "https://nas.local/app/sag/fnos-auth-callback",
      state: "secure-state",
    });
    await Promise.resolve();
    await Promise.resolve();
    appRuntime.emit(
      {
        type: "sag:fnos-nas-auth-result",
        status: "success",
        state: "secure-state",
      },
      "https://evil.example",
    );
    appRuntime.emit({
      type: "sag:fnos-nas-auth-result",
      status: "success",
      state: "secure-state",
    });

    await expect(pending).resolves.toBe("authorized");
    expect(sdk.openAppAuth).toHaveBeenCalledWith(
      "pickSharedFile",
      expect.objectContaining({
        redirectUri: "https://nas.local/app/sag/fnos-auth-callback",
        state: "secure-state",
      }),
      expect.objectContaining({ target: "_blank" }),
    );
  });

  it("sanitizes callback data and validates saved state", () => {
    const storage = runtime().sessionStorage;
    storage.setItem("sag:fnos-nas-auth-state", "expected");
    const sdk = directSDK(undefined);
    sdk.parseAppAuthCallback = vi.fn().mockReturnValue({
      status: "success",
      state: "expected",
      path: ["/must/not/escape"],
    });

    expect(parseFnOSAuthCallback(sdk, "https://nas.local/callback", storage)).toEqual({
      type: "sag:fnos-nas-auth-result",
      status: "success",
      error: undefined,
      state: "expected",
    });
    expect(storage.getItem("sag:fnos-nas-auth-state")).toBeNull();
  });
});
