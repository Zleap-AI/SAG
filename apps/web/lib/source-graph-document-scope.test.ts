import { describe, expect, it } from "vitest";

import {
  ALL_SOURCE_GRAPH_DOCUMENTS,
  normalizeSourceGraphDocumentScope,
  setAllSourceGraphDocuments,
  sourceGraphDocumentIds,
  sourceGraphSelectedDocumentCount,
  toggleSourceGraphDocument,
} from "./source-graph-document-scope";

const documents = [{ id: "doc-a" }, { id: "doc-b" }, { id: "doc-c" }];

describe("source graph document scope", () => {
  it("uses an omitted API filter for all documents", () => {
    expect(
      sourceGraphDocumentIds(ALL_SOURCE_GRAPH_DOCUMENTS, documents),
    ).toBeUndefined();
    expect(
      sourceGraphSelectedDocumentCount(ALL_SOURCE_GRAPH_DOCUMENTS, documents),
    ).toBe(3);
  });

  it("supports selecting any subset and normalizes a complete set back to all", () => {
    let scope = toggleSourceGraphDocument(
      ALL_SOURCE_GRAPH_DOCUMENTS,
      documents,
      "doc-b",
      false,
    );
    expect(sourceGraphDocumentIds(scope, documents)).toEqual([
      "doc-a",
      "doc-c",
    ]);

    scope = toggleSourceGraphDocument(scope, documents, "doc-a", false);
    expect(sourceGraphDocumentIds(scope, documents)).toEqual(["doc-c"]);

    scope = toggleSourceGraphDocument(scope, documents, "doc-a", true);
    scope = toggleSourceGraphDocument(scope, documents, "doc-b", true);
    expect(scope).toEqual(ALL_SOURCE_GRAPH_DOCUMENTS);
    expect(sourceGraphDocumentIds(scope, documents)).toBeUndefined();
  });

  it("distinguishes an explicit empty selection from all documents", () => {
    const scope = setAllSourceGraphDocuments(false);
    expect(sourceGraphDocumentIds(scope, documents)).toEqual([]);
    expect(sourceGraphSelectedDocumentCount(scope, documents)).toBe(0);
  });

  it("prunes deleted documents while keeping an explicit subset", () => {
    const scope = normalizeSourceGraphDocumentScope(
      { mode: "selected", ids: ["doc-a", "doc-c"] },
      [{ id: "doc-a" }, { id: "doc-b" }],
    );
    expect(scope).toEqual({ mode: "selected", ids: ["doc-a"] });
  });
});
