"use client";

import * as React from "react";
import { RotateCw, Save } from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { useApp } from "@/components/features/app-shell";
import { SettingsRow, SettingsSection } from "@/components/features/settings-section";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Slider } from "@/components/ui/slider";
import { Spinner } from "@/components/ui/spinner";
import { api, ApiError } from "@/lib/api";
import { SEARCH_STRATEGIES } from "@/lib/retrieval-config";
import type { ModelConfig, ModelConfigPatch } from "@/lib/types";

export function KnowledgeConfigForm() {
  const t = useTranslations("KnowledgeConfig");
  const strategies = useTranslations("SearchStrategies");
  const { refreshCapabilities } = useApp();
  const [loaded, setLoaded] = React.useState(false);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [saving, setSaving] = React.useState(false);
  const [chunkMaxTokens, setChunkMaxTokens] = React.useState(1_000);
  const [chunkMode, setChunkMode] =
    React.useState<ModelConfig["document_chunk_mode"]>("standard");
  const [extractConcurrency, setExtractConcurrency] = React.useState(30);
  const [strategy, setStrategy] = React.useState<ModelConfig["search_strategy"]>("multi");
  const [topK, setTopK] = React.useState(8);
  const [language, setLanguage] = React.useState<ModelConfig["sag_language"]>("zh");

  const hydrate = React.useCallback((config: ModelConfig) => {
    setChunkMaxTokens(config.document_chunk_max_tokens ?? 1_000);
    setChunkMode(config.document_chunk_mode ?? "standard");
    setExtractConcurrency(config.document_extract_concurrency ?? 30);
    setStrategy(config.search_strategy);
    setTopK(config.search_top_k);
    setLanguage(config.sag_language);
    setLoaded(true);
  }, []);

  const load = React.useCallback(async () => {
    setLoadError(null);
    try {
      hydrate(await api.getModelConfig());
    } catch (error) {
      setLoadError(error instanceof ApiError ? error.message : t("loadFailed"));
    }
  }, [hydrate, t]);

  React.useEffect(() => {
    void load();
  }, [load]);

  async function save() {
    setSaving(true);
    try {
      const patch: ModelConfigPatch = {
        document_chunk_max_tokens: chunkMaxTokens,
        document_chunk_mode: chunkMode,
        document_extract_concurrency: extractConcurrency,
        search_strategy: strategy,
        search_top_k: topK,
        sag_language: language,
      };
      const { config } = await api.saveModelConfig(patch);
      hydrate(config);
      await refreshCapabilities();
      toast.success(t("saved"));
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : t("saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  if (loadError) {
    return (
      <SettingsSection title={t("title")} description={t("description")}>
        <div className="p-4 sm:p-5">
          <Alert variant="destructive">
            <AlertTitle>{t("loadErrorTitle")}</AlertTitle>
            <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
              <span>{loadError}</span>
              <Button type="button" variant="outline" size="sm" onClick={() => void load()}>
                <RotateCw />
                {t("retry")}
              </Button>
            </AlertDescription>
          </Alert>
        </div>
      </SettingsSection>
    );
  }

  if (!loaded) {
    return (
      <div className="flex flex-col gap-6">
        {[
          [t("parsingTitle"), t("parsingLoading")],
          [t("retrievalTitle"), t("retrievalLoading")],
        ].map(([title, description]) => (
          <SettingsSection key={title} title={title} description={description}>
            <div className="grid gap-3 p-4 sm:grid-cols-2 sm:p-5">
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
            </div>
          </SettingsSection>
        ))}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <SettingsSection title={t("parsingTitle")} description={t("parsingDescription")}>
        <SettingsRow title={t("chunkSettings")} description={t("chunkDescription")}>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field>
              <FieldLabel htmlFor="kb-chunk-mode">{t("chunkMode")}</FieldLabel>
              <Select
                value={chunkMode}
                onValueChange={(value) =>
                  setChunkMode(value as ModelConfig["document_chunk_mode"])
                }
              >
                <SelectTrigger id="kb-chunk-mode">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="standard">{t("smartChunking")}</SelectItem>
                  <SelectItem value="heading_strict">{t("strictHeadings")}</SelectItem>
                </SelectContent>
              </Select>
              <FieldDescription>
                {chunkMode === "heading_strict"
                  ? t("strictHeadingsDescription")
                  : t("smartChunkingDescription")}
              </FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="kb-chunk-max-tokens">{t("maxTokens")}</FieldLabel>
              <Input
                id="kb-chunk-max-tokens"
                type="number"
                min={100}
                max={100000}
                step={100}
                value={chunkMaxTokens}
                onChange={(event) =>
                  setChunkMaxTokens(
                    Math.min(100000, Math.max(100, Number(event.target.value) || 100)),
                  )
                }
              />
              <FieldDescription>{t("maxTokensDescription")}</FieldDescription>
            </Field>
          </div>
        </SettingsRow>

        <SettingsRow title={t("extractionSettings")} description={t("extractionDescription")}>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field>
              <FieldLabel htmlFor="kb-extract-concurrency">{t("concurrency")}</FieldLabel>
              <Input
                id="kb-extract-concurrency"
                type="number"
                min={1}
                max={50}
                value={extractConcurrency}
                onChange={(event) =>
                  setExtractConcurrency(
                    Math.min(50, Math.max(1, Number(event.target.value) || 1)),
                  )
                }
              />
              <FieldDescription>{t("concurrencyDescription")}</FieldDescription>
            </Field>
          </div>
        </SettingsRow>
      </SettingsSection>

      <SettingsSection title={t("retrievalTitle")} description={t("retrievalDescription")}>
        <SettingsRow title={t("retrievalRules")} description={t("retrievalRulesDescription")}>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field>
              <FieldLabel htmlFor="kb-search-strategy">{t("retrievalStrategy")}</FieldLabel>
              <Select
                value={strategy}
                onValueChange={(value) =>
                  setStrategy(value as ModelConfig["search_strategy"])
                }
              >
                <SelectTrigger id="kb-search-strategy">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SEARCH_STRATEGIES.map(({ value, labelKey }) => (
                    <SelectItem key={value} value={value}>
                      {strategies(labelKey)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <Field>
              <FieldLabel htmlFor="kb-language">{t("extractionLanguage")}</FieldLabel>
              <Select
                value={language}
                onValueChange={(value) => setLanguage(value as ModelConfig["sag_language"])}
              >
                <SelectTrigger id="kb-language">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="zh">{t("chinese")}</SelectItem>
                  <SelectItem value="en">{t("english")}</SelectItem>
                </SelectContent>
              </Select>
            </Field>
            <Field className="sm:col-span-2">
              <FieldLabel>{t("topK", { count: topK })}</FieldLabel>
              <div className="flex h-9 items-center">
                <Slider
                  aria-label={t("topKAria")}
                  value={[topK]}
                  min={1}
                  max={50}
                  step={1}
                  onValueChange={([value]) => setTopK(value)}
                />
              </div>
            </Field>
          </div>
        </SettingsRow>
      </SettingsSection>

      <div className="flex justify-end border-t pt-4">
        <Button type="button" onClick={save} disabled={saving}>
          {saving ? <Spinner /> : <Save />}
          {saving ? t("saving") : t("save")}
        </Button>
      </div>
    </div>
  );
}
