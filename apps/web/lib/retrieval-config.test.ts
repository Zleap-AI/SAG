import { describe, expect, it } from "vitest";
import { isSearchStrategy, getSearchStrategiesDisabled } from "./retrieval-config";

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
});
