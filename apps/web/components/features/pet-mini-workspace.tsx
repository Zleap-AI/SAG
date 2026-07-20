"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import { usePathname } from "next/navigation";
import { AnimatePresence, motion } from "motion/react";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  CircleDot,
  FileText,
  Grip,
  History,
  List,
  Loader2,
  MessageSquarePlus,
  Search,
  SlidersHorizontal,
  X,
} from "lucide-react";

import { api, ApiError } from "@/lib/api";
import { stripCitationTransportTokens } from "@/lib/citation-presentation";
import type { ConversationMessage } from "@/lib/conversation-runtime";
import { formatDate } from "@/lib/format";
import type { PetAgent } from "@/lib/pet-agent";
import type {
  ActivityItem,
  Citation,
  SearchEvent,
  SearchResponse,
  UniverseGraphPatch,
  UniverseNodeDetail,
  UniverseRelation,
} from "@/lib/types";
import {
  UNIVERSE_ASK_EVENT,
  UNIVERSE_DETAIL_EVENT,
  UNIVERSE_PATCH_EVENT,
  UNIVERSE_PATCH_RESET_EVENT,
  dispatchUniverseFocus,
  dispatchUniverseResume,
  takePendingUniverseAsk,
  takePendingUniverseDetail,
  type UniverseAskTarget,
  type UniverseDetailTarget,
} from "@/lib/universe-events";
import { cn } from "@/lib/utils";
import {
  WORKSPACE_SECTIONS,
  type WorkspaceSection,
} from "@/lib/workspace";
import { useApp } from "@/components/features/app-shell";
import { AgentSettingsCard } from "@/components/features/agent-settings-card";
import type { AgentActivityMatch } from "@/components/features/chat/agent-activity-timeline";
import {
  useConversationRuntime,
  useConversationSession,
} from "@/components/features/chat/conversation-provider";
import { ConversationPanel } from "@/components/features/chat/conversation-panel";
import { CompactDocumentDetailWorkspace } from "@/components/features/document-detail-workspace";
import { KnowledgeWorkspace } from "@/components/features/knowledge-workspace";
import { PetHeadAvatar } from "@/components/features/pet-head-avatar";
import { SearchPanel } from "@/components/features/search/search-panel";
import { useSearchWorkspace } from "@/components/features/search/search-provider";
import { WorkspaceSectionIcon } from "@/components/features/workspace-section-icon";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";

interface MiniPanelSize {
  width: number;
  height: number;
}

export type PetMiniView = "workspace" | "assistant-settings";

interface WorkspaceUniverseNode {
  id: string;
  kind: "event" | "entity";
  source_id: string;
  label: string;
  description: string;
  chunk_id: string | null;
  importance: number;
}

interface WorkspaceUniverseDetail extends UniverseNodeDetail {
  related_nodes: WorkspaceUniverseNode[];
  relations: UniverseRelation[];
}

interface MiniCitationTarget {
  kind: "citation";
  id: string;
  citation: Citation;
}

interface MiniDocumentTarget {
  kind: "document";
  id: string;
  source_id: string;
  title: string;
}

type MiniDetailTarget = UniverseDetailTarget | MiniCitationTarget | MiniDocumentTarget;

interface MiniDetailLabels {
  localKnowledge: string;
  knowledgeNode: string;
  eventDetail: string;
}

const DEFAULT_MINI_PANEL_SIZE: MiniPanelSize = { width: 360, height: 470 };
const MINI_PANEL_MIN_SIZE: MiniPanelSize = { width: 300, height: 300 };
const MINI_PANEL_MAX_SIZE: MiniPanelSize = { width: 720, height: 760 };
function findCitationEvent(result: SearchResponse, citation: Citation) {
  return result.events.find((event) => event.chunk_id === citation.chunk_id) ?? null;
}

function clampPanelDimension(value: number, min: number, max: number) {
  const safeMax = Math.max(220, max);
  const safeMin = Math.min(min, safeMax);
  return Math.min(Math.max(value, safeMin), safeMax);
}

function searchResultNode(
  result: SearchResponse,
  kind: "event" | "entity",
  id: string,
  sourceId = "",
): WorkspaceUniverseNode | null {
  if (kind === "event") {
    const event = result.events.find((item) => item.id === id);
    if (!event) return null;
    return {
      id: event.id,
      kind,
      source_id: event.source_id ?? sourceId,
      label: event.title,
      description: event.summary,
      chunk_id: event.chunk_id,
      importance: event.score,
    };
  }
  const entity = result.entities.find((item) => item.id === id);
  if (!entity) return null;
  return {
    id: entity.id,
    kind,
    source_id: sourceId,
    label: entity.name,
    description: entity.description,
    chunk_id: null,
    importance: Math.min(1, 0.2 + entity.heat / 10),
  };
}

