import type { MinerUProvider } from "./types";

const PROVIDER_DEFAULTS: Record<MinerUProvider, string> = {
  "302": "https://api.302ai.cn",
  official: "https://mineru.net/api/v4",
};

export function mineruProviderBaseUrl(
  _current: string,
  _previous: MinerUProvider,
  next: MinerUProvider,
): string {
  return PROVIDER_DEFAULTS[next];
}
