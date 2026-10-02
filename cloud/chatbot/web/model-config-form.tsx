"use client";

import * as React from "react";
import { Save } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useApp } from "@/components/features/app-shell";
import { ModelConfigForm as StockModelConfigForm } from "./stock-model-config-form";
import { ChatbotConfigSections } from "./chatbot-config-sections";

export type ModelSettingsHandle = { save: () => Promise<void> };
export type ModelSettingsProps = {
  saveRef?: React.Ref<ModelSettingsHandle>;
  onState?: (state: { ready: boolean; changed: boolean }) => void;
};

export function ModelConfigForm() {
  const t = useTranslations("ModelConfig");
  const zh = useLocale().startsWith("zh");
  const { refreshCapabilities } = useApp();
  const original = React.useRef<ModelSettingsHandle>(null);
  const chatbot = React.useRef<ModelSettingsHandle>(null);
  const [originalState, setOriginalState] = React.useState({ ready: false, changed: false });
  const [chatbotState, setChatbotState] = React.useState({ ready: false, changed: false });
  type Target = "original" | "chatbot";
  const pending = React.useRef({ original: false, chatbot: false });
  const [saving, setSaving] = React.useState({ original: false, chatbot: false });
  const [results, setResults] = React.useState<Partial<Record<Target, { ok: boolean; message: string }>>>({});
  const [refreshError, setRefreshError] = React.useState("");
  const canSave = {
    original: originalState.ready && originalState.changed && !saving.original,
    chatbot: chatbotState.ready && chatbotState.changed && !saving.chatbot,
  };

  async function save() {
    const handles = { original: original.current, chatbot: chatbot.current };
    await Promise.all((["original", "chatbot"] as const).map(async target => {
      const handle = handles[target];
      if (!canSave[target] || pending.current[target] || !handle) return;
      pending.current[target] = true;
      setSaving(current => ({ ...current, [target]: true }));
      setResults(current => ({ ...current, [target]: undefined }));
      setRefreshError("");
      try {
        await handle.save();
        const message = target === "original"
          ? (zh ? "原模型设置已保存。" : "Original model settings saved.")
          : (zh ? "聊天连接设置已保存。" : "Chatbot connections saved.");
        setResults(current => ({ ...current, [target]: { ok: true, message } }));
        toast.success(message);
        // Refresh shared embedding identity after either independent write.
        window.dispatchEvent(new Event("sag:stock-model-saved"));
        try { await refreshCapabilities(); }
        catch {
          setRefreshError(zh ? "设置已保存，但模型信息未能刷新。请重新加载页面。"
            : "Settings saved, but model information could not refresh. Reload the page.");
        }
      } catch (e) {
        const detail = e instanceof Error ? e.message : t("saveFailed");
        const message = target === "original"
          ? (zh ? `原模型设置未能保存，请重试。${detail}` : `Original model settings could not be saved. Retry. ${detail}`)
          : (zh ? `聊天连接设置未能保存，请重试。${detail}` : `Chatbot connections could not be saved. Retry. ${detail}`);
        setResults(current => ({ ...current, [target]: { ok: false, message } }));
      } finally {
        pending.current[target] = false;
        setSaving(current => ({ ...current, [target]: false }));
      }
    }));
  }

  const status = (target: Target) => saving[target]
    ? <p role="status" className="text-sm text-muted-foreground">{target === "original"
      ? (zh ? "正在保存原模型设置…" : "Saving original model settings…")
      : (zh ? "正在保存聊天连接设置…" : "Saving chatbot connections…")}</p>
    : results[target] && <p role={results[target].ok ? "status" : "alert"}
      className={results[target].ok ? "text-sm text-muted-foreground" : "text-sm text-destructive"}>{results[target].message}</p>;
  const busy = saving.original || saving.chatbot;
  return <div className="flex flex-col gap-8">
    <fieldset disabled={saving.original} inert={saving.original} className="flex min-w-0 flex-col gap-8">
      <StockModelConfigForm saveRef={original} onState={setOriginalState} />
    </fieldset>
    {status("original")}
    <fieldset disabled={saving.chatbot} inert={saving.chatbot} className="flex min-w-0 flex-col gap-8">
      <ChatbotConfigSections saveRef={chatbot} onState={setChatbotState} />
    </fieldset>
    {status("chatbot")}
    <div className="flex flex-wrap items-center justify-end gap-3 border-t pt-4">
      {refreshError && <p role="alert" className="w-full text-sm text-destructive">{refreshError}</p>}
      <Button type="button" onClick={() => void save()} disabled={!canSave.original && !canSave.chatbot}>
        {busy ? <Spinner /> : <Save />}
        {busy && !canSave.original && !canSave.chatbot ? t("saving") : t("save")}
      </Button>
    </div>
  </div>;
}
