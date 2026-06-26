import { describe, expect, it } from "vitest";
import { buildRerankUrl } from "../src/ai/rerank-client.js";

describe("buildRerankUrl", () => {
  it("keeps the existing plural /v1/reranks default for provider base URLs", () => {
    expect(buildRerankUrl("https://api.example.com")).toBe("https://api.example.com/v1/reranks");
    expect(buildRerankUrl("https://api.example.com/")).toBe("https://api.example.com/v1/reranks");
    expect(buildRerankUrl("https://api.example.com/v1")).toBe("https://api.example.com/v1/reranks");
  });

  it("keeps explicit rerank endpoints unchanged", () => {
    expect(buildRerankUrl("https://api.example.com/v1/rerank")).toBe("https://api.example.com/v1/rerank");
    expect(buildRerankUrl("https://api.example.com/v1/reranks")).toBe("https://api.example.com/v1/reranks");
  });
});
