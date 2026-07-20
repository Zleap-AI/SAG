import type {
  SearchResponse,
  UniverseActivation,
  UniverseActivationNode,
  UniverseGraphPatch,
  UniverseRelation,
} from "./types";
import { clientErrorMessage } from "../i18n/client-errors";

export const UNIVERSE_ACTIVATE_EVENT = "sag:universe-activate";
export const UNIVERSE_RESET_EVENT = "sag:universe-reset";
export const UNIVERSE_FOCUS_EVENT = "sag:universe-focus";
export const UNIVERSE_SOURCE_FOCUS_EVENT = "sag:universe-source-focus";
export const UNIVERSE_DETAIL_EVENT = "sag:universe-detail";
export const UNIVERSE_ASK_EVENT = "sag:universe-ask";
export const UNIVERSE_INTERACTION_EVENT = "sag:universe-interaction";
export const UNIVERSE_RESUME_EVENT = "sag:universe-resume";
export const UNIVERSE_CONTEXT_EVENT = "sag:universe-context";
export const UNIVERSE_PATCH_EVENT = "sag:universe-patch";
export const UNIVERSE_PATCH_RESET_EVENT = "sag:universe-patch-reset";
export const UNIVERSE_VIEW_EVENT = "sag:universe-view";
export const UNIVERSE_PRESENTATION_EVENT = "sag:universe-presentation";

export type UniversePresentationMode = "exploration" | "accumulation";

export interface UniverseViewState {
  mode: "overview" | "detail";
  source_id: string | null;
  progress: number;
}

export interface UniverseContextState {
  active: boolean;
  section: "search" | "answer" | null;
}

export interface UniverseDetailTimelineItem {
  kind: "event";
  id: string;
  source_id: string;
}

export interface UniverseDetailTimelineNavigation {
  items: UniverseDetailTimelineItem[];
  index: number;
}

export interface UniverseDetailTarget {
  kind: "event" | "entity";
  id: string;
  source_id: string;
  navigation?: UniverseDetailTimelineNavigation;
}

export interface UniverseAskTarget extends UniverseDetailTarget {
  request_id: number;
  label: string;
  prompt: string;
}

interface UniverseAskNode {
  kind: "event" | "entity";
  rawId: string;
  sourceId: string;
  label: string;
}

let pendingUniverseDetail: UniverseDetailTarget | null = null;
let pendingUniverseAsk: UniverseAskTarget | null = null;
let universeEpoch = 0;
let universeAskSequence = 0;
let currentUniverseView: UniverseViewState = {
  mode: "overview",
  source_id: null,
  progress: 0,
};
let currentUniverseContext: UniverseContextState = {
  active: false,
  section: null,
};
let currentUniversePresentation: UniversePresentationMode = "exploration";

export function readUniverseView() {
  return currentUniverseView;
}

export function readUniverseContext() {
  return currentUniverseContext;
}

export function readUniversePresentation() {
  return currentUniversePresentation;
}

export function dispatchUniversePresentation(mode: UniversePresentationMode) {
  if (mode === currentUniversePresentation) return currentUniversePresentation;
  currentUniversePresentation = mode;
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent<UniversePresentationMode>(
      UNIVERSE_PRESENTATION_EVENT,
      { detail: mode },
    ));
  }
  return currentUniversePresentation;
}

export function dispatchUniverseContext(context: UniverseContextState) {
  const next: UniverseContextState = context.active && context.section
    ? { active: true, section: context.section }
    : { active: false, section: null };
  if (
    next.active === currentUniverseContext.active
    && next.section === currentUniverseContext.section
  ) return currentUniverseContext;
  currentUniverseContext = next;
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent<UniverseContextState>(
      UNIVERSE_CONTEXT_EVENT,
      { detail: next },
    ));
  }
  return next;
}

export function dispatchUniverseView(view: UniverseViewState) {
  const progress = Number.isFinite(view.progress)
    ? Math.min(1, Math.max(0, view.progress))
    : 0;
  const sourceId = view.mode === "detail" ? view.source_id : null;
  const next: UniverseViewState = {
    mode: sourceId ? "detail" : "overview",
    source_id: sourceId,
    progress,
  };
  if (
    next.mode === currentUniverseView.mode
    && next.source_id === currentUniverseView.source_id
    && Math.abs(next.progress - currentUniverseView.progress) < 0.001
  ) {
    return currentUniverseView;
  }
  currentUniverseView = next;
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent<UniverseViewState>(UNIVERSE_VIEW_EVENT, { detail: next }));
  }
  return next;
}

/** Signals a real canvas gesture so contextual overlays can yield the map. */
export function dispatchUniverseInteraction() {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(UNIVERSE_INTERACTION_EVENT));
}

/** Leaves a contextual search/answer workspace and resumes the retained graph. */
export function dispatchUniverseResume() {
  if (typeof window === "undefined") return;
  dispatchUniverseContext({ active: false, section: null });
  window.dispatchEvent(new Event(UNIVERSE_RESUME_EVENT));
}

