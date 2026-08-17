import type { Doc } from "./types";

export type DocumentParserMethodKey =
  | "officialMineruVlm"
  | "officialMineruPipeline"
  | "ai302Mineru"
  | "markitdown"
  | "markitdownFallback"
  | "failed"
  | "original";

export type DocumentParserProgressKey = "uploading" | "queued" | "running";

export interface DocumentParserStatus {
  methodKey: DocumentParserMethodKey;
  progressKey: DocumentParserProgressKey | null;
}

const IN_PROGRESS = new Set<DocumentParserProgressKey>([
  "uploading",
  "queued",
  "running",
]);

export function documentParserStatus(
  doc: Pick<
    Doc,
    | "parser_provider"
    | "mineru_provider"
    | "mineru_model"
    | "parser_status"
    | "fallback_from"
  >,
): DocumentParserStatus | null {
  const {
    parser_provider: parserProvider,
    mineru_provider: mineruProvider,
    mineru_model: mineruModel,
    parser_status: parserStatus,
    fallback_from: fallbackFrom,
  } = doc;
  if (!parserProvider || !parserStatus) return null;

  const progressKey = IN_PROGRESS.has(parserStatus as DocumentParserProgressKey)
    ? (parserStatus as DocumentParserProgressKey)
    : null;
  if (parserStatus === "failed") return { methodKey: "failed", progressKey: null };
  if (parserProvider === "original") return { methodKey: "original", progressKey };
  if (parserProvider === "markitdown") {
    return {
      methodKey:
        parserStatus === "fallback" || fallbackFrom === "mineru"
          ? "markitdownFallback"
          : "markitdown",
      progressKey,
    };
  }

  if (mineruProvider === "302" || mineruModel === "2.5") {
    return { methodKey: "ai302Mineru", progressKey };
  }
  return {
    methodKey:
      mineruModel === "pipeline" ? "officialMineruPipeline" : "officialMineruVlm",
    progressKey,
  };
}
