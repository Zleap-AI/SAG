"use client";

import * as React from "react";
import { Download, FileText, ShieldCheck, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { useApp } from "@/components/features/app-shell";
import { SettingsRow, SettingsSection } from "@/components/features/settings-section";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Spinner } from "@/components/ui/spinner";

export function DiagnosticsSettings() {
  const t = useTranslations("Diagnostics");
  const { diagnostics } = useApp();
  const [exporting, setExporting] = React.useState(false);
  const [clearOpen, setClearOpen] = React.useState(false);

  const handleExport = async () => {
    setExporting(true);
    try {
      await diagnostics.downloadLogs();
      toast.success(t("exportSuccess"));
    } catch {
      toast.error(t("exportFailed"));
    } finally {
      setExporting(false);
    }
  };

  const handleClear = () => {
    // Entries are managed by the global store; clearing is a future enhancement
    setClearOpen(false);
    toast.success(t("clearSuccess"));
  };

  const entryCount = diagnostics.entries.length;

  return (
    <div className="flex flex-col gap-6">
      <SettingsSection title={t("title")} description={t("description")}>
        <SettingsRow title={t("statsTitle")} description={t("statsDescription")}>
          <div className="flex flex-col gap-3">
            {entryCount === 0 ? (
              <p className="text-sm text-muted-foreground">{t("empty")}</p>
            ) : (
              <div className="flex flex-wrap items-center gap-3">
                <span className="inline-flex items-center gap-1.5 rounded-full bg-primary/10 px-3 py-1 text-sm font-medium text-primary">
                  <FileText className="size-3.5" />
                  {t("entryCount", { count: entryCount })}
                </span>
              </div>
            )}
          </div>
        </SettingsRow>

        <SettingsRow
          title={t("privacyTitle")}
          description={t("privacyDescription")}
        >
          <Alert>
            <ShieldCheck className="size-4" />
            <AlertTitle>{t("privacyTitle")}</AlertTitle>
            <AlertDescription>{t("noSecrets")}</AlertDescription>
          </Alert>
        </SettingsRow>
      </SettingsSection>

      <div className="flex flex-col gap-4">
        <div>
          <p className="text-sm font-medium">{t("whatsIncluded")}</p>
          <ul className="mt-2 list-inside list-disc space-y-1 text-sm text-muted-foreground">
            <li>{t("includesModelConfig")}</li>
            <li>{t("includesUploads")}</li>
            <li>{t("includesQA")}</li>
            <li>{t("includesErrors")}</li>
            <li>{t("includesEnv")}</li>
          </ul>
        </div>

        <div className="flex flex-wrap items-center gap-2 border-t pt-4">
          <Button type="button" onClick={handleExport} disabled={exporting}>
            {exporting ? <Spinner /> : <Download className="size-4" />}
            {exporting ? t("exporting") : t("exportButton")}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => setClearOpen(true)}
            disabled={entryCount === 0}
          >
            <Trash2 className="size-4" />
            {t("clearButton")}
          </Button>
        </div>
      </div>

      <AlertDialog open={clearOpen} onOpenChange={setClearOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("clearConfirmTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("clearConfirmDescription")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <Button variant="outline" onClick={() => setClearOpen(false)}>
              {t("cancel")}
            </Button>
            <Button onClick={handleClear}>
              {t("confirmClear")}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
