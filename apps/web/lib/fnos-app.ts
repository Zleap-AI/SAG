import { appPath } from "./deployment";

const AUTH_STATE_KEY = "sag:fnos-nas-auth-state";
const AUTH_MESSAGE_TYPE = "sag:fnos-nas-auth-result";
const SIDEBAR_GROUP = [
  "myFiles",
  "otherShare",
  "external",
  "remote",
  "favorites",
  "team",
] as const;

export interface FnOSStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface FnOSAuthMessage {
  type: typeof AUTH_MESSAGE_TYPE;
  status: "success" | "cancel" | "error";
  error?: string;
  state: string;
}

type FnOSTimer = number | ReturnType<typeof setTimeout>;

export interface FnOSSDK {
  isStandaloneWeb: boolean;
  ready(): Promise<void>;
  pickSharedFile(params: {
    title: string;
    okText: string;
    sidebarGroup: readonly string[] | string[];
  }): Promise<{ code: number; msg: string; data: string[] } | undefined>;
  openAppAuth(
    method: "pickSharedFile",
    params: {
      appName: string;
      sidebarGroup: readonly string[] | string[];
      redirectUri: string;
      state: string;
    },
    options: { target: "_blank"; features: string },
  ): Promise<string>;
  parseAppAuthCallback(input: string): {
    status?: "success" | "cancel" | "error";
    error?: string;
    method?: string;
    appName?: string;
    state?: string;
    path?: string[];
  };
}

export interface FnOSAppRuntime {
  origin: string;
  sessionStorage: FnOSStorage;
  addMessageListener(listener: (event: MessageEvent) => void): void;
  removeMessageListener(listener: (event: MessageEvent) => void): void;
  setTimer(callback: () => void, delay: number): FnOSTimer;
  clearTimer(handle: FnOSTimer): void;
}

export interface FnOSFolderAuthorizer {
  authorizeDirectory(options: {
    callbackUrl: string;
    state: string;
    title?: string;
    confirmText?: string;
  }): Promise<"authorized" | "cancelled">;
}

function browserRuntime(): FnOSAppRuntime {
  if (typeof window === "undefined") throw new Error("fnOS authorization is browser-only");
  return {
    origin: window.location.origin,
    sessionStorage: window.sessionStorage,
    addMessageListener: (listener) => window.addEventListener("message", listener),
    removeMessageListener: (listener) => window.removeEventListener("message", listener),
    setTimer: (callback, delay) => window.setTimeout(callback, delay),
    clearTimer: (handle) => window.clearTimeout(handle as number),
  };
}

async function createBrowserSDK(): Promise<FnOSSDK> {
  if (typeof window === "undefined") throw new Error("fnOS application SDK unavailable");
  const { TrimApp } = await import("@trimjs/web-app");
  return new TrimApp() as unknown as FnOSSDK;
}

function authMessage(value: unknown): value is FnOSAuthMessage {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<FnOSAuthMessage>;
  return (
    candidate.type === AUTH_MESSAGE_TYPE &&
    (candidate.status === "success" ||
      candidate.status === "cancel" ||
      candidate.status === "error") &&
    typeof candidate.state === "string" &&
    candidate.state.length > 0 &&
    (candidate.error === undefined || typeof candidate.error === "string")
  );
}

export function createFnOSAuthState(): string {
  if (typeof crypto === "undefined" || typeof crypto.getRandomValues !== "function") {
    throw new Error("Secure authorization state unavailable");
  }
  const bytes = crypto.getRandomValues(new Uint8Array(24));
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

export function fnOSAuthCallbackUrl(origin?: string): string {
  const base = origin ?? (typeof window !== "undefined" ? window.location.origin : "http://localhost");
  return new URL(appPath("/fnos-auth-callback"), base).toString();
}

export function createFnOSFolderAuthorizer(options?: {
  createSDK?: () => Promise<FnOSSDK>;
  runtime?: FnOSAppRuntime;
  timeoutMs?: number;
}): FnOSFolderAuthorizer {
  const createSDK = options?.createSDK ?? createBrowserSDK;
  const runtime = options?.runtime ?? browserRuntime();
  const timeoutMs = options?.timeoutMs ?? 120_000;

  return {
    async authorizeDirectory({ callbackUrl, state, title, confirmText }) {
      if (!state) throw new Error("Authorization state is required");
      const sdk = await createSDK();
      await sdk.ready();
      if (!sdk.isStandaloneWeb) {
        const result = await sdk.pickSharedFile({
          title: title ?? "Select and authorize a shared folder",
          okText: confirmText ?? "Authorize",
          sidebarGroup: [...SIDEBAR_GROUP],
        });
        if (result === undefined) return "cancelled";
        if (result.code !== 0) throw new Error(result.msg || "Folder authorization failed");
        return "authorized";
      }

      const redirect = new URL(callbackUrl, runtime.origin);
      if (redirect.origin !== runtime.origin) {
        throw new Error("Authorization callback must use the current origin");
      }
      runtime.sessionStorage.setItem(AUTH_STATE_KEY, state);
      return new Promise<"authorized" | "cancelled">((resolve, reject) => {
        let timer: FnOSTimer | undefined = undefined;
        const cleanup = () => {
          runtime.removeMessageListener(onMessage);
          if (timer !== undefined) runtime.clearTimer(timer);
          runtime.sessionStorage.removeItem(AUTH_STATE_KEY);
        };
        const onMessage = (event: MessageEvent) => {
          if (event.origin !== runtime.origin || !authMessage(event.data)) return;
          if (event.data.state !== state) return;
          cleanup();
          if (event.data.status === "success") resolve("authorized");
          else if (event.data.status === "cancel") resolve("cancelled");
          else reject(new Error(event.data.error || "Folder authorization failed"));
        };
        runtime.addMessageListener(onMessage);
        timer = runtime.setTimer(() => {
          cleanup();
          reject(new Error("Folder authorization timed out"));
        }, timeoutMs);
        void sdk
          .openAppAuth(
            "pickSharedFile",
            {
              appName: "sag",
              sidebarGroup: [...SIDEBAR_GROUP],
              redirectUri: redirect.toString(),
              state,
            },
            { target: "_blank", features: "width=750,height=630" },
          )
          .catch((error: unknown) => {
            cleanup();
            reject(error instanceof Error ? error : new Error("Folder authorization failed"));
          });
      });
    },
  };
}

export function parseFnOSAuthCallback(
  sdk: FnOSSDK,
  href: string,
  storage: FnOSStorage,
): FnOSAuthMessage {
  const result = sdk.parseAppAuthCallback(href);
  const savedState = storage.getItem(AUTH_STATE_KEY);
  if (
    !savedState ||
    result.state !== savedState ||
    !result.status ||
    (result.method !== undefined && result.method !== "pickSharedFile") ||
    (result.appName !== undefined && result.appName !== "sag")
  ) {
    throw new Error("Authorization callback validation failed");
  }
  storage.removeItem(AUTH_STATE_KEY);
  return {
    type: AUTH_MESSAGE_TYPE,
    status: result.status,
    error: result.error,
    state: savedState,
  };
}

export async function loadFnOSSDK(): Promise<FnOSSDK> {
  return createBrowserSDK();
}
