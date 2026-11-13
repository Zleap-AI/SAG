/**
 * Shared JEV scoring-service presets. The knowledge settings page and the
 * inline quick setup inside the source dialogs write the same three fields
 * (`jev_endpoint`, `jev_model`, `jev_api_key`), so they must agree on these.
 */
export const JEV_DEFAULT_MODEL = "jev-latest";

export const JEV_ENDPOINTS = {
  "302": "https://api.302.ai/typesafeai/v1/systemone",
  official: "https://api.typesafe.ai/v1/systemone",
} as const;

export type JevPresetProvider = keyof typeof JEV_ENDPOINTS;
export type JevProvider = JevPresetProvider | "custom";

/** Human-readable provider names shown next to the API key field. */
export const JEV_PROVIDER_NAMES: Record<JevPresetProvider, string> = {
  "302": "302.AI",
  official: "TypeSafe",
};

export function providerForEndpoint(endpoint: string): JevProvider {
  const trimmed = endpoint.trim();
  if (!trimmed || trimmed === JEV_ENDPOINTS["302"]) return "302";
  if (trimmed === JEV_ENDPOINTS.official) return "official";
  return "custom";
}

/** A complete HTTP(S) URL without embedded credentials, matching the settings form. */
export function isValidJevEndpoint(endpoint: string): boolean {
  try {
    const url = new URL(endpoint.trim());
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password;
  } catch {
    return false;
  }
}
