"use client";

import * as React from "react";
import { Database, Globe2, KeyRound, LockKeyhole, RotateCw, Terminal, Trash2 } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";

import { CodeBlock } from "@/components/features/code-block";
import { CopyButton } from "@/components/features/copy-button";
import { McpToolList } from "@/components/features/mcp-tool-list";
import { SettingsRow, SettingsSection } from "@/components/features/settings-section";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { api, ApiError } from "@/lib/api";
import { getToken } from "@/lib/auth";
import { formatDate } from "@/lib/format";
import { SAG_KNOWLEDGE_MCP_SERVER_KEY } from "@/lib/mcp-server-key";
import { buildHermesFormUrl, buildStandardMcpConfig, mcpHttpUrl } from "@/lib/mcp-quick-connect";
import type { FnOSMcpGrantIssued, KnowledgeMcpDescriptor } from "@/lib/types";

function httpUrl(descriptor: KnowledgeMcpDescriptor, origin?: string) {
  if (origin) return mcpHttpUrl(descriptor, origin);
  if (descriptor.http.url) return descriptor.http.url;
  return descriptor.http.path ?? "";
}

function httpConfig(descriptor: KnowledgeMcpDescriptor, token?: string | null, origin?: string) {
  const headers = { ...descriptor.http.headers };
  if (token) headers.Authorization = `Bearer ${token}`;
  return {
    mcpServers: {
      [SAG_KNOWLEDGE_MCP_SERVER_KEY]: {
        type: "http",
        transport: descriptor.http.transport.replace("-", "_"),
        url: httpUrl(descriptor, origin),
        headers,
      },
    },
  };
}

function stdioConfig(descriptor: KnowledgeMcpDescriptor) {
  if (!descriptor.stdio) return null;
  return {
    mcpServers: {
      [SAG_KNOWLEDGE_MCP_SERVER_KEY]: {
        command: descriptor.stdio.command,
        args: descriptor.stdio.args,
      },
    },
  };
}

