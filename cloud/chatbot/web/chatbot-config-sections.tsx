"use client";

import * as React from "react";
import { Plug } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { SettingsRow, SettingsSection } from "@/components/features/settings-section";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { API_BASE } from "@/lib/api";
import { getToken } from "@/lib/auth";
import type { ModelSettingsProps } from "./model-config-form";

type Connection = {
  enabled: boolean; base_url: string; api_key_set: boolean; credential_source: string;
  provider?: string; model?: string; responses_provider?: string;
  responses_endpoint?: string; responses_api_version?: string;
  schema_dimensions?: number; request_dimensions?: number | null;
};
type Config = { locked: boolean; encryption_configured: boolean; llm: Connection; embedding: Connection };
type Target = "llm" | "embedding";

async function call(path: string, method = "GET", body?: unknown) {
  const token = getToken();
  const response = await fetch(`${API_BASE}/api/v1/system/chatbot-config${path}`, {
    method, headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message ?? "Unable to update chatbot settings");
  return result;
}

export function ChatbotConfigSections({ saveRef, onState }: ModelSettingsProps = {}) {
  const zh = useLocale().startsWith("zh");
  const t = useTranslations("ModelConfig");
  const [config, setConfig] = React.useState<Config | null>(null);
  const [keys, setKeys] = React.useState<Record<Target, string>>({ llm: "", embedding: "" });
  const [saving, setSaving] = React.useState(false);
  const [testing, setTesting] = React.useState<Record<Target, boolean>>({ llm: false, embedding: false });
  const [error, setError] = React.useState("");
  const [results, setResults] = React.useState<Partial<Record<Target, string>>>({});
  const saved = React.useRef<Config | null>(null);
  const load = React.useCallback(async (retainEdits = false) => {
    try {
      const loaded: Config = (await call("")).config;
      if (!retainEdits || !saved.current) saved.current = loaded;
      setConfig(current => retainEdits && current ? {
        ...current, locked: loaded.locked, encryption_configured: loaded.encryption_configured,
        embedding: { ...current.embedding, model: loaded.embedding.model,
          schema_dimensions: loaded.embedding.schema_dimensions, request_dimensions: loaded.embedding.request_dimensions },
      } : loaded);
      setError("");
    }
    catch (e) { setError(e instanceof Error ? e.message : "Unable to load chatbot settings"); }
  }, []);
  React.useEffect(() => { void load(); }, [load]);
  // Refresh shared embedding identity/inherited tuning after saving the stock form.
  React.useEffect(() => {
    const refresh = () => { void load(true); };
    window.addEventListener("sag:stock-model-saved", refresh);
    return () => window.removeEventListener("sag:stock-model-saved", refresh);
  }, [load]);

  const update = (target: Target, field: string, value: string | boolean) => {
    setConfig(current => current ? { ...current, [target]: { ...current[target], [field]: value,
      ...(target === "llm" && field === "responses_provider" && value !== "azure" ? { responses_api_version: "" } : {}),
    } } : current);
    setResults(current => ({ ...current, [target]: "" }));
  };
  const draft = (target: Target, connection: Connection) => {
    const fields = target === "llm"
      ? ["enabled", "provider", "base_url", "model", "responses_provider", "responses_endpoint", "responses_api_version"]
      : ["enabled", "base_url"];
    return Object.fromEntries(fields.map(field => [field, connection[field as keyof Connection]]));
  };
  const changes = () => !config || !saved.current || config.locked ? {} : Object.fromEntries((["llm", "embedding"] as const).flatMap(target => {
      const values = draft(target, config[target]);
      return keys[target] || JSON.stringify(values) !== JSON.stringify(draft(target, saved.current![target]))
        ? [[target, { ...values, api_key: keys[target] }]] : [];
    }));
  async function save() {
    if (!config || !saved.current) throw new Error("Unable to load chatbot settings");
    const patch = changes();
    if (!Object.keys(patch).length) return;
    setSaving(true);
    try {
      const result = await call("", "PUT", patch);
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
      const result = await call("/test", "POST", {
        [target]: { ...draft(target, config[target]), api_key: keys[target] }, target,
      });
      setResults(current => ({ ...current, [target]: result.message }));
    } catch (e) {
      setResults(current => ({ ...current, [target]: e instanceof Error ? e.message : "Connection update failed" }));
    } finally { setTesting(current => ({ ...current, [target]: false })); }
  };
  if (error && !config) return <div role="alert">{error}<Button type="button" variant="outline" onClick={() => void load()}>{zh ? "重试" : "Retry"}</Button></div>;
  if (!config) return <p>{zh ? "正在加载聊天模型设置…" : "Loading chatbot settings…"}</p>;

  const disabled = (target: Target) => config.locked || saving || testing[target];
  const input = (target: Target, field: string, label: string, type = "text") => <label className="grid gap-2 text-sm">
    {label}<Input aria-label={label} type={type} disabled={disabled(target)}
      value={String(config[target][field as keyof Connection] ?? "")}
      onChange={e => update(target, field, e.target.value)} autoComplete="off" />
  </label>;
  const keyInput = (target: Target) => <label className="grid gap-2 text-sm">{zh ? "API Key（可选）" : "API Key (optional)"}
    <Input aria-label={`${target} API Key`} type="password" value={keys[target]} autoComplete="new-password"
      disabled={disabled(target)} onChange={e => setKeys(current => ({ ...current, [target]: e.target.value }))}
      placeholder={config[target].api_key_set ? (zh ? "已设置；留空保留" : "Configured; leave blank to retain") : (zh ? "端点需要认证时填写" : "Only if the endpoint requires authentication")} />
  </label>;
  const controls = (target: Target) => <div className="flex flex-wrap items-center justify-between gap-3">
    <div role="status" className="min-h-5 min-w-0">
      {results[target] && <span className="text-sm">{results[target]}</span>}
    </div>
    <Button type="button" variant="outline" disabled={disabled(target)} onClick={() => void test(target)}>
      {testing[target] ? <Spinner /> : <Plug />}
      {testing[target] ? t("testing") : target === "llm" ? t("testGeneration")
        : (zh ? "测试嵌入连接" : "Test embedding connection")}
    </Button>
  </div>;
  const enable = (target: Target) => <SettingsRow title={zh ? "启用独立连接" : "Enable separate connection"} layout="inline">
    <Switch aria-label={`Enable chatbot ${target}`} checked={config[target].enabled} disabled={disabled(target)}
      onCheckedChange={checked => update(target, "enabled", checked)} />
  </SettingsRow>;
  return <>
    {error && <p role="alert" className="text-sm text-destructive">{error}
      <Button type="button" variant="outline" onClick={() => void load(true)}>{zh ? "重试" : "Retry"}</Button>
    </p>}
    {config.locked && <p role="status" className="text-sm text-muted-foreground">{zh ? "部署环境已锁定聊天连接设置。" : "Chatbot connections are locked by the deployment environment."}</p>}
    {!config.encryption_configured && !config.locked && <p className="text-sm text-muted-foreground">{zh ? "仅保存 API Key 时需要配置凭证加密；无密钥连接无需加密即可保存。仍可测试未保存的连接。" : "Credential encryption is required only to save an API key. Keyless connections can be saved without it. Unsaved connections can still be tested."}</p>}
    <SettingsSection title={zh ? "聊天 LLM（可选）" : "Chatbot LLM (optional)"}
      description={zh ? "用于回答、工具调用、历史压缩及查询处理。其他参数继承上方设置或部署环境。" : "Used for answers, tool turns, history compression and query processing. Tuning inherits the settings above or deployment overrides."}
      footer={controls("llm")}>
      {enable("llm")}
      <SettingsRow title={zh ? "模型连接" : "Model connection"}>
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="grid gap-2 text-sm">{zh ? "提供商" : "Provider"}
            <select aria-label="Chatbot provider" className="h-10 rounded-md border bg-background px-3" disabled={disabled("llm")}
              value={config.llm.provider} onChange={e => update("llm", "provider", e.target.value)}>
              <option value="openai">OpenAI-compatible</option><option value="anthropic">Anthropic</option>
              <option value="gemini">Gemini</option><option value="responses">Responses API</option>
            </select>
          </label>
          {input("llm", "model", zh ? "模型" : "Model")}
          {config.llm.provider === "responses" ? <>
            <label className="grid gap-2 text-sm">Responses provider
              <select aria-label="Chatbot Responses provider" className="h-10 rounded-md border bg-background px-3" value={config.llm.responses_provider}
                disabled={disabled("llm")} onChange={e => update("llm", "responses_provider", e.target.value)}>
                <option value="openai">OpenAI-compatible</option><option value="azure">Azure</option>
                <option value="bedrock_runtime">Bedrock Runtime</option><option value="bedrock_mantle">Bedrock Mantle</option>
              </select>
            </label>
            {input("llm", "responses_endpoint", "Responses endpoint")}
            {config.llm.responses_provider === "azure" && input("llm", "responses_api_version", "Azure API version")}
          </> : input("llm", "base_url", zh ? "端点（留空使用官方端点）" : "Endpoint (blank for official endpoint)")}
          {keyInput("llm")}
        </div>
      </SettingsRow>
    </SettingsSection>
    <SettingsSection title={zh ? "查询 Embedding（可选）" : "Query embedding (optional)"}
      description={zh ? "用于聊天检索、搜索、MCP 及检索集成；文档索引继续使用原连接。" : "Used for chat retrieval, search, MCP and retrieval integrations. Document indexing uses the original connection."}
      footer={controls("embedding")}>
      {enable("embedding")}
      <SettingsRow title={zh ? "共享向量空间" : "Shared vector space"}>
        <p className="text-sm">{config.embedding.model} · {zh ? "Schema 维度" : "Schema dimensions"}: {config.embedding.schema_dimensions} · {zh ? "请求维度" : "Request dimensions"}: {config.embedding.request_dimensions ?? (zh ? "不发送" : "omitted")}</p>
      </SettingsRow>
      <SettingsRow title={zh ? "查询连接" : "Query connection"}><div className="grid gap-4 sm:grid-cols-2">
        {input("embedding", "base_url", zh ? "查询端点" : "Query endpoint")}{keyInput("embedding")}
      </div></SettingsRow>
    </SettingsSection>
  </>;
}
