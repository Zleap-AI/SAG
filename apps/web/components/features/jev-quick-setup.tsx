"use client";

import * as React from "react";
import { FlaskConical, Settings2 } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { useApp } from "@/components/features/app-shell";
import { Button } from "@/components/ui/button";
import { Field, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { api, ApiError } from "@/lib/api";
import {
  isValidJevEndpoint,
  JEV_DEFAULT_MODEL,
  JEV_ENDPOINTS,
  JEV_PROVIDER_NAMES,
  providerForEndpoint,
  type JevProvider,
} from "@/lib/jev-config";
import { settingsTabHref } from "@/lib/settings-config";
import type { ModelConfigPatch } from "@/lib/types";
import { cn } from "@/lib/utils";

/** The shared JEV service section inside the knowledge settings tab. */
export const JEV_SETTINGS_HREF = settingsTabHref("knowledge", "jev");

export interface JevQuickSetupState {
  provider: JevProvider;
  endpoint: string;
  apiKey: string;
  endpointValid: boolean;
  /** Endpoint and key are both usable, so `save()` may run. */
  ready: boolean;
  testing: boolean;
  saving: boolean;
  testResult: { ok: boolean; message: string } | null;
  /** Last save failure, shown inline next to the fields. */
  error: string | null;
  setProvider: (provider: JevProvider) => void;
  setEndpoint: (endpoint: string) => void;
  setApiKey: (apiKey: string) => void;
  test: () => Promise<void>;
  /**
   * Persists the shared JEV service (endpoint, model, key) and refreshes
   * capabilities so JEV modes stop being reported as disabled. Records the
   * failure in `error` and rethrows so the caller can stop its own submit.
   */
  save: () => Promise<void>;
}

/**
 * Draft state for configuring the paid JEV service without leaving a source
 * dialog. `enabled` is true while a JEV mode is selected but blocked only by a
 * missing configuration; the first time that happens a saved endpoint and
 * model are read back so a custom deployment is not silently replaced by the
 * 302.AI preset.
 */
export function useJevQuickSetup({ enabled }: { enabled: boolean }): JevQuickSetupState {
  const t = useTranslations("RetrievalModes");
  const { refreshCapabilities } = useApp();
  const [provider, setProviderState] = React.useState<JevProvider>("302");
  const [endpoint, setEndpointState] = React.useState<string>(JEV_ENDPOINTS["302"]);
  const [model, setModel] = React.useState(JEV_DEFAULT_MODEL);
  const [apiKey, setApiKeyState] = React.useState("");
  const [testing, setTesting] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [testResult, setTestResult] = React.useState<JevQuickSetupState["testResult"]>(null);
  const [error, setError] = React.useState<string | null>(null);
  const prefilledRef = React.useRef(false);
  const dirtyRef = React.useRef(false);

  React.useEffect(() => {
    if (!enabled || prefilledRef.current) return;
    prefilledRef.current = true;
    api.getModelConfig().then((config) => {
      if (dirtyRef.current) return;
      const savedEndpoint = config.jev_endpoint?.trim();
      if (savedEndpoint) {
        setEndpointState(savedEndpoint);
        setProviderState(providerForEndpoint(savedEndpoint));
      }
      const savedModel = config.jev_model?.trim();
      if (savedModel) setModel(savedModel);
    }).catch(() => {
      /* The 302.AI preset stays in place when the saved config cannot be read. */
    });
  }, [enabled]);

  const endpointValid = isValidJevEndpoint(endpoint);
  const ready = endpointValid && Boolean(apiKey.trim());

  const patch = React.useCallback((): ModelConfigPatch => ({
    jev_endpoint: endpoint.trim(),
    jev_model: model.trim() || JEV_DEFAULT_MODEL,
    jev_api_key: apiKey.trim(),
  }), [apiKey, endpoint, model]);

  const setProvider = React.useCallback((next: JevProvider) => {
    dirtyRef.current = true;
    setTestResult(null);
    setError(null);
    setProviderState(next);
    if (next === "custom") return;
    setEndpointState(JEV_ENDPOINTS[next]);
    setModel(JEV_DEFAULT_MODEL);
  }, []);

  const setEndpoint = React.useCallback((next: string) => {
    dirtyRef.current = true;
    setTestResult(null);
    setError(null);
    setEndpointState(next);
  }, []);

  const setApiKey = React.useCallback((next: string) => {
    dirtyRef.current = true;
    setTestResult(null);
    setError(null);
    setApiKeyState(next);
  }, []);

  const test = React.useCallback(async () => {
    setTesting(true);
    setTestResult(null);
    try {
      setTestResult(await api.testJevModelConfig(patch()));
    } catch (cause) {
      setTestResult({
        ok: false,
        message: cause instanceof ApiError ? cause.message : t("jevTestFailed"),
      });
    } finally {
      setTesting(false);
    }
  }, [patch, t]);

  const save = React.useCallback(async () => {
    setSaving(true);
    setError(null);
    try {
      await api.saveModelConfig(patch());
      await refreshCapabilities();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : t("jevSaveFailed"));
      throw cause;
    } finally {
      setSaving(false);
    }
  }, [patch, refreshCapabilities, t]);

  return {
    provider,
    endpoint,
    apiKey,
    endpointValid,
    ready,
    testing,
    saving,
    testResult,
    error,
    setProvider,
    setEndpoint,
    setApiKey,
    test,
    save,
  };
}

