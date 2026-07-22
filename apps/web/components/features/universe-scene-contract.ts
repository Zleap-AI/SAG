import type { SearchSourceHit, UniversePolicy } from "@/lib/types";
import type { UniverseViewPreferences } from "@/lib/universe-view-preferences";

export type UniverseSceneNodeKind = "source" | "event" | "entity";
export type UniverseSceneStrategy = "exploration" | "accumulation";

export interface UniverseSceneNode {
  id: string;
  kind: UniverseSceneNodeKind;
  rawId: string;
  sourceId: string;
  label: string;
  description: string;
  category: string;
  radius: number;
  density: number;
  eventCount: number;
  entityCount: number;
  relationCount: number;
  relatedCount: number;
  relatedCountKnown: boolean;
  /** Number of unique one-hop relations already resident in the working set. */
  relatedProgress?: number;
  /** False only when the complete known one-hop relation set is resident. */
  canExploreMore?: boolean;
  importance: number;
  statsReady: boolean;
  state: "latent" | "active";
  root: boolean;
  x: number;
  y: number;
  z: number;
  /** Data-driven presentation factors; omitted values preserve the stable view. */
  presentationScale?: number;
  presentationCardScale?: number;
  presentationOpacity?: number;
  /** Groups an event and its entities onto one shared time-transition path. */
  timelineBundleId?: string;
  /** Snapshot-stable order within the source timeline (0 = newest package). */
  timelineOrder?: number;
}

export interface UniverseSceneLink {
  id: string;
  source: string;
  target: string;
  weight: number;
  virtual: boolean;
  /** Multiplies the scene's normal/highlight link opacity. */
  presentationOpacity?: number;
}

/**
 * Everything the scene needs to fly the browsed source's counting axis. Depths
 * are world units from the source's newest moment; the window depths locate the
 * currently visible packages so flight knows when to page.
 */
export interface UniverseSceneTemporalFlight {
  sourceId: string;
  centerZ: number;
  unitsPerEvent: number;
  /**
   * Axis stretch between the entry pose (depth 0) and the first event: the
   * initial state shows only the intact nebula, exploration begins by
   * crossing it, and retreating all the way lands back on it.
   */
  vestibuleDepth: number;
  maxDepth: number;
  windowNearDepth: number;
  windowFarDepth: number;
}

export interface UniverseSceneData {
  epoch: number;
  nodes: UniverseSceneNode[];
  links: UniverseSceneLink[];
  /** Stable identity revision for the projected visible-bundle window. */
  windowRevision?: number;
  /** Identifies whether this window was committed by the active journey intent. */
  windowChangeCause?: "journey" | "synchronization";
  /** Direction is explicit so forward and reverse transitions share one path. */
  windowDirection?: UniverseTimelineDirection;
  /** Null while no browsed source carries a usable time axis. */
  temporalFlight?: UniverseSceneTemporalFlight | null;
}

export interface UniverseSceneHover {
  node: UniverseSceneNode;
  x: number;
  y: number;
}

export interface UniverseSceneView {
  mode: "overview" | "detail";
  sourceId: string | null;
  progress: number;
}

export interface UniverseSceneExplorationView {
  camera: { x: number; y: number; z: number };
  target: { x: number; y: number; z: number };
  sourceId: string | null;
  detailMix: number;
  flightDepth: number;
}

export interface UniverseTimelineJourney {
  enabled: boolean;
  phase: "idle" | "loading" | "transitioning" | "complete";
  hasNext: boolean;
  hasPrevious: boolean;
  networkExhausted: boolean;
  revision: number;
}

export type UniverseTimelineIntentResult =
  | "advanced"
  | "complete"
  | "blocked"
  | "error";

export type UniverseTimelineDirection = "next" | "previous";

export type UniverseSceneUnavailableReason =
  | "dynamic-import"
  | "context-disabled"
  | "context-creation"
  | "initialization"
  | "context-lost";

export interface UniverseSceneHandle {
  /** Marks the next exploration delivery as a retained, stable restoration. */
  prepareExplorationRestore: () => void;
  captureExplorationView: () => UniverseSceneExplorationView | null;
  restoreExplorationView: (view: UniverseSceneExplorationView) => void;
  focusOverview: () => void;
  resetOverview: () => void;
  focusResult: () => void;
  focusSource: (sourceId: string) => void;
  returnToSourceOrigin: (sourceId: string) => "moved" | "already-at-origin";
  focusNode: (nodeId: string) => void;
  lockNode: (nodeId: string) => void;
  unlockNode: () => void;
  clearSelection: () => void;
  moveTimeline: (
    direction: UniverseTimelineDirection,
  ) => Promise<UniverseTimelineIntentResult> | void;
  pause: () => void;
  resume: () => void;
}

export interface UniverseSelectionClearOptions {
  /** Keeps the contextual mini workspace visible while releasing the scene lock. */
  dismissWorkspace?: boolean;
}

export interface UniverseSceneProps {
  data: UniverseSceneData;
  /**
   * Exploration owns source nebulae and temporal flight. Accumulation is a
   * stable, source-agnostic evidence graph and must never mutate browse state.
   */
  strategy: UniverseSceneStrategy;
  policy: UniversePolicy;
  sourceHits: SearchSourceHit[];
  selectedId: string | null;
  darkTheme: boolean;
  interactive: boolean;
  reducedMotion: boolean;
  viewPreferences: UniverseViewPreferences;
  timelineJourney: UniverseTimelineJourney;
  onNodeClick: (node: UniverseSceneNode) => void;
  onHover: (value: UniverseSceneHover | null) => void;
  onViewChange: (value: UniverseSceneView) => void;
  onSourceLod: (sourceId: string, level: 0 | 1 | 2 | 3) => void;
  /** Enters a hovered source without requiring a click. */
  onSourceWheel?: (sourceId: string) => void;
  onSelectionClear: (options?: UniverseSelectionClearOptions) => void;
  onBackRequest?: () => void;
  onBackgroundClick?: () => void;
  actionLabels?: {
    viewDetails: string;
    exploreMore: string;
    askAi: string;
  };
  onViewDetails?: (node: UniverseSceneNode) => void;
  onExploreMore?: (node: UniverseSceneNode) => void;
  onAskNode?: (node: UniverseSceneNode) => void;
  /** Notifies the owner about a dismissing pointer/keyboard gesture, never scene animation. */
  onUserInteraction?: () => void;
  onTimelineIntent: (
    direction: UniverseTimelineDirection,
  ) => Promise<UniverseTimelineIntentResult> | UniverseTimelineIntentResult;
  onTimelineSettled: (revision: number) => void;
  onUnavailable?: (reason: UniverseSceneUnavailableReason) => void;
}
