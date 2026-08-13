"use client";

import * as React from "react";
import { Check, CopyPlus, Database, PackageOpen, RefreshCw, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";

import { useOctxImports } from "@/components/features/octx-import-provider";
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Spinner } from "@/components/ui/spinner";
import type { OctxImportAction } from "@/lib/types";
import { cn } from "@/lib/utils";

type DecisionAction = Exclude<OctxImportAction, "cancel">;

export function buildImportDecisionRequest(
  action: DecisionAction,
  token: string,
  sourceId: string | undefined,
  discard: boolean,
) {
  return {
    action,
    decision_token: token,
    ...(action === "update" && sourceId ? { target_source_id: sourceId } : {}),
    ...(action === "update" ? { discard_local_changes: discard } : {}),
  };
}

export function OctxImportDecisionPanel({
  sourceName, hasLocalChanges, allowUpdate, selectedAction, discard, submitting,
  onSelect, onDiscardChange, onCancel, onContinue,
}: {
  sourceName: string;
  hasLocalChanges: boolean;
  allowUpdate: boolean;
  selectedAction: DecisionAction;
  discard: boolean;
  submitting: boolean;
  onSelect: (action: DecisionAction) => void;
  onDiscardChange: (checked: boolean) => void;
  onCancel: () => void;
  onContinue: () => void;
}) {
  const t = useTranslations("Knowledge");
  const updateBlocked = selectedAction === "update" && hasLocalChanges && !discard;

  return <>
    <AlertDialogHeader className="px-6 pb-5 pt-6 pr-12">
      <div className="mb-2 grid size-10 place-items-center rounded-lg border bg-muted/50 text-foreground">
        <PackageOpen className="size-5" aria-hidden="true" />
      </div>
      <AlertDialogTitle>{t("importDecisionTitle")}</AlertDialogTitle>
      <AlertDialogDescription className="leading-6">
        {t(allowUpdate ? "importDecisionDescription" : "importDecisionCreateOnlyDescription", { name: sourceName })}
      </AlertDialogDescription>
    </AlertDialogHeader>

    <div className="flex items-center gap-3 border-y bg-muted/25 px-6 py-3.5">
      <div className="grid size-9 shrink-0 place-items-center rounded-lg border bg-background text-muted-foreground">
        <Database className="size-4" aria-hidden="true" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium text-foreground">{sourceName}</div>
        <div className="text-xs text-muted-foreground">{t("importExistingSource")}</div>
      </div>
      <span className="rounded-full border bg-background px-2.5 py-1 text-[11px] font-medium text-muted-foreground">OCTX</span>
    </div>

    <div className="space-y-3 px-6 py-5" role="radiogroup" aria-label={t("importDecisionChoiceAria")}>
      <button
        type="button" role="radio" aria-checked={selectedAction === "new"} disabled={submitting}
        onClick={() => onSelect("new")}
        className={cn(
          "group w-full rounded-lg border p-4 text-left outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
          selectedAction === "new" ? "border-foreground/25 bg-muted/45" : "border-border bg-background hover:bg-muted/30",
        )}
      >
        <div className="flex items-start gap-3">
          <div className="mt-0.5 grid size-9 shrink-0 place-items-center rounded-lg border bg-background text-muted-foreground group-aria-checked:text-foreground">
            <CopyPlus className="size-4" aria-hidden="true" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium text-foreground">{t("importCreateNew")}</span>
              <span className="rounded-full bg-foreground px-2 py-0.5 text-[10px] font-medium text-background">{t("importRecommended")}</span>
            </div>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">{t("importCreateNewDescription")}</p>
          </div>
          <ChoiceMark selected={selectedAction === "new"} />
        </div>
      </button>

      {allowUpdate && <div
        role="radio" aria-checked={selectedAction === "update"} tabIndex={submitting ? -1 : 0}
        onClick={() => !submitting && onSelect("update")}
        onKeyDown={(event) => {
          if (!submitting && (event.key === "Enter" || event.key === " ")) {
            event.preventDefault();
            onSelect("update");
          }
        }}
        className={cn(
          "rounded-lg border p-4 outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
          submitting && "pointer-events-none opacity-60",
          selectedAction === "update" ? "border-foreground/25 bg-muted/45" : "border-border bg-background hover:bg-muted/30",
        )}
      >
        <div className="flex items-start gap-3">
          <div className="mt-0.5 grid size-9 shrink-0 place-items-center rounded-lg border bg-background text-muted-foreground">
            <RefreshCw className="size-4" aria-hidden="true" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium text-foreground">{t("importUpdateExisting")}</div>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">{t("importUpdateExistingDescription")}</p>
          </div>
          <ChoiceMark selected={selectedAction === "update"} />
        </div>
        {selectedAction === "update" && hasLocalChanges && <label
          className="mt-4 flex cursor-pointer items-start gap-2.5 rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-xs leading-5"
          onClick={(event) => event.stopPropagation()}
        >
          <Checkbox checked={discard} disabled={submitting} onCheckedChange={(value) => onDiscardChange(value === true)} className="mt-0.5" />
          <span className="flex-1 text-muted-foreground">
            <span className="mb-0.5 flex items-center gap-1.5 font-medium text-amber-700 dark:text-amber-400">
              <TriangleAlert className="size-3.5" aria-hidden="true" />{t("importLocalChangesTitle")}
            </span>
            {t("importDiscardChanges")}
          </span>
        </label>}
      </div>}
    </div>

    <AlertDialogFooter className="border-t bg-muted/20 px-6 py-4 sm:items-center sm:justify-between">
      <Button variant="ghost" disabled={submitting} onClick={onCancel}>{t("importDecisionCancel")}</Button>
      <Button className="min-w-24" disabled={submitting || updateBlocked} onClick={onContinue}>
        {submitting && <Spinner />}{submitting ? t("importDecisionSubmitting") : t("importDecisionContinue")}
      </Button>
    </AlertDialogFooter>
  </>;
}