export function JevQuickSetup({
  setup,
  idPrefix,
  disabled = false,
  className,
}: {
  setup: JevQuickSetupState;
  idPrefix: string;
  disabled?: boolean;
  className?: string;
}) {
  const t = useTranslations("RetrievalModes");
  const providerName = setup.provider === "custom" ? null : JEV_PROVIDER_NAMES[setup.provider];
  const endpointInvalid = !setup.endpointValid;

  return (
    <div
      data-testid="jev-quick-setup"
      className={cn(
        "flex flex-col gap-3 rounded-md border border-dashed bg-background/70 p-3",
        className,
      )}
    >
      <div>
        <p className="text-xs font-medium text-foreground">{t("jevSetupTitle")}</p>
        <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{t("jevSetupDescription")}</p>
      </div>
      <fieldset disabled={disabled || setup.saving} className="grid gap-3 sm:grid-cols-2">
        <Field className="gap-1.5">
          <FieldLabel htmlFor={`${idPrefix}-jev-provider`} className="text-xs">{t("jevProvider")}</FieldLabel>
          <Select value={setup.provider} onValueChange={(value) => setup.setProvider(value as JevProvider)}>
            <SelectTrigger id={`${idPrefix}-jev-provider`} className="h-8 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="302">302.AI</SelectItem>
              <SelectItem value="official">{t("jevProviderOfficial")}</SelectItem>
              <SelectItem value="custom">{t("jevProviderCustom")}</SelectItem>
            </SelectContent>
          </Select>
        </Field>
        <Field className="gap-1.5">
          <FieldLabel htmlFor={`${idPrefix}-jev-endpoint`} className="text-xs">{t("jevEndpoint")}</FieldLabel>
          <Input
            id={`${idPrefix}-jev-endpoint`}
            type="url"
            className="h-8 text-xs"
            value={setup.endpoint}
            readOnly={setup.provider !== "custom"}
            aria-invalid={endpointInvalid || undefined}
            placeholder="https://…"
            onChange={(event) => setup.setEndpoint(event.target.value)}
          />
          {endpointInvalid && (
            <p role="alert" className="text-[11px] text-destructive">{t("jevEndpointInvalid")}</p>
          )}
        </Field>
        <Field className="gap-1.5 sm:col-span-2">
          <FieldLabel htmlFor={`${idPrefix}-jev-api-key`} className="text-xs">{t("jevApiKey")}</FieldLabel>
          <div className="flex items-center gap-2">
            <Input
              id={`${idPrefix}-jev-api-key`}
              type="password"
              autoComplete="new-password"
              className="h-8 text-xs"
              value={setup.apiKey}
              placeholder={providerName
                ? t("jevApiKeyPlaceholder", { provider: providerName })
                : t("jevApiKeyCustomPlaceholder")}
              onChange={(event) => setup.setApiKey(event.target.value)}
            />
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="shrink-0 text-xs"
              onClick={() => void setup.test()}
              disabled={!setup.ready || setup.testing}
              data-testid="jev-quick-setup-test"
            >
              {setup.testing ? <Spinner /> : <FlaskConical />}
              {setup.testing ? t("jevTesting") : t("jevTest")}
            </Button>
          </div>
        </Field>
      </fieldset>
      {setup.testResult && (
        <p
          role={setup.testResult.ok ? "status" : "alert"}
          className={cn("text-[11px] leading-4", setup.testResult.ok ? "text-muted-foreground" : "text-destructive")}
        >
          {setup.testResult.message}
        </p>
      )}
      {setup.error && (
        <p role="alert" className="text-[11px] leading-4 text-destructive" data-testid="jev-quick-setup-error">
          {setup.error}
        </p>
      )}
      <p className="text-[11px] leading-4 text-muted-foreground">
        {t("jevSharedNote")}{" "}
        <Link
          href={JEV_SETTINGS_HREF}
          className="inline-flex items-center gap-1 whitespace-nowrap underline underline-offset-2 transition-colors hover:text-foreground"
          data-testid="jev-quick-setup-settings"
        >
          <Settings2 className="size-3" />
          {t("jevOpenSettings")}
        </Link>
      </p>
    </div>
  );
}