export function dispatchUniverseActivation(
  activation: UniverseActivation,
  expectedEpoch?: number,
) {
  if (typeof window === "undefined") return 0;
  if (expectedEpoch !== undefined && expectedEpoch !== universeEpoch) return 0;
  const detail: UniverseActivation = {
    epoch: ++universeEpoch,
    origin: activation.origin ?? "assistant",
    query: activation.query,
    nodes: activation.nodes,
    relations: activation.relations,
    source_hits: activation.source_hits,
  };
  window.dispatchEvent(new CustomEvent(UNIVERSE_ACTIVATE_EVENT, { detail }));
  return detail.epoch ?? universeEpoch;
}

export function dispatchUniverseReset(owner = "search") {
  if (typeof window === "undefined") return 0;
  const epoch = ++universeEpoch;
  window.dispatchEvent(
    new CustomEvent(UNIVERSE_RESET_EVENT, { detail: { epoch, owner, camera: "overview" } }),
  );
  return epoch;
}

export function activationFromSearch(response: SearchResponse): UniverseActivation {
  const eventNodes: UniverseActivationNode[] = response.events.map((event) => ({
    id: event.id,
    kind: "event",
    source_id: event.source_id,
    label: event.title,
    description: event.summary,
    category: event.category,
    chunk_id: event.chunk_id,
    importance: event.score,
    state: "active",
  }));
  const sourceByEvent = new Map(
    response.events.map((event) => [event.id, event.source_id ?? ""]),
  );
  const eventById = new Map(response.events.map((event) => [event.id, event]));
  const sourcesByEntity = new Map<string, Set<string>>();
  response.relations.forEach((relation) => {
    if (relation.target_kind !== "entity") return;
    const event = eventById.get(relation.source_id);
    if (!event?.source_id) return;
    const sources = sourcesByEntity.get(relation.target_id) ?? new Set<string>();
    sources.add(event.source_id);
    sourcesByEntity.set(relation.target_id, sources);
  });
  const entityNodes: UniverseActivationNode[] = response.entities.flatMap((entity) => {
    const sourceIds = [...(sourcesByEntity.get(entity.id) ?? [])];
    const projections: Array<string | null> = sourceIds.length ? sourceIds : [null];
    return projections.map((sourceId) => ({
      id: entity.id,
      kind: "entity",
      source_id: sourceId,
      label: entity.name,
      description: entity.description,
      category: entity.type,
      importance: Math.min(1, 0.2 + entity.heat / 10),
      state: "active",
    }));
  });
  return {
    origin: "search",
    query: response.query,
    nodes: [...eventNodes, ...entityNodes],
    relations: response.relations.map(
      (relation): UniverseRelation => ({
        source_id: sourceByEvent.get(relation.source_id) ?? "",
        from_id: relation.source_id,
        to_id: relation.target_id,
        kind: relation.kind === "subevent" ? "subevent" : "mentions",
        weight: relation.weight,
        description: relation.description,
      }),
    ),
    source_hits: response.source_hits,
  };
}

export function dispatchUniverseFocus(
  kind: "event" | "entity",
  id: string,
  sourceId: string,
  options?: { lock?: boolean },
) {
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent(UNIVERSE_FOCUS_EVENT, {
      detail: { kind, id, source_id: sourceId, lock: options?.lock === true },
    }),
  );
}

export function dispatchUniverseSourceFocus(sourceId: string) {
  if (typeof window === "undefined" || !sourceId) return;
  window.dispatchEvent(
    new CustomEvent(UNIVERSE_SOURCE_FOCUS_EVENT, {
      detail: { source_id: sourceId },
    }),
  );
}

export function dispatchUniverseDetail(
  kind: "event" | "entity",
  id: string,
  sourceId: string,
  navigation?: UniverseDetailTimelineNavigation,
) {
  if (typeof window === "undefined") return;
  pendingUniverseDetail = { kind, id, source_id: sourceId, navigation };
  window.dispatchEvent(
    new CustomEvent<UniverseDetailTarget>(UNIVERSE_DETAIL_EVENT, {
      detail: pendingUniverseDetail,
    }),
  );
}

export function dispatchUniverseAsk(node: UniverseAskNode) {
  if (typeof window === "undefined") return;
  const detail: UniverseAskTarget = {
    request_id: ++universeAskSequence,
    kind: node.kind,
    id: node.rawId,
    source_id: node.sourceId,
    label: node.label,
    prompt: node.kind === "entity"
      ? clientErrorMessage("askEntity", { label: node.label })
      : clientErrorMessage("askEvent", { label: node.label }),
  };
  pendingUniverseAsk = detail;
  window.dispatchEvent(new CustomEvent<UniverseAskTarget>(UNIVERSE_ASK_EVENT, { detail }));
}

export function dispatchUniversePatch(patch: UniverseGraphPatch) {
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent<UniverseGraphPatch>(UNIVERSE_PATCH_EVENT, { detail: patch }),
  );
}

/** Invalidates snapshot-bound detail fallbacks without changing graph epoch or camera. */
export function dispatchUniversePatchReset() {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(UNIVERSE_PATCH_RESET_EVENT));
}

export function takePendingUniverseDetail() {
  const target = pendingUniverseDetail;
  pendingUniverseDetail = null;
  return target;
}

export function takePendingUniverseAsk() {
  const target = pendingUniverseAsk;
  pendingUniverseAsk = null;
  return target;
}
