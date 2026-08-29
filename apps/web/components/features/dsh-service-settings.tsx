"use client";

import * as React from "react";
import { Download, Plug, RotateCw } from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { CodeBlock } from "@/components/features/code-block";
import { CopyButton } from "@/components/features/copy-button";
import { SettingsRow, SettingsSection } from "@/components/features/settings-section";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { api, ApiError } from "@/lib/api";
import type { DshIntegrationDescriptor, Source } from "@/lib/types";

const NO_DEFAULT_SOURCE_VALUE = "no-default-source";

export type DshConnectionGuidanceKey = "connectionReady" | "connectionDownload";

export function dshInstallCommand() {
  return "dsh plugin --profile web add @zleap-ai/dsh-sag";
}

export function dshSetupCommand() {
  return "dsh plugin --profile web exec dsh-sag setup ./sag-dsh.json";
}

export function dshConnectionFilename() {
  return "sag-dsh.json";
}

export function dshConnectionGuidance(
  state: "ready" | "download",
): DshConnectionGuidanceKey {
  return state === "ready"
    ? "connectionReady"
    : "connectionDownload";
}

export function DshServiceSettings() {
  const t = useTranslations("DshService");
  const [descriptor, setDescriptor] = React.useState<DshIntegrationDescriptor | null>(null);
  const [sources, setSources] = React.useState<Source[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const [saving, setSaving] = React.useState(false);
  const [downloading, setDownloading] = React.useState(false);
  const [regenerating, setRegenerating] = React.useState(false);
  const [regenerateOpen, setRegenerateOpen] = React.useState(false);

  const load = React.useCallback(async () => {
    setError(null);
    try {
      const [nextDescriptor, nextSources] = await Promise.all([
        api.dshIntegration(),
        api.listSources(),
      ]);
      setDescriptor(nextDescriptor);
      setSources(nextSources);
    } catch (loadError) {
      setError(loadError instanceof ApiError ? loadError.message : t("loadFailed"));
    }
  }, [t]);

  React.useEffect(() => {
    void load();
  }, [load]);

  const updateDefaultSource = async (value: string) => {
    setSaving(true);
    try {
      setDescriptor(await api.updateDshIntegration(
        value === NO_DEFAULT_SOURCE_VALUE ? null : value,
      ));
      toast.success(t("defaultSaved"));
    } catch (saveError) {
      toast.error(saveError instanceof ApiError ? saveError.message : t("saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const downloadConnection = async () => {
    setDownloading(true);
    try {
      const url = URL.createObjectURL(await api.downloadDshConnection());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = dshConnectionFilename();
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast.success(t("downloadSuccess"));
    } catch (downloadError) {
      toast.error(downloadError instanceof ApiError ? downloadError.message : t("downloadFailed"));
    } finally {
      setDownloading(false);
    }
  };

  const regenerateConnection = async () => {
    setRegenerating(true);
    try {
      setDescriptor(await api.regenerateDshToken());
      setRegenerateOpen(false);
      toast.success(t("regenerateSuccess"));
    } catch (regenerateError) {
      toast.error(
        regenerateError instanceof ApiError ? regenerateError.message : t("regenerateFailed"),
      );
    } finally {
      setRegenerating(false);
    }
  };

  if (error) {
    return (
      <SettingsSection title={t("title")} description={t(dshConnectionGuidance("ready"))}>
        <SettingsRow title={t("checking")}>
          <Alert variant="destructive">
            <AlertTitle>{t("loadErrorTitle")}</AlertTitle>
            <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
              <span>{error}</span>
              <Button type="button" variant="outline" size="sm" onClick={() => void load()}>
                <RotateCw />
                {t("retry")}
              </Button>
            </AlertDescription>
          </Alert>
        </SettingsRow>
      </SettingsSection>
    );
  }

  if (!descriptor) {
    return (
      <SettingsSection title={t("title")} description={t(dshConnectionGuidance("ready"))}>
        <SettingsRow title={t("checking")}>
          <Skeleton className="h-9 w-full sm:w-72" />
        </SettingsRow>
      </SettingsSection>
    );
  }

  return (
    <SettingsSection title={t("title")} description={t(dshConnectionGuidance("ready"))}>
      <SettingsRow title={t("installPlugin")} description={t("installDescription")}>
        <CodeBlock>{dshInstallCommand()}</CodeBlock>
      </SettingsRow>

      <SettingsRow
        title={t("defaultKnowledge")}
        description={t("defaultKnowledgeDescription")}
      >
        <Select
          value={descriptor.defaultSourceId ?? NO_DEFAULT_SOURCE_VALUE}
          onValueChange={(value) => void updateDefaultSource(value)}
          disabled={saving}
        >
          <SelectTrigger className="w-full sm:w-72" aria-label={t("defaultKnowledgeAria")}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={NO_DEFAULT_SOURCE_VALUE}>{t("noDefaultSource")}</SelectItem>
            {sources.map((source) => (
              <SelectItem key={source.id} value={source.id}>{source.name}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </SettingsRow>

      <SettingsRow
        title={t("downloadConnection")}
        description={t(dshConnectionGuidance("download"))}
      >
        <div className="grid gap-3">
          <div className="grid gap-2">
            <div className="flex justify-end">
              <CopyButton text={dshSetupCommand()} label={t("setupCommand")} />
            </div>
            <CodeBlock>{dshSetupCommand()}</CodeBlock>
          </div>
          <Button type="button" onClick={() => void downloadConnection()} disabled={downloading}>
            {downloading ? <Spinner /> : <Download />}
            {downloading ? t("downloading") : t("download")}
          </Button>
        </div>
      </SettingsRow>

      <SettingsRow title={t("regenerate")} description={t("regenerateDescription")}>
        <Button type="button" variant="outline" onClick={() => setRegenerateOpen(true)}>
          <Plug />
          {t("regenerateButton")}
        </Button>
      </SettingsRow>

      <AlertDialog open={regenerateOpen} onOpenChange={setRegenerateOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("regenerateConfirmTitle")}</AlertDialogTitle>
            <AlertDialogDescription>{t("regenerateConfirmDescription")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <Button variant="outline" onClick={() => setRegenerateOpen(false)} disabled={regenerating}>
              {t("cancel")}
            </Button>
            <Button onClick={() => void regenerateConnection()} disabled={regenerating}>
              {regenerating && <Spinner />}
              {regenerating ? t("regenerating") : t("regenerateButton")}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </SettingsSection>
  );
}
