"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

import { api, ApiError } from "@/lib/api";
import { getDiagnosticsStore } from "@/lib/diagnostics";
import {
  beginDocumentMutation,
  deriveDocumentActivity,
  failedPollingDeadline,
  isCurrentDocumentSource,
  isDocumentJobTerminal,
  isLatestDocumentRefresh,
  shouldApplyDocumentJobResponse,
  shouldKeepDocumentMutation,
  runDocumentPollingCycle,
  type DocumentAction,
  type DocumentActivity,
  type DocumentMutationState,
} from "@/lib/document-activity";
import type { BackgroundJob, Doc, Source } from "@/lib/types";

type RefreshOptions = { background?: boolean };

/**
 * Shared source-detail controller for the normal page and the mini workspace.
 * It owns document mutations, optimistic feedback, job tracking and polling so
 * both presentations converge on the same server-authoritative state.
 */
export function useSourceContent(sourceId: string, active = true) {
  const t = useTranslations("Knowledge");
  const [source, setSource] = React.useState<Source | null>(null);
  const [documents, setDocuments] = React.useState<Doc[] | null>(null);
  const [mutations, setMutations] = React.useState<
    Record<string, DocumentMutationState>
  >({});
  const [failedUntil, setFailedUntil] = React.useState<Record<string, number>>({});
  const [error, setError] = React.useState("");
  const [notFound, setNotFound] = React.useState(false);
  const [refreshing, setRefreshing] = React.useState(false);
  const documentsRef = React.useRef<Doc[] | null>(null);
  const mutationsRef = React.useRef(mutations);
  const refreshRequestRef = React.useRef(0);
  const jobRequestRef = React.useRef(0);
  const latestJobRequestByIdRef = React.useRef<Record<string, number>>({});
  const sourceIdRef = React.useRef(sourceId);
  sourceIdRef.current = sourceId;

  React.useEffect(() => {
    mutationsRef.current = mutations;
  }, [mutations]);

  const refresh = React.useCallback(async (options: RefreshOptions = {}) => {
    if (!sourceId || !isCurrentDocumentSource(sourceId, sourceIdRef.current)) return;
    const requestId = ++refreshRequestRef.current;
    if (!options.background) setRefreshing(true);
    try {
      const [nextSource, nextDocuments] = await Promise.all([
        api.getSource(sourceId),
        api.listDocuments(sourceId),
      ]);
      if (
        !isCurrentDocumentSource(sourceId, sourceIdRef.current)
        || !isLatestDocumentRefresh(requestId, refreshRequestRef.current)
      ) return;

      const previousDocuments = documentsRef.current ?? [];
      const previousById = new Map(
        previousDocuments.map((document) => [document.id, document]),
      );
      // 记录文档后台处理的终态跃迁（ready / failed），这是上传后异步链路
      // 中最容易出问题的一环（如 embedding 上游失败），前端此前无埋点。
      // 仅在状态从"处理中"跃迁到终态时记录一次，避免轮询重复写入。
      for (const document of nextDocuments) {
        const prev = previousById.get(document.id);
        if (!prev || prev.status === document.status) continue;
        const wasProcessing =
          prev.status === "pending"
          || prev.status === "loading"
          || prev.status === "extracting";
        if (document.status === "ready" && wasProcessing) {
          getDiagnosticsStore().record("knowledge.process", {
            phase: "ready",
            source_id: sourceId,
            document_id: document.id,
            filename: document.filename,
            chunk_count: document.chunk_count,
            event_count: document.event_count,
            token_usage: document.token_usage,
          });
        } else if (document.status === "failed" && prev.status !== "failed") {
          getDiagnosticsStore().record("knowledge.process", {
            phase: "failed",
            source_id: sourceId,
            document_id: document.id,
            filename: document.filename,
            error_message: document.error,
            error_layer: document.error_layer ?? undefined,
            error_stage: document.error_stage ?? undefined,
          });
        }
      }
      const now = Date.now();
      setFailedUntil((current) => {
        const next: Record<string, number> = {};
        for (const document of nextDocuments) {
          const deadline = failedPollingDeadline(
            previousById.get(document.id)?.status,
            document.status,
            now,
          );
          const retained = current[document.id];
          if (deadline) next[document.id] = deadline;
          else if (retained && retained > now) next[document.id] = retained;
        }
        return next;
      });
      documentsRef.current = nextDocuments;
      setSource(nextSource);
      setDocuments(nextDocuments);
      setError("");
      setNotFound(false);
    } catch (reason) {
      if (
        !isCurrentDocumentSource(sourceId, sourceIdRef.current)
        || !isLatestDocumentRefresh(requestId, refreshRequestRef.current)
      ) return;
      const missing = reason instanceof ApiError && reason.status === 404;
      setNotFound(missing);
      setError(
        missing
          ? t("sourceGone")
          : reason instanceof ApiError
            ? reason.message
            : t("sourceContentFailed"),
      );
    } finally {
      if (
        isCurrentDocumentSource(sourceId, sourceIdRef.current)
        && isLatestDocumentRefresh(requestId, refreshRequestRef.current)
      ) {
        setRefreshing(false);
      }
    }
  }, [sourceId, t]);

  const pollJobs = React.useCallback(async () => {
    const tracked = Object.entries(mutationsRef.current).filter(
      (entry): entry is [string, DocumentMutationState & { job: BackgroundJob }] =>
        Boolean(entry[1].job && !isDocumentJobTerminal(entry[1].job.status)),
    );
    if (!tracked.length) return;
    const requests = tracked.map(([, mutation]) => {
      const requestId = ++jobRequestRef.current;
      latestJobRequestByIdRef.current[mutation.job.id] = requestId;
      return {
        requestId,
        promise: api.getJob(mutation.job.id),
      };
    });
    const results = await Promise.allSettled(
      requests.map(({ promise }) => promise),
    );
    setMutations((current) => {
      let changed = false;
      const next = { ...current };
      tracked.forEach(([documentId, trackedMutation], index) => {
        const result = results[index];
        const latest = next[documentId];
        if (
          result.status !== "fulfilled"
          || !latest?.job
          || latest.job.id !== trackedMutation.job.id
          || !shouldApplyDocumentJobResponse(
            latest.job,
            result.value,
            requests[index].requestId,
            latestJobRequestByIdRef.current[trackedMutation.job.id],
          )
        ) {
          return;
        }
        next[documentId] = { ...latest, job: result.value };
        changed = true;
      });
      return changed ? next : current;
    });
  }, []);

  React.useEffect(() => {
    documentsRef.current = null;
    mutationsRef.current = {};
    refreshRequestRef.current += 1;
    latestJobRequestByIdRef.current = {};
    setSource(null);
    setDocuments(null);
    setMutations({});
    setFailedUntil({});
    setError("");
    setNotFound(false);
    if (active) void refresh();
  }, [active, refresh, sourceId]);

  React.useEffect(() => {
    if (!documents) return;
    const byId = new Map(documents.map((document) => [document.id, document]));
    setMutations((current) => {
      const next = Object.fromEntries(
        Object.entries(current).filter(([documentId, mutation]) =>
          shouldKeepDocumentMutation(byId.get(documentId), mutation),
        ),
      );
      return Object.keys(next).length === Object.keys(current).length ? current : next;
    });
  }, [documents, mutations]);

  const documentActivities = React.useMemo(() => {
    const now = Date.now();
    return Object.fromEntries(
      (documents ?? []).map((document) => [
        document.id,
        deriveDocumentActivity(
          document,
          mutations[document.id],
          now,
          failedUntil[document.id],
        ),
      ]),
    ) as Record<string, DocumentActivity>;
  }, [documents, failedUntil, mutations]);

  const processing = Object.values(documentActivities).some((activity) => activity.poll);
  const fastPolling =
    Object.keys(mutations).length > 0
    || Object.values(failedUntil).some((deadline) => deadline > Date.now());

  React.useEffect(() => {
    if (!active || !processing) return;
    let cancelled = false;
    let timer: number | null = null;
    const delay = fastPolling ? 1_000 : 4_000;
    const scheduleNext = () => {
      if (cancelled) return;
      timer = window.setTimeout(() => {
        void runDocumentPollingCycle(async () => {
          if (!document.hidden) {
            await Promise.all([refresh({ background: true }), pollJobs()]);
          }
        }, scheduleNext);
      }, delay);
    };
    scheduleNext();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [active, fastPolling, pollJobs, processing, refresh]);

  React.useEffect(() => {
    if (!active) return;
    const refreshWhenVisible = () => {
      if (!document.hidden) {
        void Promise.all([refresh({ background: true }), pollJobs()]);
      }
    };
    window.addEventListener("focus", refreshWhenVisible);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.removeEventListener("focus", refreshWhenVisible);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [active, pollJobs, refresh]);

  const mutateDocument = React.useCallback(async (document: Doc, action: DocumentAction) => {
    const mutationSourceId = sourceId;
    refreshRequestRef.current += 1;
    const mutation = beginDocumentMutation(document, action);
    setMutations((current) => ({ ...current, [document.id]: mutation }));
    try {
      let job: BackgroundJob | null = null;
      if (action === "delete") {
        await api.deleteDocument(mutationSourceId, document.id);
      } else if (action === "reprocess") {
        job = await api.reprocessDocument(mutationSourceId, document.id);
      } else if (action === "pause") {
        job = await api.pauseDocument(mutationSourceId, document.id);
      } else {
        job = await api.resumeDocument(mutationSourceId, document.id);
      }
      if (!isCurrentDocumentSource(mutationSourceId, sourceIdRef.current)) return false;
      if (job) {
        setMutations((current) => {
          const latest = current[document.id];
          return latest
            ? { ...current, [document.id]: { ...latest, job } }
            : current;
        });
      }
      if (!isCurrentDocumentSource(mutationSourceId, sourceIdRef.current)) return false;
      await refresh({ background: true });
      return true;
    } catch (reason) {
      if (!isCurrentDocumentSource(mutationSourceId, sourceIdRef.current)) return false;
      setMutations((current) => {
        const next = { ...current };
        delete next[document.id];
        return next;
      });
      await refresh({ background: true });
      throw reason;
    }
  }, [refresh, sourceId]);

  return {
    source,
    documents,
    documentActivities,
    error,
    notFound,
    refreshing,
    processing,
    refresh,
    mutateDocument,
  };
}
