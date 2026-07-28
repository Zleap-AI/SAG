"use client";

import * as React from "react";
import { Check, Copy } from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/utils";

type RunSourceIdCopyOptions = {
  sourceId: string;
  copy?: (text: string) => Promise<void>;
  onSuccess: () => void;
  onFailure: () => void;
};

export async function runSourceIdCopy({
  sourceId,
  copy = copyText,
  onSuccess,
  onFailure,
}: RunSourceIdCopyOptions) {
  try {
    await copy(sourceId);
    onSuccess();
  } catch {
    onFailure();
  }
}

export function SourceIdCopy({
  sourceId,
  className,
}: {
  sourceId: string;
  className?: string;
}) {
  const t = useTranslations("Knowledge");
  const [done, setDone] = React.useState(false);
  const resetTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  React.useEffect(
    () => () => {
      if (resetTimer.current) clearTimeout(resetTimer.current);
    },
    [],
  );

  return (
    <div
      className={cn(
        "flex min-w-0 items-center gap-2 rounded-md bg-muted/45 px-2.5 py-1.5 text-xs",
        className,
      )}
    >
      <span className="shrink-0 text-muted-foreground">{t("sourceId")}</span>
      <code
        className="min-w-0 flex-1 truncate font-mono text-[11px] text-foreground/75"
        title={sourceId}
      >
        {sourceId}
      </code>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="size-6 shrink-0 text-muted-foreground"
        aria-label={t("copySourceId")}
        title={t("copySourceId")}
        onClick={() =>
          void runSourceIdCopy({
            sourceId,
            onSuccess: () => {
              setDone(true);
              toast.success(t("sourceIdCopied"));
              if (resetTimer.current) clearTimeout(resetTimer.current);
              resetTimer.current = setTimeout(() => setDone(false), 1500);
            },
            onFailure: () => toast.error(t("sourceIdCopyFailed")),
          })
        }
      >
        {done ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
      </Button>
    </div>
  );
}
