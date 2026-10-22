"use client";
import * as React from "react";
import { useTranslations } from "next-intl";
import { api, ApiError } from "@/lib/api";
import type { ClientAccessInfo, ClientAccessSession } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export function ClientAccessSettings() {
  const t = useTranslations("ClientAccess");
  const [info, setInfo] = React.useState<ClientAccessInfo | null>(null);
  const [sessions, setSessions] = React.useState<ClientAccessSession[]>([]);
  const [code, setCode] = React.useState("");
  const codeRevision = React.useRef(0);
  const [preview, setPreview] = React.useState<{ user_code: string; client_name: string; user_name: string; scope: string } | null>(null);
  const [enabled, setEnabled] = React.useState(false);
  const [apiUrl, setApiUrl] = React.useState("");
  const [webUrl, setWebUrl] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [message, setMessage] = React.useState("");
  const [missing, setMissing] = React.useState(false);
  const load = React.useCallback(async () => {
    try {
      const [metadata, devices] = await Promise.all([api.clientAccessInfo(), api.clientAccessSessions()]);
      setInfo(metadata); setSessions(devices); setEnabled(metadata.enabled);
      setApiUrl(metadata.api_url || ""); setWebUrl(metadata.verification_uri || "");
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) setMissing(true);
      else setMessage(t("loadFailed"));
    }
  }, [t]);
  React.useEffect(() => { void load(); }, [load]);
  async function act(operation: () => Promise<unknown>, success: string) {
    setBusy(true); setMessage("");
    try { await operation(); setMessage(success); await load(); }
    catch { setMessage(t("actionFailed")); }
    finally { setBusy(false); }
  }
  if (missing) return null;
  return <section className="space-y-4 rounded-xl border p-4" aria-label={t("title")}>
    <div><h2 className="font-semibold">{t("title")}</h2><p className="text-sm text-muted-foreground">{t("description")}</p></div>
    {message && <p role="status" className="text-sm">{message}</p>}
    {!info ? <p>{t("loading")}</p> : <>
      <dl className="grid gap-2 text-sm sm:grid-cols-[auto_1fr]">
        <dt>{t("apiUrl")}</dt><dd className="break-all">{info.api_url || t("notConfigured")}</dd>
        <dt>{t("webUrl")}</dt><dd className="break-all">{info.verification_uri || t("notConfigured")}</dd>
        <dt>{t("protocol")}</dt><dd>{info.protocol}</dd>
      </dl>
      <Button variant="outline" disabled={!info.api_url} onClick={() => void act(() => navigator.clipboard.writeText(JSON.stringify({ deployment: info.deployment, protocol: info.protocol, api_url: info.api_url, web_url: info.verification_uri, server_id: info.server_id })), t("copied"))}>{t("copyConnection")}</Button>
      {!info.host_lifecycle_synchronized && <p className="text-sm text-muted-foreground">{t("hostLifecycle")}</p>}
      {!info.enabled && <p role="status">{t("disabled")}</p>}
      <form className="space-y-2" onSubmit={(event) => { event.preventDefault(); if (busy || !info.enabled || !preview || preview.user_code !== code) return; void act(() => api.clientAccessApprove(preview.user_code, true), t("approved")); }}>
        <label htmlFor="client-user-code" className="text-sm font-medium">{t("userCode")}</label>
        <Input id="client-user-code" autoComplete="off" value={code} maxLength={12} onChange={(event) => { codeRevision.current += 1; setCode(event.target.value.trim().toUpperCase()); setPreview(null); }} />
        <Button type="button" variant="outline" disabled={busy || !code} onClick={() => { const inspectedCode = code; const revision = ++codeRevision.current; setPreview(null); void act(async () => { const result = await api.clientAccessInspect(inspectedCode); if (revision === codeRevision.current) setPreview({ ...result, user_code: inspectedCode }); }, ""); }}>{t("inspect")}</Button>
        {preview && <p className="break-words text-sm">{t("approveDevice", { device: preview.client_name, user: preview.user_name })}</p>}
        <p className="text-sm text-muted-foreground">{t("approvalScope")}</p>
        <div className="flex flex-wrap gap-2"><Button disabled={busy || !code || !info.enabled || !preview || preview.user_code !== code}>{t("approve")}</Button>
          <Button type="button" variant="outline" disabled={busy || !code} onClick={() => void act(() => api.clientAccessApprove(code, false), t("denied"))}>{t("deny")}</Button></div>
      </form>
      {info.can_manage_policy && <form className="space-y-3 border-t pt-4" onSubmit={(event) => { event.preventDefault(); void act(() => api.clientAccessPolicy({ enabled, api_url: apiUrl, verification_uri: webUrl }), t("saved")); }}>
        <label className="flex items-center gap-2"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />{t("enable")}</label>
        <label className="block text-sm">{t("apiUrl")}<Input type="url" value={apiUrl} onChange={(event) => setApiUrl(event.target.value)} /></label>
        <label className="block text-sm">{t("webUrl")}<Input type="url" value={webUrl} onChange={(event) => setWebUrl(event.target.value)} /></label>
        <Button disabled={busy}>{t("savePolicy")}</Button>
      </form>}
      <div className="space-y-3 border-t pt-4"><h3 className="font-medium">{t("devices")}</h3>
        {sessions.length === 0 && <p className="text-sm text-muted-foreground">{t("empty")}</p>}
        {sessions.map((session) => <article className="space-y-2 rounded-lg border p-3" key={session.session_id}>
          <p className="break-words font-medium">{session.client_name}</p><p className="text-xs text-muted-foreground">{session.scope}</p>
          <dl className="grid gap-1 text-sm sm:grid-cols-[auto_1fr]">
            <dt>{t("accessExpiry")}</dt><dd>{new Date(session.access_expires_at).toLocaleString()}</dd>
            <dt>{t("refreshExpiry")}</dt><dd>{new Date(session.refresh_expires_at).toLocaleString()}</dd>
            <dt>{t("absoluteExpiry")}</dt><dd>{new Date(session.absolute_expires_at).toLocaleString()}</dd>
            <dt>{t("lastUsed")}</dt><dd>{session.last_used_at ? new Date(session.last_used_at).toLocaleString() : t("neverUsed")}</dd>
          </dl>
          {session.revoked_at ? <p>{t("revoked")}</p> : <Button variant="destructive" disabled={busy} onClick={() => void act(() => api.clientAccessRevoke(session.session_id), t("revoked"))}>{t("revoke")}</Button>}
        </article>)}
      </div>
    </>}
  </section>;
}
