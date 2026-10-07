"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

import { api, ApiError } from "@/lib/api";
import { failedPollingDeadline, shouldPollDocument } from "@/lib/document-activity";
import { DOCUMENT_CHANGED_EVENT } from "@/lib/document-events";
import type { Doc } from "@/lib/types";

/** One request at a time; changing the selection invalidates every old response. */
export function useDocumentDetail(sourceId: string, documentId: string) {
  const t = useTranslations("DetailPanel");
  const tRef = React.useRef(t);
  tRef.current = t;
  const key = `${sourceId}:${documentId}`;
  const [state, setState] = React.useState<{ key: string; doc: Doc | null; error: string } | null>(null);

  React.useEffect(() => {
    let alive = true;
    let running = false;
    let refreshAgain = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let current: Doc | null = null;
    let failedUntil: number | undefined;
    let polling = true;

    function schedule() {
      if (!alive || !polling) return;
      timer = setTimeout(() => void refresh(), failedUntil && failedUntil > Date.now() ? 1_000 : 4_000);
    }

    async function refresh() {
      if (!alive) return;
      if (running) {
        refreshAgain = true;
        return;
      }
      clearTimeout(timer);
      if (document.hidden) {
        schedule();
        return;
      }
      running = true;
      try {
        const next = await api.getDocument(sourceId, documentId);
        if (!alive) return;
        failedUntil = failedPollingDeadline(current?.status, next.status) ?? failedUntil;
        current = next;
        polling = shouldPollDocument(next, undefined, Date.now(), failedUntil);
        setState({ key, doc: next, error: "" });
      } catch (error) {
        if (!alive) return;
        const missing = error instanceof ApiError && error.status === 404;
        if (missing) current = null;
        polling = !missing;
        setState({ key, doc: current, error: error instanceof ApiError ? error.message : tRef.current("document.loadFailed") });
      } finally {
        running = false;
        if (alive && refreshAgain) {
          refreshAgain = false;
          void refresh();
        } else {
          schedule();
        }
      }
    }

    const onVisible = () => {
      if (!document.hidden) void refresh();
    };
    const onChanged = (event: Event) => {
      const changed = (event as CustomEvent<{ sourceId: string; documentId: string }>).detail;
      if (changed?.sourceId === sourceId && changed.documentId === documentId) void refresh();
    };
    void refresh();
    window.addEventListener("focus", onVisible);
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener(DOCUMENT_CHANGED_EVENT, onChanged);
    return () => {
      alive = false;
      clearTimeout(timer);
      window.removeEventListener("focus", onVisible);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener(DOCUMENT_CHANGED_EVENT, onChanged);
    };
  }, [documentId, key, sourceId]);

  return state?.key === key ? state : { doc: null, error: "" };
}
