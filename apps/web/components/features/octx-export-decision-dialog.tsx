"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

import { exportDecisionItems } from "@/lib/octx-export-decision";
import { useOctxExports } from "@/components/features/octx-export-provider";
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";

export function OctxExportDecisionDialog() {
  const t = useTranslations("SourceCard");
  const [submitting, setSubmitting] = React.useState<"confirm" | "cancel" | null>(
    null,
  );
  const { tasks, confirmReadyOnly, cancelDecision } = useOctxExports();
  const decisionTask = tasks.find(
    (task) => task.transfer.status === "decision_required",
  );
  const decision = decisionTask?.transfer ?? null;
  const items = decision ? exportDecisionItems(decision) : [];

  async function confirm() {
    setSubmitting("confirm");
    try {
      if (decisionTask && decision?.decision_token) {
        await confirmReadyOnly(decisionTask.transferId, decision.decision_token);
      }
    } finally {
      setSubmitting(null);
    }
  }

  async function cancel() {
    setSubmitting("cancel");
    try {
      if (decisionTask && decision?.decision_token) {
        await cancelDecision(decisionTask.transferId, decision.decision_token);
      }
    } finally {
      setSubmitting(null);
    }
  }

  return (
    <AlertDialog
      open={Boolean(decision)}
      onOpenChange={(open) => {
        if (!open && decision && !submitting) void cancel();
      }}
    >
      <AlertDialogContent className="max-w-lg">
        <AlertDialogHeader>
          <AlertDialogTitle>{t("exportDecisionTitle")}</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-3">
              <p>{t("exportDecisionDescription", { count: items.length })}</p>
              <ul className="max-h-48 list-disc space-y-1 overflow-auto pl-5 text-left">
                {items.map((item) => (
                  <li key={item} className="break-all">
                    {item}
                  </li>
                ))}
              </ul>
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <Button
            type="button"
            variant="outline"
            disabled={Boolean(submitting)}
            onClick={() => void cancel()}
          >
            {submitting === "cancel" && <Spinner />}
            {t("exportDecisionCancel")}
          </Button>
          <Button
            type="button"
            disabled={Boolean(submitting)}
            onClick={() => void confirm()}
          >
            {submitting === "confirm" && <Spinner />}
            {t("exportReadyOnly")}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
