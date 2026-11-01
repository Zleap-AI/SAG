"use client";
import { useTranslations } from "next-intl";
import { cn } from "@/lib/utils";

export function KnowledgeEnginePill({ kind = "sag1", className }: {
  kind?: "sag1" | "sag2"; className?: string;
}) {
  const t = useTranslations("SourceForm");
  return <span data-testid="source-engine-kind" data-engine-kind={kind}
    title={t("algorithmImmutable")}
    className={cn("inline-flex rounded-full border px-1.5 py-0.5 text-[10px] text-muted-foreground", className)}>
    {t(kind)}
  </span>;
}
