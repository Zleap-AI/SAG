"use client";

import * as React from "react";
import { FlaskConical, RotateCw, Save } from "lucide-react";
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
import {
  getSearchStrategiesDisabled,
  isJevConfigDisabledReason,
  isJevScoredStrategy,
  JEV_SCORED_STRATEGIES,
  SEARCH_STRATEGIES,
  type JevScoredStrategy,
} from "@/lib/retrieval-config";
import type { ModelConfig, ModelConfigPatch } from "@/lib/types";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

const JEV_DEFAULT_MODEL = "jev-latest";
const JEV_ENDPOINTS = {
  "302": "https://api.302.ai/typesafeai/v1/systemone",
  official: "https://api.typesafe.ai/v1/systemone",
} as const;
type JevProvider = keyof typeof JEV_ENDPOINTS | "custom";
type SavedJevDraft = {
  endpoint: string;
  model: string;
  timeout: number;
  failurePolicy: ModelConfig["jev_failure_policy"];
  strategy: ModelConfig["search_strategy"];
};

function providerForEndpoint(endpoint: string): JevProvider {
  if (!endpoint.trim() || endpoint.trim() === JEV_ENDPOINTS["302"]) return "302";
  if (endpoint.trim() === JEV_ENDPOINTS.official) return "official";
  return "custom";
}

