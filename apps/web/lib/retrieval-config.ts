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
} as const;

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
  const supportsJev = capabilities?.search_strategies?.includes("multi_jev")
    || Object.hasOwn(disabled, "multi_jev");
  if (!supportsJev) {
    disabled.multi_jev = { reason: "unsupported_backend", message: unsupportedMessage };
  } else if (jevDraftReady && disabled.multi_jev?.reason === "jev_not_configured") {
    delete disabled.multi_jev;
  }
  return disabled;
}