function detailFromSearchResult(
  result: SearchResponse | null,
  target: UniverseDetailTarget,
  labels: MiniDetailLabels,
): WorkspaceUniverseDetail | null {
  if (!result) return null;
  const event = target.kind === "event"
    ? result.events.find((item) => item.id === target.id)
    : null;
  const entity = target.kind === "entity"
    ? result.entities.find((item) => item.id === target.id)
    : null;
  if (!event && !entity) return null;

  const matchingRelations = result.relations.filter(
    (relation) => relation.source_id === target.id || relation.target_id === target.id,
  );
  const relations: UniverseRelation[] = matchingRelations.flatMap((relation) =>
    relation.kind === "mentions" || relation.kind === "subevent"
      ? [{
          source_id:
            result.events.find((event) => event.id === relation.source_id)?.source_id
            ?? target.source_id,
          from_id: relation.source_id,
          to_id: relation.target_id,
          kind: relation.kind,
          weight: relation.weight,
          description: relation.description,
        }]
      : [],
  );
  const sourceId = target.source_id;
  const relatedNodes = matchingRelations.flatMap((relation) => {
    const sourceIsTarget = relation.source_id === target.id;
    const relatedId = sourceIsTarget ? relation.target_id : relation.source_id;
    const relatedKind = sourceIsTarget ? relation.target_kind : relation.source_kind;
    if (relatedKind !== "event" && relatedKind !== "entity") return [];
    const node = searchResultNode(result, relatedKind, relatedId, sourceId);
    return node ? [node] : [];
  });
  const supportingEvent = event ?? relatedNodes
    .filter((node) => node.kind === "event")
    .map((node) => result.events.find((item) => item.id === node.id))
    .find(Boolean) ?? null;
  const section = supportingEvent
    ? result.sections.find((item) => item.chunk_id === supportingEvent.chunk_id) ?? null
    : null;
  const eventDetail = event
    ? section?.content?.trim() || event.summary
    : "";

  return {
    id: target.id,
    kind: target.kind,
    source_id: supportingEvent?.source_id ?? sourceId,
    source_name: supportingEvent?.source_name ?? labels.localKnowledge,
    label: event?.title ?? entity?.name ?? labels.knowledgeNode,
    description: event ? eventDetail : entity?.description ?? "",
    category: event?.category ?? entity?.type ?? "",
    start_time: event?.start_time ?? null,
    evidence: section
      ? {
          source_id: section.source_id ?? supportingEvent?.source_id ?? "",
          source_name: section.source_name ?? supportingEvent?.source_name ?? labels.localKnowledge,
          document_id: supportingEvent?.document_id ?? null,
          document_name: null,
          chunk_id: section.chunk_id,
          heading: section.heading,
          content: section.content,
        }
      : null,
    related_nodes: relatedNodes,
    relations,
  };
}

function detailFromGraphPatch(
  patch: UniverseGraphPatch | null,
  target: UniverseDetailTarget,
  labels: MiniDetailLabels,
): WorkspaceUniverseDetail | null {
  if (
    !patch
    || patch.anchor.id !== target.id
    || patch.anchor.kind !== target.kind
    || patch.anchor.source_id !== target.source_id
  ) return null;
  return {
    id: patch.anchor.id,
    kind: patch.anchor.kind,
    source_id: patch.anchor.source_id,
    source_name: labels.localKnowledge,
    label: patch.anchor.label || labels.knowledgeNode,
    description: patch.anchor.description || "",
    category: patch.anchor.category || "",
    start_time: patch.anchor.start_time ?? null,
    evidence: null,
    related_nodes: patch.nodes.map((node) => ({
      id: node.id,
      kind: node.kind,
      source_id: node.source_id,
      label: node.label,
      description: node.description,
      chunk_id: node.chunk_id,
      importance: node.importance,
    })),
    relations: patch.relations,
  };
}

function universeDetailKey(kind: "event" | "entity", id: string, sourceId: string) {
  return `${sourceId}:${kind}:${id}`;
}

