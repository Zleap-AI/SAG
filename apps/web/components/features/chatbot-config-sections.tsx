"use client";

import * as React from "react";
import { Plug } from "lucide-react";
import { useTranslations } from "next-intl";
import { SettingsRow, SettingsSection } from "@/components/features/settings-section";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { api } from "@/lib/api";
import type { ChatbotConfig as Config, ChatbotConnection as Connection, ModelProviderSpec } from "@/lib/types";
import type { ModelSettingsProps } from "./model-config-form";

type Target = "llm" | "embedding";

export function ChatbotConfigSections({ saveRef, onState }: ModelSettingsProps = {}) {
  const c = useTranslations("ChatbotConfig");
  const t = useTranslations("ModelConfig");
  const [providers, setProviders] = React.useState<ModelProviderSpec[]>([]);
  const [config, setConfig] = React.useState<Config | null>(null);
  const [keys, setKeys] = React.useState<Record<Target, string>>({ llm: "", embedding: "" });
  const [saving, setSaving] = React.useState(false);
  const [testing, setTesting] = React.useState<Record<Target, boolean>>({ llm: false, embedding: false });
  const [error, setError] = React.useState("");
  const [results, setResults] = React.useState<Partial<Record<Target, string>>>({});
  const saved = React.useRef<Config | null>(null);
  const load = React.useCallback(async (retainEdits = false) => {
    try {
      const [result, catalog] = await Promise.all([
        api.getChatbotConfig(),
        retainEdits && saved.current ? Promise.resolve(null) : api.getModelProviders(),
      ]);
      const loaded = result.config;
      if (catalog) {
        if (!catalog.some(provider => provider.id === loaded.llm.provider)) throw new Error(c("loadFailed"));
        setProviders(catalog);
      }
      if (!retainEdits || !saved.current) saved.current = loaded;
      setConfig(current => retainEdits && current ? {
        ...current, locked: loaded.locked, encryption_configured: loaded.encryption_configured,
        embedding: { ...current.embedding, model: loaded.embedding.model,
          schema_dimensions: loaded.embedding.schema_dimensions, request_dimensions: loaded.embedding.request_dimensions },
      } : loaded);
      setError("");
    }
    catch (e) { setError(e instanceof Error ? e.message : c("loadFailed")); }
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
    setResults(current => ({ ...current, [target]: "" }));
  };
  const draft = (target: Target, connection: Connection) => {
    const fields = target === "llm"
      ? ["enabled", "provider", "base_url", "model"]
      : ["enabled", "base_url"];
    return Object.fromEntries(fields.map(field => [field, connection[field as keyof Connection]]));
  };
  const changes = () => !config || !saved.current || config.locked ? {} : Object.fromEntries((["llm", "embedding"] as const).flatMap(target => {
      const values = draft(target, config[target]);
      return keys[target] || JSON.stringify(values) !== JSON.stringify(draft(target, saved.current![target]))
        ? [[target, { ...values, api_key: keys[target] }]] : [];
    }));
  async function save() {
    if (!config || !saved.current) throw new Error(c("loadFailed"));
    const patch = changes();
    if (!Object.keys(patch).length) return;
    setSaving(true);
    try {
      const result = await api.saveChatbotConfig(patch);
      saved.current = result.config;
      setConfig(result.config);
      setKeys({ llm: "", embedding: "" });
      setError("");
    } finally { setSaving(false); }
  }
  React.useImperativeHandle(saveRef, () => ({ save }));
  const changed = Object.keys(changes()).length > 0;
  React.useEffect(() => { onState?.({ ready: Boolean(config && !saving && !testing.llm && !testing.embedding), changed }); }, [config, saving, testing, changed, onState]);

  const test = async (target: Target) => {
    if (!config || config.locked || saving || testing[target]) return;
    setTesting(current => ({ ...current, [target]: true }));
    setResults(current => ({ ...current, [target]: "" }));
    try {
      const result = await api.testChatbotConfig({
        [target]: { ...draft(target, config[target]), api_key: keys[target] }, target,
      });
      setResults(current => ({ ...current, [target]: result.message }));
    } catch (e) {
      setResults(current => ({ ...current, [target]: e instanceof Error ? e.message : c("testFailed") }));
    } finally { setTesting(current => ({ ...current, [target]: false })); }
  };
  if (error && !config) return <div role="alert">{error}<Button type="button" variant="outline" onClick={() => void load()}>{c("retry")}</Button></div>;
  if (!config) return <p>{c("loading")}</p>;

  const disabled = (target: Target) => config.locked || saving || testing[target];
  const input = (target: Target, field: string, label: string, type = "text") => <label className="grid gap-2 text-sm">
    {label}<Input aria-label={label} type={type} disabled={disabled(target)}
      value={String(config[target][field as keyof Connection] ?? "")}
      onChange={e => update(target, field, e.target.value)} autoComplete="off" />
  </label>;
  const keyInput = (target: Target) => <label className="grid gap-2 text-sm">{c("optionalKey")}
    <Input aria-label={`${target} API Key`} type="password" value={keys[target]} autoComplete="new-password"
      disabled={disabled(target)} onChange={e => setKeys(current => ({ ...current, [target]: e.target.value }))}
      placeholder={config[target].api_key_set ? (c("keyConfigured")) : (c("keyPlaceholder"))} />
  </label>;
  const controls = (target: Target) => <div className="flex flex-wrap items-center justify-between gap-3">
    <div role="status" className="min-h-5 min-w-0">
      {results[target] && <span className="text-sm">{results[target]}</span>}
    </div>
    <Button type="button" variant="outline" disabled={disabled(target)} onClick={() => void test(target)}>
      {testing[target] ? <Spinner /> : <Plug />}
      {testing[target] ? t("testing") : target === "llm" ? t("testGeneration")
        : (c("testEmbedding"))}
    </Button>
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
          <label className="grid gap-2 text-sm">{t("provider")}
            <select aria-label="Chatbot provider" className="h-10 rounded-md border bg-background px-3" disabled={disabled("llm")}
              value={config.llm.provider} onChange={e => update("llm", "provider", e.target.value)}>
              {providers.map(provider => <option key={provider.id} value={provider.id}>{provider.display_name}</option>)}
            </select>
          </label>
          {input("llm", "model", c("model"))}
          {input("llm", "base_url", c("endpoint"))}
          {keyInput("llm")}
        </div>
      </SettingsRow>
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
