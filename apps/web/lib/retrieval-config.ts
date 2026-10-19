export const SEARCH_STRATEGIES = [
  {
    value: "vector",
    labelKey: "vectorLabel",
    descriptionKey: "vectorDescription",
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

export type SearchStrategyDisabledMap = Partial<Record<SearchStrategy, {
  reason: string;
  message: string;
}>>;

export function getSearchStrategiesDisabled(
  capabilities: { search_strategies?: readonly string[]; search_strategies_disabled?: SearchStrategyDisabledMap } | null | undefined,
  unsupportedMessage: string,
  jevDraftReady = false,
): SearchStrategyDisabledMap {
  const disabled = { ...capabilities?.search_strategies_disabled };
  const supportsJev = capabilities?.search_strategies?.includes("multi_jev")
    || Object.hasOwn(disabled, "multi_jev");
  if (!supportsJev) disabled.multi_jev = { reason: "unsupported_backend", message: unsupportedMessage };
  else if (jevDraftReady && disabled.multi_jev?.reason === "jev_connection_not_configured") delete disabled.multi_jev;
  return disabled;
}
