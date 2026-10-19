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
import { getSearchStrategiesDisabled, SEARCH_STRATEGIES } from "@/lib/retrieval-config";
import type { ModelConfig, ModelConfigPatch } from "@/lib/types";

const JEV_ENDPOINTS = {
  "302": "https://api.302.ai/typesafeai/v1/systemone",
  official: "https://api.typesafe.ai/v1/systemone",
} as const;
type JevProvider = keyof typeof JEV_ENDPOINTS | "custom";
type JevDraft = { endpoint: string; model: string; timeout: number; failurePolicy: "error" | "fallback_to_score" };
function providerForEndpoint(endpoint: string): JevProvider {
  if (!endpoint || endpoint === JEV_ENDPOINTS["302"]) return "302";
  return endpoint === JEV_ENDPOINTS.official ? "official" : "custom";
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
  const [strategy, setStrategy] = React.useState<ModelConfig["search_strategy"]>("vector");
  const [topK, setTopK] = React.useState(8);
  const [language, setLanguage] = React.useState<ModelConfig["sag_language"]>("zh");
  const [jevProvider, setJevProvider] = React.useState<JevProvider>("302");
  const [jevEndpoint, setJevEndpoint] = React.useState<string>(JEV_ENDPOINTS["302"]);
  const [jevModel, setJevModel] = React.useState("jev-latest");
  const [jevKey, setJevKey] = React.useState("");
  const [jevTimeout, setJevTimeout] = React.useState(30);
  const [jevFailurePolicy, setJevFailurePolicy] = React.useState<JevDraft["failurePolicy"]>("error");
  const [savedJev, setSavedJev] = React.useState<JevDraft | null>(null);
  const [savedEndpoint, setSavedEndpoint] = React.useState("");
  const [savedKeySet, setSavedKeySet] = React.useState(false);
  const [savedStrategy, setSavedStrategy] = React.useState<ModelConfig["search_strategy"]>("vector");
  const [testingJev, setTestingJev] = React.useState(false);
  const [jevTestResult, setJevTestResult] = React.useState<{ ok: boolean; message: string } | null>(null);
  let validEndpoint = false;
  try {
    const url = new URL(jevEndpoint.trim());
    validEndpoint = ["http:", "https:"].includes(url.protocol) && !url.username && !url.password && !url.hash && jevEndpoint.trim().length <= 1000;
  } catch { /* An incomplete draft cannot enable or test JEV. */ }
  const hasJevKey = Boolean(jevKey.trim()) || (savedKeySet && jevEndpoint.trim() === savedEndpoint);
  const validJevOptions = Boolean(jevModel.trim()) && jevModel.trim().length <= 200
    && Number.isFinite(jevTimeout) && jevTimeout >= 0.1 && jevTimeout <= 120 && jevKey.trim().length <= 8192;
  const jevReady = validEndpoint && hasJevKey && validJevOptions;
  const disabledStrategies = getSearchStrategiesDisabled(capabilities, strategies("unsupportedBackend"), jevReady);
  const jevUnsupported = disabledStrategies.multi_jev?.reason === "unsupported_backend";
  const jevChanged = savedJev !== null && (jevEndpoint.trim() !== savedJev.endpoint || jevModel.trim() !== savedJev.model
    || jevTimeout !== savedJev.timeout || jevFailurePolicy !== savedJev.failurePolicy || Boolean(jevKey.trim()));
  const enablingJev = strategy === "multi_jev" && strategy !== savedStrategy;
  const invalidJevDraft = !jevUnsupported && ((jevChanged && (!validJevOptions || (Boolean(jevEndpoint.trim()) && !validEndpoint)))
    || (strategy === "multi_jev" && !jevReady));

  const hydrate = React.useCallback((config: ModelConfig) => {
    const endpoint = config.jev_endpoint?.trim() || JEV_ENDPOINTS["302"];
    const model = config.jev_model ?? "jev-latest";
    const timeout = config.jev_ranking_timeout_seconds ?? 30;
    const failurePolicy = config.jev_failure_policy ?? "error";
    setChunkMaxTokens(config.document_chunk_max_tokens ?? 1_000);
    setChunkMode(config.document_chunk_mode ?? "standard");
    setExtractConcurrency(config.document_extract_concurrency ?? 30);
    setStrategy(config.search_strategy);
    setTopK(config.search_top_k);
    setLanguage(config.sag_language);
    setJevProvider(providerForEndpoint(config.jev_endpoint?.trim() ?? ""));
    setJevEndpoint(endpoint); setJevModel(model); setJevTimeout(timeout); setJevFailurePolicy(failurePolicy);
    setJevKey(""); setSavedKeySet(config.jev_api_key_set ?? false);
    setSavedEndpoint(config.jev_endpoint?.trim() ?? ""); setSavedStrategy(config.search_strategy);
    setSavedJev({ endpoint, model, timeout, failurePolicy });
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

  function jevPatch(): ModelConfigPatch {
    return { jev_endpoint: jevEndpoint.trim() || null, jev_model: jevModel.trim(),
      jev_ranking_timeout_seconds: jevTimeout, jev_failure_policy: jevFailurePolicy,
      ...(jevKey.trim() ? { jev_api_key: jevKey.trim() } : {}) };
  }

  function selectJevProvider(provider: JevProvider) {
    setJevProvider(provider); setJevTestResult(null);
    if (provider !== "custom") {
      if (jevEndpoint.trim() !== JEV_ENDPOINTS[provider]) setJevKey("");
      setJevEndpoint(JEV_ENDPOINTS[provider]); setJevModel("jev-latest");
    }
  }

  async function testJev() {
    setTestingJev(true); setJevTestResult(null);
    try { setJevTestResult(await api.testJevModelConfig(jevPatch())); }
    catch (error) { setJevTestResult({ ok: false, message: error instanceof ApiError ? error.message : t("jevTestFailed") }); }
    finally { setTestingJev(false); }
  }

  async function save() {
    setSaving(true);
    try {
      const patch: ModelConfigPatch = {
        document_chunk_max_tokens: chunkMaxTokens,
        document_chunk_mode: chunkMode,
        document_extract_concurrency: extractConcurrency,
        ...(strategy !== savedStrategy ? { search_strategy: strategy } : {}),
        search_top_k: topK,
        sag_language: language,
        ...(!jevUnsupported && (jevChanged || enablingJev) ? jevPatch() : {}),
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
                    <SelectItem key={value} value={value} disabled={Boolean(disabledStrategies[value])}>
                      {strategies(labelKey)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {disabledStrategies[strategy] && <FieldDescription>{disabledStrategies[strategy]?.message}</FieldDescription>}
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
        <SettingsRow title={t("jevTitle")} description={t("jevDescription")}>
          <fieldset disabled={jevUnsupported || saving || testingJev} className="grid min-w-0 gap-4 sm:grid-cols-2">
            <Field className="sm:col-span-2">
              <FieldLabel htmlFor="kb-jev-provider">{t("jevProvider")}</FieldLabel>
              <Select value={jevProvider} onValueChange={value => selectJevProvider(value as JevProvider)}>
                <SelectTrigger id="kb-jev-provider"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="302">302.AI</SelectItem><SelectItem value="official">{t("jevProviderOfficial")}</SelectItem><SelectItem value="custom">{t("jevProviderCustom")}</SelectItem></SelectContent>
              </Select>
              <FieldDescription>{t("jevProviderDescription")}</FieldDescription>
            </Field>
            <Field className="sm:col-span-2 min-w-0">
              <FieldLabel htmlFor="kb-jev-endpoint">{t("jevEndpoint")}</FieldLabel>
              <Input id="kb-jev-endpoint" className="min-w-0" type="url" value={jevEndpoint} readOnly={jevProvider !== "custom"}
                onChange={event => { setJevEndpoint(event.target.value); setJevKey(""); setJevTestResult(null); }} />
              <FieldDescription>{t("jevEndpointDescription")}</FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="kb-jev-model">{t("jevModel")}</FieldLabel>
              <Input id="kb-jev-model" value={jevModel} onChange={event => { setJevModel(event.target.value); setJevTestResult(null); }} />
              <FieldDescription>{t("jevModelDescription")}</FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="kb-jev-api-key">{t("jevKey")}</FieldLabel>
              <Input id="kb-jev-api-key" type="password" autoComplete="new-password" value={jevKey}
                placeholder={savedKeySet && jevEndpoint.trim() === savedEndpoint ? t("jevKeyConfigured") : t("jevKeyPlaceholder")}
                onChange={event => { setJevKey(event.target.value); setJevTestResult(null); }} />
              <FieldDescription>{t("jevKeyDescription")}</FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="kb-jev-timeout">{t("jevTimeout")}</FieldLabel>
              <Input id="kb-jev-timeout" type="number" min={0.1} max={120} step={0.1} value={jevTimeout}
                onChange={event => { setJevTimeout(Number(event.target.value)); setJevTestResult(null); }} />
              <FieldDescription>{t("jevTimeoutDescription")}</FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="kb-jev-failure-policy">{t("jevFailurePolicy")}</FieldLabel>
              <Select value={jevFailurePolicy} onValueChange={value => { setJevFailurePolicy(value as JevDraft["failurePolicy"]); setJevTestResult(null); }}>
                <SelectTrigger id="kb-jev-failure-policy"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="error">{t("jevError")}</SelectItem><SelectItem value="fallback_to_score">{t("jevFallback")}</SelectItem></SelectContent>
              </Select>
            </Field>
          </fieldset>
          {jevUnsupported && <p className="mt-3 text-sm text-muted-foreground">{strategies("unsupportedBackend")}</p>}
          {invalidJevDraft && <p role="alert" className="mt-3 text-sm text-destructive">{t("jevInvalidDraft")}</p>}
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <Button type="button" variant="outline" size="sm" onClick={() => void testJev()} disabled={jevUnsupported || !jevReady || saving || testingJev}>
              {testingJev ? <Spinner /> : <FlaskConical />}{testingJev ? t("jevTesting") : t("jevTest")}
            </Button>
            {jevTestResult && <p role={jevTestResult.ok ? "status" : "alert"} className={jevTestResult.ok ? "text-sm text-muted-foreground" : "text-sm text-destructive"}>{jevTestResult.message}</p>}
          </div>
        </SettingsRow>
      </SettingsSection>

      <div className="flex justify-end border-t pt-4">
        <Button type="button" onClick={save} disabled={saving || testingJev || invalidJevDraft || (strategy !== savedStrategy && Boolean(disabledStrategies[strategy]))}>
          {saving ? <Spinner /> : <Save />}
          {saving ? t("saving") : t("save")}
        </Button>
      </div>
    </div>
  );
}
