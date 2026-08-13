import { describe, expect, it } from "vitest";

import {
  importDurationParts,
  importStage,
  vectorProgressMessageKey,
} from "@/lib/octx-import-tasks";
import type { OctxTransfer } from "@/lib/types";

function transfer(phase: string): OctxTransfer {
  return {
    id: "transfer-1", direction: "import", status: "indexing", progress: 0.75,
    asset: null, release: null, target_source_id: null, installation_id: null,
    allowed_actions: [], decision_token: null, conflicts: [], excluded_documents: [],
    record_counts: {}, capabilities: {}, progress_detail: { phase },
    validation_report: null, warnings: [], error: null, cancellation_requested: false,
    created_at: "2026-08-11T01:00:00Z", updated_at: "2026-08-11T01:00:10Z",
  };
}

describe("OCTX import progress presentation", () => {
  it("uses the server vector and shadow-validation phases", () => {
    expect(importStage(transfer("vectorizing"))).toBe("vectorizing");
    expect(importStage(transfer("validating_shadow"))).toBe("validating_shadow");
  });

  it("splits real elapsed seconds into stable user-facing units", () => {
    expect(importDurationParts(0)).toEqual({ hours: 0, minutes: 0, seconds: 0 });
    expect(importDurationParts(512)).toEqual({ hours: 0, minutes: 8, seconds: 32 });
    expect(importDurationParts(3671)).toEqual({ hours: 1, minutes: 1, seconds: 11 });
  });

  it("distinguishes reused, generated, mixed, and legacy vector work", () => {
    expect(vectorProgressMessageKey("reuse")).toBe("importVectorProgressReuse");
    expect(vectorProgressMessageKey("generate")).toBe("importVectorProgressGenerate");
    expect(vectorProgressMessageKey("mixed")).toBe("importVectorProgressMixed");
    expect(vectorProgressMessageKey(undefined)).toBe("importVectorProgressProcess");
  });
});
