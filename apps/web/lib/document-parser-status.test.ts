import { describe, expect, it } from "vitest";

import { documentParserStatus } from "./document-parser-status";

const base = {
  parser_provider: "mineru" as const,
  mineru_provider: "official" as const,
  mineru_model: "vlm" as const,
  parser_status: "done" as const,
  fallback_from: null,
};

describe("documentParserStatus", () => {
  it("maps official MinerU models and progress states", () => {
    expect(documentParserStatus(base)).toEqual({ methodKey: "officialMineruVlm", progressKey: null });
    expect(documentParserStatus({ ...base, mineru_model: "pipeline" })).toEqual({ methodKey: "officialMineruPipeline", progressKey: null });
    expect(documentParserStatus({ ...base, parser_status: "uploading" })).toEqual({ methodKey: "officialMineruVlm", progressKey: "uploading" });
    expect(documentParserStatus({ ...base, parser_status: "queued" })).toEqual({ methodKey: "officialMineruVlm", progressKey: "queued" });
    expect(documentParserStatus({ ...base, parser_status: "running" })).toEqual({ methodKey: "officialMineruVlm", progressKey: "running" });
  });

  it("maps 302.AI MinerU success", () => {
    expect(documentParserStatus({ ...base, mineru_provider: "302", mineru_model: "2.5" })).toEqual({ methodKey: "ai302Mineru", progressKey: null });
  });

  it("maps direct MarkItDown", () => {
    expect(documentParserStatus({ ...base, parser_provider: "markitdown" })).toEqual({ methodKey: "markitdown", progressKey: null });
  });

  it("maps MarkItDown fallback after MinerU", () => {
    expect(documentParserStatus({ ...base, parser_provider: "markitdown", parser_status: "fallback", fallback_from: "mineru" })).toEqual({ methodKey: "markitdownFallback", progressKey: null });
  });

  it("maps complete parser failure", () => {
    expect(documentParserStatus({ ...base, parser_status: "failed" })).toEqual({ methodKey: "failed", progressKey: null });
  });

  it("maps original Markdown content", () => {
    expect(documentParserStatus({ ...base, parser_provider: "original" })).toEqual({ methodKey: "original", progressKey: null });
  });

  it("returns null for legacy rows", () => {
    expect(documentParserStatus({ parser_provider: null, mineru_provider: null, mineru_model: null, parser_status: null, fallback_from: null })).toBeNull();
    expect(documentParserStatus({})).toBeNull();
  });
});
