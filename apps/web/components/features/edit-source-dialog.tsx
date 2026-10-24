"use client";

import * as React from "react";
import { Pencil } from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { useApp } from "@/components/features/app-shell";
import { useJevQuickSetup } from "@/components/features/jev-quick-setup";
import { RetrievalModePicker } from "@/components/features/retrieval-mode-picker";
import { api, ApiError } from "@/lib/api";
import {
  DEFAULT_SEARCH_STRATEGY,
  getSearchStrategiesDisabled,
  isSearchStrategy,
  needsJevQuickSetup,
  type SearchStrategy,
} from "@/lib/retrieval-config";
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
  const strategies = useTranslations("SearchStrategies");
  const modes = useTranslations("RetrievalModes");
  const { capabilities } = useApp();
  const [open, setOpen] = React.useState(false);
  const [name, setName] = React.useState(source.name);
  const [description, setDescription] = React.useState(source.description ?? "");
  const [mode, setMode] = React.useState<SearchStrategy | null>(
    source.default_search_strategy ?? null,
  );
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const globalStrategy = isSearchStrategy(capabilities?.search_strategy)
    ? capabilities.search_strategy
    : DEFAULT_SEARCH_STRATEGY;
  const unsupportedMessage = strategies("unsupportedBackend");
  const disabledMap = React.useMemo(
    () => getSearchStrategiesDisabled(capabilities, unsupportedMessage),
    [capabilities, unsupportedMessage],
  );
  const jevSetupNeeded = open && needsJevQuickSetup(mode, disabledMap);
  const jevSetup = useJevQuickSetup({ enabled: jevSetupNeeded });
  const jevKeyMissing = jevSetupNeeded && !jevSetup.ready;

  React.useEffect(() => {
    if (!open) return;
    setName(source.name);
    setDescription(source.description ?? "");
    setMode(source.default_search_strategy ?? null);
    setError(null);
  }, [open, source]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const cleanName = name.trim();
    if (!cleanName || jevKeyMissing) return;
    setLoading(true);
    setError(null);
    try {
      if (jevSetupNeeded) {
        try {
          await jevSetup.save();
        } catch {
          // The quick setup shows the failure next to its fields.
          return;
        }
      }
      const updated = await api.updateSource(source.id, {
        name: cleanName,
        description: description.trim(),
        default_search_strategy: mode,
      });
      toast.success(t("updated"));
      onUpdated?.(updated);
      setOpen(false);
    } catch (err) {
      const message = err instanceof ApiError ? err.message : t("updateFailed");
      setError(message);
      toast.error(message);
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
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t("edit")}</DialogTitle>
          <DialogDescription>{t("editDescription")}</DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="flex flex-col gap-4">
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
          <RetrievalModePicker
            id={`source-mode-${source.id}`}
            value={mode}
            onValueChange={(next) => {
              setMode(next);
              setError(null);
            }}
            globalStrategy={globalStrategy}
            disabledMap={disabledMap}
            disabled={loading}
            jevSetup={jevSetup}
          />

          {error && (
            <p role="alert" className="text-xs text-destructive" data-testid="source-form-error">
              {error}
            </p>
          )}

          <DialogFooter className="sm:items-center">
            {jevKeyMissing && (
              <span className="text-xs text-muted-foreground sm:mr-auto" data-testid="source-form-blocked">
                {modes("keyRequiredToSubmit")}
              </span>
            )}
            <Button type="submit" disabled={loading || !name.trim() || jevKeyMissing}>
              {loading ? t("saving") : t("saveChanges")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
