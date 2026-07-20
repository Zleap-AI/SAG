import type { UniverseActivationOrigin } from "./types";

export type UniverseWorkspaceMode =
  | "home"
  | "exploration"
  | "accumulation";

export interface UniverseSessionState {
  mode: UniverseWorkspaceMode;
  sourceId: string | null;
  explorationSnapshotAvailable: boolean;
  evidenceBatchCount: number;
  evidenceOrigin: UniverseActivationOrigin | null;
  selectedKey: string | null;
  lockedKey: string | null;
  detailKey: string | null;
  revision: number;
}

export type UniverseSessionAction =
  | { type: "GO_HOME" }
  | { type: "ENTER_EXPLORATION"; sourceId: string }
  | {
      type: "ENTER_ACCUMULATION";
      origin: UniverseActivationOrigin;
      sourceId?: string | null;
      snapshotAvailable?: boolean;
    }
  | {
      type: "APPEND_EVIDENCE";
      origin: UniverseActivationOrigin;
      addedEvents: number;
    }
  | { type: "RETURN_TO_EXPLORATION"; sourceId?: string | null }
  | { type: "SELECT"; key: string | null }
  | { type: "LOCK"; key: string | null }
  | { type: "TOGGLE_LOCK"; key: string }
  | { type: "OPEN_DETAIL"; key: string }
  | { type: "CLEAR_FOCUS" };

export function createUniverseSessionState(): UniverseSessionState {
  return {
    mode: "home",
    sourceId: null,
    explorationSnapshotAvailable: false,
    evidenceBatchCount: 0,
    evidenceOrigin: null,
    selectedKey: null,
    lockedKey: null,
    detailKey: null,
    revision: 0,
  };
}

function nextRevision(state: UniverseSessionState) {
  return state.revision + 1;
}

function clearFocus(state: UniverseSessionState): UniverseSessionState {
  if (
    state.selectedKey === null
    && state.lockedKey === null
    && state.detailKey === null
  ) return state;
  return {
    ...state,
    selectedKey: null,
    lockedKey: null,
    detailKey: null,
    revision: nextRevision(state),
  };
}

/**
 * The only authority for page/workspace state. Search, assistant and expansion
 * are evidence origins; none of them implicitly defines the presentation mode.
 */
export function reduceUniverseSession(
  state: UniverseSessionState,
  action: UniverseSessionAction,
): UniverseSessionState {
  switch (action.type) {
    case "GO_HOME":
      return {
        ...createUniverseSessionState(),
        revision: nextRevision(state),
      };
    case "ENTER_EXPLORATION": {
      const sourceId = action.sourceId.trim();
      if (!sourceId) return state;
      return {
        ...createUniverseSessionState(),
        mode: "exploration",
        sourceId,
        revision: nextRevision(state),
      };
    }
    case "ENTER_ACCUMULATION": {
      const snapshotAvailable = action.snapshotAvailable
        ?? state.mode === "exploration";
      const sourceId = action.sourceId === undefined
        ? state.sourceId
        : action.sourceId;
      return {
        ...state,
        mode: "accumulation",
        sourceId,
        explorationSnapshotAvailable: snapshotAvailable,
        evidenceBatchCount: 0,
        evidenceOrigin: action.origin,
        selectedKey: null,
        lockedKey: null,
        detailKey: null,
        revision: nextRevision(state),
      };
    }
    case "APPEND_EVIDENCE": {
      const accumulation = state.mode === "accumulation"
        ? state
        : reduceUniverseSession(state, {
            type: "ENTER_ACCUMULATION",
            origin: action.origin,
          });
      if (action.addedEvents <= 0) {
        return accumulation.evidenceOrigin === action.origin
          ? accumulation
          : {
              ...accumulation,
              evidenceOrigin: action.origin,
              revision: nextRevision(accumulation),
            };
      }
      return {
        ...accumulation,
        evidenceOrigin: action.origin,
        evidenceBatchCount: accumulation.evidenceBatchCount + 1,
        selectedKey: null,
        lockedKey: null,
        detailKey: null,
        revision: nextRevision(accumulation),
      };
    }
    case "RETURN_TO_EXPLORATION": {
      const sourceId = action.sourceId ?? state.sourceId;
      if (!state.explorationSnapshotAvailable || !sourceId) {
        return reduceUniverseSession(state, { type: "GO_HOME" });
      }
      return {
        ...createUniverseSessionState(),
        mode: "exploration",
        sourceId,
        revision: nextRevision(state),
      };
    }
    case "SELECT": {
      if (state.selectedKey === action.key) return state;
      const changesLock = Boolean(
        state.lockedKey && state.lockedKey !== action.key,
      );
      return {
        ...state,
        selectedKey: action.key,
        lockedKey: changesLock ? null : state.lockedKey,
        detailKey: changesLock ? null : state.detailKey,
        revision: nextRevision(state),
      };
    }
    case "LOCK":
      if (
        state.lockedKey === action.key
        && state.selectedKey === action.key
        && (action.key !== null || state.detailKey === null)
      ) return state;
      return {
        ...state,
        selectedKey: action.key,
        lockedKey: action.key,
        detailKey: action.key === state.detailKey ? state.detailKey : null,
        revision: nextRevision(state),
      };
    case "TOGGLE_LOCK":
      return state.lockedKey === action.key
        ? clearFocus(state)
        : {
            ...state,
            selectedKey: action.key,
            lockedKey: action.key,
            detailKey: null,
            revision: nextRevision(state),
          };
    case "OPEN_DETAIL":
      if (
        state.selectedKey === action.key
        && state.lockedKey === action.key
        && state.detailKey === action.key
      ) return state;
      return {
        ...state,
        selectedKey: action.key,
        lockedKey: action.key,
        detailKey: action.key,
        revision: nextRevision(state),
      };
    case "CLEAR_FOCUS":
      return clearFocus(state);
    default:
      return state;
  }
}