export function KnowledgeConfigForm() {
  const t = useTranslations("KnowledgeConfig");
  const strategies = useTranslations("SearchStrategies");
  const { refreshCapabilities, capabilities } = useApp();
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
  const [jevProvider, setJevProvider] = React.useState<JevProvider>("302");
  const [jevEndpoint, setJevEndpoint] = React.useState<string>(JEV_ENDPOINTS["302"]);
  const [savedJevEndpoint, setSavedJevEndpoint] = React.useState("");
  const [jevModel, setJevModel] = React.useState(JEV_DEFAULT_MODEL);
  const [jevKey, setJevKey] = React.useState("");
  const [jevKeySet, setJevKeySet] = React.useState(false);
  const [jevTimeout, setJevTimeout] = React.useState(30);
  const [jevFailurePolicy, setJevFailurePolicy] =
    React.useState<ModelConfig["jev_failure_policy"]>("error");
  const [savedJevDraft, setSavedJevDraft] = React.useState<SavedJevDraft | null>(null);
  const [testingJev, setTestingJev] = React.useState(false);
  const [jevTestResult, setJevTestResult] = React.useState<{ ok: boolean; message: string } | null>(null);
  let validEndpoint = false;
  try {
    const url = new URL(jevEndpoint.trim());
    validEndpoint = ["http:", "https:"].includes(url.protocol) && !url.username && !url.password;
  } catch { /* An empty or incomplete draft cannot enable JEV. */ }
  // A saved key only applies to the endpoint it was saved for.
  const savedKeyApplies = jevKeySet && jevEndpoint.trim() === savedJevEndpoint;
  // JEV is a paid service: a key saved for this exact endpoint or typed into the draft is required.
  const jevKeyReady = savedKeyApplies || Boolean(jevKey.trim());
  const jevServiceDraftValid = validEndpoint && Boolean(jevModel.trim());
  const jevDraftReady = jevServiceDraftValid && jevKeyReady;
  const disabledStrategies = getSearchStrategiesDisabled(capabilities, strategies("unsupportedBackend"), jevDraftReady);
  const jevStorageUnsupported = disabledStrategies.multi_jev?.reason === "vector_provider_lacks_lexical";
  const sag2StorageUnsupported = disabledStrategies.sag2?.reason === "sag2_relational_unsupported";
  // A JEV mode is serviceable when only its (shared) service configuration stands in the way.
  const jevServiceable = (mode: JevScoredStrategy) => {
    const reason = disabledStrategies[mode]?.reason;
    return reason === undefined || isJevConfigDisabledReason(reason);
  };
  const jevUnsupported = !JEV_SCORED_STRATEGIES.some(jevServiceable);
  // Only hide the service form when storage rules out every JEV mode; PostgreSQL
  // still runs SAG2 even though JEV reranking lacks lexical search there.
  const jevFormHidden = jevStorageUnsupported && !jevServiceable("sag2");
  const savedJevModeBlocked = savedJevDraft !== null && isJevScoredStrategy(savedJevDraft.strategy)
    && Boolean(disabledStrategies[savedJevDraft.strategy]) && !jevServiceable(savedJevDraft.strategy);
  const strategyChanged = savedJevDraft !== null && strategy !== savedJevDraft.strategy;
  const legacyVectorFallback = !strategyChanged && strategy === "multi_es_fast"
    && disabledStrategies.multi_es_fast?.reason === "vector_provider_lacks_lexical";
  const vectorProvider = capabilities?.vector_provider;
  const storageName = vectorProvider ? ({
    lancedb: "LanceDB", es: "Elasticsearch", pgvector: "PostgreSQL (pgvector)", oceanbase: "OceanBase",
  }[vectorProvider] ?? vectorProvider) : null;
  const jevTimeoutValid = Number.isFinite(jevTimeout) && jevTimeout >= 0.1 && jevTimeout <= 120;
  const jevDraftFieldsInvalid = (Boolean(jevEndpoint.trim()) && !jevServiceDraftValid)
    || !jevModel.trim()
    || !jevTimeoutValid;
  const jevModeSelected = isJevScoredStrategy(strategy);
  const jevKeyMissing = jevModeSelected && !jevKeyReady;
  const invalidJevDraft = !jevUnsupported && (jevDraftFieldsInvalid || (jevModeSelected && !jevDraftReady));

  const hydrate = React.useCallback((config: ModelConfig) => {
    const endpoint = config.jev_endpoint?.trim() || JEV_ENDPOINTS["302"];
    const model = config.jev_model?.trim() || JEV_DEFAULT_MODEL;
    const timeout = config.jev_ranking_timeout_seconds ?? 30;
    const failurePolicy = config.jev_failure_policy ?? "error";
    setChunkMaxTokens(config.document_chunk_max_tokens ?? 1_000);
    setChunkMode(config.document_chunk_mode ?? "standard");
    setExtractConcurrency(config.document_extract_concurrency ?? 30);
    setStrategy(config.search_strategy);
    setTopK(config.search_top_k);
    setLanguage(config.sag_language);
    setJevProvider(providerForEndpoint(config.jev_endpoint ?? ""));
    setJevEndpoint(endpoint);
    setSavedJevEndpoint(config.jev_endpoint ?? "");
    setJevModel(model);
    setJevKey("");
    setJevKeySet(config.jev_api_key_set ?? false);
    setJevTimeout(timeout);
    setJevFailurePolicy(failurePolicy);
    // Display defaults are a preview; only edits or explicitly enabling JEV save them.
    setSavedJevDraft({ endpoint, model, timeout, failurePolicy, strategy: config.search_strategy });
    setJevTestResult(null);
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

  function selectJevProvider(provider: JevProvider) {
    setJevProvider(provider);
    setJevTestResult(null);
    if (provider === "custom") return;
    const endpoint = JEV_ENDPOINTS[provider];
    if (endpoint !== jevEndpoint.trim()) setJevKey("");
    setJevEndpoint(endpoint);
    setJevModel(JEV_DEFAULT_MODEL);
  }

  function jevPatch(): ModelConfigPatch {
    return {
      jev_endpoint: jevEndpoint.trim() || null,
      jev_model: jevModel.trim(),
      jev_ranking_timeout_seconds: jevTimeout,
      jev_failure_policy: jevFailurePolicy,
      ...(jevKey.trim() ? { jev_api_key: jevKey.trim() } : {}),
    };
  }

  async function testJev() {
    setTestingJev(true);
    setJevTestResult(null);
    try {
      setJevTestResult(await api.testJevModelConfig(jevPatch()));
    } catch (error) {
      setJevTestResult({ ok: false, message: error instanceof ApiError ? error.message : t("jevTestFailed") });
    } finally {
      setTestingJev(false);
    }
  }

  async function save() {
    setSaving(true);
    try {
      const jevChanged = savedJevDraft !== null && (
        jevEndpoint.trim() !== savedJevDraft.endpoint
        || jevModel.trim() !== savedJevDraft.model
        || jevTimeout !== savedJevDraft.timeout
        || jevFailurePolicy !== savedJevDraft.failurePolicy
        || Boolean(jevKey.trim())
        // Enabling either JEV-scored mode persists the (possibly untouched) service draft with it.
        || (isJevScoredStrategy(strategy) && strategy !== savedJevDraft.strategy)
      );
      const patch: ModelConfigPatch = {
        document_chunk_max_tokens: chunkMaxTokens,
        document_chunk_mode: chunkMode,
        document_extract_concurrency: extractConcurrency,
        ...(strategyChanged ? { search_strategy: strategy } : {}),
        search_top_k: topK,
        sag_language: language,
        ...(!jevUnsupported && jevChanged ? jevPatch() : {}),
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
          ["general", t("generalTitle"), t("generalLoading")],
          ["document-processing", t("parsingTitle"), t("parsingLoading")],
          ["retrieval", t("retrievalTitle"), t("retrievalLoading")],
        ].map(([id, title, description]) => {
          const section = (
            <SettingsSection key={id} title={title} description={description}>
              <div className="grid gap-3 p-4 sm:grid-cols-2 sm:p-5">
                <Skeleton className="h-16 w-full" />
                <Skeleton className="h-16 w-full" />
              </div>
            </SettingsSection>
          );
          return id === "document-processing" ? (
            <div key={id} data-settings-section="document-processing" tabIndex={-1} className="scroll-mt-6 outline-none">
              {section}
            </div>
          ) : section;
        })}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <SettingsSection key="general" title={t("generalTitle")} description={t("generalDescription")}>
        <SettingsRow
          title={t("processingLanguage")}
          description={<span id="kb-language-description">{t("processingLanguageDescription")}</span>}
          layout="inline"
          contentClassName="w-full sm:w-40"
        >
          <Field>
            <FieldLabel htmlFor="kb-language" className="sr-only">{t("processingLanguage")}</FieldLabel>
            <Select
              value={language}
              onValueChange={(value) => setLanguage(value as ModelConfig["sag_language"])}
            >
              <SelectTrigger id="kb-language" aria-describedby="kb-language-description">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="zh">{t("chinese")}</SelectItem>
                <SelectItem value="en">{t("english")}</SelectItem>
              </SelectContent>
            </Select>
          </Field>
        </SettingsRow>
      </SettingsSection>

      <div key="document-processing" data-settings-section="document-processing" tabIndex={-1} className="scroll-mt-6 outline-none">
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
      </div>

      <SettingsSection key="retrieval" title={t("retrievalTitle")} description={t("retrievalDescription")}>
        <SettingsRow title={t("retrievalRules")} description={<>
          {t("retrievalRulesDescription")}
          {storageName && <span className="mt-1 block">{t("retrievalStorage", { provider: storageName })}</span>}
        </>}>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field>
              <FieldLabel htmlFor="kb-search-strategy">{t("retrievalStrategy")}</FieldLabel>
              <Select
                value={strategy}
                onValueChange={(value) => {
                  if (disabledStrategies[value as ModelConfig["search_strategy"]]) {
                    // 保险:后端拒的策略这里不允许赋值,避免 save 时 422 起手
                    return;
                  }
                  setStrategy(value as ModelConfig["search_strategy"]);
                }}
              >
                <SelectTrigger id="kb-search-strategy" aria-describedby={legacyVectorFallback ? "kb-legacy-vector-fallback" : undefined}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <TooltipProvider delayDuration={150}>
                    {SEARCH_STRATEGIES.map(({ value, labelKey }) => {
                      const info = disabledStrategies[value];
                      const item = (
                        <SelectItem key={value} value={value} disabled={Boolean(info)}>
                          <span className="flex items-center gap-1.5">
                            {strategies(labelKey)}
                            {isJevScoredStrategy(value) && (
                              <span className="rounded bg-muted px-1 py-0.5 text-[10px] leading-none text-muted-foreground">
                                {strategies("paid")}
                              </span>
                            )}
                            {info && (
                              <span className="text-[10px] text-muted-foreground">
                                {strategies("disabledSuffix")}
                              </span>
                            )}
                          </span>
                        </SelectItem>
                      );
                      if (!info) return item;
                      return (
                        <Tooltip key={value}>
                          <TooltipTrigger asChild>{item}</TooltipTrigger>
                          <TooltipContent side="right" className="max-w-[220px] text-left">
                            {info.message}
                          </TooltipContent>
                        </Tooltip>
                      );
                    })}
                  </TooltipProvider>
                </SelectContent>
              </Select>
              {legacyVectorFallback && <FieldDescription id="kb-legacy-vector-fallback">{t("legacyVectorFallback")}</FieldDescription>}
            </Field>
            <Field>
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
        <div data-settings-section="jev" tabIndex={-1} className="scroll-mt-6 border-t outline-none">
        <SettingsRow title={t("jevTitle")} description={jevFormHidden
          ? t("jevStorageUnavailable", { provider: storageName ?? vectorProvider ?? "" })
          : <>
            {t("jevDescription")}
            <span className="mt-1 block">{t("jevSharedNotice")}</span>
          </>}>
          {jevFormHidden ? (
            <div className="space-y-2 text-sm leading-6 text-muted-foreground">
              <p>{t(savedJevModeBlocked ? "jevStorageSelectCompatible" : "jevStorageUnchanged")}</p>
              {sag2StorageUnsupported && <p>{t("sag2StorageUnavailable")}</p>}
              <p>{t("jevStorageNoChanges")}</p>
            </div>
          ) : <>
          {(jevStorageUnsupported || sag2StorageUnsupported || savedJevModeBlocked) && (
            <div className="mb-4 space-y-1 text-sm leading-6 text-muted-foreground">
              {jevStorageUnsupported && <p>{t("jevStorageUnavailable", { provider: storageName ?? vectorProvider ?? "" })}</p>}
              {sag2StorageUnsupported && <p>{t("sag2StorageUnavailable")}</p>}
              {savedJevModeBlocked && <p>{t("jevStorageSelectCompatible")}</p>}
            </div>
          )}
          <fieldset disabled={jevUnsupported || saving || testingJev} className="grid min-w-0 gap-4 sm:grid-cols-2">
            <Field className="sm:col-span-2">
              <FieldLabel htmlFor="kb-jev-provider">{t("jevProvider")}</FieldLabel>
              <Select value={jevProvider} onValueChange={(value) => selectJevProvider(value as JevProvider)}>
                <SelectTrigger id="kb-jev-provider"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="302">302.AI</SelectItem>
                  <SelectItem value="official">{t("jevProviderOfficial")}</SelectItem>
                  <SelectItem value="custom">{t("jevProviderCustom")}</SelectItem>
                </SelectContent>
              </Select>
              <FieldDescription>{t("jevProviderDescription")}</FieldDescription>
            </Field>
            <Field className="sm:col-span-2">
              <FieldLabel htmlFor="kb-jev-endpoint">{t("jevEndpoint")}</FieldLabel>
              <Input id="kb-jev-endpoint" type="url" placeholder="https://…" value={jevEndpoint}
                readOnly={jevProvider !== "custom"}
                onChange={(event) => { setJevEndpoint(event.target.value); setJevKey(""); setJevTestResult(null); }} />
              <FieldDescription>{t("jevEndpointDescription")}</FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="kb-jev-model">{t("jevModel")}</FieldLabel>
              <Input id="kb-jev-model" value={jevModel}
                onChange={(event) => { setJevModel(event.target.value); setJevTestResult(null); }} />
              <FieldDescription>{t("jevModelDescription")}</FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="kb-jev-api-key">{t("jevKey")}</FieldLabel>
              <Input id="kb-jev-api-key" type="password" autoComplete="new-password" value={jevKey}
                aria-invalid={jevKeyMissing || undefined}
                placeholder={savedKeyApplies ? t("jevKeyConfigured")
                  : jevProvider === "custom" ? t("jevKeyRequiredPlaceholder")
                    : t("jevKeyProviderPlaceholder", { provider: jevProvider === "302" ? "302.AI" : "TypeSafe" })}
                onChange={(event) => { setJevKey(event.target.value); setJevTestResult(null); }} />
              {jevProvider !== "custom" && <FieldDescription>{t("jevKeyProviderDescription", { provider: jevProvider === "302" ? "302.AI" : "TypeSafe" })}</FieldDescription>}
              <FieldDescription>{t("jevKeyDescription")}</FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="kb-jev-timeout">{t("jevTimeout")}</FieldLabel>
              <Input id="kb-jev-timeout" type="number" min={0.1} max={120} step={0.1} value={jevTimeout}
                onChange={(event) => { setJevTimeout(Number(event.target.value)); setJevTestResult(null); }} />
              <FieldDescription>{t("jevTimeoutDescription")}</FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="kb-jev-failure-policy">{t("jevFailurePolicy")}</FieldLabel>
              <Select value={jevFailurePolicy} onValueChange={(value) => {
                setJevFailurePolicy(value as ModelConfig["jev_failure_policy"]); setJevTestResult(null);
              }}>
                <SelectTrigger id="kb-jev-failure-policy"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="error">{t("jevError")}</SelectItem>
                  <SelectItem value="fallback_to_score">{t("jevFallback")}</SelectItem>
                </SelectContent>
              </Select>
            </Field>
          </fieldset>
          {jevUnsupported && <p className="text-sm text-muted-foreground">{strategies("unsupportedBackend")}</p>}
          {invalidJevDraft && <p role="alert" className="text-sm text-destructive">{t(jevDraftFieldsInvalid ? "jevInvalidDraft" : "jevKeyRequired")}</p>}
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <Button type="button" variant="outline" size="sm" onClick={() => void testJev()}
              disabled={jevUnsupported || !jevDraftReady || jevDraftFieldsInvalid || saving || testingJev}>
              {testingJev ? <Spinner /> : <FlaskConical />}
              {testingJev ? t("jevTesting") : t("jevTest")}
            </Button>
            {jevTestResult && <p role={jevTestResult.ok ? "status" : "alert"}
              className={jevTestResult.ok ? "text-sm text-muted-foreground" : "text-sm text-destructive"}>{jevTestResult.message}</p>}
          </div>
          </>}
        </SettingsRow>
        </div>
      </SettingsSection>

      <div className="flex justify-end border-t pt-4">
        <Button type="button" onClick={save} disabled={saving || testingJev || invalidJevDraft || (strategyChanged && Boolean(disabledStrategies[strategy]))}>
          {saving ? <Spinner /> : <Save />}
          {saving ? t("saving") : t("save")}
        </Button>
      </div>
    </div>
  );
}
