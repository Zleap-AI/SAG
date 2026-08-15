"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { useApp } from "@/components/features/app-shell";
import { NasImportPanel } from "@/components/features/nas-import-panel";
import { UploadZone } from "@/components/features/upload-zone";
import { useFnOSNasStatus } from "@/components/features/use-fnos-nas-status";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

export function DocumentAddPanel({
  sourceId,
  sourceName,
  compact = false,
  onCompleted,
}: {
  sourceId: string;
  sourceName: string;
  compact?: boolean;
  onCompleted: () => void | Promise<void>;
}) {
  const t = useTranslations("FnOSNas");
  const { capabilities } = useApp();
  const nas = useFnOSNasStatus();
  const [activeTab, setActiveTab] = useState("local");
  const localUpload = (
    <UploadZone
      sourceId={sourceId}
      onUploaded={() => void onCompleted()}
      maxMb={capabilities?.max_upload_mb ?? 25}
      allowedExts={capabilities?.allowed_upload_exts}
      compact={compact}
    />
  );

  const nasAvailable = nas.enabled && Boolean(nas.status?.eligible);

  useEffect(() => {
    if (!nasAvailable && activeTab === "nas") setActiveTab("local");
  }, [activeTab, nasAvailable]);

  if (!nasAvailable || !nas.status) {
    return (
      <div className="space-y-3">
        {localUpload}
        {nas.enabled && nas.error ? (
          <Alert className="flex items-center justify-between gap-3">
            <AlertDescription>{t("accessUnavailable")}</AlertDescription>
            <Button type="button" size="sm" variant="ghost" onClick={() => nas.refresh()}>
              {t("retry")}
            </Button>
          </Alert>
        ) : null}
      </div>
    );
  }

  return (
    <Tabs value={activeTab} onValueChange={setActiveTab} className="w-full">
      <TabsList className="grid w-full grid-cols-2">
        <TabsTrigger value="local">{t("localUpload")}</TabsTrigger>
        <TabsTrigger value="nas">{t("nasDocuments")}</TabsTrigger>
      </TabsList>
      <TabsContent value="local" forceMount className="mt-4 data-[state=inactive]:hidden">
        {localUpload}
      </TabsContent>
      <TabsContent value="nas" forceMount className="mt-4 data-[state=inactive]:hidden">
        <NasImportPanel
          sourceId={sourceId}
          sourceName={sourceName}
          status={nas.status}
          refreshStatus={nas.refresh}
          compact={compact}
          onImported={onCompleted}
        />
      </TabsContent>
    </Tabs>
  );
}
