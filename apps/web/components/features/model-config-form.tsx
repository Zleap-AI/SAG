"use client";

import * as React from "react";
import { Check, Plug, RotateCw, Save, Sparkles, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { OpenAIAPIFormatField, ResponsesBaseURLField, ResponsesConnectionFields } from "./responses-connection-fields";
import { ChatbotConfigSections } from "./chatbot-config-sections";
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
import { isLlmConfigLocked } from "@/lib/model-config-lock";
import { mineruProviderBaseUrl } from "@/lib/mineru-config";
import { getDiagnosticsStore } from "@/lib/diagnostics";
import type {
  ModelConfig,
  ModelConfigPatch,
  ModelProviderId,
  ModelProviderSpec,
} from "@/lib/types";
import { cn } from "@/lib/utils";

function is302Api(url: string | null) {
  try {
    const host = new URL(url ?? "").hostname;
    return host === "api.302.ai" || host === "api.302ai.cn";
  } catch {
    return false;
  }
}

type BaseTarget = "generation" | "embedding" | "parser";
const belongsTo = (target: BaseTarget, key: string) => target === "generation" ? key.startsWith("llm_")
  : target === "embedding" ? key.startsWith("embedding_") : key === "document_parser" || key.startsWith("mineru_");
const generationLocked = (config: ModelConfig) => isLlmConfigLocked(config) ||
  config.locked_fields.some(field => field.startsWith("llm_responses_"));

function BaseModelConfigForm() {
  const t = useTranslations("ModelConfig");
  const { refreshCapabilities } = useApp();
  const [cfg, setCfg] = React.useState<ModelConfig | null>(null);
  const [providers, setProviders] = React.useState<ModelProviderSpec[]>([]);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [saving, setSaving] = React.useState<Record<BaseTarget, boolean>>({ generation: false, embedding: false, parser: false });
  const pending = React.useRef<Record<BaseTarget, boolean>>({ generation: false, embedding: false, parser: false });
  const [saveResults, setSaveResults] = React.useState<Partial<Record<BaseTarget, { ok: boolean; message: string }>>>({});
  const [testing, setTesting] = React.useState(false);
  const [testResult, setTestResult] = React.useState<{ ok: boolean; message: string } | null>(null);
  const [testingEmbedding, setTestingEmbedding] = React.useState(false);
  const [embeddingTestResult, setEmbeddingTestResult] = React.useState<{ ok: boolean; message: string } | null>(null);

  const [llmProvider, setLlmProvider] = React.useState<ModelProviderId>("openai");
  const [responses, setResponses] = React.useState({
    provider: "openai" as import("@/lib/types").ResponsesProviderId,
    endpoint: "https://api.openai.com/v1/responses", api_version: "", send_temperature: false,
  });
  const [llmBaseUrl, setLlmBaseUrl] = React.useState("");
  const [llmKey, setLlmKey] = React.useState("");
  const [llmModel, setLlmModel] = React.useState("");
  const [temperature, setTemperature] = React.useState(0.3);
  const [maxTokens, setMaxTokens] = React.useState(20_000);
  const [timeoutMs, setTimeoutMs] = React.useState(60_000);
  const [maxRetries, setMaxRetries] = React.useState(2);
  const [ctxWindow, setCtxWindow] = React.useState(128000);
  const [embModel, setEmbModel] = React.useState("");
  const [embBaseUrl, setEmbBaseUrl] = React.useState("");
  const [embKey, setEmbKey] = React.useState("");
  const [embDims, setEmbDims] = React.useState("");
  const [documentParser, setDocumentParser] =
    React.useState<ModelConfig["document_parser"]>("auto");
  const [mineruProvider, setMineruProvider] =
    React.useState<ModelConfig["mineru_provider"]>("302");
  const [mineruBaseUrl, setMineruBaseUrl] = React.useState("");
  const [mineruVersion, setMineruVersion] =
    React.useState<ModelConfig["mineru_version"]>("2.5");
  const [mineruOfficialModel, setMineruOfficialModel] =
    React.useState<ModelConfig["mineru_official_model"]>("vlm");
  const [mineruKey, setMineruKey] = React.useState("");

  const hydrate = React.useCallback((config: ModelConfig) => {
    setCfg(config);
    setLlmProvider(config.llm_provider);
    setResponses({ provider: config.llm_responses_provider ?? "openai",
      endpoint: config.llm_responses_endpoint ?? "https://api.openai.com/v1/responses",
      api_version: config.llm_responses_api_version ?? "",
      send_temperature: config.llm_responses_send_temperature ?? false });
    setLlmBaseUrl(config.llm_base_url ?? "");
    setLlmModel(config.llm_model);
    setTemperature(config.llm_temperature);
    setMaxTokens(config.llm_max_tokens);
    setTimeoutMs(config.llm_timeout_ms ?? 60_000);
    setMaxRetries(config.llm_max_retries ?? 2);
    setCtxWindow(config.llm_context_window ?? 128000);
    setEmbModel(config.embedding_model);
    setEmbBaseUrl(config.embedding_base_url ?? "");
    setEmbDims(config.embedding_dimensions != null ? String(config.embedding_dimensions) : "");
    setDocumentParser(config.document_parser);
    setMineruProvider(config.mineru_provider);
    setMineruBaseUrl(config.mineru_base_url ?? "");
    setMineruVersion(config.mineru_version);
    setMineruOfficialModel(config.mineru_official_model);
    setLlmKey("");
    setEmbKey("");
    setMineruKey("");
  }, []);

  const load = React.useCallback(async () => {
    setLoadError(null);
    try {
      const [config, providerCatalog] = await Promise.all([
        api.getModelConfig(),
        api.getModelProviders(),
      ]);
      if (!providerCatalog.some((provider) => provider.id === config.llm_provider)) {
        throw new Error("Configured model provider is missing from the provider catalog");
      }
      setProviders(providerCatalog);
      hydrate(config);
      getDiagnosticsStore().record("model.load", {
        llm_provider: config.llm_provider,
        llm_base_url: config.llm_base_url,
        llm_model: config.llm_model,
        llm_context_window: config.llm_context_window,
        llm_temperature: config.llm_temperature,
        llm_max_tokens: config.llm_max_tokens,
        llm_timeout_ms: config.llm_timeout_ms,
        llm_max_retries: config.llm_max_retries,
        embedding_model: config.embedding_model,
        embedding_base_url: config.embedding_base_url,
        embedding_dimensions: config.embedding_dimensions,
        document_parser: config.document_parser,
        mineru_provider: config.mineru_provider,
        mineru_base_url: config.mineru_base_url,
        mineru_version: config.mineru_version,
        mineru_official_model: config.mineru_official_model,
      });
    } catch (error) {
      setLoadError(error instanceof ApiError ? error.message : t("loadFailed"));
    }
  }, [hydrate, t]);

  React.useEffect(() => {
    void load();
  }, [load]);

  function currentPatch(): ModelConfigPatch {
    const patch: ModelConfigPatch = {
      llm_provider: llmProvider,
      llm_base_url: llmBaseUrl.trim() || null,
      llm_model: llmModel.trim(),
      llm_temperature: temperature,
      llm_max_tokens: maxTokens,
      llm_timeout_ms: timeoutMs,
      llm_max_retries: maxRetries,
      llm_context_window: ctxWindow,
      embedding_model: embModel.trim(),
      embedding_base_url: embBaseUrl.trim(),
      embedding_dimensions: embDims.trim() ? Number(embDims) : null,
      document_parser: documentParser,
      mineru_provider: mineruProvider,
      mineru_base_url: mineruBaseUrl.trim() || null,
      mineru_version: mineruVersion,
      mineru_official_model: mineruOfficialModel,
    };
    if (llmProvider === "responses") Object.assign(patch, {
      llm_responses_provider: responses.provider, llm_responses_endpoint: responses.endpoint.trim(),
      llm_responses_api_version: responses.api_version.trim(), llm_responses_send_temperature: responses.send_temperature,
    });
    if (llmKey.trim()) patch.llm_api_key = llmKey.trim();
    if (embKey.trim()) patch.embedding_api_key = embKey.trim();
    if (mineruKey.trim()) patch.mineru_api_key = mineruKey.trim();
    return patch;
  }

  function changedPatch(target: BaseTarget): ModelConfigPatch {
    if (!cfg) return {};
    const configured: Record<string, unknown> = { ...cfg,
      llm_base_url: cfg.llm_base_url ?? null,
      llm_timeout_ms: cfg.llm_timeout_ms ?? 60_000,
      llm_max_retries: cfg.llm_max_retries ?? 2,
      llm_context_window: cfg.llm_context_window ?? 128000,
      embedding_base_url: cfg.embedding_base_url ?? "",
      embedding_dimensions: cfg.embedding_dimensions ?? null,
      mineru_base_url: cfg.mineru_base_url ?? null,
    };
    return Object.fromEntries(Object.entries(currentPatch()).filter(([key, value]) =>
      belongsTo(target, key) && !cfg.locked_fields.includes(key) &&
      !(key.startsWith("llm_") && generationLocked(cfg)) && value !== configured[key]));
  }

  function applySaved(config: ModelConfig, target: BaseTarget) {
    // The server returns all cards. Merge only this response's owner so concurrent
    // saves cannot restore stale fields or erase another card's draft.
    setCfg(current => current ? { ...current, ...Object.fromEntries(
      Object.entries(config).filter(([key]) => belongsTo(target, key))),
      locked_fields: config.locked_fields,
    } : config);
    if (target === "generation") {
      setLlmProvider(config.llm_provider);
      setResponses({ provider: config.llm_responses_provider ?? "openai",
        endpoint: config.llm_responses_endpoint ?? "https://api.openai.com/v1/responses",
        api_version: config.llm_responses_api_version ?? "",
        send_temperature: config.llm_responses_send_temperature ?? false });
      setLlmBaseUrl(config.llm_base_url ?? ""); setLlmModel(config.llm_model);
      setTemperature(config.llm_temperature); setMaxTokens(config.llm_max_tokens);
      setTimeoutMs(config.llm_timeout_ms ?? 60000); setMaxRetries(config.llm_max_retries ?? 2);
      setCtxWindow(config.llm_context_window ?? 128000); setLlmKey("");
    } else if (target === "embedding") {
      setEmbModel(config.embedding_model); setEmbBaseUrl(config.embedding_base_url ?? "");
      setEmbDims(config.embedding_dimensions == null ? "" : String(config.embedding_dimensions)); setEmbKey("");
    } else {
      setDocumentParser(config.document_parser); setMineruProvider(config.mineru_provider);
      setMineruBaseUrl(config.mineru_base_url ?? ""); setMineruVersion(config.mineru_version);
      setMineruOfficialModel(config.mineru_official_model); setMineruKey("");
    }
  }

  function validate(target: BaseTarget) {
    let field = "";
    let message = "";
    if (target === "generation" && !llmModel.trim()) { field = "llm-model"; message = t("modelRequired"); }
    if (target === "embedding" && !embModel.trim()) { field = "emb-model"; message = t("modelRequired"); }
    else if (target === "embedding" && embDims.trim() &&
      (!Number.isInteger(Number(embDims)) || Number(embDims) < 1 || Number(embDims) > 8192)) {
      field = "emb-dims"; message = t("dimensionsInvalid");
    }
    if (field) document.getElementById(field)?.focus();
    return message;
  }

  async function save(target: BaseTarget) {
    if (!cfg || pending.current[target] || (target === "generation" && testing) ||
      (target === "embedding" && testingEmbedding)) return;
    const patch = changedPatch(target);
    if (!Object.keys(patch).length) return;
    const validationError = validate(target);
    if (validationError) { setSaveResults(current => ({ ...current, [target]: { ok: false, message: validationError } })); return; }
    pending.current[target] = true;
    setSaving(current => ({ ...current, [target]: true }));
    setSaveResults(current => ({ ...current, [target]: undefined }));
    try {
      const { config } = await api.saveModelConfig(patch);
      applySaved(config, target);
      const message = t("cardSaved", { name: t(`${target}Title`) });
      setSaveResults(current => ({ ...current, [target]: { ok: true, message } }));
      toast.success(message);
      getDiagnosticsStore().record("model.save", { target, fields: Object.keys(patch) });
      window.dispatchEvent(new Event("sag:stock-model-saved"));
      try { await refreshCapabilities(); }
      catch { setSaveResults(current => ({ ...current, [target]: { ok: false, message: t("refreshFailed") } })); }
    } catch (error) {
      setSaveResults(current => ({ ...current, [target]: { ok: false,
        message: t("cardSaveFailed", { name: t(`${target}Title`), detail: error instanceof Error ? error.message : t("saveFailed") }),
      } }));
    } finally {
      pending.current[target] = false;
      setSaving(current => ({ ...current, [target]: false }));
    }
  }

  async function test() {
    if (!cfg || generationLocked(cfg) || pending.current.generation || testing) return;
    const message = validate("generation");
    if (message) { setTestResult({ ok: false, message }); return; }
    setTesting(true);
    setTestResult(null);
    try {
      const draft = Object.fromEntries(
        Object.entries(currentPatch()).filter(([key]) => key.startsWith("llm_")),
      );
      const result = await api.testModelConfig(draft);
      setTestResult(result);
      getDiagnosticsStore().record("model.test", {
        ok: result.ok,
        message: result.message,
      });
    } catch (error) {
      const message = error instanceof ApiError ? error.message : t("testFailed");
      setTestResult({ ok: false, message });
      getDiagnosticsStore().record("model.test", {
        ok: false,
        message,
      });
    } finally {
      setTesting(false);
    }
  }

  async function testEmbedding() {
    if (pending.current.embedding || testingEmbedding) return;
    const message = validate("embedding");
    if (message) { setEmbeddingTestResult({ ok: false, message }); return; }
    setTestingEmbedding(true);
    setEmbeddingTestResult(null);
    try {
      // An unchanged schema dimension must preserve the saved request policy.
      const { embedding_dimensions: dimensions } = changedPatch("embedding");
      const result = await api.testEmbeddingModelConfig({
        embedding_model: embModel.trim(),
        embedding_base_url: embBaseUrl.trim(),
        embedding_api_key: embKey.trim(),
        ...(dimensions !== undefined ? { embedding_dimensions: dimensions } : {}),
      });
      setEmbeddingTestResult(result);
      getDiagnosticsStore().record("model.test", { target: "embedding", ok: result.ok, message: result.message });
    } catch (error) {
      setEmbeddingTestResult({ ok: false, message: error instanceof ApiError ? error.message : t("testFailed") });
    } finally {
      setTestingEmbedding(false);
    }
  }

  function changeProvider(value: string) {
    const next = providers.find((provider) => provider.id === value);
    const current = providers.find((provider) => provider.id === llmProvider);
    if (!next) return;
    const knownUrls = new Set(
      providers.map((provider) => provider.default_base_url).filter(Boolean),
    );
    const knownModels = new Set(providers.map((provider) => provider.default_model));
    const knownContextWindows = new Set(
      providers.map((provider) => provider.default_context_window),
    );
    if (!llmBaseUrl.trim() || knownUrls.has(llmBaseUrl.trim())) {
      setLlmBaseUrl(next.default_base_url ?? "");
    }
    if (!llmModel.trim() || knownModels.has(llmModel.trim())) {
      setLlmModel(next.default_model);
    }
    if (knownContextWindows.has(ctxWindow)) {
      setCtxWindow(next.default_context_window);
    }
    if (
      !next.temperature_configurable ||
      !current ||
      !current.temperature_configurable ||
      temperature === current.default_temperature
    ) {
      setTemperature(next.default_temperature);
    }
    setLlmProvider(next.id);
    setTestResult(null);
  }

  function changeMineruProvider(value: ModelConfig["mineru_provider"]) {
    setMineruBaseUrl(
      mineruProviderBaseUrl(mineruBaseUrl, mineruProvider, value),
    );
    setMineruProvider(value);
    setTestResult(null);
  }

  async function setup302MinerU() {
    if (pending.current.parser) return;
    pending.current.parser = true;
    setSaving(current => ({ ...current, parser: true }));
    try {
      const { config } = await api.setup302MinerU();
      applySaved(config, "parser");
      await refreshCapabilities();
      window.dispatchEvent(new Event("sag:stock-model-saved"));
      toast.success(t("mineruEnabled"));
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : t("mineruFailed"));
    } finally {
      pending.current.parser = false;
      setSaving(current => ({ ...current, parser: false }));
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

  if (!cfg || providers.length === 0) {
    return (
      <div className="flex flex-col gap-6">
        {[
          [t("generationTitle"), t("generationLoading")],
          [t("embeddingTitle"), t("embeddingLoading")],
          [t("parserTitle"), t("parserLoading")],
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

  const providerSpec = providers.find((provider) => provider.id === llmProvider)!;
  const providerFamily = llmProvider === "responses" ? "openai" : llmProvider;
  const llmLocked = generationLocked(cfg);

  const keyPlaceholder = (isSet: boolean) => (isSet ? t("keyConfigured") : "sk-…");
  const responsesIdentityChanged = (llmProvider === "responses" || cfg.llm_provider === "responses") && (
    cfg.llm_provider !== llmProvider || cfg.llm_responses_provider !== responses.provider ||
    cfg.llm_responses_endpoint !== responses.endpoint || cfg.llm_responses_api_version !== responses.api_version
  );
  const generationKeyPlaceholder =
    cfg.llm_api_key_set && cfg.llm_provider === llmProvider && !responsesIdentityChanged
      ? t("keyConfigured")
      : providerSpec.api_key_placeholder;
  const canReuse302Key =
    (cfg.llm_api_key_set && is302Api(cfg.llm_base_url)) ||
    (cfg.embedding_api_key_set && is302Api(cfg.embedding_base_url));
  const embeddingUrl = embBaseUrl.trim();
  const generationUrl = cfg.llm_base_url?.trim() ?? "";
  const savedProviderSpec = providers.find(provider => provider.id === cfg.llm_provider)!;
  let embeddingAddressPlaceholder: string;
  if (!savedProviderSpec.can_reuse_embedding_credentials) {
    embeddingAddressPlaceholder = t("separateEmbeddingAddress");
  } else if (generationUrl) {
    embeddingAddressPlaceholder = t("embeddingAddressPlaceholder");
  } else {
    embeddingAddressPlaceholder = t("embeddingAddressDefaultPlaceholder");
  }
  const inheritsEmbeddingUrl = !embeddingUrl && savedProviderSpec.can_reuse_embedding_credentials;
  const embeddingAddressChanged = embeddingUrl !== (cfg.embedding_base_url ?? "");
  const embeddingAddressStatus = t(
    embeddingAddressChanged ? "embeddingAddressAfterSave" : "embeddingAddressCurrent",
  );
  let embeddingAddressDescription: string;
  if (embeddingUrl) {
    embeddingAddressDescription = t("embeddingAddressIndependent", { status: embeddingAddressStatus, address: embeddingUrl });
  } else if (!inheritsEmbeddingUrl) {
    embeddingAddressDescription = t("embeddingAddressNativeDefault", { status: embeddingAddressStatus });
  } else if (generationUrl) {
    embeddingAddressDescription = t("embeddingAddressInherited", { status: embeddingAddressStatus, address: generationUrl });
  } else {
    embeddingAddressDescription = t("embeddingAddressDefault", { status: embeddingAddressStatus });
  }
  const hasSeparateEmbeddingKey = Boolean(embKey.trim() || cfg.embedding_api_key_set);
  const hasGenerationKey = Boolean(cfg.llm_api_key_set);
  let embeddingKeyDescription: string;
  if (embKey.trim()) {
    embeddingKeyDescription = t("embeddingKeyNew");
  } else if (cfg.embedding_api_key_set) {
    embeddingKeyDescription = t("embeddingKeySaved");
  } else if (savedProviderSpec.can_reuse_embedding_credentials && hasGenerationKey) {
    embeddingKeyDescription = t("embeddingKeyInherited");
  } else {
    embeddingKeyDescription = t("embeddingKeyRequired");
  }
  const showInheritedAddressKeyNote = inheritsEmbeddingUrl && Boolean(generationUrl) && hasSeparateEmbeddingKey;

  const saveFeedback = (target: BaseTarget) => saveResults[target] &&
    <p role={saveResults[target]?.ok ? "status" : "alert"}
      className={cn("break-words text-sm", saveResults[target]?.ok ? "text-success" : "text-destructive")}>
      {saveResults[target]?.message}
    </p>;
  const saveControls = (target: BaseTarget) =>
    <Button type="button" aria-label={t("saveCard", { name: t(`${target}Title`) })}
      disabled={saving[target] || (target === "generation" && (llmLocked || testing)) ||
        (target === "embedding" && testingEmbedding) || !Object.keys(changedPatch(target)).length}
      onClick={() => void save(target)}>
      {saving[target] ? <Spinner /> : <Save />}{saving[target] ? t("saving") : t("saveCard", { name: t(`${target}Title`) })}
    </Button>;

  return (
    <div className="flex flex-col gap-6">
      {llmLocked && (
        <Alert>
          <AlertTitle>{t("deploymentLockTitle")}</AlertTitle>
          <AlertDescription>{t("deploymentLockDescription")}</AlertDescription>
        </Alert>
      )}
      <SettingsSection title={t("generationTitle")} description={t("generationDescription")}
        footer={
          <div className="flex flex-col gap-3">
            <div className="min-w-0 space-y-2">
                <div role="status" className="min-h-5 min-w-0">
                  {testResult && (
                    <span
                      className={cn(
                        "inline-flex items-center gap-1.5 text-sm",
                        testResult.ok ? "text-success" : "text-destructive",
                      )}
                    >
                      {testResult.ok ? <Check className="size-4" /> : <X className="size-4" />}
                      {testResult.message}
                    </span>
                  )}
                </div>
                {saveFeedback("generation")}
              </div>
              <div className="flex flex-wrap justify-end gap-2">
                  <Button type="button" onClick={test} variant="outline" disabled={llmLocked || testing || saving.generation}>
                    {testing ? <Spinner /> : <Plug />}
                    {testing ? t("testing") : t("testGeneration")}
                  </Button>
                  {saveControls("generation")}
                </div>
              </div>
            }
          >
            <fieldset disabled={llmLocked || saving.generation || testing} inert={saving.generation || testing} className="min-w-0">
            <SettingsRow title={t("connectionTitle")} description={t("connectionDescription")}>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field>
                  <FieldLabel htmlFor="llm-provider">{t("provider")}</FieldLabel>
                  <Select value={providerFamily} onValueChange={changeProvider} disabled={llmLocked}>
                    <SelectTrigger id="llm-provider">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {providers.filter(provider => provider.id !== "responses").map((provider) => (
                        <SelectItem key={provider.id} value={provider.id}>
                          {provider.display_name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <FieldDescription>{t(`providerDescription.${providerFamily}`)}</FieldDescription>
                </Field>
                {providerFamily === "openai" && providers.some(provider => provider.id === "responses") &&
                  <OpenAIAPIFormatField id="llm-api-format" value={llmProvider as "openai" | "responses"}
                    disabled={llmLocked} onChange={value => { setLlmProvider(value); setTestResult(null); }} />}
                {llmProvider === "responses" ?
                  <ResponsesBaseURLField id="original-responses" value={responses.endpoint} disabled={llmLocked}
                    onChange={endpoint => setResponses(current => ({ ...current, endpoint }))} /> : <Field>
                  <FieldLabel htmlFor="llm-url">Base URL</FieldLabel>
                  <Input
                    id="llm-url"
                    value={llmBaseUrl}
                    disabled={llmLocked}
                    onChange={(event) => setLlmBaseUrl(event.target.value)}
                    placeholder={providerSpec.default_base_url ?? t("officialEndpoint")}
                  />
                  <FieldDescription>{t(`baseUrlDescription.${llmProvider}`)}</FieldDescription>
                </Field>}
                <Field>
                  <FieldLabel htmlFor="llm-key">API Key</FieldLabel>
                  <Input
                    id="llm-key"
                    type="password"
                    autoComplete="off"
                    value={llmKey}
                    disabled={llmLocked}
                    onChange={(event) => setLlmKey(event.target.value)}
                    placeholder={generationKeyPlaceholder}
                  />
                  <FieldDescription>
                    {(cfg.llm_provider !== llmProvider || responsesIdentityChanged) && cfg.llm_api_key_set
                      ? t(llmProvider === "responses" || cfg.llm_provider === "responses" ? "responsesChangedKeyDescription" : "providerChangedKeyDescription")
                      : t("secretDescription")}
                  </FieldDescription>
                </Field>
              </div>
            </SettingsRow>

            {llmProvider === "responses" && <SettingsRow title={t("responsesTitle")}>
              <ResponsesConnectionFields id="original-responses" value={responses} disabled={llmLocked}
                onChange={setResponses} />
            </SettingsRow>}

            <SettingsRow title={t("generationParams")} description={t("generationParamsDescription")}>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field>
                  <FieldLabel htmlFor="llm-model">{t("model")}</FieldLabel>
                  <Input
                    id="llm-model"
                    value={llmModel}
                    disabled={llmLocked}
                    onChange={(event) => setLlmModel(event.target.value)}
                    placeholder={providerSpec.default_model}
                  />
                </Field>
                <Field>
                  <FieldLabel htmlFor="llm-ctxwin">{t("contextWindow")}</FieldLabel>
                  <Input
                    id="llm-ctxwin"
                    type="number"
                    min={1024}
                    max={2000000}
                    value={ctxWindow}
                    disabled={llmLocked}
                    onChange={(event) =>
                      setCtxWindow(Math.max(1024, Number(event.target.value) || 1024))
                    }
                  />
                  <FieldDescription>{t("contextWindowDescription")}</FieldDescription>
                </Field>
                <Field>
                  <FieldLabel htmlFor="llm-maxtok">{t("maxOutputTokens")}</FieldLabel>
                  <Input
                    id="llm-maxtok"
                    type="number"
                    min={1}
                    max={32768}
                    value={maxTokens}
                    disabled={llmLocked}
                    onChange={(event) =>
                      setMaxTokens(Math.max(1, Number(event.target.value) || 1))
                    }
                  />
                </Field>
                <Field>
                  <FieldLabel>
                    {t("temperature", {
                      value: (
                        providerSpec.temperature_configurable
                          ? temperature
                          : providerSpec.default_temperature
                      ).toFixed(1),
                    })}
                  </FieldLabel>
                  <div className="flex h-9 items-center">
                    <Slider
                      value={[
                        providerSpec.temperature_configurable
                          ? temperature
                          : providerSpec.default_temperature,
                      ]}
                      min={0}
                      max={2}
                      step={0.1}
                      disabled={llmLocked || !providerSpec.temperature_configurable}
                      onValueChange={([value]) => setTemperature(value)}
                    />
                  </div>
                  <FieldDescription>
                    {t(
                      !providerSpec.temperature_configurable
                        ? "fixedTemperatureDescription"
                        : "temperatureDescription",
                    )}
                  </FieldDescription>
                </Field>
                <Field>
                  <FieldLabel htmlFor="llm-timeout">{t("timeout")}</FieldLabel>
                  <Input
                    id="llm-timeout"
                    type="number"
                    min={1000}
                    max={600000}
                    step={1000}
                    value={timeoutMs}
                    disabled={llmLocked}
                    onChange={(event) =>
                      setTimeoutMs(
                        Math.min(600000, Math.max(1000, Number(event.target.value) || 1000)),
                      )
                    }
                  />
                  <FieldDescription>{t("timeoutDescription")}</FieldDescription>
                </Field>
                <Field>
                  <FieldLabel htmlFor="llm-retries">{t("retries")}</FieldLabel>
                  <Input
                    id="llm-retries"
                    type="number"
                    min={0}
                    max={10}
                    step={1}
                    value={maxRetries}
                    disabled={llmLocked}
                    onChange={(event) =>
                      setMaxRetries(Math.min(10, Math.max(0, Number(event.target.value) || 0)))
                    }
                  />
                  <FieldDescription>{t("retriesDescription")}</FieldDescription>
                </Field>
              </div>
            </SettingsRow>
            </fieldset>
          </SettingsSection>

          <SettingsSection title={t("embeddingTitle")} description={t("embeddingDescription")}
            footer={<div className="flex flex-col gap-3">
              <div className="min-w-0 space-y-2">
              <div role="status" className="min-h-5 min-w-0">
                {embeddingTestResult && <span className={cn("inline-flex items-center gap-1.5 text-sm",
                  embeddingTestResult.ok ? "text-success" : "text-destructive")}>
                  {embeddingTestResult.ok ? <Check className="size-4" /> : <X className="size-4" />}
                  {embeddingTestResult.message}
                </span>}
              </div>
              {saveFeedback("embedding")}
            </div>
            <div className="flex flex-wrap justify-end gap-2">
            <Button type="button" onClick={testEmbedding} variant="outline" disabled={testingEmbedding || saving.embedding}>
              {testingEmbedding ? <Spinner /> : <Plug />}
              {testingEmbedding ? t("testing") : t("testEmbedding")}
            </Button>
            {saveControls("embedding")}
          </div>
        </div>}
      >
        <fieldset disabled={saving.embedding || testingEmbedding} inert={saving.embedding || testingEmbedding} className="min-w-0">
        <SettingsRow
          title={t("modelAndConnection")}
          description={t(
            savedProviderSpec.can_reuse_embedding_credentials
              ? "embeddingConnectionDescription"
              : "embeddingNativeConnectionDescription",
          )}
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <Field>
              <FieldLabel htmlFor="emb-model">{t("model")}</FieldLabel>
              <Input
                id="emb-model"
                disabled={cfg.locked_fields.includes("embedding_model")}
                value={embModel}
                onChange={(event) => setEmbModel(event.target.value)}
                placeholder="Qwen/Qwen3-Embedding-4B"
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="emb-dims">{t("dimensions")}</FieldLabel>
              <Input
                id="emb-dims"
                disabled={cfg.locked_fields.includes("embedding_dimensions")}
                type="number"
                min={1}
                max={8192}
                value={embDims}
                onChange={(event) => setEmbDims(event.target.value)}
                placeholder={t("modelDefault")}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="emb-url">{t("optionalBaseUrl")}</FieldLabel>
              <Input
                id="emb-url"
                disabled={cfg.locked_fields.includes("embedding_base_url")}
                value={embBaseUrl}
                onChange={(event) => setEmbBaseUrl(event.target.value)}
                placeholder={embeddingAddressPlaceholder}
                aria-describedby={showInheritedAddressKeyNote
                  ? "emb-url-description emb-url-key-note" : "emb-url-description"}
              />
              <FieldDescription id="emb-url-description" className="break-words [overflow-wrap:anywhere]">
                {embeddingAddressDescription}
              </FieldDescription>
              {showInheritedAddressKeyNote && (
                <FieldDescription id="emb-url-key-note">
                  {t("embeddingInheritedAddressIndependentKey")}
                </FieldDescription>
              )}
            </Field>
            <Field>
              <FieldLabel htmlFor="emb-key">{t("optionalApiKey")}</FieldLabel>
              <Input
                id="emb-key"
                disabled={cfg.locked_fields.includes("embedding_api_key")}
                type="password"
                autoComplete="off"
                value={embKey}
                onChange={(event) => setEmbKey(event.target.value)}
                aria-describedby="emb-key-description"
                placeholder={
                  cfg.embedding_api_key_set
                    ? t("keyConfigured")
                    : savedProviderSpec.can_reuse_embedding_credentials
                      ? t("reuseGeneration")
                      : t("separateEmbeddingKey")
                }
              />
              <FieldDescription id="emb-key-description">
                {embeddingKeyDescription}
              </FieldDescription>
            </Field>
          </div>
        </SettingsRow>
        </fieldset>
      </SettingsSection>

      <SettingsSection
        title={t("parserTitle")}
        description={t("parserDescription")}
        footer={<div className="flex flex-col gap-3">
          {saveFeedback("parser")}
          <div className="flex flex-wrap justify-end gap-2">{saveControls("parser")}</div>
        </div>}
      >
        <fieldset disabled={saving.parser} inert={saving.parser} className="min-w-0">
        <SettingsRow title={t("parserEngine")} description={t("parserEngineDescription")}>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field>
              <FieldLabel htmlFor="document-parser">{t("parserMethod")}</FieldLabel>
              <Select
                disabled={cfg.locked_fields.includes("document_parser")}
                value={documentParser}
                onValueChange={(value) =>
                  setDocumentParser(value as ModelConfig["document_parser"])
                }
              >
                <SelectTrigger id="document-parser">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="auto">{t("autoRecommended")}</SelectItem>
                  <SelectItem value="markitdown">MarkItDown</SelectItem>
                  <SelectItem value="mineru">MinerU</SelectItem>
                  <SelectItem value="anydoc">AnyDoc</SelectItem>
                </SelectContent>
              </Select>
              <FieldDescription>
                {documentParser === "auto"
                  ? t("autoDescription")
                  : documentParser === "markitdown"
                    ? t("markitdownDescription")
                    : documentParser === "anydoc"
                      ? t("anydocDescription")
                      : t("mineruDescription")}
              </FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="mineru-provider">{t("mineruProvider")}</FieldLabel>
              <Select
                disabled={cfg.locked_fields.includes("mineru_provider")}
                value={mineruProvider}
                onValueChange={(value) =>
                  changeMineruProvider(value as ModelConfig["mineru_provider"])
                }
              >
                <SelectTrigger id="mineru-provider">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="302">302.AI</SelectItem>
                  <SelectItem value="official">{t("mineruProviderOfficial")}</SelectItem>
                </SelectContent>
              </Select>
            </Field>
            <Field>
              <FieldLabel htmlFor="mineru-model">
                {mineruProvider === "302" ? t("mineruVersion") : t("mineruModel")}
              </FieldLabel>
              <Select
                disabled={cfg.locked_fields.includes(mineruProvider === "302" ? "mineru_version" : "mineru_official_model")}
                value={
                  mineruProvider === "302" ? mineruVersion : mineruOfficialModel
                }
                onValueChange={(value) => {
                  if (mineruProvider === "302") {
                    setMineruVersion(value as ModelConfig["mineru_version"]);
                  } else {
                    setMineruOfficialModel(
                      value as ModelConfig["mineru_official_model"],
                    );
                  }
                }}
              >
                <SelectTrigger id="mineru-model">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {mineruProvider === "302" ? (
                    <>
                      <SelectItem value="2.5">2.5</SelectItem>
                      <SelectItem value="2.0">2.0</SelectItem>
                    </>
                  ) : (
                    <>
                      <SelectItem value="vlm">VLM</SelectItem>
                      <SelectItem value="pipeline">Pipeline</SelectItem>
                    </>
                  )}
                </SelectContent>
              </Select>
            </Field>
            <Field>
              <FieldLabel htmlFor="mineru-url">MinerU Base URL</FieldLabel>
              <Input
                id="mineru-url"
                disabled={cfg.locked_fields.includes("mineru_base_url")}
                value={mineruBaseUrl}
                onChange={(event) => setMineruBaseUrl(event.target.value)}
                placeholder={
                  mineruProvider === "302"
                    ? "https://api.302ai.cn"
                    : "https://mineru.net/api/v4"
                }
              />
              <FieldDescription>
                {mineruProvider === "302"
                  ? t("mineru302Pricing")
                  : t("mineruOfficialPricing")}
              </FieldDescription>
              {mineruProvider === "302" && canReuse302Key && !cfg.mineru_api_key_set && (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={saving.parser}
                  onClick={() => void setup302MinerU()}
                  className="w-fit"
                >
                  <Sparkles />
                  {t("reuse302Key")}
                </Button>
              )}
            </Field>
            <Field>
              <FieldLabel htmlFor="mineru-key">MinerU API Key</FieldLabel>
              <Input
                id="mineru-key"
                disabled={cfg.locked_fields.includes("mineru_api_key")}
                type="password"
                autoComplete="off"
                value={mineruKey}
                onChange={(event) => setMineruKey(event.target.value)}
                placeholder={keyPlaceholder(cfg.mineru_api_key_set)}
              />
              <FieldDescription>{t("secretDescription")}</FieldDescription>
            </Field>
          </div>
        </SettingsRow>
        </fieldset>
      </SettingsSection>

    </div>
  );
}

export function ModelConfigForm() {
  return <div className="flex flex-col gap-8">
    <BaseModelConfigForm />
    <ChatbotConfigSections />
  </div>;
}
