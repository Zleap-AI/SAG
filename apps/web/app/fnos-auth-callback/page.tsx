"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { loadFnOSSDK, parseFnOSAuthCallback } from "@/lib/fnos-app";

export default function FnOSAuthCallbackPage() {
  const t = useTranslations("FnOSNas");
  const [message, setMessage] = useState(() => t("callbackPending"));

  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const sdk = await loadFnOSSDK();
        await sdk.ready();
        const result = parseFnOSAuthCallback(
          sdk,
          window.location.href,
          window.sessionStorage,
        );
        if (!active) return;
        if (window.opener && !window.opener.closed) {
          window.opener.postMessage(result, window.location.origin);
        }
        setMessage(
          result.status === "success" ? t("callbackSuccess") : t("callbackCancelled"),
        );
        window.setTimeout(() => window.close(), 150);
      } catch {
        if (active) setMessage(t("callbackError"));
      }
    })();
    return () => {
      active = false;
    };
  }, [t]);

  return (
    <main className="flex min-h-screen items-center justify-center bg-background p-6">
      <p role="status" className="text-sm text-muted-foreground">
        {message}
      </p>
    </main>
  );
}