function ChoiceMark({ selected }: { selected: boolean }) {
  return <div className={cn(
    "mt-1 grid size-5 shrink-0 place-items-center rounded-full border",
    selected ? "border-foreground bg-foreground text-background" : "border-muted-foreground/40",
  )}>{selected && <Check className="size-3" aria-hidden="true" />}</div>;
}

export function OctxImportDecisionDialog() {
  const t = useTranslations("Knowledge");
  const { tasks, decideImport } = useOctxImports();
  const task = tasks.find((item) => item.transfer.status === "decision_required");
  const conflict = task?.transfer.conflicts?.[0] || {};
  const sourceId = typeof conflict.source_id === "string" ? conflict.source_id : undefined;
  const sourceName = typeof conflict.source_name === "string" ? conflict.source_name : t("importExistingSource");
  const hasLocalChanges = conflict.local_changes === true;
  const allowUpdate = task?.transfer.allowed_actions.includes("update") === true;
  const [selectedAction, setSelectedAction] = React.useState<DecisionAction>("new");
  const [discard, setDiscard] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);

  React.useEffect(() => {
    setSelectedAction("new");
    setDiscard(false);
  }, [allowUpdate, task?.transferId]);

  async function decide(action: OctxImportAction) {
    const token = task?.transfer.decision_token;
    if (!task || !token) return;
    setSubmitting(true);
    try {
      if (action === "cancel") await decideImport(task.transferId, { action, decision_token: token });
      else await decideImport(task.transferId, buildImportDecisionRequest(action, token, sourceId, discard));
    } finally {
      setSubmitting(false);
    }
  }

  return <AlertDialog open={Boolean(task)}>
    <AlertDialogContent className="max-h-[calc(100svh-2rem)] max-w-[520px] gap-0 overflow-y-auto p-0">
      <OctxImportDecisionPanel
        sourceName={sourceName} hasLocalChanges={hasLocalChanges} allowUpdate={allowUpdate} selectedAction={selectedAction}
        discard={discard} submitting={submitting}
        onSelect={(action) => { setSelectedAction(action); if (action === "new") setDiscard(false); }}
        onDiscardChange={setDiscard}
        onCancel={() => void decide("cancel")}
        onContinue={() => void decide(selectedAction)}
      />
    </AlertDialogContent>
  </AlertDialog>;
}
