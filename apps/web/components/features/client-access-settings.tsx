"use client";
import * as React from "react";
import { useTranslations } from "next-intl";
import { useApp } from "@/components/features/app-shell";
import { SettingsSection } from "@/components/features/settings-section";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import type { ClientAccessInfo, ClientAccessSession, ClientDevicePreview } from "@/lib/types";

export function ClientAccessSettings() {
  const t = useTranslations("ClientAccess");
  const { user } = useApp();
  const [mode, setMode] = React.useState<string>();
  const [info, setInfo] = React.useState<ClientAccessInfo>();
  const [sessions, setSessions] = React.useState<ClientAccessSession[]>([]);
  const [code, setCode] = React.useState("");
  const [preview, setPreview] = React.useState<ClientDevicePreview>();
  const [message, setMessage] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const pending = React.useRef(false);
  const load = React.useCallback(async () => {
    const status = await api.authStatus(); setMode(status.mode);
    if (status.mode !== "password") return;
    const [descriptor, records] = await Promise.all([api.clientAccessInfo(), api.clientAccessSessions()]);
    setInfo(descriptor); setSessions(records);
  }, []);
  React.useEffect(() => { let current = true; void load().catch(() => { if (current) setMessage(t("loadFailed")); });
    return () => { current = false; }; }, [load, t]);
  async function run(work: () => Promise<void>) {
    if (pending.current) return;
    pending.current = true; setBusy(true); setMessage("");
    try { await work(); } catch { setMessage(t("actionFailed")); }
    finally { pending.current = false; setBusy(false); }
  }
  async function approve(allowed: boolean) {
    if (!preview) return;
    await api.approveClientAccess(preview.user_code, allowed);
    setPreview(undefined); setCode(""); setMessage(t(allowed ? "approved" : "denied"));
    await load();
  }
  return <SettingsSection title={t("title")} description={t("description")}>
    <div className="space-y-5 p-4 sm:p-5">
      {message && <p role="status" className="text-sm">{message}</p>}
      {mode === "local" ? <p className="text-sm text-muted-foreground">{t("passwordRequired")}</p> : info && <>
        <div className="space-y-1 break-all text-sm">
          <p>{t("identity")} · {user?.email}</p><p>{t("server")} · {info.server_id}</p>
          <p className="text-muted-foreground">{t("scope")}</p>
          <p className="text-muted-foreground">{t("expiry")}</p>
          {info.api_url ? <><p>{t("apiAddress")} · {info.api_url}</p>
            <Button variant="outline" disabled={busy} onClick={() => void run(async () => {
              await navigator.clipboard.writeText(JSON.stringify({protocol:info.protocol,deployment:info.deployment,
                api_url:info.api_url,server_id:info.server_id,verification_uri:info.verification_uri},null,2));
              setMessage(t("copied"));
            })}>{t("copyConnection")}</Button></> : <p>{t("addressRequired")}</p>}
        </div>
        <form onSubmit={event => { event.preventDefault(); void run(async () => { setPreview(await api.clientAccessDevice(code.trim().toUpperCase().replaceAll("-", ""))); }); }}
          className="space-y-3 border-t pt-5">
          <label htmlFor="client-user-code" className="text-sm font-medium">{t("userCode")}</label>
          <Input id="client-user-code" value={code} maxLength={12} autoComplete="off" disabled={busy}
            onChange={event => { setCode(event.target.value); setPreview(undefined); }} placeholder="AB12CD34" />
          <Button data-action="inspect" type="submit" variant="outline" disabled={busy || code.trim().length < 8}>{t("inspect")}</Button>
        </form>
        {preview && <div className="space-y-3 rounded-md border p-4">
          <p className="text-sm font-medium">{t("confirmDevice")} · {preview.client_name}</p>
          <p className="text-sm">{preview.user_code} · {info.server_id} · {user?.email}</p>
          <p className="text-sm text-muted-foreground">{t("confirmScope")}</p>
          <Button data-action="approve" disabled={busy} onClick={() => void run(() => approve(true))}>{t("approve")}</Button>{" "}
          <Button variant="outline" disabled={busy} onClick={() => void run(() => approve(false))}>{t("deny")}</Button>
        </div>}
        <div className="space-y-3 border-t pt-5"><h3 className="text-sm font-medium">{t("sessions")}</h3>
          {sessions.length === 0 && <p className="text-sm text-muted-foreground">{t("noSessions")}</p>}
          {sessions.map(record => <div key={record.session_id} className="space-y-2 rounded-md border p-3 text-sm">
            <p className="font-medium">{record.client_name}{record.revoked_at ? ` · ${t("revoked")}` : ""}</p>
            <p className="text-muted-foreground">{t("lastUsed")} · {record.last_used_at ?? t("neverUsed")}</p>
            <p className="text-muted-foreground">Access · {record.access_expires_at}<br />Refresh · {record.refresh_expires_at}<br />{t("absoluteExpiry")} · {record.absolute_expires_at}</p>
            {!record.revoked_at && <Button variant="outline" disabled={busy} onClick={() => void run(async () => {
              await api.revokeClientAccess(record.session_id); await load(); setMessage(t("revoked"));
            })}>{t("revoke")}</Button>}
          </div>)}
          <Button variant="outline" disabled={busy} onClick={() => void run(load)}>{t("refresh")}</Button>
        </div>
      </>}
    </div>
  </SettingsSection>;
}
