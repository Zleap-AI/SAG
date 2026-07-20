"use client";

import * as React from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useParams, useRouter } from "next/navigation";
import {
  ArrowLeft,
  FileText,
  FlaskConical,
  List,
  Network,
  Orbit,
  Plus,
  RefreshCw,
  Search,
  TriangleAlert,
} from "lucide-react";

import { useApp } from "@/components/features/app-shell";
import { DocumentList } from "@/components/features/document-list";
import { EmptyState } from "@/components/features/empty-state";
import { RetrievalTestDialog } from "@/components/features/retrieval-test-dialog";
import { SourceGraph } from "@/components/features/source-graph";
import { SyncPanel } from "@/components/features/sync-panel";
import { UploadZone } from "@/components/features/upload-zone";
import { useSourceContent } from "@/components/features/use-source-content";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Skeleton } from "@/components/ui/skeleton";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { cn } from "@/lib/utils";

type ContentView = "list" | "graph" | "graph3d";

export default function SourceDetailPage() {
  const t = useTranslations("Knowledge");
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { capabilities } = useApp();
  const { source, documents, refresh, notFound } = useSourceContent(id);
  const [contentView, setContentView] = React.useState<ContentView>("list");
  const graphViewActive = contentView !== "list";

  React.useEffect(() => {
    const saved = window.localStorage.getItem("sag:source-content-view");
    if (saved === "graph3d2") {
      setContentView("graph3d");
      window.localStorage.setItem("sag:source-content-view", "graph3d");
    } else if (saved === "graph" || saved === "graph3d") {
      setContentView(saved);
    }
  }, []);

  const changeContentView = (view: ContentView) => {
    setContentView(view);
    window.localStorage.setItem("sag:source-content-view", view);
  };

  React.useEffect(() => {
    if (notFound) router.replace("/knowledge");
  }, [notFound, router]);

  const [addOpen, setAddOpen] = React.useState(false);
  const [retrievalOpen, setRetrievalOpen] = React.useState(false);
  const isFileSource = !source || source.connector_kind === "file_upload";

  return (
    <div
      className={cn(
        "min-h-full",
        graphViewActive && "flex h-full min-h-0 flex-col overflow-hidden",
      )}
    >
      <div className="flex shrink-0 flex-wrap items-start justify-between gap-4 border-b px-4 py-5 md:px-6">
        <div className="min-w-0">
          <Link
            href="/knowledge"
            className="mb-2 inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
          >
            <ArrowLeft className="size-3.5" />
            {t("allSources")}
          </Link>
          {source ? (
            <>
              <h1 className="font-display text-xl font-semibold tracking-tight text-foreground">
                {source.name}
              </h1>
              <p className="mt-1.5 text-sm text-muted-foreground">
                {t("sourceStats", {
                  documents: source.document_count,
                  chunks: source.chunk_count,
                  events: source.event_count,
                })}
                {source.description ? ` · ${source.description}` : ""}
              </p>
            </>
          ) : (
            <Skeleton className="h-8 w-48" />
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="outline"
                size="icon"
                onClick={() => setAddOpen(true)}
                disabled={!source}
                aria-label={isFileSource ? t("addDocument") : t("syncSource")}
                title={isFileSource ? t("addDocument") : t("syncSource")}
              >
                {isFileSource ? (
                  <Plus className="size-4" />
                ) : (
                  <RefreshCw className="size-4" />
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">
              {isFileSource ? t("addDocument") : t("syncSource")}
            </TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="outline"
                size="icon"
                onClick={() => setRetrievalOpen(true)}
                disabled={!source}
                aria-label={t("retrievalTest")}
                title={t("retrievalTest")}
              >
                <FlaskConical className="size-4" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">{t("retrievalTest")}</TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                asChild
                size="icon"
                aria-label={t("searchSource")}
                title={t("searchSource")}
              >
                <Link href={source ? `/search?source=${source.id}` : "/search"}>
                  <Search className="size-4" />
                </Link>
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">{t("searchSource")}</TooltipContent>
          </Tooltip>
        </div>
      </div>

      <div
        className={cn(
          "mx-auto flex w-full flex-col gap-4 p-4 transition-[max-width,padding]",
          graphViewActive
            ? "min-h-0 max-w-none flex-1 overflow-hidden md:p-5"
            : "max-w-4xl md:p-6",
        )}
      >
        {capabilities && !capabilities.llm_configured && (
          <Alert>
            <TriangleAlert className="size-4" />
            <AlertTitle>{t("modelNotConfigured")}</AlertTitle>
            <AlertDescription>
              {t("modelWarningBefore")}
              <strong>{t("eventExtraction")}</strong>
              {t("and")}
              <strong>{t("qa")}</strong>
              {t("modelWarningSettings")}
              <Link
                href="/settings"
                className="font-medium underline underline-offset-2"
              >
                {t("settings")}
              </Link>
              {t("modelWarningAfter")}
            </AlertDescription>
          </Alert>
        )}

        <div className={cn(graphViewActive && "flex min-h-0 flex-1 flex-col")}>
          <div className="mb-2 flex shrink-0 flex-wrap items-center justify-between gap-3">
            <h2 className="text-sm font-medium text-muted-foreground">
              {contentView === "list"
                ? t("documents")
                : contentView === "graph3d"
                  ? t("graph3d")
                  : t("graph2d")}{" "}
              {documents
                ? t("parenthesizedCount", { count: documents.length })
                : ""}
            </h2>
            <ToggleGroup
              type="single"
              variant="outline"
              size="sm"
              value={contentView}
              onValueChange={(value) =>
                value && changeContentView(value as ContentView)
              }
              aria-label={t("contentViewAria")}
              className="rounded-md bg-card max-sm:w-full"
            >
              <ToggleGroupItem
                value="list"
                className="gap-1.5 px-3 max-sm:flex-1 max-sm:px-2"
                aria-label={t("listView")}
              >
                <List className="size-3.5" />
                {t("list")}
              </ToggleGroupItem>
              <ToggleGroupItem
                value="graph"
                className="gap-1.5 px-3 max-sm:flex-1 max-sm:px-2"
                aria-label={t("graph2dView")}
              >
                <Network className="size-3.5" />
                <span className="sm:hidden">2D</span>
                <span className="hidden sm:inline">{t("graph2d")}</span>
              </ToggleGroupItem>
              <ToggleGroupItem
                value="graph3d"
                className="gap-1.5 px-3 max-sm:flex-1 max-sm:px-2"
                aria-label={t("graph3dView")}
              >
                <Orbit className="size-3.5" />
                <span className="sm:hidden">3D</span>
                <span className="hidden sm:inline">{t("graph3d")}</span>
              </ToggleGroupItem>
            </ToggleGroup>
          </div>
          {documents === null || !source ? (
            <div className="flex flex-col gap-2">
              {Array.from({ length: 2 }).map((_, i) => (
                <Skeleton key={i} className="h-16" />
              ))}
            </div>
          ) : graphViewActive ? (
            <div className="min-h-0 flex-1">
              <SourceGraph
                source={source}
                documents={documents}
                mode={contentView === "graph3d" ? "3d" : "2d"}
                refreshKey={documents
                  .map(
                    (document) =>
                      `${document.id}:${document.status}:${document.event_count}`,
                  )
                  .join("|")}
              />
            </div>
          ) : documents.length === 0 ? (
            <EmptyState
              icon={FileText}
              title={t("emptyDocuments")}
              description={t("emptyDocumentsDescription")}
            />
          ) : (
            <DocumentList
              sourceId={id}
              documents={documents}
              onChange={refresh}
            />
          )}
        </div>
      </div>

      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {isFileSource ? t("addDocument") : t("syncSource")}
            </DialogTitle>
            <DialogDescription>
              {isFileSource
                ? t("addDialogDescription")
                : t("syncDialogDescription")}
            </DialogDescription>
          </DialogHeader>
          {source &&
            (source.connector_kind === "file_upload" ? (
              <UploadZone
                sourceId={id}
                onUploaded={() => {
                  setAddOpen(false);
                  void refresh();
                }}
                maxMb={capabilities?.max_upload_mb ?? 25}
                allowedExts={capabilities?.allowed_upload_exts}
              />
            ) : (
              <SyncPanel
                sourceId={id}
                onSynced={() => {
                  setAddOpen(false);
                  void refresh();
                }}
              />
            ))}
        </DialogContent>
      </Dialog>

      {source && (
        <RetrievalTestDialog
          sourceId={source.id}
          sourceName={source.name}
          open={retrievalOpen}
          onOpenChange={setRetrievalOpen}
        />
      )}
    </div>
  );
}
