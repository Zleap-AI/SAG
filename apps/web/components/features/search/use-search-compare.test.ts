import { describe, expect, it } from "vitest";

import type { SearchStrategy } from "@/lib/retrieval-config";
import {
  COMPARE_MAX_STRATEGIES,
  COMPARE_MIN_STRATEGIES,
  defaultCompareStrategies,
  selectCompareStrategies,
  sortCompareStrategies,
} from "./use-search-compare";

const all: SearchStrategy[] = ["vector", "multi_es_fast", "multi", "multi_jev", "sag2"];

describe("default compare selection", () => {
  it("pairs the current mode with sag2 when it is enabled", () => {
    expect(defaultCompareStrategies("vector", all)).toEqual(["vector", "sag2"]);
    expect(defaultCompareStrategies("multi", all)).toEqual(["multi", "sag2"]);
  });
  it("falls back to multi_jev, then multi, when sag2 is unavailable", () => {
    expect(defaultCompareStrategies("vector", ["vector", "multi", "multi_jev"])).toEqual(["vector", "multi_jev"]);
    expect(defaultCompareStrategies("vector", ["vector", "multi_es_fast", "multi"])).toEqual(["vector", "multi"]);
  });
  it("never pairs a mode with itself", () => {
    expect(defaultCompareStrategies("sag2", all)).toEqual(["multi_jev", "sag2"]);
    expect(defaultCompareStrategies("multi", ["vector", "multi"])).toEqual(["vector", "multi"]);
  });
  it("uses any other enabled mode when no preferred alternative exists", () => {
    expect(defaultCompareStrategies("vector", ["vector", "multi_es_fast"])).toEqual(["vector", "multi_es_fast"]);
  });
  it("leaves a single pick when nothing else can be served", () => {
    expect(defaultCompareStrategies("vector", ["vector"])).toEqual(["vector"]);
    expect(defaultCompareStrategies("sag2", ["vector"])).toEqual(["vector"]);
  });
});

describe("compare selection bounds", () => {
  it("accepts two or three enabled modes in catalog order", () => {
    expect(selectCompareStrategies(["vector", "sag2"], ["sag2", "multi", "vector"], all)).toEqual(["vector", "multi", "sag2"]);
    expect(selectCompareStrategies(["vector", "multi", "sag2"], ["multi", "sag2"], all)).toEqual(["multi", "sag2"]);
  });
  it("keeps the previous picks when the change would leave fewer than two", () => {
    expect(selectCompareStrategies(["vector", "sag2"], ["sag2"], all)).toEqual(["vector", "sag2"]);
    expect(selectCompareStrategies(["vector", "sag2"], [], all)).toEqual(["vector", "sag2"]);
  });
  it("keeps the previous picks when the change would exceed three", () => {
    expect(selectCompareStrategies(["vector", "multi", "sag2"], ["vector", "multi", "multi_jev", "sag2"], all))
      .toEqual(["vector", "multi", "sag2"]);
  });
  it("drops unknown and disabled values before applying the bounds", () => {
    expect(selectCompareStrategies(["vector", "multi"], ["vector", "multi", "bogus", "sag2"], ["vector", "multi"]))
      .toEqual(["vector", "multi"]);
  });
  it("exposes the 2–3 bounds used by the chips", () => {
    expect(COMPARE_MIN_STRATEGIES).toBe(2);
    expect(COMPARE_MAX_STRATEGIES).toBe(3);
    expect(sortCompareStrategies(["sag2", "vector"])).toEqual(["vector", "sag2"]);
  });
});
