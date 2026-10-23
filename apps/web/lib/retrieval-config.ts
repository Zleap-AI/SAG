export const SEARCH_STRATEGIES = [
  {
    value: "vector",
    labelKey: "vectorLabel",
    descriptionKey: "vectorDescription",
  },
  {
    value: "multi_es_fast",
    labelKey: "multiEsFastLabel",
    descriptionKey: "multiEsFastDescription",
  },
  {
    value: "multi",
    labelKey: "multiLabel",
    descriptionKey: "multiDescription",
  },
  {
    value: "multi_jev",
    labelKey: "multiJevLabel",
    descriptionKey: "multiJevDescription",
  },
  {
    value: "sag2",
    labelKey: "sag2Label",
    descriptionKey: "sag2Description",
  },
] as const;

export type SearchStrategy = (typeof SEARCH_STRATEGIES)[number]["value"];

export const DEFAULT_SEARCH_STRATEGY: SearchStrategy = "vector";

/**
 * Capability keys a strategy needs from the current vector provider.
 * 后端 apps/api/sag_api/enums.py::SEARCH_STRATEGY_REQUIREMENTS 是权威来源;
 * 前端只用来在拿不到 disabled map 时兜底渲染,避免用户点了才发现打不通。
 */
export const SEARCH_STRATEGY_REQUIREMENTS = {
  vector: [] as const,
  multi: [] as const,
  multi_es_fast: ["lexical_search"] as const,
  multi_jev: ["lexical_search", "jev"] as const,
  sag2: ["jev", "sag2_relational"] as const,
} as const;

/**
 * Strategies whose relevance scores come from the paid JEV service.
 * They share one service configuration and all need an API key.
 */
export const JEV_SCORED_STRATEGIES = ["multi_jev", "sag2"] as const satisfies readonly SearchStrategy[];

export type JevScoredStrategy = (typeof JEV_SCORED_STRATEGIES)[number];

export function isJevScoredStrategy(value: unknown): value is JevScoredStrategy {
  return JEV_SCORED_STRATEGIES.some((strategy) => strategy === value);
}

/**
 * Disabled reasons that only describe an incomplete JEV service configuration.
 * A complete local draft (endpoint + model + key) can lift them; storage and
 * backend-version reasons are never lifted client-side.
 */
export const JEV_CONFIG_DISABLED_REASONS = ["jev_not_configured", "jev_api_key_missing"] as const;

export function isJevConfigDisabledReason(reason: unknown): boolean {
  return JEV_CONFIG_DISABLED_REASONS.some((candidate) => candidate === reason);
}

export function isSearchStrategy(value: unknown): value is SearchStrategy {
  return (
    typeof value === "string" &&
    SEARCH_STRATEGIES.some((strategy) => strategy.value === value)
  );
}

export function getSearchStrategy(value: unknown) {
  return (
    SEARCH_STRATEGIES.find((strategy) => strategy.value === value) ??
    SEARCH_STRATEGIES.find((strategy) => strategy.value === DEFAULT_SEARCH_STRATEGY)!
  );
}

export interface SearchStrategyDisabledInfo {
  reason: string;
  message: string;
}

export type SearchStrategyDisabledMap = Partial<
  Record<SearchStrategy, SearchStrategyDisabledInfo>
>;

/** New modes require an explicit capability signal so older APIs remain usable. */
export function getSearchStrategiesDisabled(
  capabilities: {
    search_strategies?: readonly string[];
    search_strategies_disabled?: SearchStrategyDisabledMap;
  } | null | undefined,
  unsupportedMessage: string,
  jevDraftReady = false,
): SearchStrategyDisabledMap {
  const disabled = { ...capabilities?.search_strategies_disabled };
  for (const strategy of JEV_SCORED_STRATEGIES) {
    const supported = capabilities?.search_strategies?.includes(strategy)
      || Object.hasOwn(disabled, strategy);
    if (!supported) {
      disabled[strategy] = { reason: "unsupported_backend", message: unsupportedMessage };
    } else if (jevDraftReady && isJevConfigDisabledReason(disabled[strategy]?.reason)) {
      delete disabled[strategy];
    }
  }
  return disabled;
}
