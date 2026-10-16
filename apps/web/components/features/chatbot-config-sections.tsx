"use client";

import * as React from "react";
import { Check, Plug, Save, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { SettingsRow, SettingsSection } from "@/components/features/settings-section";
import { Button } from "@/components/ui/button";
import { Field, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { useApp } from "@/components/features/app-shell";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { ChatbotConfig as Config, ChatbotConnection as Connection, ModelProviderId, ModelProviderSpec } from "@/lib/types";
import { OpenAIAPIFormatField, ResponsesBaseURLField, ResponsesConnectionFields } from "./responses-connection-fields";

type Target = "llm" | "embedding";

export function ChatbotConfigSections() {
  const { refreshCapabilities } = useApp();
  const c = useTranslations("ChatbotConfig");
  const t = useTranslations("ModelConfig");
  const [providers, setProviders] = React.useState<ModelProviderSpec[]>([]);
  const [config, setConfig] = React.useState<Config | null>(null);
  const [keys, setKeys] = React.useState<Record<Target, string>>({ llm: "", embedding: "" });
  const [saving, setSaving] = React.useState<Record<Target, boolean>>({ llm: false, embedding: false });
  const pending = React.useRef<Record<Target, boolean>>({ llm: false, embedding: false });
  const [saveResults, setSaveResults] = React.useState<Partial<Record<Target, { ok: boolean; message: string }>>>({});
  const [testing, setTesting] = React.useState<Record<Target, boolean>>({ llm: false, embedding: false });
  const [error, setError] = React.useState("");
  const [results, setResults] = React.useState<Partial<Record<Target, { ok: boolean; message: string }>>>({});
  const saved = React.useRef<Config | null>(null);
  const loadVersion = React.useRef(0);
  const load = React.useCallback(async (retainEdits = false) => {
    const version = ++loadVersion.current;
    try {
      const [result, catalog] = await Promise.all([
        api.getChatbotConfig(),
        retainEdits && saved.current ? Promise.resolve(null) : api.getModelProviders(),
      ]);
      if (version !== loadVersion.current) return;
      const loaded = result.config;
      if (catalog) {
        if (!catalog.some(provider => provider.id === loaded.llm.provider)) throw new Error(c("loadFailed"));
        setProviders(catalog);
      }
      if (!retainEdits || !saved.current) saved.current = loaded;
      else saved.current = { ...saved.current, locked: loaded.locked, encryption_configured: loaded.encryption_configured,
        embedding: { ...saved.current.embedding, model: loaded.embedding.model,
          schema_dimensions: loaded.embedding.schema_dimensions, request_dimensions: loaded.embedding.request_dimensions } };
      setConfig(current => retainEdits && current ? {
        ...current, locked: loaded.locked, encryption_configured: loaded.encryption_configured,
        embedding: { ...current.embedding, model: loaded.embedding.model,
          schema_dimensions: loaded.embedding.schema_dimensions, request_dimensions: loaded.embedding.request_dimensions },
      } : loaded);
      setError("");
    }
    catch (e) {
      if (version === loadVersion.current) setError(e instanceof Error ? e.message : c("loadFailed"));
    }
  }, [c]);
  React.useEffect(() => { void load(); }, [load]);
  // Refresh shared embedding identity/inherited tuning after saving the stock form.
  React.useEffect(() => {
    const refresh = () => { void load(true); };
    window.addEventListener("sag:stock-model-saved", refresh);
    return () => window.removeEventListener("sag:stock-model-saved", refresh);
  }, [load]);

  const update = (target: Target, field: string, value: string | boolean) => {
    setConfig(current => current ? { ...current, [target]: { ...current[target], [field]: value,
    } } : current);
    setResults(current => ({ ...current, [target]: undefined }));
  };
  const draft = (target: Target, connection: Connection) => {
    const fields = target === "llm"
      ? ["enabled", "provider", "base_url", "model"]
      : ["enabled", "base_url"];
    if (target === "llm" && connection.provider === "responses") fields.push(
      "responses_provider", "responses_endpoint", "responses_api_version", "responses_send_temperature");
    return Object.fromEntries(fields.map(field => [field, connection[field as keyof Connection]]));
  };
  const changes = (only?: Target) => !config || !saved.current || config.locked ? {} : Object.fromEntries((["llm", "embedding"] as const).flatMap(target => {
      if (only && target !== only) return [];
      const values = draft(target, config[target]);
      return keys[target] || JSON.stringify(values) !== JSON.stringify(draft(target, saved.current![target]))
        ? [[target, { ...values, api_key: keys[target] }]] : [];
    }));
  const validate = (target: Target) => {
    if (target === "llm" && config?.llm.enabled && !config.llm.model.trim()) {
      document.getElementById("chatbot-llm-model")?.focus();
      return t("modelRequired");
    }
    return "";
  };
  async function save(target: Target) {
    if (!config || !saved.current || config.locked || pending.current[target] || testing[target]) return;
    const patch = changes(target);
    if (!Object.keys(patch).length) return;
    const validationError = validate(target);
    if (validationError) { setSaveResults(current => ({ ...current, [target]: { ok: false, message: validationError } })); return; }
    pending.current[target] = true;
    setSaving(current => ({ ...current, [target]: true }));
    setSaveResults(current => ({ ...current, [target]: undefined }));
    try {
      const result = await api.saveChatbotConfig(patch);
      // A full response may predate a sibling save or shared vector refresh.
      const connection = target === "embedding" ? { ...result.config.embedding,
        model: saved.current.embedding.model, schema_dimensions: saved.current.embedding.schema_dimensions,
        request_dimensions: saved.current.embedding.request_dimensions } : result.config.llm;
      saved.current = { ...saved.current, [target]: connection };
      setConfig(current => current ? { ...current, [target]: connection } : current);
      setKeys(current => ({ ...current, [target]: "" }));
      const message = t("cardSaved", { name: c(target === "llm" ? "llmTitle" : "embeddingTitle") });
      setSaveResults(current => ({ ...current, [target]: { ok: true, message } }));
      toast.success(message);
      try { await refreshCapabilities(); }
      catch { setSaveResults(current => ({ ...current, [target]: { ok: false, message: t("refreshFailed") } })); }
    } catch (error) {
      setSaveResults(current => ({ ...current, [target]: { ok: false,
        message: t("cardSaveFailed", { name: c(target === "llm" ? "llmTitle" : "embeddingTitle"),
          detail: error instanceof Error ? error.message : t("saveFailed") }),
      } }));
    } finally {
      pending.current[target] = false;
      setSaving(current => ({ ...current, [target]: false }));
    }
  }
  const test = async (target: Target) => {
    if (!config || config.locked || saving[target] || testing[target]) return;
    const message = validate(target);
    if (message) { setResults(current => ({ ...current, [target]: { ok: false, message } })); return; }
    setTesting(current => ({ ...current, [target]: true }));
    setResults(current => ({ ...current, [target]: undefined }));
    try {
      const result = await api.testChatbotConfig({
        [target]: { ...draft(target, config[target]), api_key: keys[target] }, target,
      });
      setResults(current => ({ ...current, [target]: result }));
    } catch (e) {
      setResults(current => ({ ...current, [target]: {
        ok: false, message: e instanceof Error ? e.message : c("testFailed"),
      } }));
    } finally { setTesting(current => ({ ...current, [target]: false })); }
  };
  if (error && !config) return <div role="alert">{error}<Button type="button" variant="outline" onClick={() => void load()}>{c("retry")}</Button></div>;
  if (!config) return <p>{c("loading")}</p>;

  const disabled = (target: Target) => config.locked || saving[target] || testing[target];
  const providerFamily = config.llm.provider === "responses" ? "openai" : config.llm.provider;
  const changeLlmProvider = (value: ModelProviderId) => {
    update("llm", "provider", value);
    if (value === "responses" && !config.llm.responses_endpoint) {
      update("llm", "responses_endpoint", "https://api.openai.com/v1/responses");
    }
  };
  const input = (target: Target, field: string, label: string, type = "text") => <Field>
    <FieldLabel htmlFor={`chatbot-${target}-${field}`}>{label}</FieldLabel>
    <Input id={`chatbot-${target}-${field}`} aria-label={label} type={type} disabled={disabled(target)}
      value={String(config[target][field as keyof Connection] ?? "")}
      onChange={e => update(target, field, e.target.value)} autoComplete="off" />
  </Field>;
  const keyInput = (target: Target) => <Field>
    <FieldLabel htmlFor={`chatbot-${target}-api-key`}>{c("optionalKey")}</FieldLabel>
    <Input id={`chatbot-${target}-api-key`} aria-label={`${target} API Key`} type="password" value={keys[target]} autoComplete="new-password"
      disabled={disabled(target)} onChange={e => setKeys(current => ({ ...current, [target]: e.target.value }))}
      placeholder={config[target].api_key_set ? (c("keyConfigured")) : (c("keyPlaceholder"))} />
  </Field>;
  const controls = (target: Target) => <div className="flex flex-col gap-3">
    <div className="min-w-0 space-y-2">
      <div role="status" className="min-h-5 min-w-0">
        {results[target] && <span className={cn("inline-flex items-center gap-1.5 text-sm",
          results[target]?.ok ? "text-success" : "text-destructive")}>
          {results[target]?.ok ? <Check className="size-4" /> : <X className="size-4" />}
          {results[target]?.message}
        </span>}
      </div>
      {saveResults[target] && <p role={saveResults[target]?.ok ? "status" : "alert"}
        className={cn("break-words text-sm", saveResults[target]?.ok ? "text-success" : "text-destructive")}>
        {saveResults[target]?.message}
      </p>}
    </div>
    <div className="flex flex-wrap justify-end gap-2">
      <Button type="button" variant="outline" disabled={disabled(target)} onClick={() => void test(target)}>
        {testing[target] ? <Spinner /> : <Plug />}
        {testing[target] ? t("testing") : target === "llm" ? t("testGeneration")
          : t("testEmbedding")}
      </Button>
      <Button type="button" aria-label={t("saveCard", { name: c(target === "llm" ? "llmTitle" : "embeddingTitle") })}
        disabled={disabled(target) || !Object.keys(changes(target)).length} onClick={() => void save(target)}>
        {saving[target] ? <Spinner /> : <Save />}
        {saving[target] ? t("saving") : t("saveCard", { name: c(target === "llm" ? "llmTitle" : "embeddingTitle") })}
      </Button>
    </div>
  </div>;
  const enable = (target: Target) => <SettingsRow title={c("enable")} layout="inline">
    <Switch aria-label={`Enable chatbot ${target}`} checked={config[target].enabled} disabled={disabled(target)}
      onCheckedChange={checked => update(target, "enabled", checked)} />
  </SettingsRow>;
  return <>
    {error && <p role="alert" className="text-sm text-destructive">{error}
      <Button type="button" variant="outline" onClick={() => void load(true)}>{c("retry")}</Button>
    </p>}
    {config.locked && <p role="status" className="text-sm text-muted-foreground">{c("locked")}</p>}
    {!config.encryption_configured && !config.locked && <p className="text-sm text-muted-foreground">{c("encryptionRequired")}</p>}
    <SettingsSection title={c("llmTitle")}
      description={c("llmDescription")}
      footer={controls("llm")}>
      {enable("llm")}
      <SettingsRow title={c("connection")}>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field>
            <FieldLabel htmlFor="chatbot-provider">{t("provider")}</FieldLabel>
            <Select value={providerFamily} disabled={disabled("llm")}
              onValueChange={provider => changeLlmProvider(provider as ModelProviderId)}>
              <SelectTrigger id="chatbot-provider" aria-label="Chatbot provider"><SelectValue /></SelectTrigger>
              <SelectContent>
                {providers.filter(provider => provider.id !== "responses").map(provider =>
                  <SelectItem key={provider.id} value={provider.id}>{provider.display_name}</SelectItem>)}
              </SelectContent>
            </Select>
          </Field>
          {providerFamily === "openai" && providers.some(provider => provider.id === "responses") &&
            <OpenAIAPIFormatField id="chatbot-api-format" value={config.llm.provider as "openai" | "responses"}
              disabled={disabled("llm")} onChange={changeLlmProvider} />}
          {input("llm", "model", c("model"))}
          {config.llm.provider === "responses" ?
            <ResponsesBaseURLField id="chatbot-responses" value={config.llm.responses_endpoint ?? ""}
              disabled={disabled("llm")} onChange={endpoint => update("llm", "responses_endpoint", endpoint)} />
            : input("llm", "base_url", c("endpoint"))}
          {keyInput("llm")}
        </div>
      </SettingsRow>
      {config.llm.provider === "responses" && <SettingsRow title={t("responsesTitle")}>
        <ResponsesConnectionFields id="chatbot-responses" disabled={disabled("llm")} value={{
          provider: config.llm.responses_provider ?? "openai",
          endpoint: config.llm.responses_endpoint ?? "",
          api_version: config.llm.responses_api_version ?? "",
          send_temperature: config.llm.responses_send_temperature ?? false,
        }} onChange={value => {
          update("llm", "responses_provider", value.provider);
          update("llm", "responses_endpoint", value.endpoint);
          update("llm", "responses_api_version", value.api_version);
          update("llm", "responses_send_temperature", value.send_temperature);
        }} />
      </SettingsRow>}
    </SettingsSection>
    <SettingsSection title={c("embeddingTitle")}
      description={c("embeddingDescription")}
      footer={controls("embedding")}>
      {enable("embedding")}
      <SettingsRow title={c("vectorSpace")}>
        <p className="text-sm">{config.embedding.model} · {c("schemaDimensions")}: {config.embedding.schema_dimensions} · {c("requestDimensions")}: {config.embedding.request_dimensions ?? (c("omitted"))}</p>
      </SettingsRow>
      <SettingsRow title={c("queryConnection")}><div className="grid gap-4 sm:grid-cols-2">
        {input("embedding", "base_url", c("queryEndpoint"))}{keyInput("embedding")}
      </div></SettingsRow>
    </SettingsSection>
  </>;
}
