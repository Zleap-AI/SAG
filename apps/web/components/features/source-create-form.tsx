"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
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
  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [engineKind, setEngineKind] = React.useState<"sag1" | "sag2">("sag1");
  const [loading, setLoading] = React.useState(false);
  const nameId = React.useId();
  const descriptionId = React.useId();

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setLoading(true);
    try {
      const source = await api.createSource({ name: name.trim(), description: description.trim(), engine_kind: engineKind });
      toast.success(t("created"));
      onCreated(source);
      setName("");
      setDescription("");
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : t("createFailed"));
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

      <fieldset className="grid gap-2" disabled={loading}>
        <legend className="mb-2 text-sm font-medium">{t("algorithm")}</legend>
        {(["sag1", "sag2"] as const).map(kind => (
          <label key={kind} className="flex cursor-pointer items-start gap-3 rounded-md border p-3">
            <input type="radio" name="engine_kind" value={kind} checked={engineKind === kind}
              onChange={() => setEngineKind(kind)} className="mt-1" />
            <span className="text-sm"><span className="block font-medium">{t(kind)}</span>
              <span className="text-xs text-muted-foreground">{t(`${kind}Description`)}</span></span>
          </label>
        ))}
        <p className="text-xs text-muted-foreground">{t("algorithmImmutable")}</p>
      </fieldset>
      <div className="flex justify-end gap-2 pt-1">
        {onCancel && (
          <Button type="button" variant="ghost" onClick={onCancel} disabled={loading}>
            {t("cancel")}
          </Button>
        )}
        <Button type="submit" disabled={loading || !name.trim()}>
          {loading ? t("creating") : t("create")}
        </Button>
      </div>
    </form>
  );
}
