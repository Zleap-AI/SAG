import type { OctxTransfer } from "@/lib/types";

type ExportDecisionTransfer = Pick<
  OctxTransfer,
  "status" | "allowed_actions" | "decision_token" | "excluded_documents"
>;

export function isExportDecisionRequired(
  transfer: ExportDecisionTransfer,
): boolean {
  return (
    transfer.status === "decision_required" &&
    transfer.allowed_actions.includes("export_ready_only") &&
    Boolean(transfer.decision_token)
  );
}

export function exportDecisionItems(
  transfer: Pick<OctxTransfer, "excluded_documents">,
): string[] {
  return transfer.excluded_documents.map((document) => {
    const filename = document.filename || document.id || "document";
    return `${filename} (${document.status})`;
  });
}
