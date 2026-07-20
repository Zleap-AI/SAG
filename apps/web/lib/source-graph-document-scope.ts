export interface SourceGraphDocumentRef {
  id: string;
}

export type SourceGraphDocumentScope =
  { mode: "all" } | { mode: "selected"; ids: string[] };

export const ALL_SOURCE_GRAPH_DOCUMENTS: SourceGraphDocumentScope = {
  mode: "all",
};

function availableDocumentIds(documents: readonly SourceGraphDocumentRef[]) {
  return Array.from(
    new Set(documents.map((document) => document.id).filter(Boolean)),
  );
}

export function normalizeSourceGraphDocumentScope(
  scope: SourceGraphDocumentScope,
  documents: readonly SourceGraphDocumentRef[],
): SourceGraphDocumentScope {
  if (scope.mode === "all") return scope;
  const availableIds = availableDocumentIds(documents);
  const selected = new Set(scope.ids);
  const ids = availableIds.filter((id) => selected.has(id));
  if (availableIds.length > 0 && ids.length === availableIds.length) {
    return ALL_SOURCE_GRAPH_DOCUMENTS;
  }
  if (
    ids.length === scope.ids.length &&
    ids.every((id, index) => id === scope.ids[index])
  ) {
    return scope;
  }
  return { mode: "selected", ids };
}

export function toggleSourceGraphDocument(
  scope: SourceGraphDocumentScope,
  documents: readonly SourceGraphDocumentRef[],
  documentId: string,
  checked: boolean,
): SourceGraphDocumentScope {
  const availableIds = availableDocumentIds(documents);
  if (!availableIds.includes(documentId)) return scope;
  const selected = new Set(scope.mode === "all" ? availableIds : scope.ids);
  if (checked) selected.add(documentId);
  else selected.delete(documentId);
  return normalizeSourceGraphDocumentScope(
    { mode: "selected", ids: availableIds.filter((id) => selected.has(id)) },
    documents,
  );
}

export function setAllSourceGraphDocuments(
  checked: boolean,
): SourceGraphDocumentScope {
  return checked ? ALL_SOURCE_GRAPH_DOCUMENTS : { mode: "selected", ids: [] };
}

/** Undefined means the unfiltered/all-documents API scope; [] is an explicit empty scope. */
export function sourceGraphDocumentIds(
  scope: SourceGraphDocumentScope,
  documents: readonly SourceGraphDocumentRef[],
): string[] | undefined {
  const normalized = normalizeSourceGraphDocumentScope(scope, documents);
  return normalized.mode === "all" ? undefined : normalized.ids;
}

export function sourceGraphSelectedDocumentCount(
  scope: SourceGraphDocumentScope,
  documents: readonly SourceGraphDocumentRef[],
) {
  return (
    sourceGraphDocumentIds(scope, documents)?.length ??
    availableDocumentIds(documents).length
  );
}