export function McpServiceSettings() {
  const t = useTranslations("McpService");
  const locale = useLocale();
  const [descriptor, setDescriptor] = React.useState<KnowledgeMcpDescriptor | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [mode, setMode] = React.useState<"http" | "stdio">("http");
  const [expiresInDays, setExpiresInDays] = React.useState<7 | 30 | 90>(7);
  const [issuedGrant, setIssuedGrant] = React.useState<FnOSMcpGrantIssued | null>(null);
  const [issuing, setIssuing] = React.useState(false);
  const [revokingId, setRevokingId] = React.useState<string | null>(null);
  const [deletingId, setDeletingId] = React.useState<string | null>(null);
  const [showInactiveGrants, setShowInactiveGrants] = React.useState(false);

  const load = React.useCallback(async () => {
    setError(null);
    try {
      setDescriptor(await api.knowledgeMcp());
    } catch (loadError) {
      setError(loadError instanceof ApiError ? loadError.message : t("loadFailed"));
    }
  }, [t]);

  React.useEffect(() => {
    void load();
  }, [load]);

  const isFnOS = descriptor?.mode === "fnos";
  const origin = typeof window === "undefined" ? undefined : window.location.origin;
  const snippets = React.useMemo(() => {
    if (!descriptor) return null;
    const previewToken = isFnOS ? "<SAG_FNOS_MCP_TOKEN>" : "<SAG_TOKEN>";
    return {
      httpPreview: JSON.stringify(httpConfig(descriptor, previewToken, origin), null, 2),
      httpCopy: isFnOS
        ? issuedGrant
          ? JSON.stringify(httpConfig(descriptor, issuedGrant.token, origin), null, 2)
          : null
        : JSON.stringify(httpConfig(descriptor, getToken(), origin), null, 2),
      stdio: descriptor.stdio ? JSON.stringify(stdioConfig(descriptor), null, 2) : null,
    };
  }, [descriptor, isFnOS, issuedGrant, origin]);

  const quickConnect = React.useMemo(() => {
    if (!descriptor || !isFnOS || !issuedGrant || !origin) return null;
    return {
      standardConfig: JSON.stringify(
        buildStandardMcpConfig(descriptor, issuedGrant.token, origin),
        null,
        2,
      ),
      hermesUrl: buildHermesFormUrl(descriptor, issuedGrant.token, origin),
    };
  }, [descriptor, isFnOS, issuedGrant, origin]);

  const issueGrant = async () => {
    setIssuing(true);
    setError(null);
    try {
      setIssuedGrant(await api.issueFnOSMcpGrant(expiresInDays));
      await load();
    } catch (issueError) {
      setError(issueError instanceof ApiError ? issueError.message : t("issueFailed"));
    } finally {
      setIssuing(false);
    }
  };

  const revokeGrant = async (grantId: string) => {
    setRevokingId(grantId);
    setError(null);
    try {
      await api.revokeFnOSMcpGrant(grantId);
      if (issuedGrant?.id === grantId) setIssuedGrant(null);
      await load();
    } catch (revokeError) {
      setError(revokeError instanceof ApiError ? revokeError.message : t("revokeFailed"));
    } finally {
      setRevokingId(null);
    }
  };

  const deleteInactiveGrant = async (grantId: string) => {
    setDeletingId(grantId);
    setError(null);
    try {
      await api.deleteFnOSMcpGrantRecord(grantId);
      await load();
    } catch (deleteError) {
      setError(deleteError instanceof ApiError ? deleteError.message : t("deleteCredentialRecordFailed"));
    } finally {
      setDeletingId(null);
    }
  };

  if (error && !descriptor) {
    return (
      <SettingsSection title={t("title")} description={t("description")}>
        <SettingsRow title={t("serviceConfig")}>
          <Alert variant="destructive">
            <AlertTitle>{t("loadErrorTitle")}</AlertTitle>
            <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
              <span>{error}</span>
              <Button type="button" variant="outline" size="sm" onClick={() => void load()}>
                <RotateCw />
                {t("retry")}
              </Button>
            </AlertDescription>
          </Alert>
        </SettingsRow>
      </SettingsSection>
    );
  }

  if (!descriptor || !snippets) {
    return (
      <SettingsSection title={t("title")} description={t("description")}>
        <SettingsRow title={t("scope")}>
          <div className="flex gap-2"><Skeleton className="h-7 w-28" /><Skeleton className="h-7 w-20" /></div>
        </SettingsRow>
        <SettingsRow title={t("connectionConfig")}>
          <div className="grid gap-3"><Skeleton className="h-8 w-44" /><Skeleton className="h-52 w-full" /></div>
        </SettingsRow>
      </SettingsSection>
    );
  }

  const activeMode = isFnOS ? "http" : mode;
  const preview = activeMode === "http" ? snippets.httpPreview : snippets.stdio;
  const copyValue = activeMode === "http" ? snippets.httpCopy : snippets.stdio;
  const note = activeMode === "http" ? descriptor.http.note : descriptor.stdio?.note;

  return (
    <SettingsSection title={t("title")} description={t("fullDescription")}>
      {error ? <Alert variant="destructive"><AlertTitle>{t("operationFailed")}</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : null}
      <SettingsRow title={t("scope")} description={t("scopeDescription")} layout="inline">
        <div className="flex flex-wrap justify-start gap-2 sm:justify-end">
          <Badge variant="secondary" className="gap-1.5"><Database />{t("allKnowledge")}</Badge>
          <Badge variant="outline">{t("sourceCount", { count: descriptor.source_count })}</Badge>
          <Badge variant="outline">{t("toolCount", { count: descriptor.tools.length })}</Badge>
        </div>
      </SettingsRow>

      {isFnOS ? (
        <SettingsRow title={t("fnosCredential")} description={t("fnosCredentialDescription")}>
          <div className="grid gap-3">
            <div className="flex flex-wrap items-center gap-2">
              <label className="text-sm font-medium" htmlFor="fnos-mcp-expiry">{t("credentialValidity")}</label>
              <select id="fnos-mcp-expiry" value={expiresInDays} onChange={(event) => setExpiresInDays(Number(event.target.value) as 7 | 30 | 90)} className="h-8 rounded-md border bg-background px-2 text-sm" disabled={issuing}>
                <option value={7}>{t("validDays", { days: 7 })}</option><option value={30}>{t("validDays", { days: 30 })}</option><option value={90}>{t("validDays", { days: 90 })}</option>
              </select>
              <Button type="button" size="sm" onClick={() => void issueGrant()} disabled={issuing}><KeyRound />{issuing ? t("issuing") : t("issueCredential")}</Button>
            </div>
            {issuedGrant ? (
              <Alert>
                <KeyRound className="size-4" />
                <AlertTitle>{t("quickConnect")}</AlertTitle>
                <AlertDescription className="grid gap-3">
                  <span>{t("quickConnectDescription")}</span>
                  <span>{t("credentialExpiresAt", { date: formatDate(issuedGrant.expires_at, undefined, { dateStyle: "medium", timeStyle: "short" }, locale) })}</span>
                  {quickConnect ? (
                    <div className="grid gap-3">
                      <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-background/60 p-3">
                        <div className="grid gap-0.5">
                          <span className="font-medium">{t("standardConfig")}</span>
                          <span className="text-xs text-muted-foreground">{t("standardConfigDescription")}</span>
                        </div>
                        <CopyButton text={quickConnect.standardConfig} label={t("copyStandardConfig")} />
                      </div>
                      <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-background/60 p-3">
                        <div className="grid gap-2">
                          <span className="font-medium">Hermes Agent</span>
                          <span className="text-xs text-muted-foreground">{t("hermesDescription")}</span>
                          <div className="grid gap-2 text-xs">
                            <div className="flex flex-wrap items-center gap-2"><span className="text-muted-foreground">{t("hermesName")}</span><span>{descriptor.name}</span><CopyButton text={descriptor.name} label={t("copyName")} /></div>
                            <div className="flex flex-wrap items-center gap-2"><span className="text-muted-foreground">{t("hermesTransport")}</span><span>HTTP/SSE</span></div>
                            <div className="flex flex-wrap items-center gap-2"><span className="text-muted-foreground">URL</span><CopyButton text={quickConnect.hermesUrl} label={t("copyUrl")} /></div>
                            <div className="flex flex-wrap items-center gap-2"><span className="text-muted-foreground">{t("hermesEnvironment")}</span><span>{t("leaveBlank")}</span></div>
                          </div>
                        </div>
                      </div>
                      <span className="text-xs text-muted-foreground">{t("unsupportedAgentNote")}</span>
                    </div>
                  ) : null}
                </AlertDescription>
              </Alert>
            ) : null}
            <div className="grid gap-2">
              {(descriptor.grants ?? []).map((grant) => {
                const expired = new Date(grant.expires_at).getTime() <= Date.now();
                const unusable = Boolean(grant.revoked_at) || expired;
                return (
                  <div key={grant.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-2 text-sm">
                    <div className="grid gap-0.5">
                      <span>{t("credentialExpiresAt", { date: formatDate(grant.expires_at, undefined, { dateStyle: "medium", timeStyle: "short" }, locale) })}</span>
                      <span className="text-xs text-muted-foreground">{unusable ? t(grant.revoked_at ? "credentialRevoked" : "credentialExpired") : t("credentialActive")}</span>
                    </div>
                    {!unusable ? <Button type="button" variant="outline" size="sm" onClick={() => void revokeGrant(grant.id)} disabled={revokingId === grant.id}><Trash2 />{revokingId === grant.id ? t("revoking") : t("revokeCredential")}</Button> : null}
                  </div>
                );
              })}
            </div>
            {(descriptor.inactive_grants?.length ?? 0) > 0 ? (
              <div className="grid gap-2">
                <Button type="button" variant="ghost" size="sm" className="w-fit" onClick={() => setShowInactiveGrants((visible) => !visible)}>
                  {t("inactiveCredentials", { count: descriptor.inactive_grants?.length ?? 0 })}
                </Button>
                {showInactiveGrants ? (
                  <div className="grid gap-2">
                    {descriptor.inactive_grants?.map((grant) => {
                      const expired = new Date(grant.expires_at).getTime() <= Date.now();
                      return (
                        <div key={grant.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-2 text-sm">
                          <div className="grid gap-0.5">
                            <span>{t("credentialExpiresAt", { date: formatDate(grant.expires_at, undefined, { dateStyle: "medium", timeStyle: "short" }, locale) })}</span>
                            <span className="text-xs text-muted-foreground">{t(grant.revoked_at ? "credentialRevoked" : expired ? "credentialExpired" : "credentialRevoked")}</span>
                          </div>
                          <Button type="button" variant="outline" size="sm" onClick={() => void deleteInactiveGrant(grant.id)} disabled={deletingId === grant.id}><Trash2 />{deletingId === grant.id ? t("deletingCredentialRecord") : t("deleteCredentialRecord")}</Button>
                        </div>
                      );
                    })}
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
        </SettingsRow>
      ) : null}

      <SettingsRow title={t("connectionConfig")} description={isFnOS ? t("fnosConnectionDescription") : t("connectionDescription")}>
        <div className="grid gap-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            {!isFnOS ? (
              <ToggleGroup type="single" variant="outline" size="sm" value={mode} onValueChange={(value) => value && setMode(value as typeof mode)} aria-label={t("connectionAria")}>
                <ToggleGroupItem value="http"><Globe2 />HTTP</ToggleGroupItem><ToggleGroupItem value="stdio"><Terminal />{t("localCommand")}</ToggleGroupItem>
              </ToggleGroup>
            ) : <Badge variant="outline" className="gap-1.5"><Globe2 />Streamable HTTP</Badge>}
            {copyValue ? <CopyButton text={copyValue} label={t("mcpConfig")} /> : null}
          </div>
          {preview ? <CodeBlock>{preview}</CodeBlock> : null}
          <div className="flex items-start gap-2 text-xs leading-5 text-muted-foreground">
            {activeMode === "http" ? <LockKeyhole className="mt-0.5 size-3.5 shrink-0" /> : <Terminal className="mt-0.5 size-3.5 shrink-0" />}
            <span>{activeMode === "http" ? (isFnOS ? note : t("tokenNote", { note: note ?? "" })) : note}</span>
          </div>
        </div>
      </SettingsRow>
      <SettingsRow title={t("availableTools")} description={t("toolsDescription")}><McpToolList tools={descriptor.tool_details} /></SettingsRow>
    </SettingsSection>
  );
}