export function PetMiniWorkspace({
  character,
  panelClassName,
  alignRight = false,
  panelAbove = true,
  panelView,
  onPanelViewChange,
  onClose,
}: {
  character: PetAgent;
  panelClassName?: string;
  alignRight?: boolean;
  panelAbove?: boolean;
  panelView: PetMiniView;
  onPanelViewChange: (view: PetMiniView) => void;
  onClose: () => void;
}) {
  const locale = useLocale();
  const t = useTranslations("PetMini");
  const nav = useTranslations("Navigation");
  const detailLabels = React.useMemo<MiniDetailLabels>(() => ({
    localKnowledge: t("detail.localKnowledge"),
    knowledgeNode: t("detail.knowledgeNode"),
    eventDetail: t("detail.eventDetail"),
  }), [t]);
  const pathname = usePathname();
  const conversationRuntime = useConversationRuntime();
  const {
    agent,
    appMode,
    user,
    workspaceSection,
    enterExploreMode,
    threads,
    timezone,
  } = useApp();
  const searchWorkspace = useSearchWorkspace();
  const routeThreadId = pathname.match(/^\/chat\/([^/]+)/)?.[1] ?? null;
  const [answerSessionId, setAnswerSessionId] = React.useState<string | null>(null);
  const answerSnapshot = useConversationSession(answerSessionId);
  const [answerHistoryOpen, setAnswerHistoryOpen] = React.useState(false);
  const [answerDraft, setAnswerDraft] = React.useState<{
    id: number;
    text: string;
  } | null>(null);
  const [detailTrail, setDetailTrail] = React.useState<MiniDetailTarget[]>([]);
  const [detail, setDetail] = React.useState<WorkspaceUniverseDetail | null>(null);
  const [detailLoading, setDetailLoading] = React.useState(false);
  const [detailError, setDetailError] = React.useState("");
  const detailTargetRef = React.useRef<MiniDetailTarget | null>(null);
  const detailPatchRef = React.useRef(new Map<string, UniverseGraphPatch>());
  const [panelSize, setPanelSize] = React.useState<MiniPanelSize>(DEFAULT_MINI_PANEL_SIZE);
  const observedRouteThreadRef = React.useRef<string | null>(null);
  const panelRef = React.useRef<HTMLDivElement>(null);
  const detailTarget = detailTrail[detailTrail.length - 1] ?? null;
  detailTargetRef.current = detailTarget;
  const eventNavigation = React.useMemo(() => {
    if (detailTarget?.kind !== "event" || !detailTarget.navigation?.items.length) {
      return null;
    }
    const navigation = detailTarget.navigation;
    const matchedIndex = navigation.items.findIndex((item) =>
      item.id === detailTarget.id && item.source_id === detailTarget.source_id);
    const index = matchedIndex >= 0
      ? matchedIndex
      : Math.min(Math.max(0, navigation.index), navigation.items.length - 1);
    return {
      index,
      total: navigation.items.length,
      previous: index > 0 ? index - 1 : null,
      next: index < navigation.items.length - 1 ? index + 1 : null,
    };
  }, [detailTarget]);
  const miniPanelStorageKey = `sag:mini-workspace-size:${user?.id ?? "local"}`;
  const answerRun = answerSnapshot?.run ?? null;
  const answerBusy = answerRun !== null;
  const answerIdentity = agent ?? character.getSnapshot().identity;

  const clampPanelSize = React.useCallback(
    (size: MiniPanelSize): MiniPanelSize => {
      if (typeof window === "undefined") return size;
      const anchor = panelRef.current?.parentElement?.getBoundingClientRect();
      const availableWidth = anchor
        ? alignRight
          ? anchor.right - 16
          : window.innerWidth - anchor.left - 16
        : window.innerWidth - 32;
      const availableHeight = anchor
        ? panelAbove
          ? anchor.top - 16
          : window.innerHeight - anchor.bottom - 16
        : window.innerHeight - 32;
      const maxWidth = Math.min(MINI_PANEL_MAX_SIZE.width, availableWidth);
      const maxHeight = Math.min(MINI_PANEL_MAX_SIZE.height, availableHeight);
      return {
        width: Math.round(
          clampPanelDimension(size.width, MINI_PANEL_MIN_SIZE.width, maxWidth),
        ),
        height: Math.round(
          clampPanelDimension(size.height, MINI_PANEL_MIN_SIZE.height, maxHeight),
        ),
      };
    },
    [alignRight, panelAbove],
  );

  React.useEffect(() => {
    let saved = DEFAULT_MINI_PANEL_SIZE;
    try {
      const raw = window.localStorage.getItem(miniPanelStorageKey);
      if (raw) {
        const value = JSON.parse(raw) as Partial<MiniPanelSize>;
        if (typeof value.width === "number" && typeof value.height === "number") {
          saved = { width: value.width, height: value.height };
        }
      }
    } catch {
      /* Keep the compact default when local preferences are unavailable. */
    }
    const frame = window.requestAnimationFrame(() => setPanelSize(clampPanelSize(saved)));
    return () => window.cancelAnimationFrame(frame);
  }, [clampPanelSize, miniPanelStorageKey]);

  React.useEffect(() => {
    const resize = () => setPanelSize((current) => clampPanelSize(current));
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, [clampPanelSize]);

  const openDetail = React.useCallback(
    (target: MiniDetailTarget, append = false) => {
      onPanelViewChange("workspace");
      setAnswerHistoryOpen(false);
      setDetailTrail((current) => {
        if (!append) return [target];
        const previous = current[current.length - 1];
        if (previous?.kind === target.kind && previous.id === target.id) return current;
        return [...current, target];
      });
    },
    [onPanelViewChange],
  );

  const openTimelineEvent = React.useCallback(
    (index: number) => {
      const current = detailTargetRef.current;
      if (current?.kind !== "event" || !current.navigation?.items.length) return;
      const item = current.navigation.items[index];
      if (!item) return;
      const navigation = { ...current.navigation, index };
      dispatchUniverseFocus(item.kind, item.id, item.source_id, { lock: true });
      openDetail({ ...item, navigation });
    },
    [openDetail],
  );

  const openSearchEvent = React.useCallback(
    (event: SearchEvent) => {
      if (!event.source_id) return;
      dispatchUniverseFocus("event", event.id, event.source_id);
      openDetail({ kind: "event", id: event.id, source_id: event.source_id });
    },
    [openDetail],
  );

  const openSearchCitation = React.useCallback(
    (citation: Citation, result: SearchResponse) => {
      const event = findCitationEvent(result, citation);
      if (event) {
        const sourceId = event.source_id || citation.source_id;
        if (!sourceId) return;
        dispatchUniverseFocus("event", event.id, sourceId);
        openDetail({ kind: "event", id: event.id, source_id: sourceId });
        return;
      }
      openDetail({
        kind: "citation",
        id: `${citation.source_id}:${citation.chunk_id}:${citation.n}`,
        citation,
      });
    },
    [openDetail],
  );

  const openActivityDocument = React.useCallback(
    (item: ActivityItem) => {
      if (!item.source_id) return;
      openDetail({
        kind: "document",
        id: item.id,
        source_id: item.source_id,
        title: item.title,
      });
    },
    [openDetail],
  );

  const onSearchStart = React.useCallback(
    (query: string) => character.search(t("search.searching", { query }), { duration: null }),
    [character, t],
  );

  const onSearchComplete = React.useCallback(
    (result: SearchResponse) => {
      character.complete(
        result.sections.length
          ? t("search.found", { count: result.sections.length })
          : t("search.noEvidence"),
      );
    },
    [character, t],
  );

  const onSearchError = React.useCallback(
    (message: string) => character.fail(message),
    [character],
  );

  const onSearchCancel = React.useCallback(
    () => character.idle(),
    [character],
  );

  const openAnswerCitation = React.useCallback(
    (citation: Citation, message: ConversationMessage) => {
      const node = message.universeActivation?.nodes.find(
        (item) =>
          item.kind === "event" &&
          (item.chunk_id === citation.chunk_id || item.citation_numbers?.includes(citation.n)),
      );
      if (node) {
        const sourceId = node.source_id || citation.source_id;
        if (sourceId) {
          dispatchUniverseFocus("event", node.id, sourceId);
          openDetail({ kind: "event", id: node.id, source_id: sourceId });
        }
        return;
      }
      openDetail({
        kind: "citation",
        id: `${citation.source_id}:${citation.chunk_id}:${citation.n}`,
        citation,
      });
    },
    [openDetail],
  );

  const openAnswerToolMatch = React.useCallback(
    (match: AgentActivityMatch) => {
      if (!match.chunk_id || !match.source_id) return;
      const citation: Citation = {
        n: match.n ?? 0,
        chunk_id: match.chunk_id,
        heading: match.heading ?? "",
        snippet: match.snippet ?? "",
        score: match.score ?? 0,
        source_id: match.source_id,
        source_name: match.source_name,
      };
      openDetail({
        kind: "citation",
        id: `${match.source_id}:${match.chunk_id}:${match.n ?? 0}`,
        citation,
      });
    },
    [openDetail],
  );

  React.useEffect(() => {
    const pending = takePendingUniverseDetail();
    if (pending) openDetail(pending);
    const onDetail = (event: Event) => {
      takePendingUniverseDetail();
      const target = (event as CustomEvent<UniverseDetailTarget>).detail;
      if (target) openDetail(target);
    };
    const onPatch = (event: Event) => {
      const patch = (event as CustomEvent<UniverseGraphPatch>).detail;
      if (!patch) return;
      const patchKey = universeDetailKey(
        patch.anchor.kind,
        patch.anchor.id,
        patch.anchor.source_id,
      );
      detailPatchRef.current.set(patchKey, patch);
      while (detailPatchRef.current.size > 24) {
        const oldest = detailPatchRef.current.keys().next().value;
        if (typeof oldest !== "string") break;
        detailPatchRef.current.delete(oldest);
      }
      const target = detailTargetRef.current;
      if (!target || target.kind === "citation" || target.kind === "document") return;
      const patchDetail = detailFromGraphPatch(patch, target, detailLabels);
      if (!patchDetail) return;
      setDetail((current) => current
        ? {
            ...current,
            related_nodes: patchDetail.related_nodes,
            relations: patchDetail.relations,
          }
        : patchDetail);
    };
    const onPatchReset = () => {
      detailPatchRef.current.clear();
    };
    window.addEventListener(UNIVERSE_DETAIL_EVENT, onDetail);
    window.addEventListener(UNIVERSE_PATCH_EVENT, onPatch);
    window.addEventListener(UNIVERSE_PATCH_RESET_EVENT, onPatchReset);
    return () => {
      window.removeEventListener(UNIVERSE_DETAIL_EVENT, onDetail);
      window.removeEventListener(UNIVERSE_PATCH_EVENT, onPatch);
      window.removeEventListener(UNIVERSE_PATCH_RESET_EVENT, onPatchReset);
    };
  }, [detailLabels, openDetail]);

  React.useEffect(() => {
    const openAsk = (target: UniverseAskTarget) => {
      onPanelViewChange("workspace");
      setAnswerHistoryOpen(false);
      setDetailTrail([]);
      setAnswerDraft({ id: target.request_id, text: target.prompt });
      enterExploreMode("answer");
    };
    const pending = takePendingUniverseAsk();
    if (pending) openAsk(pending);
    const onAsk = (event: Event) => {
      const target = (event as CustomEvent<UniverseAskTarget>).detail;
      takePendingUniverseAsk();
      if (target) openAsk(target);
    };
    window.addEventListener(UNIVERSE_ASK_EVENT, onAsk);
    return () => window.removeEventListener(UNIVERSE_ASK_EVENT, onAsk);
  }, [enterExploreMode, onPanelViewChange]);

  React.useEffect(() => {
    if (!detailTarget) {
      setDetail(null);
      setDetailError("");
      return;
    }
    if (detailTarget.kind === "citation" || detailTarget.kind === "document") {
      setDetail(null);
      setDetailError("");
      setDetailLoading(false);
      return;
    }
    let alive = true;
    const fallback = detailFromSearchResult(searchWorkspace.result, detailTarget, detailLabels);
    setDetail(null);
    setDetailError("");
    setDetailLoading(true);
    api
      .universeNode(detailTarget.kind, detailTarget.id, detailTarget.source_id)
      .then((value) => {
        if (alive) {
          const patchFallback = detailFromGraphPatch(
            detailPatchRef.current.get(
              universeDetailKey(detailTarget.kind, detailTarget.id, detailTarget.source_id),
            ) ?? null,
            detailTarget,
            detailLabels,
          );
          setDetail({
            ...value,
            related_nodes: patchFallback?.related_nodes ?? fallback?.related_nodes ?? [],
            relations: patchFallback?.relations ?? fallback?.relations ?? [],
          });
        }
      })
      .catch((reason) => {
        if (!alive) return;
        const patchFallback = detailFromGraphPatch(
          detailPatchRef.current.get(
            universeDetailKey(detailTarget.kind, detailTarget.id, detailTarget.source_id),
          ) ?? null,
          detailTarget,
          detailLabels,
        );
        if (patchFallback || fallback) {
          setDetail(patchFallback ?? fallback);
          return;
        }
        setDetailError(reason instanceof ApiError ? reason.message : t("detail.loadFailed"));
      })
      .finally(() => {
        if (alive) setDetailLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [detailLabels, detailTarget, searchWorkspace.result, t]);

  const startResize = React.useCallback(
    (event: React.PointerEvent<HTMLButtonElement>) => {
      event.preventDefault();
      event.stopPropagation();
      const startX = event.clientX;
      const startY = event.clientY;
      const startSize = panelSize;
      let nextSize = startSize;
      const previousCursor = document.documentElement.style.cursor;
      const previousSelect = document.documentElement.style.userSelect;
      const cursor = alignRight === panelAbove ? "nwse-resize" : "nesw-resize";
      document.documentElement.style.cursor = cursor;
      document.documentElement.style.userSelect = "none";

      const onMove = (moveEvent: PointerEvent) => {
        nextSize = clampPanelSize({
          width: startSize.width + (alignRight ? startX - moveEvent.clientX : moveEvent.clientX - startX),
          height: startSize.height + (panelAbove ? startY - moveEvent.clientY : moveEvent.clientY - startY),
        });
        setPanelSize(nextSize);
      };
      const onUp = () => {
        document.documentElement.style.cursor = previousCursor;
        document.documentElement.style.userSelect = previousSelect;
        try {
          window.localStorage.setItem(miniPanelStorageKey, JSON.stringify(nextSize));
        } catch {
          /* The resized panel still applies for this session. */
        }
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        window.removeEventListener("pointercancel", onUp);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp, { once: true });
      window.addEventListener("pointercancel", onUp, { once: true });
    },
    [alignRight, clampPanelSize, miniPanelStorageKey, panelAbove, panelSize],
  );

  const resizeWithKeyboard = React.useCallback(
    (event: React.KeyboardEvent<HTMLButtonElement>) => {
      const delta = event.shiftKey ? 32 : 16;
      const changes: Partial<MiniPanelSize> = {};
      if (event.key === "ArrowRight") changes.width = panelSize.width + delta;
      else if (event.key === "ArrowLeft") changes.width = panelSize.width - delta;
      else if (event.key === "ArrowDown") changes.height = panelSize.height + delta;
      else if (event.key === "ArrowUp") changes.height = panelSize.height - delta;
      else return;
      event.preventDefault();
      event.stopPropagation();
      const next = clampPanelSize({ ...panelSize, ...changes });
      setPanelSize(next);
      try {
        window.localStorage.setItem(miniPanelStorageKey, JSON.stringify(next));
      } catch {
        /* Keep the keyboard resize in memory. */
      }
    },
    [clampPanelSize, miniPanelStorageKey, panelSize],
  );

  React.useEffect(() => {
    if (routeThreadId !== observedRouteThreadRef.current) {
      observedRouteThreadRef.current = routeThreadId;
      if (routeThreadId) {
        const sessionId = conversationRuntime.forThread(routeThreadId);
        setAnswerSessionId((current) => (current === sessionId ? current : sessionId));
        return;
      }
    }
    const activeSessionId = !answerSessionId
      ? conversationRuntime.getIndexSnapshot().activeSessionId
      : null;
    if (activeSessionId) {
      setAnswerSessionId(activeSessionId);
      return;
    }
    const preferredThreadId = !answerSessionId
      ? [...threads].sort(
          (left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at),
        )[0]?.id
      : null;
    if (!preferredThreadId) {
      if (workspaceSection === "answer" && !answerSessionId) {
        setAnswerSessionId(conversationRuntime.createDraft({ activate: true }));
      }
      return;
    }
    const sessionId = conversationRuntime.forThread(preferredThreadId);
    setAnswerSessionId((current) => (current === sessionId ? current : sessionId));
  }, [answerSessionId, conversationRuntime, routeThreadId, threads, workspaceSection]);

  React.useEffect(() => {
    if (workspaceSection !== "answer" || !answerSessionId) return;
    conversationRuntime.activate(answerSessionId);
    void conversationRuntime.ensureHistory(answerSessionId);
  }, [answerSessionId, conversationRuntime, workspaceSection]);

  const recentThreads = React.useMemo(
    () =>
      [...threads]
        .sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at))
        .slice(0, 6),
    [threads],
  );
  const answerTitle = React.useMemo(() => {
    if (!answerSnapshot?.threadId) return t("answer.newConversation");
    return (
      threads.find((thread) => thread.id === answerSnapshot.threadId)?.title ||
      t("answer.currentConversation")
    );
  }, [answerSnapshot?.threadId, t, threads]);
  const searchTitle =
    searchWorkspace.lastQuery || searchWorkspace.query.trim() || t("search.title");

  const activateAnswerThread = React.useCallback(
    (threadId: string) => {
      const sessionId = conversationRuntime.forThread(threadId, { activate: true });
      setAnswerSessionId(sessionId);
      setAnswerHistoryOpen(false);
      void conversationRuntime.ensureHistory(sessionId);
    },
    [conversationRuntime],
  );

  React.useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      const viewport = panelRef.current?.querySelector<HTMLElement>(
        "[data-radix-scroll-area-viewport]",
      );
      viewport?.scrollTo({ top: 0 });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [detailTarget?.id, detailTarget?.kind, workspaceSection]);

  const changeSection = (section: WorkspaceSection) => {
    onPanelViewChange("workspace");
    if (section === workspaceSection) return;
    setAnswerHistoryOpen(false);
    enterExploreMode(section);
  };

  const returnToSearchHome = () => {
    onPanelViewChange("workspace");
    setAnswerHistoryOpen(false);
    setDetailTrail([]);
    if (workspaceSection !== "search") enterExploreMode("search");
  };

  const returnToExploration = React.useCallback(() => {
    setAnswerHistoryOpen(false);
    setDetailTrail([]);
    dispatchUniverseResume();
    onClose();
  }, [onClose]);

  const newAnswer = () => {
    if (answerBusy) return;
    observedRouteThreadRef.current = routeThreadId;
    const sessionId = conversationRuntime.createDraft({ activate: true });
    setAnswerSessionId(sessionId);
    setAnswerHistoryOpen(false);
  };

  const answerHistory = (
    <AnimatePresence initial={false}>
      {answerHistoryOpen && (
        <motion.div
          initial={{ opacity: 0, y: -6, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: -4, scale: 0.98 }}
          transition={{ duration: 0.16 }}
          className="absolute inset-x-2 top-12 z-30 max-h-[min(15rem,calc(100%-3.5rem))] overflow-y-auto rounded-lg border bg-popover p-1 shadow-xl"
        >
          {recentThreads.length ? (
            recentThreads.map((thread) => (
              <button
                key={thread.id}
                type="button"
                onClick={() => activateAnswerThread(thread.id)}
                className={cn(
                  "flex h-9 w-full items-center rounded-md px-2.5 text-left text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
                  answerSnapshot?.threadId === thread.id && "bg-muted text-foreground",
                )}
              >
                <span className="min-w-0 flex-1 truncate">
                  {thread.title || t("answer.untitled")}
                </span>
              </button>
            ))
          ) : (
            <p className="px-2 py-4 text-center text-xs text-muted-foreground">
              {t("answer.noHistory")}
            </p>
          )}
        </motion.div>
      )}
    </AnimatePresence>
  );

  return (
    <motion.div
      ref={panelRef}
      data-mini-workspace="true"
      layoutId="sag-workspace-panel"
      initial={{ opacity: 0, scale: 0.94, y: 7 }}
      animate={{ opacity: 1, scale: 1, y: 0 }}
      exit={{ opacity: 0, scale: 0.96, y: 5 }}
      transition={{ type: "spring", stiffness: 380, damping: 28 }}
      style={{ width: panelSize.width, height: panelSize.height }}
      className={cn(
        "absolute flex max-h-[calc(100dvh-2rem)] max-w-[calc(100vw-2rem)] flex-col overflow-hidden rounded-lg border bg-popover/96 shadow-2xl backdrop-blur-xl",
        panelClassName,
      )}
      onPointerDown={(event) => event.stopPropagation()}
    >
      <div
        className={cn(
          "flex h-12 shrink-0 items-center gap-2 border-b px-2.5",
          panelAbove && alignRight && "pl-7",
          panelAbove && !alignRight && "pr-7",
        )}
      >
        {panelView === "assistant-settings" ? (
          <>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="size-7"
              onClick={() => onPanelViewChange("workspace")}
              aria-label={t("controls.backToWorkspace")}
              title={t("controls.backToWorkspace")}
            >
              <ArrowLeft className="size-3.5" />
            </Button>
            <SlidersHorizontal className="size-3.5 shrink-0 text-muted-foreground" />
            <span className="min-w-0 flex-1 truncate text-xs font-medium">
              {t("controls.assistantSettings")}
            </span>
          </>
        ) : detailTarget ? (
          <>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="size-7"
              onClick={() =>
                setDetailTrail((current) =>
                  current.length > 1 ? current.slice(0, -1) : [],
                )
              }
              aria-label={t("controls.back")}
              title={t("controls.back")}
            >
              <ArrowLeft className="size-3.5" />
            </Button>
            {detailTarget.kind === "event" ? (
              <SparklesDot />
            ) : detailTarget.kind === "entity" ? (
              <CircleDot className="size-3.5 shrink-0 text-cyan-500" />
            ) : (
              <FileText className="size-3.5 shrink-0 text-muted-foreground" />
            )}
            <span className="min-w-0 flex-1 truncate text-xs font-medium">
              {detail?.label ??
                (detailTarget.kind === "document"
                  ? detailTarget.title
                  : detailTarget.kind === "citation"
                    ? detailTarget.citation.heading || detailTarget.citation.source_name || t("detail.citationTitle")
                    : t("detail.nodeTitle"))}
            </span>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="size-7"
              onClick={returnToSearchHome}
              aria-label={t("controls.searchHome")}
              title={t("controls.searchHome")}
            >
              <Search className="size-3.5" />
            </Button>
          </>
        ) : (
          <>
            <div className="grid min-w-0 flex-1 grid-cols-3 rounded-md bg-muted p-0.5">
              {WORKSPACE_SECTIONS.map((item) => {
                return (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => changeSection(item.id)}
                    className={cn(
                      "relative flex h-7 min-w-0 items-center justify-center gap-1 overflow-hidden rounded text-[11px] text-muted-foreground transition-colors",
                      workspaceSection === item.id && "text-foreground",
                    )}
                    aria-pressed={workspaceSection === item.id}
                  >
                    {workspaceSection === item.id && (
                      <motion.span
                        layoutId="sag-mini-workspace-section"
                        className="absolute inset-0 rounded bg-background shadow-sm"
                        transition={{ type: "spring", stiffness: 430, damping: 34 }}
                      />
                    )}
                    <WorkspaceSectionIcon
                      section={item.id}
                      className="relative size-3.5 shrink-0"
                    />
                    <span className="relative truncate">{nav(item.id)}</span>
                  </button>
                );
              })}
            </div>
          </>
        )}
        {panelView !== "assistant-settings" && (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="size-7"
            onClick={() => {
              setAnswerHistoryOpen(false);
              setDetailTrail([]);
              onPanelViewChange("assistant-settings");
            }}
            aria-label={t("controls.assistantSettings")}
            title={t("controls.assistantSettings")}
          >
            <SlidersHorizontal className="size-3.5" />
          </Button>
        )}
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-7"
          onClick={appMode === "explore" ? returnToExploration : onClose}
          aria-label={t(
            appMode === "explore" ? "controls.returnToExplore" : "controls.close",
          )}
          title={t(
            appMode === "explore" ? "controls.returnToExplore" : "controls.close",
          )}
        >
          <X className="size-3.5" />
        </Button>
      </div>

      <div
        className={cn(
          "min-h-0 flex-1",
          panelView !== "assistant-settings" && "hidden",
        )}
        aria-hidden={panelView !== "assistant-settings"}
      >
        <ScrollArea className="h-full">
          <div className="p-3">
            <AgentSettingsCard compact />
          </div>
        </ScrollArea>
      </div>

      <div
        className={cn(
          "min-h-0 flex-1",
          (panelView === "assistant-settings"
            || detailTarget
            || workspaceSection !== "search") && "hidden",
        )}
        aria-hidden={panelView === "assistant-settings"
          || Boolean(detailTarget)
          || workspaceSection !== "search"}
      >
        <div className="relative flex h-full min-h-0 flex-col">
          <div className="flex h-11 shrink-0 items-center gap-1.5 border-b px-3">
            <div className="min-w-0 flex-1">
              <p className="truncate text-xs font-medium">{searchTitle}</p>
              <p className="text-[10px] text-muted-foreground">{nav("search")}</p>
            </div>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className={cn(
                "size-7",
                searchWorkspace.contentView === "history" && "bg-muted text-foreground",
              )}
              onClick={() => searchWorkspace.setContentView("history")}
              disabled={searchWorkspace.busy}
              aria-label={t("search.history")}
              title={t("search.history")}
              aria-pressed={searchWorkspace.contentView === "history"}
            >
              <History className="size-3.5" />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className={cn(
                "size-7",
                searchWorkspace.contentView === "activity" && "bg-muted text-foreground",
              )}
              onClick={() => searchWorkspace.setContentView("activity")}
              disabled={searchWorkspace.busy}
              aria-label={t("search.recentActivity")}
              title={t("search.recentActivity")}
              aria-pressed={searchWorkspace.contentView === "activity"}
            >
              <List className="size-3.5" />
            </Button>
          </div>
          <div className="min-h-0 flex-1">
            <SearchPanel
              active={panelView === "workspace"
                && !detailTarget
                && workspaceSection === "search"}
              showGraphView={false}
              showRecentActivity
              showContentSwitcher={false}
              onActivityClick={openActivityDocument}
              onSearchStart={onSearchStart}
              onSearchComplete={onSearchComplete}
              onSearchError={onSearchError}
              onSearchCancel={onSearchCancel}
              onEventClick={openSearchEvent}
              onCitationClick={openSearchCitation}
            />
          </div>
        </div>
      </div>

      <div
        className={cn(
          "min-h-0 flex-1",
          (panelView === "assistant-settings"
            || detailTarget
            || workspaceSection !== "answer") && "hidden",
        )}
        aria-hidden={panelView === "assistant-settings"
          || Boolean(detailTarget)
          || workspaceSection !== "answer"}
      >
        <div className="relative flex h-full min-h-0 flex-col">
          <div className="flex h-11 shrink-0 items-center gap-1.5 border-b px-3">
            <div className="min-w-0 flex-1">
              <p className="truncate text-xs font-medium">{answerTitle}</p>
              <p className="text-[10px] text-muted-foreground">{nav("answer")}</p>
            </div>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="size-7"
              onClick={() => setAnswerHistoryOpen((current) => !current)}
              aria-label={t("answer.history")}
              title={t("answer.history")}
              aria-pressed={answerHistoryOpen}
            >
              <History className="size-3.5" />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="size-7"
              onClick={newAnswer}
              disabled={answerBusy}
              aria-label={t("answer.newConversation")}
              title={t("answer.newConversation")}
            >
              <MessageSquarePlus className="size-3.5" />
            </Button>
          </div>
          {answerHistory}
          <div className="min-h-0 flex-1">
            {answerSessionId ? (
              <ConversationPanel
                key={answerSessionId}
                sessionId={answerSessionId}
                active={panelView === "workspace"
                  && !detailTarget
                  && workspaceSection === "answer"}
                showPromptPreview={false}
                avatarNode={(
                  <PetHeadAvatar
                    face={answerIdentity.avatar}
                    size="sm"
                    className="mt-0.5"
                  />
                )}
                heroNode={<PetHeadAvatar face={answerIdentity.avatar} size="lg" />}
                emptyTitle={answerIdentity.name}
                emptyHint={agent?.persona?.greeting || t("answer.emptyHint")}
                suggestions={[
                  t("answer.suggestions.summary"),
                  t("answer.suggestions.conclusions"),
                  t("answer.suggestions.timeline"),
                ]}
                draftPrompt={answerDraft}
                placeholder={t("answer.placeholder", { name: answerIdentity.name })}
                onCitationClick={openAnswerCitation}
                onToolMatchClick={openAnswerToolMatch}
              />
            ) : (
              <div className="flex h-full items-center justify-center gap-2 text-xs text-muted-foreground">
                <Loader2 className="size-3.5 animate-spin" />
                {t("answer.loading")}
              </div>
            )}
          </div>
        </div>
      </div>

      <div
        className={cn(
          "min-h-0 flex-1",
          (panelView === "assistant-settings"
            || detailTarget
            || workspaceSection !== "knowledge") && "hidden",
        )}
        aria-hidden={panelView === "assistant-settings"
          || Boolean(detailTarget)
          || workspaceSection !== "knowledge"}
      >
        <KnowledgeWorkspace
          variant="compact"
          active={panelView === "workspace"
            && !detailTarget
            && workspaceSection === "knowledge"}
        />
      </div>

      <div
        className={cn(
          "min-h-0 flex-1",
          (panelView === "assistant-settings" || !detailTarget) && "hidden",
        )}
      >
        {detailTarget?.kind === "document" ? (
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={`${detailTarget.kind}:${detailTarget.id}`}
              initial={{ opacity: 0, x: 10 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -10 }}
              className="flex h-full min-h-0 flex-col overflow-hidden"
            >
              <CompactDocumentDetailWorkspace
                sourceId={detailTarget.source_id}
                documentId={detailTarget.id}
              />
            </motion.div>
          </AnimatePresence>
        ) : (
          <div className="flex h-full min-h-0 flex-col">
            <ScrollArea className="min-h-0 flex-1">
              <AnimatePresence mode="wait" initial={false}>
                {detailTarget ? (
                  <motion.div
                    key={`${detailTarget.kind}:${detailTarget.id}`}
                    initial={{ opacity: 0, x: 10 }}
                    animate={{ opacity: 1, x: 0 }}
                    exit={{ opacity: 0, x: -10 }}
                    className="p-4"
                  >
                  {detailTarget.kind === "citation" ? (
                    <div className="space-y-4">
                      <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
                        <FileText className="size-3.5" />
                        <span className="min-w-0 flex-1 truncate">
                          {detailTarget.citation.source_name || t("detail.localKnowledge")}
                        </span>
                      </div>
                      {detailTarget.citation.heading && (
                        <h2 className="text-sm font-medium leading-5">
                          {detailTarget.citation.heading}
                        </h2>
                      )}
                      <p className="whitespace-pre-wrap text-xs leading-5 text-foreground/75">
                        {stripCitationTransportTokens(detailTarget.citation.snippet)
                          || t("detail.noCitationSource")}
                      </p>
                    </div>
                  ) : detailLoading && !detail ? (
                    <div className="flex h-52 items-center justify-center gap-2 text-xs text-muted-foreground">
                      <Loader2 className="size-4 animate-spin" />
                      {t("detail.loading")}
                    </div>
                  ) : detail ? (
                    <div className="space-y-5">
                      <section>
                        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground">
                          <span>{detail.source_name || t("detail.localKnowledge")}</span>
                          {detail.category && <span>· {detail.category}</span>}
                          {detail.start_time && (
                            <span>· {formatDate(detail.start_time, timezone, { dateStyle: "medium" }, locale)}</span>
                          )}
                        </div>
                        <h2 className="mt-2 text-sm font-medium leading-5">{detail.label}</h2>
                        {detail.description && (
                          <div className="mt-3">
                            {detailTarget.kind === "event" && (
                              <p className="text-[10px] font-medium tracking-wide text-muted-foreground/75">
                                {detailLabels.eventDetail}
                              </p>
                            )}
                            <p className="mt-1 whitespace-pre-wrap text-xs leading-5 text-foreground/75">
                              {detail.description}
                            </p>
                          </div>
                        )}
                      </section>

                      {detail.related_nodes.length > 0 && (
                        <section className="border-t pt-4">
                          <h3 className="text-[11px] font-medium text-muted-foreground">
                            {t("detail.relatedNodes")}
                          </h3>
                          <div className="mt-2 space-y-1">
                            {detail.related_nodes.slice(0, 12).map((node) => (
                              <button
                                key={`${node.kind}:${node.id}`}
                                type="button"
                                onClick={() => {
                                  dispatchUniverseFocus(node.kind, node.id, node.source_id);
                                  openDetail(
                                    { kind: node.kind, id: node.id, source_id: node.source_id },
                                    true,
                                  );
                                }}
                                className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                              >
                                {node.kind === "event" ? (
                                  <SparklesDot />
                                ) : (
                                  <CircleDot className="size-3 shrink-0 text-cyan-500" />
                                )}
                                <span className="min-w-0 flex-1 truncate">{node.label}</span>
                              </button>
                            ))}
                          </div>
                        </section>
                      )}

                      {detail.evidence && (
                        <section className="border-t pt-4">
                          <div className="flex items-center justify-between gap-3">
                            <h3 className="text-[11px] font-medium text-muted-foreground">
                              {t("detail.sourceEvidence")}
                            </h3>
                            <span className="max-w-44 truncate text-[10px] text-muted-foreground/70">
                              {detail.evidence.document_name || detail.evidence.source_name}
                            </span>
                          </div>
                          {detail.evidence.heading && (
                            <p className="mt-2 text-xs font-medium">{detail.evidence.heading}</p>
                          )}
                          <p className="mt-2 whitespace-pre-wrap text-xs leading-5 text-foreground/70">
                            {stripCitationTransportTokens(detail.evidence.content)}
                          </p>
                        </section>
                      )}
                    </div>
                  ) : (
                    <div className="flex h-52 items-center justify-center text-xs text-destructive">
                      {detailError || t("detail.unavailable")}
                    </div>
                  )}
                  </motion.div>
                ) : null}
              </AnimatePresence>
            </ScrollArea>
            {eventNavigation && (
              <div className="flex h-11 shrink-0 items-center justify-between gap-2 border-t px-2">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-8 gap-1 px-2 text-xs"
                  disabled={eventNavigation.previous === null}
                  onClick={() => {
                    if (eventNavigation.previous !== null) {
                      openTimelineEvent(eventNavigation.previous);
                    }
                  }}
                  aria-label={t("detail.previousEvent")}
                  title={t("detail.previousEvent")}
                >
                  <ChevronLeft className="size-3.5" />
                  {t("detail.previousEvent")}
                </Button>
                <span className="whitespace-nowrap text-[10px] text-muted-foreground">
                  {t("detail.eventPosition", {
                    current: eventNavigation.index + 1,
                    total: eventNavigation.total,
                  })}
                </span>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-8 gap-1 px-2 text-xs"
                  disabled={eventNavigation.next === null}
                  onClick={() => {
                    if (eventNavigation.next !== null) {
                      openTimelineEvent(eventNavigation.next);
                    }
                  }}
                  aria-label={t("detail.nextEvent")}
                  title={t("detail.nextEvent")}
                >
                  {t("detail.nextEvent")}
                  <ChevronRight className="size-3.5" />
                </Button>
              </div>
            )}
          </div>
        )}
      </div>

      <button
        type="button"
        onPointerDown={startResize}
        onKeyDown={resizeWithKeyboard}
        aria-label={t("controls.resize")}
        title={t("controls.resize")}
        className={cn(
          "absolute z-30 flex size-6 touch-none items-center justify-center text-muted-foreground/45 transition-colors hover:text-foreground",
          panelAbove ? "top-0" : "bottom-0",
          alignRight ? "left-0" : "right-0",
          alignRight === panelAbove ? "cursor-nwse-resize" : "cursor-nesw-resize",
        )}
      >
        <Grip className="size-3.5 rotate-45" />
      </button>
    </motion.div>
  );
}

function SparklesDot() {
  return (
    <span className="mt-1 size-2 shrink-0 rounded-full bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.65)]" />
  );
}
