"use client";

import * as React from "react";
import {
  Check,
  ChevronDown,
  Eye,
  EyeOff,
  KeyRound,
  Sparkles,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { api, ApiError, type QuickSetupEmbeddingProvider } from "@/lib/api";
import type { Capabilities } from "@/lib/types";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

interface QuickModelSetupDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfigured: (capabilities: Capabilities) => void;
}

type SetupProvider = "302" | "deepseek";

const EMBEDDING_PRESETS = {
  "302": {
    labelKey: "embedding302Provider",
    model: "Qwen/Qwen3-Embedding-4B",
    keyUrl: "https://dash.302.ai/",
  },
  zhipu: {
    labelKey: "embeddingZhipuProvider",
    model: "embedding-3",
    keyUrl: "https://docs.bigmodel.cn/cn/api/introduction",
  },
  bailian: {
    labelKey: "embeddingBailianProvider",
    model: "text-embedding-v4",
    keyUrl: "https://help.aliyun.com/zh/model-studio/get-api-key",
  },
} as const;

export function QuickModelSetupDialog({
  open,
  onOpenChange,
  onConfigured,
}: QuickModelSetupDialogProps) {
  const t = useTranslations("QuickSetup");
  const [provider, setProvider] = React.useState<SetupProvider>("302");
  const [apiKeys, setApiKeys] = React.useState({ "302": "", deepseek: "" });
  const [embeddingProvider, setEmbeddingProvider] = React.useState<QuickSetupEmbeddingProvider>("302");
  const [embeddingApiKeys, setEmbeddingApiKeys] = React.useState({ "302": "", zhipu: "", bailian: "" });
  const [showKey, setShowKey] = React.useState(false);
  const [showEmbeddingKey, setShowEmbeddingKey] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState("");
  const inputRef = React.useRef<HTMLInputElement>(null);
  const embeddingInputRef = React.useRef<HTMLInputElement>(null);
  const savingRef = React.useRef(false);
  const isDeepSeek = provider === "deepseek";
  const apiKey = apiKeys[provider];
  const embeddingApiKey = embeddingApiKeys[embeddingProvider];
  const embeddingPreset = EMBEDDING_PRESETS[embeddingProvider];
  const embeddingProviderLabel = t(embeddingPreset.labelKey);
  const needsEmbeddingKey = isDeepSeek || embeddingProvider !== "302";

  React.useEffect(() => {
    if (!open) return;
    setError("");
    const timer = window.setTimeout(() => inputRef.current?.focus({ preventScroll: true }), 100);
    return () => window.clearTimeout(timer);
  }, [open]);

  const handleOpenChange = (next: boolean) => {
    if (!savingRef.current) onOpenChange(next);
  };

  const selectProvider = (next: SetupProvider) => {
    if (savingRef.current) return;
    setProvider(next);
    setError("");
    setShowKey(false);
    setShowEmbeddingKey(false);
  };

  const selectEmbeddingProvider = (next: QuickSetupEmbeddingProvider) => {
    if (savingRef.current) return;
    setEmbeddingProvider(next);
    setError("");
    setShowEmbeddingKey(false);
  };

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (savingRef.current) return;
    const key = apiKey.trim();
    if (!key) {
      setError(t(isDeepSeek ? "deepseekKeyRequired" : "keyRequired"));
      inputRef.current?.focus();
      return;
    }
    const embeddingKey = embeddingApiKey.trim();
    if (needsEmbeddingKey && !embeddingKey) {
      setError(t("embeddingKeyRequired", { provider: embeddingProviderLabel }));
      embeddingInputRef.current?.focus();
      return;
    }

    savingRef.current = true;
    setSaving(true);
    setError("");
    try {
      const result = isDeepSeek
        ? await api.quickSetupDeepSeek(key, embeddingKey, embeddingProvider === "302" ? undefined : embeddingProvider)
        : await api.quickSetup302(key, needsEmbeddingKey ? { provider: embeddingProvider, apiKey: embeddingKey } : undefined);
      setApiKeys({ "302": "", deepseek: "" });
      setEmbeddingApiKeys({ "302": "", zhipu: "", bailian: "" });
      setShowKey(false);
      setShowEmbeddingKey(false);
      onConfigured(result.capabilities);
      toast.success(t("configured"));
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : t("failed"));
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className="max-h-[calc(100svh-2rem)] w-[calc(100%_-_2rem)] max-w-3xl gap-0 overflow-y-auto p-0"
        onEscapeKeyDown={(event) => event.preventDefault()}
        onInteractOutside={(event) => event.preventDefault()}
      >
        <form onSubmit={submit}>
          <DialogHeader className="px-6 pb-4 pt-5 pr-12">
            <div className="flex items-center gap-3">
              <div className="grid size-8 shrink-0 place-items-center rounded-lg border bg-muted/50 text-foreground">
                <KeyRound className="size-4" />
              </div>
              <DialogTitle>{t("title")}</DialogTitle>
            </div>
            <DialogDescription className="leading-5">
              {t("description")}
            </DialogDescription>
          </DialogHeader>

          <div className="divide-y border-y">
            {[
              {
                id: "quick-setup-api-key",
                title: t("generationModel"),
                purpose: t("generationPurpose"),
                selectorId: "quick-setup-provider",
                selectorValue: provider,
                onSelect: (value: string) => selectProvider(value as SetupProvider),
                options: (["302", "deepseek"] as const).map((value) => ({
                  value,
                  label: `${value === "302" ? "302.AI" : t("deepseekProvider")} · ${t(value === "deepseek" ? "deepseekGenerationValue" : "generationValue")}`,
                })),
                showKeyField: true,
                ref: inputRef,
                label: isDeepSeek ? "DeepSeek API Key" : "302.AI API Key",
                value: apiKey,
                onChange: (value: string) => setApiKeys((keys) => ({ ...keys, [provider]: value })),
                visible: showKey,
                toggle: () => setShowKey((value) => !value),
                help: isDeepSeek ? t("deepseekKeyDescription") : t(needsEmbeddingKey ? "keyDescription" : "shared302KeyDescription"),
                link: isDeepSeek ? "https://platform.deepseek.com/api_keys" : "https://dash.302.ai/",
                linkLabel: t(isDeepSeek ? "getDeepseekKey" : "get302Key"),
              },
              {
                id: "quick-setup-embedding-api-key",
                title: t("embeddingModel"),
                purpose: t("embeddingPurpose"),
                selectorId: "quick-setup-embedding-provider",
                selectorValue: embeddingProvider,
                onSelect: (value: string) => selectEmbeddingProvider(value as QuickSetupEmbeddingProvider),
                options: (Object.keys(EMBEDDING_PRESETS) as QuickSetupEmbeddingProvider[]).map((value) => ({
                  value,
                  label: `${t(EMBEDDING_PRESETS[value].labelKey)} · ${t("embeddingValue", { model: EMBEDDING_PRESETS[value].model })}`,
                })),
                showKeyField: needsEmbeddingKey,
                ref: embeddingInputRef,
                label: t("embeddingApiKeyLabel", { provider: embeddingProviderLabel }),
                value: embeddingApiKey,
                onChange: (value: string) => setEmbeddingApiKeys((keys) => ({ ...keys, [embeddingProvider]: value })),
                visible: showEmbeddingKey,
                toggle: () => setShowEmbeddingKey((value) => !value),
                help: embeddingProvider === "bailian" ? t("bailianKeyDescription") : t("embeddingKeyDescription"),
                link: embeddingPreset.keyUrl,
                linkLabel: t("getEmbeddingKey", { provider: embeddingProviderLabel }),
              },
            ].map((field) => (
              <section key={field.id} aria-labelledby={`${field.id}-title`} className="space-y-3 px-6 py-4">
                <div className="space-y-2">
                  <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                    <FieldLabel id={`${field.id}-title`} htmlFor={field.selectorId}>{field.title}</FieldLabel>
                    <p id={`${field.id}-purpose`} className="text-xs text-muted-foreground">{field.purpose}</p>
                  </div>
                  <div className="relative">
                    <select
                      id={field.selectorId}
                      value={field.selectorValue}
                      onChange={(event) => field.onSelect(event.target.value)}
                      disabled={saving}
                      aria-describedby={`${field.id}-purpose`}
                      className="h-10 w-full min-w-0 appearance-none rounded-md border border-input bg-background py-2 pl-3 pr-9 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {field.options.map((option) => (
                        <option key={option.value} value={option.value}>{option.label}</option>
                      ))}
                    </select>
                    <ChevronDown className="pointer-events-none absolute right-3 top-3 size-4 text-muted-foreground" aria-hidden="true" />
                  </div>
                </div>
                {field.showKeyField ? (
                  <Field data-invalid={Boolean(error)} className="min-w-0 gap-1.5">
                    <FieldLabel htmlFor={field.id}>{field.label}</FieldLabel>
                    <div className="relative">
                      <Input
                        ref={field.ref}
                        id={field.id}
                        type={field.visible ? "text" : "password"}
                        value={field.value}
                        onChange={(event) => {
                          field.onChange(event.target.value);
                          if (error) setError("");
                        }}
                        placeholder="sk-..."
                        autoComplete="off"
                        spellCheck={false}
                        disabled={saving}
                        aria-describedby={`${field.id}-help`}
                        aria-invalid={Boolean(error)}
                        className="h-10 pr-10 font-mono"
                      />
                      <TooltipProvider delayDuration={300}>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <button
                              type="button"
                              onClick={field.toggle}
                              disabled={saving}
                              aria-label={field.visible ? t("hideApiKey") : t("showApiKey")}
                              className="absolute right-1 top-1 grid size-8 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
                            >
                              {field.visible ? <EyeOff /> : <Eye />}
                            </button>
                          </TooltipTrigger>
                          <TooltipContent>{field.visible ? t("hideKey") : t("showKey")}</TooltipContent>
                        </Tooltip>
                      </TooltipProvider>
                    </div>
                    <FieldDescription id={`${field.id}-help`}>
                      {field.help}{" "}
                      <a href={field.link} target="_blank" rel="noopener noreferrer">{field.linkLabel}</a>
                    </FieldDescription>
                  </Field>
                ) : (
                  <p className="flex items-center gap-2 text-xs text-muted-foreground">
                    <Check className="size-3.5 shrink-0" aria-hidden="true" />
                    {t("sharedKeyNotice")}
                  </p>
                )}
              </section>
            ))}
          </div>
          <div className="space-y-3 px-6 py-3">
            {error && <FieldError>{error}</FieldError>}

            <Alert className="border-border/80 bg-background py-2.5">
              <Check className="size-4" />
              <AlertDescription className="text-muted-foreground">
                {t(isDeepSeek ? "deepseekDefaults" : "defaults")}
              </AlertDescription>
            </Alert>
          </div>

          <DialogFooter className="border-t bg-muted/20 px-6 py-4 sm:items-center sm:justify-between">
            <Button
              type="button"
              variant="ghost"
              onClick={() => handleOpenChange(false)}
              disabled={saving}
            >
              {t("skip")}
            </Button>
            <Button type="submit" disabled={saving || !apiKey.trim() || (needsEmbeddingKey && !embeddingApiKey.trim())} className="min-w-28">
              {saving ? <Spinner aria-label={t("configuringAria")} /> : <Sparkles />}
              {saving ? t("configuring") : t("enable")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
