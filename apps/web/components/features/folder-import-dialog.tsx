"use client";

import * as React from "react";
import {
  CheckCircle2,
  FileWarning,
  FolderOpen,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { useLocale, useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { api } from "@/lib/api";
import {
  buildFolderImportPlan,
  hasUnresolvedFolderImportConflicts,
  resolveFolderImportItem,
  selectedFolderImportItems,
  setAllFolderImportItemsSelected,
  setFolderImportItemSelected,
  type FolderImportItem,
  type FolderImportPlan,
} from "@/lib/folder-import";
import { formatBytes } from "@/lib/format";
import {
  folderImportDiagnosticData,
  getDiagnosticsStore,
} from "@/lib/diagnostics";
import {
  createFolderImportUploadSession,
  folderImportDispatchItems,
  type CompletedFolderImportBatch,
  type FolderImportUploadProgress,
  type FolderImportUploadRunResult,
  type FolderImportUploadSession,
} from "./folder-import-upload";

type FolderImportStep =
  | "choose"
  | "summary"
  | "selection"
  | "conflicts"
  | "confirm"
  | "uploading"
  | "complete";

export interface FolderImportDialogHandle {
  dismiss: () => void;
}

export function dismissFolderImportDialog(
  handle: FolderImportDialogHandle | null,
  setOpen: (open: boolean) => void,
) {
  handle?.dismiss();
  setOpen(false);
}

interface FolderImportDialogProps {
  sourceId: string;
  existingDocumentNames: string[];
  allowedExts: string[];
  maxMb: number;
  onFinished: (result: CompletedFolderImportBatch) => void;
  onClose: () => void;
}

interface FolderImportSelectionListProps {
  plan: FolderImportPlan;
  onSelectAll: (selected: boolean) => void;
  onSelectItem: (itemId: string, selected: boolean) => void;
}

export function FolderImportSelectionList({
  plan,
  onSelectAll,
  onSelectItem,
}: FolderImportSelectionListProps) {
  const t = useTranslations("FolderImport");
  const locale = useLocale();
  const selectableItems = plan.items.filter((item) => item.status !== "rejected");
  const allSelected = selectableItems.length > 0 && selectableItems.every((item) => item.selected);
  const selectedCount = selectedFolderImportItems(plan).length;

  return (
    <div className="flex flex-col gap-3">
      <label className="flex items-center gap-2 text-sm font-medium">
        <input
          type="checkbox"
          checked={allSelected}
          disabled={selectableItems.length === 0}
          onChange={(event) => onSelectAll(event.target.checked)}
        />
        {t("selectAll")}
      </label>
      <p className="text-xs text-muted-foreground">
        {t("selectedCount", { count: selectedCount })}
      </p>
      <ul
        className="max-h-64 space-y-2 overflow-auto"
        aria-label={t("selectedFiles", { count: selectedCount })}
      >
        {plan.items.map((item) => (
          <FolderImportSelectionItem
            key={item.id}
            item={item}
            locale={locale}
            onSelectItem={onSelectItem}
          />
        ))}
      </ul>
    </div>
  );
}

function FolderImportSelectionItem({
  item,
  locale,
  onSelectItem,
}: {
  item: FolderImportItem;
  locale: string;
  onSelectItem: (itemId: string, selected: boolean) => void;
}) {
  const t = useTranslations("FolderImport");
  const disabled = item.status === "rejected";
  const inputId = `folder-import-select-${item.id}`;

  return (
    <li className="rounded-md border p-3">
      <div className="flex items-start gap-2">
        <input
          id={inputId}
          type="checkbox"
          checked={item.selected}
          disabled={disabled}
          onChange={(event) => onSelectItem(item.id, event.target.checked)}
          className="mt-0.5"
        />
        <label htmlFor={inputId} className="min-w-0 flex-1 text-xs">
          <span className="block truncate font-medium">{item.name || t("missingName")}</span>
          <span className="block truncate text-muted-foreground">{item.displayPath}</span>
          <span className="block text-muted-foreground">{formatBytes(item.file.size, locale)}</span>
          {item.rejectReason ? (
            <span className="block text-destructive">{t(`rejection.${item.rejectReason}`)}</span>
          ) : null}
        </label>
      </div>
    </li>
  );
}

export const FolderImportDialog = React.forwardRef<
  FolderImportDialogHandle,
  FolderImportDialogProps
>(function FolderImportDialog({
  sourceId,
  existingDocumentNames,
  allowedExts,
  maxMb,
  onFinished,
  onClose,
}, ref) {
  const t = useTranslations("FolderImport");
  const fallbackInputRef = React.useRef<HTMLInputElement>(null);
  const directoryInputRef = React.useRef<HTMLInputElement>(null);
  const cancelRef = React.useRef(false);
  const sessionRef = React.useRef<FolderImportUploadSession | null>(null);
  const mountedRef = React.useRef(true);
  const [step, setStep] = React.useState<FolderImportStep>("choose");
  const [directorySupported, setDirectorySupported] = React.useState<
    boolean | null
  >(null);
  const [plan, setPlan] = React.useState<FolderImportPlan | null>(null);
  const [liveMessage, setLiveMessage] = React.useState(t("chooseFolder"));
  const [progress, setProgress] =
    React.useState<FolderImportUploadProgress | null>(null);
  const [failures, setFailures] = React.useState<
    FolderImportUploadRunResult["failures"]
  >([]);
  const [result, setResult] = React.useState<CompletedFolderImportBatch>({
    attempted: 0,
    succeeded: 0,
    failed: 0,
    cancelled: 0,
  });

  React.useEffect(() => {
    mountedRef.current = true;
    const input = document.createElement("input");
    setDirectorySupported("webkitdirectory" in input);
    return () => {
      mountedRef.current = false;
      cancelRef.current = true;
      sessionRef.current?.dismiss();
    };
  }, []);

  React.useImperativeHandle(
    ref,
    () => ({
      dismiss() {
        cancelRef.current = true;
        sessionRef.current?.dismiss();
      },
    }),
    [],
  );

  const accept = allowedExts.length > 0 ? allowedExts.join(",") : undefined;
  const directoryInputProps = {
    webkitdirectory: "",
  } as React.InputHTMLAttributes<HTMLInputElement> & { webkitdirectory: string };

  function resetBatch() {
    sessionRef.current?.dismiss();
    sessionRef.current = null;
    cancelRef.current = false;
    setPlan(null);
    setFailures([]);
    setProgress(null);
    setResult({ attempted: 0, succeeded: 0, failed: 0, cancelled: 0 });
    setStep("choose");
    setLiveMessage(t("chooseFolder"));
  }

  function handleSelection(fileList: FileList | null) {
    const files = fileList ? Array.from(fileList) : [];
    if (files.length === 0) {
      setLiveMessage(t("emptyFolder"));
      return;
    }

    const startedAt = Date.now();
    const nextPlan = buildFolderImportPlan(
      files,
      existingDocumentNames,
      allowedExts,
      maxMb * 1024 * 1024,
    );
    const batchId = crypto.randomUUID();
    const diagnostics = getDiagnosticsStore();
    sessionRef.current = createFolderImportUploadSession({
      batchId,
      upload: ({ item, batchId: uploadBatchId, onProgress }) =>
        api.uploadDocumentWithProgress(
          sourceId,
          item.file,
          onProgress,
          undefined,
          { folderImportId: uploadBatchId },
        ),
      record: diagnostics.record.bind(diagnostics),
      onFinished,
      onProgress: (nextProgress) => {
        if (!mountedRef.current) return;
        setProgress(nextProgress);
        setLiveMessage(
          t("uploadProgress", {
            current: nextProgress.current,
            total: nextProgress.total,
          }),
        );
      },
      fallbackErrorMessage: t("uploadFailed"),
    });
    setPlan(nextPlan);
    setFailures([]);
    setResult({ attempted: 0, succeeded: 0, failed: 0, cancelled: 0 });
    setStep("summary");
    setLiveMessage(
      t("scanSummary", {
        eligible: nextPlan.summary.eligible,
        conflicts: nextPlan.summary.conflicts,
        rejected: nextPlan.summary.rejected,
      }),
    );
    getDiagnosticsStore().record(
      "knowledge.folder_scan",
      folderImportDiagnosticData({
        batch_id: batchId,
        eligible_count: nextPlan.summary.eligible,
        conflict_count: nextPlan.summary.conflicts,
        rejected_count: nextPlan.summary.rejected,
        duration_ms: Date.now() - startedAt,
      }),
    );

    if (fallbackInputRef.current) fallbackInputRef.current.value = "";
    if (directoryInputRef.current) directoryInputRef.current.value = "";
  }

  function goForwardFromSummary() {
    if (!plan) return;
    setStep("selection");
    setLiveMessage(t("selectFiles"));
  }

  function goForwardFromSelection() {
    if (!plan) return;
    if (plan.items.some((item) => item.selected && item.status === "conflict")) {
      setStep("conflicts");
      setLiveMessage(t("inspectConflicts"));
      return;
    }
    setStep("confirm");
    setLiveMessage(t("finalConfirmation"));
  }

  function decideConflict(itemId: string, decision: "skip" | "upload") {
    if (!plan) return;
    const nextPlan = resolveFolderImportItem(plan, itemId, decision);
    setPlan(nextPlan);
    setLiveMessage(
      hasUnresolvedFolderImportConflicts(nextPlan)
        ? t("conflictsRemain")
        : t("conflictsResolved"),
    );
  }

  function prepareUpload(total: number) {
    cancelRef.current = false;
    setFailures([]);
    setStep("uploading");
    setLiveMessage(t("uploadProgress", { current: 0, total }));
  }

  function applyUploadResult(uploadResult: FolderImportUploadRunResult) {
    setFailures(uploadResult.failures);
    setProgress(null);
    setResult(uploadResult.batch);
    setStep("complete");
    setLiveMessage(
      t("uploadComplete", {
        succeeded: uploadResult.succeeded,
        failed: uploadResult.failures.length,
        cancelled: uploadResult.cancelled,
      }),
    );
  }

  async function uploadConfirmedPlan() {
    if (!plan || !sessionRef.current) return;
    const items = folderImportDispatchItems(plan, true);
    if (items.length === 0) return;
    prepareUpload(items.length);
    const uploadResult = await sessionRef.current.runInitial(items);
    if (mountedRef.current) applyUploadResult(uploadResult);
  }

  async function retryFailedItems() {
    if (!sessionRef.current || failures.length === 0) return;
    prepareUpload(failures.length);
    const uploadResult = await sessionRef.current.retryFailures();
    if (mountedRef.current && uploadResult) applyUploadResult(uploadResult);
  }

  const conflicts =
    plan?.items.filter((item) => item.selected && item.status === "conflict") ?? [];
  const rejected =
    plan?.items.filter((item) => item.status === "rejected") ?? [];
  const uploadable = plan ? folderImportDispatchItems(plan, true) : [];
  const selectedItems = plan ? selectedFolderImportItems(plan) : [];
  const skippedCount = plan ? plan.items.length - uploadable.length : 0;
  const unresolved = plan ? hasUnresolvedFolderImportConflicts(plan) : false;

  return (
    <section
      className="flex flex-col gap-4 border-t pt-4"
      aria-labelledby="folder-import-title"
    >
      <div>
        <h3
          id="folder-import-title"
          className="text-sm font-semibold text-foreground"
        >
          {t("title")}
        </h3>
        <p className="mt-1 text-xs text-muted-foreground">
          {t("relativePathsNotSent")}
        </p>
      </div>

      <ol
        className="grid grid-cols-2 gap-2 text-xs text-muted-foreground sm:grid-cols-4"
        aria-label={t("steps")}
      >
        <li className="rounded-md bg-muted px-2 py-1.5">
          1. {t("scanResult")}
        </li>
        <li className="rounded-md bg-muted px-2 py-1.5">
          2. {t("selectFiles")}
        </li>
        <li className="rounded-md bg-muted px-2 py-1.5">
          3. {t("inspectConflicts")}
        </li>
        <li className="rounded-md bg-muted px-2 py-1.5">
          4. {t("finalConfirmation")}
        </li>
      </ol>

      <p className="sr-only" aria-live="polite" aria-atomic="true">
        {liveMessage}
      </p>

      <input
        ref={fallbackInputRef}
        type="file"
        multiple
        accept={accept}
        className="hidden"
        onChange={(event) => handleSelection(event.target.files)}
      />
      <input
        {...directoryInputProps}
        ref={directoryInputRef}
        type="file"
        multiple
        accept={accept}
        className="hidden"
        onChange={(event) => handleSelection(event.target.files)}
      />

      {step === "choose" ? (
        <div className="flex flex-col gap-3">
          <div className="grid gap-2 sm:grid-cols-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => fallbackInputRef.current?.click()}
            >
              <FileWarning className="size-4" />
              {t("chooseFilesFallback")}
            </Button>
            <Button
              type="button"
              onClick={() => directoryInputRef.current?.click()}
              disabled={directorySupported !== true}
            >
              <FolderOpen className="size-4" />
              {t("chooseFolder")}
            </Button>
          </div>
          {directorySupported === false ? (
            <p className="text-xs text-muted-foreground">
              {t("browserUnsupported")}
            </p>
          ) : null}
          <Button
            type="button"
            variant="ghost"
            onClick={onClose}
            className="self-end"
          >
            {t("close")}
          </Button>
        </div>
      ) : null}

      {step === "summary" && plan ? (
        <div className="flex flex-col gap-3">
          <h4 className="text-sm font-medium">{t("scanResult")}</h4>
          <div className="grid grid-cols-3 gap-2 text-center text-xs">
            <div className="rounded-md border p-2">
              <CheckCircle2 className="mx-auto mb-1 size-4 text-emerald-600" />
              {t("eligibleCount", { count: plan.summary.eligible })}
            </div>
            <div className="rounded-md border p-2">
              <FileWarning className="mx-auto mb-1 size-4 text-amber-600" />
              {t("conflictCount", { count: plan.summary.conflicts })}
            </div>
            <div className="rounded-md border p-2">
              <XCircle className="mx-auto mb-1 size-4 text-destructive" />
              {t("rejectedCount", { count: plan.summary.rejected })}
            </div>
          </div>
          {rejected.length > 0 ? (
            <ul className="max-h-32 space-y-1 overflow-auto text-xs text-muted-foreground">
              {rejected.map((item) => (
                <li
                  key={item.id}
                  className="flex items-center justify-between gap-2"
                >
                  <span className="truncate">
                    {item.displayPath || t("missingName")}
                  </span>
                  <span className="shrink-0">
                    {item.rejectReason ? t(`rejection.${item.rejectReason}`) : null}
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
          <div className="flex justify-between gap-2">
            <Button type="button" variant="outline" onClick={resetBatch}>
              {t("chooseAgain")}
            </Button>
            <Button type="button" onClick={goForwardFromSummary}>
              {t("selectFiles")}
            </Button>
          </div>
        </div>
      ) : null}

      {step === "selection" && plan ? (
        <div className="flex flex-col gap-3">
          <div>
            <h4 className="text-sm font-medium">{t("selectFiles")}</h4>
            <p className="mt-1 text-xs text-muted-foreground">
              {t("selectionDescription")}
            </p>
          </div>
          <FolderImportSelectionList
            plan={plan}
            onSelectAll={(selected) => {
              const nextPlan = setAllFolderImportItemsSelected(plan, selected);
              setPlan(nextPlan);
              setLiveMessage(t("selectedCount", {
                count: selectedFolderImportItems(nextPlan).length,
              }));
            }}
            onSelectItem={(itemId, selected) => {
              const nextPlan = setFolderImportItemSelected(plan, itemId, selected);
              setPlan(nextPlan);
              setLiveMessage(t("selectedCount", {
                count: selectedFolderImportItems(nextPlan).length,
              }));
            }}
          />
          <div className="flex justify-between gap-2">
            <Button type="button" variant="outline" onClick={() => setStep("summary")}>
              {t("back")}
            </Button>
            <Button
              type="button"
              disabled={selectedItems.length === 0}
              onClick={goForwardFromSelection}
            >
              {t("continue")}
            </Button>
          </div>
        </div>
      ) : null}

      {step === "conflicts" && plan ? (
        <div className="flex flex-col gap-3">
          <h4 className="text-sm font-medium">{t("inspectConflicts")}</h4>
          <p className="text-xs text-muted-foreground">{t("undecided")}</p>
          <div className="max-h-64 space-y-3 overflow-auto">
            {conflicts.map((item) => (
              <fieldset key={item.id} className="rounded-md border p-3">
                <legend className="max-w-full truncate px-1 text-xs font-medium">
                  {item.displayPath}
                </legend>
                <div className="mt-2 flex gap-4 text-sm">
                  <label className="flex items-center gap-2">
                    <input
                      type="radio"
                      name={`folder-import-${item.id}`}
                      value="skip"
                      checked={item.decision === "skip"}
                      onChange={() => decideConflict(item.id, "skip")}
                    />
                    {t("skip")}
                  </label>
                  <label className="flex items-center gap-2">
                    <input
                      type="radio"
                      name={`folder-import-${item.id}`}
                      value="upload"
                      checked={item.decision === "upload"}
                      onChange={() => decideConflict(item.id, "upload")}
                    />
                    {t("addAnyway")}
                  </label>
                </div>
              </fieldset>
            ))}
          </div>
          <div className="flex justify-between gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => setStep("selection")}
            >
              {t("back")}
            </Button>
            <Button
              type="button"
              disabled={unresolved}
              onClick={() => {
                setStep("confirm");
                setLiveMessage(t("finalConfirmation"));
              }}
            >
              {t("finalConfirmation")}
            </Button>
          </div>
        </div>
      ) : null}

      {step === "confirm" && plan ? (
        <div className="flex flex-col gap-3">
          <div className="flex items-start gap-2 rounded-md border p-3">
            <ShieldCheck className="mt-0.5 size-4 shrink-0 text-emerald-600" />
            <div>
              <h4 className="text-sm font-medium">{t("finalConfirmation")}</h4>
              <p className="mt-1 text-xs text-muted-foreground">
                {t("confirmDescription", { count: uploadable.length })}
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                {t("selectedFiles", { count: selectedItems.length })} · {t("skippedCount", { count: skippedCount })}
              </p>
            </div>
          </div>
          <div className="flex justify-between gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={() =>
                setStep(conflicts.length > 0 ? "conflicts" : "selection")
              }
            >
              {t("back")}
            </Button>
            <Button
              type="button"
              disabled={unresolved || uploadable.length === 0}
              onClick={() => void uploadConfirmedPlan()}
            >
              {t("uploadCount", { count: uploadable.length })}
            </Button>
          </div>
        </div>
      ) : null}

      {step === "uploading" && progress ? (
        <div className="flex flex-col gap-3">
          <h4 className="text-sm font-medium">
            {t("uploadProgress", {
              current: progress.current,
              total: progress.total,
            })}
          </h4>
          <p className="truncate text-xs text-muted-foreground">
            {progress.item.name}
          </p>
          <Progress
            value={progress.percent}
            aria-label={t("uploadPercent", { percent: progress.percent })}
          />
          <Button
            type="button"
            variant="outline"
            onClick={() => {
              cancelRef.current = true;
              sessionRef.current?.cancel();
              setLiveMessage(t("cancellingRemaining"));
            }}
          >
            {t("cancelRemaining")}
          </Button>
        </div>
      ) : null}

      {step === "complete" ? (
        <div className="flex flex-col gap-3">
          <h4 className="text-sm font-medium">{t("completeTitle")}</h4>
          <p className="text-xs text-muted-foreground">
            {t("uploadComplete", {
              succeeded: result.succeeded,
              failed: result.failed,
              cancelled: result.cancelled,
            })}
          </p>
          {failures.length > 0 ? (
            <ul className="max-h-32 space-y-1 overflow-auto text-xs">
              {failures.map(({ item, message }) => (
                <li
                  key={item.id}
                  className="flex items-center gap-2 text-destructive"
                >
                  <XCircle className="size-3.5 shrink-0" />
                  <span className="truncate">
                    {item.name}: {message}
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
          <div className="flex justify-end gap-2">
            {failures.length > 0 ? (
              <Button
                type="button"
                variant="outline"
                onClick={() => void retryFailedItems()}
              >
                {t("retryFailed")}
              </Button>
            ) : null}
            <Button type="button" onClick={onClose}>
              {t("done")}
            </Button>
          </div>
        </div>
      ) : null}
    </section>
  );
});
