import { describe, expect, it } from "vitest";
import {
  getSearchStrategiesDisabled,
  isJevConfigDisabledReason,
  isJevScoredStrategy,
  isSearchStrategy,
  JEV_SCORED_STRATEGIES,
  SEARCH_STRATEGIES,
  SEARCH_STRATEGY_REQUIREMENTS,
} from "./retrieval-config";

describe("JEV strategy availability", () => {
  it("accepts JEV as a request strategy", () => {
    expect(isSearchStrategy("multi_jev")).toBe(true);
  });
  it("disables JEV when an older backend does not advertise it", () => {
    expect(getSearchStrategiesDisabled({ search_strategies: ["vector", "multi"] }, "Upgrade required").multi_jev)
      .toEqual({ reason: "unsupported_backend", message: "Upgrade required" });
    expect(getSearchStrategiesDisabled(undefined, "Upgrade required", true).multi_jev?.reason).toBe("unsupported_backend");
  });
  it("enables JEV when the backend declares that it can execute the mode", () => {
    expect(getSearchStrategiesDisabled({ search_strategies: ["vector", "multi_jev"] }, "Upgrade").multi_jev).toBeUndefined();
  });
  it("allows a ready service draft without bypassing storage requirements", () => {
    const missing = { reason: "jev_not_configured", message: "Configure JEV" };
    expect(getSearchStrategiesDisabled({ search_strategies_disabled: { multi_jev: missing } }, "Upgrade", true).multi_jev).toBeUndefined();
    const storage = { reason: "vector_provider_lacks_lexical", message: "Lexical search required" };
    expect(getSearchStrategiesDisabled({ search_strategies_disabled: { multi_jev: storage } }, "Upgrade", true).multi_jev).toEqual(storage);
  });
  it("lifts a missing API key only when the draft is complete", () => {
    const keyMissing = { reason: "jev_api_key_missing", message: "Add a JEV API key" };
    const capabilities = { search_strategies: ["vector"], search_strategies_disabled: { multi_jev: keyMissing, sag2: keyMissing } };
    expect(getSearchStrategiesDisabled(capabilities, "Upgrade").multi_jev).toEqual(keyMissing);
    expect(getSearchStrategiesDisabled(capabilities, "Upgrade").sag2).toEqual(keyMissing);
    expect(getSearchStrategiesDisabled(capabilities, "Upgrade", true).multi_jev).toBeUndefined();
    expect(getSearchStrategiesDisabled(capabilities, "Upgrade", true).sag2).toBeUndefined();
  });
});

describe("SAG2 strategy availability", () => {
  it("lists SAG2 after JEV reranking and marks both as JEV-scored", () => {
    expect(SEARCH_STRATEGIES.map(strategy => strategy.value)).toEqual(["vector", "multi_es_fast", "multi", "multi_jev", "sag2"]);
    expect(isSearchStrategy("sag2")).toBe(true);
    expect(JEV_SCORED_STRATEGIES).toEqual(["multi_jev", "sag2"]);
    expect(isJevScoredStrategy("sag2")).toBe(true);
    expect(isJevScoredStrategy("multi_jev")).toBe(true);
    expect(isJevScoredStrategy("multi")).toBe(false);
    expect(SEARCH_STRATEGY_REQUIREMENTS.sag2).toEqual(["jev", "sag2_relational"]);
  });
  it("marks SAG2 unsupported on an older backend that still supports JEV reranking", () => {
    const disabled = getSearchStrategiesDisabled({ search_strategies: ["vector", "multi_jev"] }, "Upgrade required", true);
    expect(disabled.multi_jev).toBeUndefined();
    expect(disabled.sag2).toEqual({ reason: "unsupported_backend", message: "Upgrade required" });
    expect(getSearchStrategiesDisabled(undefined, "Upgrade required").sag2?.reason).toBe("unsupported_backend");
  });
  it("enables SAG2 when the backend advertises it", () => {
    expect(getSearchStrategiesDisabled({ search_strategies: ["vector", "multi_jev", "sag2"] }, "Upgrade").sag2).toBeUndefined();
  });
  it("never lifts storage or relational reasons with a ready draft", () => {
    const relational = { reason: "sag2_relational_unsupported", message: "SQLite or PostgreSQL required" };
    const storage = { reason: "vector_provider_lacks_lexical", message: "Lexical search required" };
    const disabled = getSearchStrategiesDisabled({ search_strategies_disabled: { sag2: relational, multi_jev: storage } }, "Upgrade", true);
    expect(disabled.sag2).toEqual(relational);
    expect(disabled.multi_jev).toEqual(storage);
    expect(isJevConfigDisabledReason("sag2_relational_unsupported")).toBe(false);
    expect(isJevConfigDisabledReason("vector_provider_lacks_lexical")).toBe(false);
    expect(isJevConfigDisabledReason("jev_api_key_missing")).toBe(true);
    expect(isJevConfigDisabledReason("jev_not_configured")).toBe(true);
  });
});
