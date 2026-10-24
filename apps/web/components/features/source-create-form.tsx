"use client";

import * as React from "react";
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
import { Field, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

export function SourceCreateForm({
  onCreated,
  onCancel,
  compact = false,
}: {
  onCreated: (source: Source) => void;
  onCancel?: () => void;
  compact?: boolean;
}) {
  const t = useTranslations("SourceForm");
  const strategies = useTranslations("SearchStrategies");
  const modes = useTranslations("RetrievalModes");
  const { capabilities } = useApp();
  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [mode, setMode] = React.useState<SearchStrategy | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const nameId = React.useId();
  const descriptionId = React.useId();
  const modeId = React.useId();

  const globalStrategy = isSearchStrategy(capabilities?.search_strategy)
    ? capabilities.search_strategy
    : DEFAULT_SEARCH_STRATEGY;
  const unsupportedMessage = strategies("unsupportedBackend");
  const disabledMap = React.useMemo(
    () => getSearchStrategiesDisabled(capabilities, unsupportedMessage),
    [capabilities, unsupportedMessage],
  );
  const jevSetupNeeded = needsJevQuickSetup(mode, disabledMap);
  const jevSetup = useJevQuickSetup({ enabled: jevSetupNeeded });
  const jevKeyMissing = jevSetupNeeded && !jevSetup.ready;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (jevKeyMissing) return;
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
      const source = await api.createSource({
        name: name.trim(),
        description: description.trim(),
        ...(mode ? { default_search_strategy: mode } : {}),
      });
      toast.success(t("created"));
      onCreated(source);
      setName("");
      setDescription("");
      setMode(null);
    } catch (cause) {
      const message = cause instanceof ApiError ? cause.message : t("createFailed");
      setError(message);
      toast.error(message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <form
      onSubmit={submit}
      className={cn("flex flex-col gap-4", compact && "gap-3")}
    >
      <Field>
        <FieldLabel htmlFor={nameId}>{t("name")}</FieldLabel>
        <Input
          id={nameId}
          required
          autoFocus
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder={t("namePlaceholder")}
          maxLength={200}
        />
      </Field>
      <Field>
        <FieldLabel htmlFor={descriptionId}>{t("description")}</FieldLabel>
        <Textarea
          id={descriptionId}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder={t("descriptionPlaceholder")}
          rows={compact ? 3 : 2}
        />
      </Field>
      <RetrievalModePicker
        id={modeId}
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

      <div className="flex flex-wrap items-center justify-end gap-2 pt-1">
        {jevKeyMissing && (
          <span className="mr-auto text-xs text-muted-foreground" data-testid="source-form-blocked">
            {modes("keyRequiredToSubmit")}
          </span>
        )}
        {onCancel && (
          <Button type="button" variant="ghost" onClick={onCancel} disabled={loading}>
            {t("cancel")}
          </Button>
        )}
        <Button type="submit" disabled={loading || !name.trim() || jevKeyMissing}>
          {loading ? t("creating") : t("create")}
        </Button>
      </div>
    </form>
  );
}
