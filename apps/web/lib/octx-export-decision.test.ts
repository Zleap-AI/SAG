import { describe, expect, it } from "vitest";

import {
  exportDecisionItems,
  isExportDecisionRequired,
} from "@/lib/octx-export-decision";
import type { OctxTransfer } from "@/lib/types";

describe("OCTX export decision", () => {
  it("requires a signed READY-only decision and preserves every excluded document", () => {
    const transfer: Pick<
      OctxTransfer,
      "status" | "allowed_actions" | "decision_token" | "excluded_documents"
    > = {
      status: "decision_required",
      allowed_actions: ["export_ready_only", "cancel"],
      decision_token: "signed-token",
      excluded_documents: [
        { id: "busy", filename: "busy.md", status: "extracting" },
        { id: "failed", filename: "failed.md", status: "failed" },
      ],
    };

    expect(isExportDecisionRequired(transfer)).toBe(true);
    expect(exportDecisionItems(transfer)).toEqual([
      "busy.md (extracting)",
      "failed.md (failed)",
    ]);
  });

  it("rejects incomplete or unrelated decision responses", () => {
    expect(
      isExportDecisionRequired({
        status: "decision_required",
        allowed_actions: ["export_ready_only"],
        decision_token: null,
        excluded_documents: [],
      }),
    ).toBe(false);
    expect(
      isExportDecisionRequired({
        status: "ready",
        allowed_actions: ["export_ready_only"],
        decision_token: "signed-token",
        excluded_documents: [],
      }),
    ).toBe(false);
  });
});
