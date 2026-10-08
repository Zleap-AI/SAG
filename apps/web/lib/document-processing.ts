import type { DocumentActivity } from "./document-activity";
import type { Doc } from "./types";

const STAGES = [
  "queued", "parsing", "chunking", "indexing", "waiting_extraction",
  "extracting", "finalizing", "waiting_retry",
] as const;

export type DocumentProcessingStage = typeof STAGES[number];

/** Control and terminal states always win over the last recorded processing stage. */
export function documentProcessingState(document: Doc, activity?: DocumentActivity) {
  const phase = activity?.phase ?? document.status;
  const active = phase === document.status
    && (phase === "pending" || phase === "loading" || phase === "extracting");
  let stage = active && STAGES.includes(document.processing_stage as DocumentProcessingStage)
    ? document.processing_stage as DocumentProcessingStage
    : null;
  const completed = document.processed_chunks;
  const total = document.total_chunks;
  const validCounts = typeof completed === "number" && Number.isSafeInteger(completed) && completed >= 0
    && typeof total === "number" && Number.isSafeInteger(total) && total > 0 && completed <= total;

  // The last chunk callback can precede the finalizing snapshot; it is not readiness.
  if (stage === "extracting" && validCounts && completed === total) stage = "finalizing";
  const retainCounts = (phase === "paused" || phase === "failed")
    && (document.processing_stage === "extracting" || document.processing_stage === "finalizing");
  const counts = validCounts && (stage === "extracting" || retainCounts) ? { completed, total } : null;
  return { phase, stage, counts, showBar: stage === "extracting" && counts !== null };
}
