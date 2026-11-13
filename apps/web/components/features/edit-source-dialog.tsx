"use client";

import * as React from "react";
import { Pencil } from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import type { Source } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Field, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

export function EditSourceDialog({
  source,
  onUpdated,
  buttonClassName,
  tooltipSide = "bottom",
}: {
  source: Source;
  onUpdated?: (source: Source) => void;
  buttonClassName?: string;
  tooltipSide?: React.ComponentProps<typeof TooltipContent>["side"];
}) {
  const t = useTranslations("SourceForm");
  const [open, setOpen] = React.useState(false);
  const [name, setName] = React.useState(source.name);
  const [description, setDescription] = React.useState(source.description ?? "");
  const [loading, setLoading] = React.useState(false);

  React.useEffect(() => {
    if (!open) return;
    setName(source.name);
    setDescription(source.description ?? "");
  }, [open, source]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const cleanName = name.trim();
    if (!cleanName) return;
    setLoading(true);
    try {
      const updated = await api.updateSource(source.id, {
        name: cleanName,
        description: description.trim(),
      });
      toast.success(t("updated"));
      onUpdated?.(updated);
      setOpen(false);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : t("updateFailed"));
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <Tooltip>
        <TooltipTrigger asChild>
          <DialogTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label={t("edit")}
              title={t("edit")}
              className={cn("text-muted-foreground hover:text-foreground", buttonClassName)}
            >
              <Pencil className="size-4" />
            </Button>
          </DialogTrigger>
        </TooltipTrigger>
        <TooltipContent side={tooltipSide}>{t("edit")}</TooltipContent>
      </Tooltip>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>{t("edit")}</DialogTitle>
          <DialogDescription>{t("editDescription")}</DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="flex flex-col gap-4">
          <div className="rounded-md border bg-muted/30 p-3 text-sm">
            <p>{t("algorithm")}：{t(source.engine_kind ?? "sag1")}</p>
            <p className="mt-1 text-xs text-muted-foreground">{t("algorithmImmutable")}</p>
          </div>
          <Field>
            <FieldLabel htmlFor={`source-name-${source.id}`}>{t("name")}</FieldLabel>
            <Input
              id={`source-name-${source.id}`}
              required
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t("namePlaceholder")}
              maxLength={200}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor={`source-desc-${source.id}`}>{t("description")}</FieldLabel>
            <Textarea
              id={`source-desc-${source.id}`}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t("descriptionPlaceholder")}
              rows={2}
            />
          </Field>

          <DialogFooter>
            <Button type="submit" disabled={loading || !name.trim()}>
              {loading ? t("saving") : t("saveChanges")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
