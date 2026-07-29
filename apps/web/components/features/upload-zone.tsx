"use client";

import * as React from "react";
import Link from "next/link";
import { UploadCloud } from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import { uploadConcurrencyGuidance } from "@/lib/upload-guidance";
import { cn } from "@/lib/utils";
import { Progress } from "@/components/ui/progress";
import { Spinner } from "@/components/ui/spinner";

export function UploadZone({
  sourceId,
  onUploaded,
  maxMb = 25,
  allowedExts,
  compact = false,
}: {
  sourceId: string;
  onUploaded: () => void;
  maxMb?: number;
  allowedExts?: string[];
  compact?: boolean;
}) {
  const t = useTranslations("UploadZone");
  const inputRef = React.useRef<HTMLInputElement>(null);
  const [drag, setDrag] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [progress, setProgress] = React.useState<{ name: string; pct: number; idx: number; total: number } | null>(null);

  const extOf = (name: string) => {
    const i = name.lastIndexOf(".");
    return i >= 0 ? name.slice(i).toLowerCase() : "";
  };
  const accept = allowedExts && allowedExts.length > 0 ? allowedExts.join(",") : undefined;
  const guidance = uploadConcurrencyGuidance(compact);

  async function handleFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    setBusy(true);
    let ok = 0;
    for (const file of Array.from(files)) {
      // 客户端先行拦截：不支持的扩展名即时提示，省一次往返
      if (allowedExts && allowedExts.length > 0 && !allowedExts.includes(extOf(file.name))) {
        toast.error(t("unsupportedType", { name: file.name }));
        continue;
      }
      try {
        const idx = ok + 1;
        setProgress({ name: file.name, pct: 0, idx, total: files.length });
        await api.uploadDocumentWithProgress(sourceId, file, (pct) =>
          setProgress((p) => (p ? { ...p, pct } : p)),
        );
        ok += 1;
      } catch (err) {
        toast.error(t("fileFailed", {
          name: file.name,
          error: err instanceof ApiError ? err.message : t("uploadFailed"),
        }));
      }
    }
    setBusy(false);
    setProgress(null);
    if (ok > 0) {
      toast.success(t("uploaded", { count: ok }));
      onUploaded();
    }
  }

  return (
    <div className="flex flex-col items-center gap-2">
      <div
        role="button"
        tabIndex={0}
        onClick={() => inputRef.current?.click()}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDrag(true);
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          handleFiles(e.dataTransfer.files);
        }}
        className={cn(
          "flex w-full cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border border-dashed text-center transition-colors",
          compact ? "px-4 py-6" : "px-6 py-10",
          drag ? "border-border bg-muted" : "bg-card/40 hover:border-border",
        )}
      >
        <input
          ref={inputRef}
          type="file"
          multiple
          accept={accept}
          className="hidden"
          onChange={(e) => handleFiles(e.target.files)}
        />
        <div
          className={cn(
            "grid place-items-center rounded-full bg-muted text-foreground",
            compact ? "size-9" : "size-10",
          )}
        >
          {busy ? (
            <Spinner className={compact ? "size-4" : "size-5"} />
          ) : (
            <UploadCloud className={compact ? "size-4" : "size-5"} />
          )}
        </div>
        <div className="text-sm font-medium text-foreground">
          {busy ? t("uploading") : t("prompt")}
        </div>
        {busy && progress ? (
          <div className="flex w-full max-w-xs flex-col gap-1.5">
            <Progress
              value={progress.pct}
              className="h-1.5"
              aria-label={t("uploadProgress")}
            />
            <div className="flex items-center justify-between text-xs tabular-nums text-muted-foreground">
              <span className="min-w-0 flex-1 truncate">{progress.name}</span>
              <span className="shrink-0 pl-2">
                {progress.total > 1 ? `${progress.idx}/${progress.total} · ` : ""}
                {progress.pct}%
              </span>
            </div>
          </div>
        ) : (
          <div className="text-xs text-muted-foreground">
            {t("support", { maxMb })}
          </div>
        )}
      </div>
      <p className={guidance.textClassName}>
        {t("concurrencyHint")}{" "}
        <Link
          href={guidance.href}
          className="font-medium text-foreground underline underline-offset-2"
        >
          {t("concurrencyAction")}
        </Link>
      </p>
    </div>
  );
}
