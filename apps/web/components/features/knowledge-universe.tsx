"use client";

import * as React from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useLocale, useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import {
  ArrowDownUp,
  ChevronLeft,
  ChevronRight,
  CircleDot,
  Focus,
  Loader2,
  LocateFixed,
  Orbit,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Sparkles,
} from "lucide-react";

import { api, ApiError } from "@/lib/api";
import type {
  UniverseActivation,
  UniverseActivationOrigin,
  UniverseGraphPatch,
  UniverseManifest,
  UniverseNodeKind,
  UniverseTimelineDirection,
} from "@/lib/types";
import {
  UNIVERSE_ACTIVATE_EVENT,
  UNIVERSE_CONTEXT_EVENT,
  UNIVERSE_FOCUS_EVENT,
  dispatchUniverseDetail,
  dispatchUniverseInteraction,
  dispatchUniversePresentation,
  dispatchUniverseResume,
  readUniverseContext,
  UNIVERSE_RESET_EVENT,
  UNIVERSE_RESUME_EVENT,
  UNIVERSE_SOURCE_FOCUS_EVENT,
  dispatchUniverseAsk,
  dispatchUniversePatch,
  dispatchUniversePatchReset,
  dispatchUniverseView,
  type UniverseContextState,
} from "@/lib/universe-events";
import {
  UNIVERSE_RESIDENT_BUDGET,
  UNIVERSE_SCENE_BUDGET,
  emptyUniverseWorkingSet,
  replaceUniverseWorkingSet,
  trimUniverseWorkingSet,
  universeAnchorProgress,
  universeNodeKey,
  universeRelationKey,
  type UniverseWorkingSet,
} from "@/lib/universe-working-set";
import {
  advanceUniverseTimelineWindow,
  markUniverseTimelineNetworkExhausted,
  projectUniverseBundleWindowWithinBudget,
  queriedUniverseTimelineEventCount,
  reconfigureUniverseTimelineWindow,
  retainUniverseWorkingSetBundles,
  settleUniverseTimelineWindow,
  universeTimelinePageBundleLimit,
  universeTimelineRewindStartActiveIndex,
  type UniverseTimelineWindowState,
} from "@/lib/universe-timeline-window";
import { admitUniverseExpansionPage } from "@/lib/universe-expansion-admission";
import { admitUniverseTimelinePage } from "@/lib/universe-timeline-admission";
import {
  admitUniverseTimelineDequePage,
  resizeUniverseTimelineDeque,
  syncUniverseTimelineWindowToDeque,
} from "@/lib/universe-timeline-deque";
import {
  createUniverseTemporalAxis,
  projectUniverseTemporalAxis,
  UNIVERSE_TEMPORAL_AXIS_VESTIBULE_UNITS,
  universeTemporalAxisDepth,
} from "@/lib/universe-temporal-axis";
import { planUniversePrefetch } from "@/lib/universe-prefetch-controller";
import {
  advanceUniverseEvidenceTransition,
  appendUniverseEvidence,
  createUniverseAccumulationState,
  type UniverseAccumulationState,
} from "@/lib/universe-accumulation";
import {
  universeActivationFromEventBundles,
  universeEventBundlesFromActivation,
  universeEventBundlesFromTimeline,
} from "@/lib/universe-event-bundle-adapter";
import {
  admitUniverseEventBundles,
  createUniverseEventCache,
  resizeUniverseEventCache,
  type UniverseEventCache,
} from "@/lib/universe-event-cache";
import {
  configureUniverseSceneWindow,
  createUniverseSceneWindow,
  projectUniverseSceneWindow,
} from "@/lib/universe-scene-window";
import {
  createUniverseSessionState,
  reduceUniverseSession,
  type UniverseSessionAction,
  type UniverseSessionState,
} from "@/lib/universe-session-state";
import {
  planUniverseTimelinePlayback,
  toggleUniverseTimelinePlaybackOrder,
  type UniverseTimelinePlaybackOrder,
} from "@/lib/universe-timeline-playback";
import { detectUniverseWebGLCapability } from "@/lib/universe-webgl-capability";
import {
  effectiveUniverseBudget,
  effectiveUniverseBundleWindow,
  projectUniverseWorkingSet,
  publishUniverseEntityCategories,
  useUniverseViewPreferences,
} from "@/lib/universe-view-preferences";
import { cn } from "@/lib/utils";
import {
  UniverseScene,
  universeSourceAccent,
} from "@/components/features/universe-scene";
import type {
  UniverseSceneData,
  UniverseSceneExplorationView,
  UniverseSceneHandle,
  UniverseSceneNode,
  UniverseSceneStrategy,
  UniverseSceneUnavailableReason,
  UniverseSelectionClearOptions,
  UniverseTimelineIntentResult,
  UniverseTimelineJourney,
  UniverseSceneView,
} from "@/components/features/universe-scene-contract";
import {
  EMPTY_TIMELINE_BUNDLE_IDS,
  ENTITY_EXPANSION_EVENT_LIMIT,
  EVENT_ENTITY_PROJECTION_LIMIT,
  LOCAL_ENTITY_SPREAD_MIN,
  LOCAL_ENTITY_SPREAD_RANGE,
  PARTITION_RENDER_LIMIT,
  TEMPORAL_AXIS_UNITS_PER_EVENT,
  TIMELINE_EVENT_LATERAL_SPREAD,
  dominantSource,
  emptySourceBrowseSession,
  expansionLineageRootKey,
  isConcreteUniverseNode,
  stableOffset,
  stableAccumulationEventOffset,
  stableRootEventOffset,
  stableSatelliteOffset,
  stableUnit,
  synchronizeTimelineWindowWithDeque,
  timelineProjectionBundleIds,
  timelineRetentionBundleIds,
  universeBundleWindowProtection,
  universeExpansionCacheKey,
  universeSceneDataSignature,
  visualNebulaRadius,
  waitForAbortableDelay,
  type ActivationSummary,
  type Position3D,
  type SourceBrowseSession,
  type SourceTimelineLoadCause,
  type SourceTimelineLoadResult,
  type SourceTimelineRequest,
  type Universe3DLink,
  type Universe3DNode,
  type UniverseConcrete3DNode,
} from "@/components/features/knowledge-universe-model";
import {
  UniverseIconControl as IconControl,
  UniverseLoadProgressPanel,
  compactCount,
  type SourceLoadProgress,
} from "@/components/features/knowledge-universe-overlays";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

interface RetainedExploration {
  session: SourceBrowseSession | null;
  working: UniverseWorkingSet;
  timelineWindow: UniverseTimelineWindowState | null;
  activePartition: string | null;
  viewportSourceId: string | null;
  selectedKey: string | null;
  lockedKey: string | null;
  sourceHits: NonNullable<UniverseActivation["source_hits"]>;
  summary: ActivationSummary | null;
  moreHint: string;
  view: UniverseSceneExplorationView | null;
  expansionSnapshots: Map<string, ExpansionSnapshotContext>;
  completedSources: Set<string>;
  expansionCache: Map<string, UniverseGraphPatch>;
  cursors: Map<string, string>;
  expandedAnchors: Set<string>;
}

interface ContextGraphSession {
  sceneEpoch: number;
  activationCount: number;
  answerTurnCount: number;
}

interface ExpansionSnapshotContext {
  snapshotId: string;
  sourceRevision: string;
  asOf: string;
}

export function KnowledgeUniverse({
  interactive = true,
}: {
  interactive?: boolean;
}) {
  const locale = useLocale();
  const t = useTranslations("KnowledgeUniverse");
  const reducedMotion = useReducedMotion();
  const { resolvedTheme } = useTheme();
  const darkTheme = resolvedTheme === "dark";
  const containerRef = React.useRef<HTMLDivElement>(null);
  const graphRef = React.useRef<UniverseSceneHandle | null>(null);
  const interactiveRef = React.useRef(interactive);
  interactiveRef.current = interactive;
  const focusTimerRef = React.useRef<number | null>(null);
  const cameraFrameRef = React.useRef<number | null>(null);
  const epochRef = React.useRef(0);
  const activationEventEpochRef = React.useRef(0);
  const retainedExplorationRef = React.useRef<RetainedExploration | null>(null);
  const contextGraphSessionRef = React.useRef<ContextGraphSession | null>(null);
  const contextProjectionClosedRef = React.useRef(false);
  const workingRef = React.useRef<UniverseWorkingSet>(emptyUniverseWorkingSet());
  const budgetRef = React.useRef<{ nodes: number; edges: number }>(
    UNIVERSE_SCENE_BUDGET.mobile,
  );
  const residentBudgetRef = React.useRef<{ nodes: number; edges: number }>(
    UNIVERSE_RESIDENT_BUDGET.mobile,
  );
  const expandAbortRef = React.useRef<AbortController | null>(null);
  const expansionCacheRef = React.useRef(new Map<string, UniverseGraphPatch>());
  const expansionInflightRef = React.useRef(new Map<string, {
    promise: Promise<UniverseGraphPatch>;
    signal: AbortSignal;
  }>());
  const rebuildAbortRef = React.useRef<AbortController | null>(null);
  const autoRebuildAttemptedRef = React.useRef(false);
  const observedManifestRef = React.useRef(false);
  const manifestVersionRef = React.useRef<string | null>(null);
  const timelineRequestRef = React.useRef<SourceTimelineRequest | null>(null);
  const timelinePageLoaderRef = React.useRef<((
    sourceId: string,
    cause: SourceTimelineLoadCause,
    direction?: UniverseTimelineDirection,
  ) => Promise<SourceTimelineLoadResult>) | null>(null);
  const snapshotReloadTimerRef = React.useRef<number | null>(null);
  const snapshotReloadAttemptsRef = React.useRef(new Map<string, number>());
  const timelineSettleTimerRef = React.useRef<number | null>(null);
  const accumulationSettleTimerRef = React.useRef<number | null>(null);
  const timelinePlaybackTimerRef = React.useRef<number | null>(null);
  const sourceSessionRef = React.useRef<SourceBrowseSession | null>(null);
  const eventCacheRef = React.useRef<UniverseEventCache>(
    createUniverseEventCache(),
  );
  const accumulationStateRef = React.useRef<UniverseAccumulationState>(
    createUniverseAccumulationState(createUniverseSceneWindow()),
  );
  const accumulationMetaRef = React.useRef<{
    query: string;
    origin: UniverseActivationOrigin;
    sourceHits: NonNullable<UniverseActivation["source_hits"]>;
  }>({
    query: "",
    origin: "assistant",
    sourceHits: [],
  });
  const timelineJourneyCommitRef = React.useRef<{
    session: SourceBrowseSession;
    revision: number;
    cause: "journey";
    direction: "next" | "previous";
  } | null>(null);
  const expansionSnapshotsRef = React.useRef(new Map<string, ExpansionSnapshotContext>());
  const completedSourcesRef = React.useRef(new Set<string>());
  const sessionStateRef = React.useRef<UniverseSessionState>(
    createUniverseSessionState(),
  );
  const evidenceOriginRef = React.useRef<UniverseActivationOrigin>("browse");
  const viewportSourceRef = React.useRef<string | null>(null);
  const lockedKeyRef = React.useRef<string | null>(null);
  const cursorsRef = React.useRef(new Map<string, string>());
  const expandedAnchorsRef = React.useRef(new Set<string>());
  const expandingAnchorRef = React.useRef<string | null>(null);
  const nodeByIdRef = React.useRef(new Map<string, Universe3DNode>());
  const graphDataCacheRef = React.useRef<{
    signature: string;
    data: UniverseSceneData;
  } | null>(null);
  const { preferences: viewPreferences } = useUniverseViewPreferences();
  const entityTypeSignature = viewPreferences.entityTypes === null
    ? "null"
    : JSON.stringify([...viewPreferences.entityTypes].sort());
  const projectedEntityTypes = React.useMemo<string[] | null>(
    () => JSON.parse(entityTypeSignature) as string[] | null,
    [entityTypeSignature],
  );
  const [dimensions, setDimensions] = React.useState({ width: 1, height: 1 });
  const [manifest, setManifest] = React.useState<UniverseManifest | null>(null);
  const [working, setWorking] = React.useState<UniverseWorkingSet>(emptyUniverseWorkingSet());
  const [timelineWindow, setTimelineWindow] =
    React.useState<UniverseTimelineWindowState | null>(null);
  const [activePartition, setActivePartition] = React.useState<string | null>(null);
  const [viewportSourceId, setViewportSourceId] = React.useState<string | null>(null);
  const [sessionState, setSessionState] = React.useState<UniverseSessionState>(
    createUniverseSessionState,
  );
  const [timelinePlaying, setTimelinePlaying] = React.useState(false);
  const [contextWorkspace, setContextWorkspace] =
    React.useState<UniverseContextState>(readUniverseContext);
  const [contextActivationCount, setContextActivationCount] = React.useState(0);
  const [timelinePlaybackOrder, setTimelinePlaybackOrder] =
    React.useState<UniverseTimelinePlaybackOrder>("reverse");
  const [documentHidden, setDocumentHidden] = React.useState(false);
  const [sourceHits, setSourceHits] = React.useState<
    NonNullable<UniverseActivation["source_hits"]>
  >([]);
  const [summary, setSummary] = React.useState<ActivationSummary | null>(null);
  const [expandingKey, setExpandingKey] = React.useState<string | null>(null);
  const [moreHint, setMoreHint] = React.useState("");
  const [evidenceOrigin, setEvidenceOrigin] =
    React.useState<UniverseActivationOrigin>("browse");
  const sceneStrategy: UniverseSceneStrategy =
    sessionState.mode === "accumulation"
      ? "accumulation"
      : "exploration";
  const selectedKey = sessionState.selectedKey;
  const lockedKey = sessionState.lockedKey;
  const [, setLoadProgressRevision] = React.useState(0);
  const [loading, setLoading] = React.useState(true);
  const [rebuilding, setRebuilding] = React.useState(false);
  const [error, setError] = React.useState("");
  const [webglAvailable, setWebglAvailable] = React.useState<boolean | null>(null);
  const [sceneUnavailableReason, setSceneUnavailableReason] =
    React.useState<UniverseSceneUnavailableReason | null>(null);
  const [sceneAttempt, setSceneAttempt] = React.useState(0);
  const sceneRetryFrameRef = React.useRef<number | null>(null);

  React.useEffect(() => {
    const onContext = (event: Event) => {
      const value = (event as CustomEvent<UniverseContextState>).detail;
      if (value) {
        if (value.active) contextProjectionClosedRef.current = false;
        setContextWorkspace(value);
      }
    };
    window.addEventListener(UNIVERSE_CONTEXT_EVENT, onContext);
    return () => window.removeEventListener(UNIVERSE_CONTEXT_EVENT, onContext);
  }, []);

  React.useEffect(() => {
    if (!interactive) return;
    dispatchUniversePresentation(sceneStrategy);
  }, [interactive, sceneStrategy]);

  // The loaded browse session is the data authority for timeline paging.
  // `activePartition` is intentionally kept as camera/presentation state so
  // visual LOD changes cannot disconnect a valid cached timeline.
  const browseSessionSourceId = sessionState.mode === "exploration" && timelineWindow
    ? sourceSessionRef.current?.sourceId ?? null
    : null;

  const dispatchSession = React.useCallback((action: UniverseSessionAction) => {
    const next = reduceUniverseSession(sessionStateRef.current, action);
    sessionStateRef.current = next;
    setSessionState(next);
  }, []);

  const setSelectedKey = React.useCallback((key: string | null) => {
    dispatchSession({ type: "SELECT", key });
  }, [dispatchSession]);

  const setLockedKey = React.useCallback((key: string | null) => {
    lockedKeyRef.current = key;
    dispatchSession({ type: "LOCK", key });
  }, [dispatchSession]);

  const commitWorkingSet = React.useCallback((next: UniverseWorkingSet) => {
    workingRef.current = next;
    const session = sourceSessionRef.current;
    if (session && session.working.epoch === next.epoch) {
      session.working = next;
    }
    setWorking(next);
  }, []);

  const commitTimelineWindow = React.useCallback((
    session: SourceBrowseSession,
    next: UniverseTimelineWindowState,
  ) => {
    if (sourceSessionRef.current !== session) return;
    session.timeline.window = next;
    setTimelineWindow(next);
  }, []);

  const scheduleTimelineSettle = React.useCallback((
    session: SourceBrowseSession,
    next: UniverseTimelineWindowState,
  ) => {
    if (timelineSettleTimerRef.current !== null) {
      window.clearTimeout(timelineSettleTimerRef.current);
    }
    if (next.phase !== "transitioning") {
      timelineSettleTimerRef.current = null;
      return;
    }
    timelineSettleTimerRef.current = window.setTimeout(() => {
      timelineSettleTimerRef.current = null;
      if (
        sourceSessionRef.current !== session
        || session.timeline.window.revision !== next.revision
      ) return;
      commitTimelineWindow(session, settleUniverseTimelineWindow(next));
    // Scene completion is authoritative. This timer is only a renderer-failure
    // fallback so controls never remain disabled if WebGL disappears mid-page.
    }, reducedMotion ? 0 : 1_600);
  }, [commitTimelineWindow, reducedMotion]);

  const clearTimelineSettle = React.useCallback(() => {
    if (timelineSettleTimerRef.current !== null) {
      window.clearTimeout(timelineSettleTimerRef.current);
      timelineSettleTimerRef.current = null;
    }
  }, []);

  const settleTimelineBeforeSuspend = React.useCallback(() => {
    const session = sourceSessionRef.current;
    if (session) {
      // The session object is the timeline authority. Commit its settled
      // window through the shared writer so the ref and React state resume
      // from the exact same revision after this view becomes interactive.
      commitTimelineWindow(
        session,
        settleUniverseTimelineWindow(session.timeline.window),
      );
    }
    // A journey marker is meaningful for one scene delivery only. Once the
    // scene is suspended there is no remaining transition that may consume it.
    timelineJourneyCommitRef.current = null;
  }, [commitTimelineWindow]);

  const mobile = dimensions.width < 768;
  const configuredBundleWindow = React.useMemo(
    () => effectiveUniverseBundleWindow(viewPreferences, mobile),
    [mobile, viewPreferences],
  );
  const policyBudget = mobile
    ? UNIVERSE_SCENE_BUDGET.mobile
    : UNIVERSE_SCENE_BUDGET.desktop;
  const budget = React.useMemo(
    () => effectiveUniverseBudget(policyBudget),
    [policyBudget],
  );
  const timelineBundleEntityLimit = Math.max(0, Math.min(
    EVENT_ENTITY_PROJECTION_LIMIT,
    manifest?.policy.event_entity_limit ?? EVENT_ENTITY_PROJECTION_LIMIT,
  ));
  const timelineWindowPlan = React.useMemo(() => {
    const nodesPerPackage = Math.max(1, timelineBundleEntityLimit + 1);
    const edgesPerPackage = timelineBundleEntityLimit;
    const nodeCapacity = Math.max(0, Math.floor(budget.nodes / nodesPerPackage));
    const edgeCapacity = edgesPerPackage > 0
      ? Math.max(0, Math.floor(budget.edges / edgesPerPackage))
      : nodeCapacity;
    const packageCapacity = Math.min(nodeCapacity, edgeCapacity);
    const eventWindowSize = Math.max(
      1,
      Math.min(configuredBundleWindow.eventWindowSize, packageCapacity || 1),
    );
    const transitionHeadroomPackages = Math.max(0, Math.min(
      viewPreferences.temporalPageSize,
      packageCapacity - eventWindowSize,
    ));
    return {
      window: {
        eventWindowSize,
        cacheCapacity: configuredBundleWindow.cacheCapacity,
      },
      projectionBudget: {
        nodes: Math.max(
          0,
          budget.nodes - transitionHeadroomPackages * nodesPerPackage,
        ),
        edges: Math.max(
          0,
          budget.edges - transitionHeadroomPackages * edgesPerPackage,
        ),
      },
      transitionHeadroomPackages,
    };
  }, [
    budget,
    configuredBundleWindow,
    timelineBundleEntityLimit,
    viewPreferences.temporalPageSize,
  ]);
  const bundleWindow = timelineWindowPlan.window;
  const timelineProjectionBudget = timelineWindowPlan.projectionBudget;
  const residentBudget = mobile
    ? UNIVERSE_RESIDENT_BUDGET.mobile
    : UNIVERSE_RESIDENT_BUDGET.desktop;
  const timelineCacheBundleIds = timelineWindow?.cacheBundleIds
    ?? EMPTY_TIMELINE_BUNDLE_IDS;
  const timelineVisibleBundleIds = timelineWindow?.visibleBundleIds
    ?? EMPTY_TIMELINE_BUNDLE_IDS;
  const timelineWindowRevision = timelineWindow?.revision ?? -1;
  budgetRef.current = budget;
  residentBudgetRef.current = residentBudget;

  const clearAccumulationSettle = React.useCallback(() => {
    if (accumulationSettleTimerRef.current === null) return;
    window.clearTimeout(accumulationSettleTimerRef.current);
    accumulationSettleTimerRef.current = null;
  }, []);

  const resetAccumulationKernel = React.useCallback((clearCache: boolean) => {
    clearAccumulationSettle();
    if (clearCache) {
      eventCacheRef.current = createUniverseEventCache(
        viewPreferences.cacheCapacity,
      );
    }
    accumulationStateRef.current = createUniverseAccumulationState(
      createUniverseSceneWindow(
        bundleWindow.eventWindowSize,
        viewPreferences.temporalPageSize,
      ),
    );
    accumulationMetaRef.current = {
      query: "",
      origin: "assistant",
      sourceHits: [],
    };
  }, [
    bundleWindow.eventWindowSize,
    clearAccumulationSettle,
    viewPreferences.cacheCapacity,
    viewPreferences.temporalPageSize,
  ]);

  const commitAccumulationProjection = React.useCallback((
    state: UniverseAccumulationState,
  ) => {
    const projection = projectUniverseSceneWindow(state.window);
    const meta = accumulationMetaRef.current;
    const activation = universeActivationFromEventBundles({
      bundles: projection.bundles,
      epoch: epochRef.current,
      query: meta.query,
      origin: meta.origin,
      sourceHits: meta.sourceHits,
    });
    const next = replaceUniverseWorkingSet(activation, budget);
    commitWorkingSet(next);
    setSourceHits(meta.sourceHits);
    setSummary({
      query: meta.query,
      events: projection.events.length,
      entities: projection.entities.length,
      relations: projection.relations.length,
    });
  }, [budget, commitWorkingSet]);

  const scheduleAccumulationSettle = React.useCallback((
    expectedRevision: number,
  ) => {
    clearAccumulationSettle();
    const run = (revision: number) => {
      accumulationSettleTimerRef.current = window.setTimeout(() => {
        accumulationSettleTimerRef.current = null;
        const current = accumulationStateRef.current;
        if (current.window.revision !== revision) return;
        const next = advanceUniverseEvidenceTransition(current);
        accumulationStateRef.current = next;
        commitAccumulationProjection(next);
        if (next.window.phase === "transitioning") {
          run(next.window.revision);
        }
      }, reducedMotion ? 0 : 780);
    };
    run(expectedRevision);
  }, [
    clearAccumulationSettle,
    commitAccumulationProjection,
    reducedMotion,
  ]);

  const appendActivationToAccumulation = React.useCallback((
    activation: UniverseActivation,
    origin: UniverseActivationOrigin,
  ) => {
    const bundles = universeEventBundlesFromActivation(
      { ...activation, origin },
      {
        entityTypes: projectedEntityTypes,
        documentIds: viewPreferences.documentIds,
      },
    );
    accumulationMetaRef.current = {
      query: activation.query,
      origin,
      sourceHits: activation.source_hits ?? [],
    };
    const result = appendUniverseEvidence(
      accumulationStateRef.current,
      eventCacheRef.current,
      bundles,
    );
    accumulationStateRef.current = result.state;
    eventCacheRef.current = result.cache;
    dispatchSession({
      type: "APPEND_EVIDENCE",
      origin,
      addedEvents: result.newBundleKeys.length,
    });
    if (!result.hasNewEvidence) {
      if (result.updatedBundleKeys.length > 0) {
        // Duplicate facts may still carry a richer entity/relation projection.
        // Refresh the stable graph in place without scheduling camera work.
        commitAccumulationProjection(result.state);
      }
      setMoreHint(t("accumulation.noNewEvidence"));
      return result;
    }
    commitAccumulationProjection(result.state);
    setMoreHint("");
    if (result.state.window.phase === "transitioning") {
      scheduleAccumulationSettle(result.state.window.revision);
    }
    return result;
  }, [
    commitAccumulationProjection,
    dispatchSession,
    projectedEntityTypes,
    scheduleAccumulationSettle,
    t,
    viewPreferences.documentIds,
  ]);

  React.useEffect(() => {
    eventCacheRef.current = resizeUniverseEventCache(
      eventCacheRef.current,
      viewPreferences.cacheCapacity,
    ).cache;
    const current = accumulationStateRef.current;
    const window = configureUniverseSceneWindow(
      current.window,
      bundleWindow.eventWindowSize,
      viewPreferences.temporalPageSize,
    );
    accumulationStateRef.current = { ...current, window };
    if (sessionStateRef.current.mode === "accumulation") {
      commitAccumulationProjection(accumulationStateRef.current);
    }
  }, [
    bundleWindow.eventWindowSize,
    commitAccumulationProjection,
    viewPreferences.cacheCapacity,
    viewPreferences.temporalPageSize,
  ]);

  const refreshLoadProgress = React.useCallback(() => {
    setLoadProgressRevision((current) => current + 1);
  }, []);

  React.useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const update = () => {
      const bounds = container.getBoundingClientRect();
      setDimensions({
        width: Math.max(1, Math.round(bounds.width)),
        height: Math.max(1, Math.round(bounds.height)),
      });
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  React.useEffect(() => {
    // Do not allocate a disposable probe context here. Chrome has a small
    // per-process context pool, so a probe can fail transiently or compete with
    // the real Three renderer and incorrectly look like permanent lack of WebGL.
    const capability = detectUniverseWebGLCapability(window);
    setWebglAvailable(capability === "available");
  }, [sceneAttempt]);

  React.useEffect(() => () => {
    if (sceneRetryFrameRef.current !== null) {
      window.cancelAnimationFrame(sceneRetryFrameRef.current);
    }
  }, []);

  const loadManifest = React.useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setManifest(await api.universeManifest());
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : t("errors.unavailable"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  React.useEffect(() => {
    if (!interactive) return;
    void loadManifest();
  }, [interactive, loadManifest]);

  React.useEffect(() => {
    if (!interactive || manifest?.status !== "building") return;
    let alive = true;
    const timer = window.setInterval(() => {
      void api.universeManifest().then((next) => {
        if (alive) setManifest(next);
      }).catch(() => undefined);
    }, 1500);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [interactive, manifest?.status]);

  const sourcePartitions = React.useMemo(
    () => manifest?.partitions.filter((partition) => partition.kind === "source") ?? [],
    [manifest],
  );
  const sourceById = React.useMemo(
    () => new Map(sourcePartitions.map((partition) => [partition.source_id, partition])),
    [sourcePartitions],
  );
  const viewportSource = viewportSourceId
    ? sourceById.get(viewportSourceId) ?? null
    : null;
  // The focused source owns the secondary accent across the shell. Fall back
  // to the active partition during the entry flight so controls switch before
  // the scene reports its settled detail state.
  const activeSourceId = viewportSourceId ?? browseSessionSourceId ?? activePartition;
  const activeSourceAccent = activeSourceId
    ? universeSourceAccent(activeSourceId, darkTheme)
    : null;
  const showReturnHomeControl = Boolean(browseSessionSourceId);
  const viewportLoadProgress: SourceLoadProgress | null = (() => {
    if (!viewportSource) return null;
    const session = sourceSessionRef.current?.sourceId === viewportSource.source_id
      ? sourceSessionRef.current
      : null;
    const eventState = session?.timeline;
    const entityTotal = Math.max(0, viewportSource.entity_count);
    const eventTotal = Math.max(0, viewportSource.event_count);
    const residentNodes = working.nodes.filter(
      (node) => node.source_id === viewportSource.source_id,
    );
    // Entity progress is deliberately resident, not an all-time unique count:
    // the working set is bounded while a source may contain arbitrarily many
    // identities across its full timeline.
    const entityLoaded = Math.min(
      entityTotal,
      residentNodes.filter((node) => node.kind === "entity").length,
    );
    const eventLoaded = Math.min(eventTotal, eventState
      ? queriedUniverseTimelineEventCount(eventState.window)
      : residentNodes.filter((node) => node.kind === "event").length);
    const timelineComplete = eventState?.window.phase === "complete";
    const entities = {
      loaded: entityLoaded,
      total: entityTotal,
      done: eventState
        ? timelineComplete
        : entityTotal === 0 || entityLoaded >= entityTotal,
      loading: Boolean(eventState?.loading),
    };
    const events = {
      loaded: eventLoaded,
      total: eventTotal,
      done: eventState
        ? timelineComplete
        : eventTotal === 0 || eventLoaded >= eventTotal,
      loading: Boolean(eventState?.loading),
    };
    return {
      sourceId: viewportSource.source_id,
      label: viewportSource.label,
      entities,
      events,
      allDone: entities.done && events.done,
      loading: entities.loading || events.loading,
    };
  })();
  const renderedSourcePartitions = React.useMemo(() => {
    const limit = mobile
      ? PARTITION_RENDER_LIMIT.mobile
      : PARTITION_RENDER_LIMIT.desktop;
    const ranked = [...sourcePartitions].sort(
      (left, right) => right.importance - left.importance || left.id.localeCompare(right.id),
    );
    const relevantSourceIds = sessionState.mode === "accumulation"
      ? new Set([
          ...sourceHits.map((hit) => hit.source_id),
          ...working.nodes.map((node) => node.source_id).filter(Boolean),
        ])
      : null;
    const candidates = relevantSourceIds?.size
      ? ranked.filter((partition) => relevantSourceIds.has(partition.source_id))
      : ranked;
    const rendered = candidates.slice(0, limit);
    const retainedSourceId = browseSessionSourceId ?? activePartition;
    if (retainedSourceId && !rendered.some((item) => item.source_id === retainedSourceId)) {
      const active = sourceById.get(retainedSourceId);
      if (active) {
        if (rendered.length >= limit) rendered.pop();
        rendered.push(active);
      }
    }
    return rendered;
  }, [
    activePartition,
    browseSessionSourceId,
    mobile,
    sessionState.mode,
    sourceById,
    sourceHits,
    sourcePartitions,
    working.nodes,
  ]);

  const focusOverview = React.useCallback(() => {
    graphRef.current?.unlockNode();
    setLockedKey(null);
    setSelectedKey(null);
    graphRef.current?.focusOverview();
  }, [setLockedKey, setSelectedKey]);

  const focusResult = React.useCallback(() => {
    graphRef.current?.focusResult();
  }, []);

  const focusPartition = React.useCallback(
    (sourceId: string) => {
      if (!sourceById.has(sourceId)) return;
      setActivePartition(sourceId);
      graphRef.current?.focusSource(sourceId);
    },
    [sourceById],
  );

  const focusNode = React.useCallback((node: Universe3DNode) => {
    graphRef.current?.focusNode(node.id);
  }, []);

  const clearCameraSchedule = React.useCallback(() => {
    if (focusTimerRef.current !== null) window.clearTimeout(focusTimerRef.current);
    if (cameraFrameRef.current !== null) window.cancelAnimationFrame(cameraFrameRef.current);
    focusTimerRef.current = null;
    cameraFrameRef.current = null;
  }, []);

  const pruneExpansionState = React.useCallback((current: UniverseWorkingSet) => {
    const kept = new Map(
      current.nodes.map((node) => [
        universeNodeKey(node.kind, node.id, node.source_id),
        node,
      ]),
    );
    const invalidatedAnchors = new Set<string>();
    for (const key of cursorsRef.current.keys()) {
      if (!kept.has(key)) {
        cursorsRef.current.delete(key);
        invalidatedAnchors.add(key);
      }
    }
    for (const key of expandedAnchorsRef.current) {
      if (!kept.has(key)) {
        expandedAnchorsRef.current.delete(key);
        invalidatedAnchors.add(key);
      }
    }
    if (
      expandingAnchorRef.current
      && invalidatedAnchors.has(expandingAnchorRef.current)
    ) expandAbortRef.current?.abort();
  }, []);

  React.useEffect(() => {
    const current = workingRef.current;
    const session = sourceSessionRef.current;
    if (session) {
      if (session.timeline.pausedReason === "capacity") {
        session.timeline.pausedReason = null;
        setMoreHint("");
        refreshLoadProgress();
      }
      // Timeline cache eviction is bundle-atomic. Let page admission and the
      // timeline-window reconfiguration path converge under the new budget;
      // generic node trimming would desynchronise bundle ownership from cache.
      return;
    }
    let next = trimUniverseWorkingSet(
      current,
      budget,
      lockedKey ? [lockedKey] : [],
    );
    if (
      next.nodes.length > budget.nodes
      || next.relations.length > budget.edges
    ) {
      graphRef.current?.unlockNode();
      setLockedKey(null);
      setSelectedKey(null);
      next = trimUniverseWorkingSet(
        current,
        budget,
      );
      setMoreHint(t("timeline.lockReleasedForCapacity"));
    }
    const unchanged = next.nodes.length === current.nodes.length
      && next.relations.length === current.relations.length
      && next.node_order.every((key, index) => key === current.node_order[index]);
    if (unchanged) return;
    commitWorkingSet(next);
    pruneExpansionState(next);
  }, [
    budget,
    commitWorkingSet,
    lockedKey,
    pruneExpansionState,
    refreshLoadProgress,
    setLockedKey,
    setSelectedKey,
    t,
  ]);

  const resetScene = React.useCallback(
    (epoch: number) => {
      if (epoch < epochRef.current) return;
      epochRef.current = epoch;
      expandAbortRef.current?.abort();
      expandAbortRef.current = null;
      expandingAnchorRef.current = null;
      timelineRequestRef.current?.controller.abort();
      timelineRequestRef.current = null;
      if (snapshotReloadTimerRef.current !== null) {
        window.clearTimeout(snapshotReloadTimerRef.current);
        snapshotReloadTimerRef.current = null;
      }
      snapshotReloadAttemptsRef.current.clear();
      clearTimelineSettle();
      timelineJourneyCommitRef.current = null;
      retainedExplorationRef.current = null;
      contextGraphSessionRef.current = null;
      resetAccumulationKernel(true);
      setContextActivationCount(0);
      sourceSessionRef.current = null;
      setTimelineWindow(null);
      expansionSnapshotsRef.current.clear();
      completedSourcesRef.current.clear();
      refreshLoadProgress();
      expansionCacheRef.current.clear();
      expansionInflightRef.current.clear();
      dispatchUniversePatchReset();
      graphRef.current?.unlockNode();
      clearCameraSchedule();
      cursorsRef.current.clear();
      expandedAnchorsRef.current.clear();
      const empty = emptyUniverseWorkingSet(epoch);
      commitWorkingSet(empty);
      setSummary(null);
      evidenceOriginRef.current = "browse";
      setEvidenceOrigin("browse");
      dispatchSession({ type: "GO_HOME" });
      setLockedKey(null);
      setSelectedKey(null);
      setSourceHits([]);
      viewportSourceRef.current = null;
      setViewportSourceId(null);
      setActivePartition(null);
      dispatchUniverseView({ mode: "overview", source_id: null, progress: 0 });
      setExpandingKey(null);
      setMoreHint("");
      if (interactiveRef.current) {
        const resetEpoch = epoch;
        cameraFrameRef.current = window.requestAnimationFrame(() => {
          cameraFrameRef.current = null;
          if (epochRef.current === resetEpoch) focusOverview();
        });
      }
    },
    [
      clearCameraSchedule,
      clearTimelineSettle,
      commitWorkingSet,
      focusOverview,
      refreshLoadProgress,
      dispatchSession,
      resetAccumulationKernel,
      setLockedKey,
      setSelectedKey,
    ],
  );

  React.useEffect(() => {
    if (!manifest) return;
    if (!observedManifestRef.current) {
      observedManifestRef.current = true;
      manifestVersionRef.current = manifest.version;
      return;
    }
    if (manifestVersionRef.current === manifest.version) return;
    manifestVersionRef.current = manifest.version;
    if (workingRef.current.nodes.length > 0 || sourceSessionRef.current) {
      resetScene(epochRef.current + 1);
    }
  }, [manifest, resetScene]);

  const invalidateSourceSnapshot = React.useCallback((sourceId: string, epoch: number) => {
    if (epoch !== epochRef.current) return;
    const currentSession = sourceSessionRef.current;
    if (currentSession && currentSession.sourceId !== sourceId) return;
    expandAbortRef.current?.abort();
    expandAbortRef.current = null;
    expandingAnchorRef.current = null;
    timelineRequestRef.current?.controller.abort();
    timelineRequestRef.current = null;
    clearTimelineSettle();
    expansionCacheRef.current.clear();
    expansionInflightRef.current.clear();
    dispatchUniversePatchReset();
    expansionSnapshotsRef.current.clear();
    cursorsRef.current.clear();
    expandedAnchorsRef.current.clear();
    completedSourcesRef.current.delete(sourceId);
    graphRef.current?.unlockNode();
    setLockedKey(null);
    setSelectedKey(null);
    setExpandingKey(null);
    const refreshedSession = emptySourceBrowseSession(
      epoch,
      sourceId,
      bundleWindow.eventWindowSize,
      bundleWindow.cacheCapacity,
    );
    sourceSessionRef.current = refreshedSession;
    commitWorkingSet(refreshedSession.working);
    setTimelineWindow(refreshedSession.timeline.window);
    refreshLoadProgress();
    setSummary({
      query: sourceById.get(sourceId)?.label ?? t("timeline.defaultTitle"),
      events: 0,
      entities: 0,
      relations: 0,
    });
    const reloadAttempt = (snapshotReloadAttemptsRef.current.get(sourceId) ?? 0) + 1;
    snapshotReloadAttemptsRef.current.set(sourceId, reloadAttempt);
    if (snapshotReloadTimerRef.current !== null) {
      window.clearTimeout(snapshotReloadTimerRef.current);
      snapshotReloadTimerRef.current = null;
    }
    if (reloadAttempt > 1) {
      setMoreHint(t("timeline.snapshotReset"));
      return;
    }
    setMoreHint(t("timeline.snapshotReloading"));
    snapshotReloadTimerRef.current = window.setTimeout(() => {
      snapshotReloadTimerRef.current = null;
      if (
        !interactiveRef.current
        || epochRef.current !== epoch
        || sourceSessionRef.current !== refreshedSession
      ) return;
      const loader = timelinePageLoaderRef.current;
      if (!loader) {
        setMoreHint(t("timeline.snapshotReset"));
        return;
      }
      void loader(sourceId, "source-entry").then((result) => {
        if (
          result === "loaded"
          && sourceSessionRef.current === refreshedSession
        ) {
          snapshotReloadAttemptsRef.current.delete(sourceId);
        }
      });
    }, 0);
  }, [bundleWindow, clearTimelineSettle, commitWorkingSet, refreshLoadProgress, setLockedKey, setSelectedKey, sourceById, t]);

  const releaseReadingFocus = React.useCallback(() => {
    graphRef.current?.clearSelection();
    setLockedKey(null);
    setSelectedKey(null);
  }, [setLockedKey, setSelectedKey]);

  const retainCurrentExploration = React.useCallback(() => {
    const retained = retainedExplorationRef.current;
    if (retained) return retained;
    const session = sourceSessionRef.current;
    const settledWindow = session
      ? settleUniverseTimelineWindow(session.timeline.window)
      : timelineWindow;
    if (session) {
      session.timeline.loading = false;
      session.timeline.window = settledWindow as UniverseTimelineWindowState;
    }
    const snapshot: RetainedExploration = {
      session,
      working: workingRef.current,
      timelineWindow: settledWindow,
      activePartition,
      viewportSourceId: viewportSourceRef.current,
      selectedKey,
      lockedKey,
      sourceHits: [...sourceHits],
      summary: summary ? { ...summary } : null,
      moreHint,
      view: graphRef.current?.captureExplorationView() ?? null,
      expansionSnapshots: new Map(expansionSnapshotsRef.current),
      completedSources: new Set(completedSourcesRef.current),
      expansionCache: new Map(expansionCacheRef.current),
      cursors: new Map(cursorsRef.current),
      expandedAnchors: new Set(expandedAnchorsRef.current),
    };
    retainedExplorationRef.current = snapshot;
    setTimelinePlaying(false);
    expandAbortRef.current?.abort();
    expandAbortRef.current = null;
    timelineRequestRef.current?.controller.abort();
    timelineRequestRef.current = null;
    clearTimelineSettle();
    timelineJourneyCommitRef.current = null;
    if (snapshotReloadTimerRef.current !== null) {
      window.clearTimeout(snapshotReloadTimerRef.current);
      snapshotReloadTimerRef.current = null;
    }
    return snapshot;
  }, [
    activePartition,
    clearTimelineSettle,
    lockedKey,
    moreHint,
    selectedKey,
    sourceHits,
    summary,
    timelineWindow,
  ]);

  const prepareContextScene = React.useCallback((
    sceneEpoch: number,
    origin: UniverseActivationOrigin,
  ) => {
    contextProjectionClosedRef.current = false;
    epochRef.current = sceneEpoch;
    contextGraphSessionRef.current = {
      sceneEpoch,
      activationCount: 0,
      answerTurnCount: 0,
    };
    expandAbortRef.current?.abort();
    expandAbortRef.current = null;
    expandingAnchorRef.current = null;
    timelineRequestRef.current?.controller.abort();
    timelineRequestRef.current = null;
    snapshotReloadAttemptsRef.current.clear();
    clearTimelineSettle();
    timelineJourneyCommitRef.current = null;
    sourceSessionRef.current = null;
    setTimelineWindow(null);
    expansionSnapshotsRef.current.clear();
    completedSourcesRef.current.clear();
    expansionCacheRef.current.clear();
    expansionInflightRef.current.clear();
    dispatchUniversePatchReset();
    graphRef.current?.unlockNode();
    clearCameraSchedule();
    cursorsRef.current.clear();
    expandedAnchorsRef.current.clear();
    commitWorkingSet(emptyUniverseWorkingSet(sceneEpoch));
    setContextActivationCount(0);
    resetAccumulationKernel(false);
    setLockedKey(null);
    setSelectedKey(null);
    setSourceHits([]);
    setSummary(null);
    evidenceOriginRef.current = origin;
    setEvidenceOrigin(origin);
    dispatchSession({
      type: "ENTER_ACCUMULATION",
      origin,
      sourceId: retainedExplorationRef.current?.session?.sourceId ?? null,
      snapshotAvailable: Boolean(retainedExplorationRef.current),
    });
    viewportSourceRef.current = null;
    setViewportSourceId(null);
    setActivePartition(null);
    setExpandingKey(null);
    setMoreHint("");
    refreshLoadProgress();
  }, [
    clearCameraSchedule,
    clearTimelineSettle,
    commitWorkingSet,
    dispatchSession,
    refreshLoadProgress,
    resetAccumulationKernel,
    setLockedKey,
    setSelectedKey,
  ]);

  const restoreRetainedExploration = React.useCallback(() => {
    const snapshot = retainedExplorationRef.current;
    contextGraphSessionRef.current = null;
    setContextActivationCount(0);
    if (!snapshot) {
      resetScene(epochRef.current + 1);
      return;
    }
    retainedExplorationRef.current = null;
    resetAccumulationKernel(false);
    expandAbortRef.current?.abort();
    expandAbortRef.current = null;
    expandingAnchorRef.current = null;
    expansionInflightRef.current.clear();
    dispatchUniversePatchReset();
    graphRef.current?.unlockNode();
    clearCameraSchedule();
    sourceSessionRef.current = snapshot.session;
    if (snapshot.session) {
      snapshot.session.working = snapshot.working;
      snapshot.session.timeline.loading = false;
      if (snapshot.timelineWindow) {
        snapshot.session.timeline.window = snapshot.timelineWindow;
      }
    }
    epochRef.current = snapshot.working.epoch;
    expansionSnapshotsRef.current = new Map(snapshot.expansionSnapshots);
    completedSourcesRef.current = new Set(snapshot.completedSources);
    expansionCacheRef.current = new Map(snapshot.expansionCache);
    cursorsRef.current = new Map(snapshot.cursors);
    expandedAnchorsRef.current = new Set(snapshot.expandedAnchors);
    commitWorkingSet(snapshot.working);
    setTimelineWindow(snapshot.timelineWindow);
    evidenceOriginRef.current = "browse";
    setEvidenceOrigin("browse");
    if (snapshot.session?.sourceId) {
      dispatchSession({
        type: "RETURN_TO_EXPLORATION",
        sourceId: snapshot.session.sourceId,
      });
    } else {
      dispatchSession({ type: "GO_HOME" });
    }
    setSourceHits(snapshot.sourceHits);
    setSummary(snapshot.summary);
    viewportSourceRef.current = snapshot.viewportSourceId;
    setViewportSourceId(snapshot.viewportSourceId);
    setActivePartition(snapshot.activePartition);
    setLockedKey(snapshot.lockedKey);
    setSelectedKey(snapshot.selectedKey);
    setExpandingKey(null);
    setMoreHint(snapshot.moreHint);
    refreshLoadProgress();
    const view = snapshot.view;
    const sessionSourceId = snapshot.session?.sourceId ?? null;
    const lockedNodeKey = snapshot.lockedKey;
    cameraFrameRef.current = window.requestAnimationFrame(() => {
      cameraFrameRef.current = null;
      if (view) graphRef.current?.restoreExplorationView(view);
      else if (sessionSourceId) graphRef.current?.focusSource(sessionSourceId);
      else graphRef.current?.focusOverview();
      if (lockedNodeKey) graphRef.current?.lockNode(lockedNodeKey);
    });
  }, [
    clearCameraSchedule,
    commitWorkingSet,
    dispatchSession,
    refreshLoadProgress,
    resetScene,
    resetAccumulationKernel,
    setLockedKey,
    setSelectedKey,
  ]);

  React.useEffect(() => {
    const onActivate = (event: Event) => {
      const activation = (event as CustomEvent<UniverseActivation>).detail;
      const eventEpoch = activation?.epoch ?? activationEventEpochRef.current + 1;
      if (!activation || eventEpoch < activationEventEpochRef.current) return;
      activationEventEpochRef.current = eventEpoch;
      const origin = activation.origin ?? "assistant";
      const context = readUniverseContext();
      if (
        origin !== "browse"
        && contextProjectionClosedRef.current
        && !context.active
        && sessionStateRef.current.mode !== "accumulation"
      ) {
        // A result that finishes after the user returned must never replace the
        // restored timeline. The conversation may keep streaming in its panel;
        // its graph projection belongs only to an active contextual workspace.
        return;
      }
      const retained = sessionStateRef.current.mode === "exploration"
        ? retainCurrentExploration()
        : retainedExplorationRef.current;
      if (origin === "assistant") {
        // A question starts a new reading beat. Release the card that launched
        // it so the incoming evidence owns the visual focus.
        if (retained) {
          retained.lockedKey = null;
          retained.selectedKey = null;
        }
        releaseReadingFocus();
      }
      if (!contextGraphSessionRef.current) {
        prepareContextScene(eventEpoch, origin);
      }
      const contextSession = contextGraphSessionRef.current;
      if (!contextSession) return;
      const firstActivation = contextSession.activationCount === 0;
      const normalizedActivation = {
        ...activation,
        epoch: contextSession.sceneEpoch,
      };
      contextSession.activationCount += 1;
      if (origin === "assistant") contextSession.answerTurnCount += 1;
      setContextActivationCount(contextSession.answerTurnCount);
      evidenceOriginRef.current = origin;
      setEvidenceOrigin(origin);
      appendActivationToAccumulation(normalizedActivation, origin);
      const sourceId = dominantSource(activation);
      setActivePartition(sourceId);
      if (
        firstActivation
        && sourceId
        && !(activation.source_hits?.length)
        && interactiveRef.current
      ) {
        focusTimerRef.current = window.setTimeout(() => {
          focusTimerRef.current = null;
          if (contextGraphSessionRef.current === contextSession) {
            focusPartition(sourceId);
          }
        }, reducedMotion ? 0 : 80);
      }
    };
    const onReset = (event: Event) => {
      const value = (event as CustomEvent<{ epoch: number; owner?: string }>).detail;
      const eventEpoch = value?.epoch ?? activationEventEpochRef.current + 1;
      if (eventEpoch < activationEventEpochRef.current) return;
      activationEventEpochRef.current = eventEpoch;
      const searchReset = value?.owner?.startsWith("search") ?? false;
      const context = readUniverseContext();
      if (
        searchReset
        && (context.active || Boolean(contextGraphSessionRef.current))
      ) {
        retainCurrentExploration();
        prepareContextScene(eventEpoch, "search");
        return;
      }
      if (
        searchReset
        && sourceSessionRef.current
        && sessionStateRef.current.mode === "exploration"
      ) {
        return;
      }
      retainedExplorationRef.current = null;
      contextGraphSessionRef.current = null;
      setContextActivationCount(0);
      resetScene(eventEpoch);
    };
    const onResume = () => {
      contextProjectionClosedRef.current = true;
      restoreRetainedExploration();
    };
    const onFocus = (event: Event) => {
      const value = (
        event as CustomEvent<{
          kind: UniverseNodeKind;
          id: string;
          source_id: string;
          lock?: boolean;
        }>
      ).detail;
      if (!value) return;
      clearCameraSchedule();
      const exactKey = universeNodeKey(value.kind, value.id, value.source_id);
      const node = nodeByIdRef.current.get(exactKey);
      if (!node) return;
      if (value.lock && isConcreteUniverseNode(node)) {
        setTimelinePlaying(false);
        if (timelineRequestRef.current?.cause !== "source-entry") {
          timelineRequestRef.current?.controller.abort();
        }
        setLockedKey(node.id);
        setSelectedKey(node.id);
        graphRef.current?.lockNode(node.id);
        if (interactiveRef.current) focusNode(node);
        return;
      }
      graphRef.current?.unlockNode();
      setLockedKey(null);
      setSelectedKey(node.id);
      if (interactiveRef.current) focusNode(node);
    };
    window.addEventListener(UNIVERSE_ACTIVATE_EVENT, onActivate);
    window.addEventListener(UNIVERSE_RESET_EVENT, onReset);
    window.addEventListener(UNIVERSE_RESUME_EVENT, onResume);
    window.addEventListener(UNIVERSE_FOCUS_EVENT, onFocus);
    return () => {
      window.removeEventListener(UNIVERSE_ACTIVATE_EVENT, onActivate);
      window.removeEventListener(UNIVERSE_RESET_EVENT, onReset);
      window.removeEventListener(UNIVERSE_RESUME_EVENT, onResume);
      window.removeEventListener(UNIVERSE_FOCUS_EVENT, onFocus);
    };
  }, [
    appendActivationToAccumulation,
    clearCameraSchedule,
    focusNode,
    focusPartition,
    prepareContextScene,
    reducedMotion,
    releaseReadingFocus,
    restoreRetainedExploration,
    retainCurrentExploration,
    resetScene,
    setLockedKey,
    setSelectedKey,
  ]);

  const graphData = React.useMemo(() => {
    const accumulationMode = sceneStrategy === "accumulation";
    const visualRadiusBySource = new Map(
      renderedSourcePartitions.map((partition) => [
        partition.source_id,
        visualNebulaRadius(
          partition.event_count,
          partition.entity_count,
          renderedSourcePartitions.length,
        ),
      ]),
    );
    const nodes: Universe3DNode[] = accumulationMode ? [] : renderedSourcePartitions.map((partition) => ({
      id: `partition:${partition.source_id}`,
      kind: "source",
      rawId: partition.id,
      sourceId: partition.source_id,
      label: partition.label,
      description: "",
      category: t("nodeKinds.source"),
      radius: visualRadiusBySource.get(partition.source_id) ?? partition.radius,
      density: partition.density,
      eventCount: partition.event_count,
      entityCount: partition.entity_count,
      relationCount: partition.relation_count,
      relatedCount: partition.relation_count,
      relatedCountKnown: true,
      importance: partition.importance,
      statsReady: Boolean(manifest?.version),
      state: "active",
      root: true,
      x: partition.x,
      y: partition.y,
      z: partition.z,
    }));
    const links: Universe3DLink[] = [];

    const visibleTimelineBundleIds = timelineVisibleBundleIds;
    const categoryProjectedWorking = projectUniverseWorkingSet(
      working,
      projectedEntityTypes,
    );
    const timelineProjectionIds = timelineProjectionBundleIds(
      categoryProjectedWorking,
      timelineCacheBundleIds,
      visibleTimelineBundleIds,
    );
    const visibleTimelineSet = new Set(visibleTimelineBundleIds);
    const timelineSupportBundleIds = timelineProjectionIds.filter((id) =>
      !visibleTimelineSet.has(id)).reverse();
    const projectedWorking = browseSessionSourceId
      && timelineProjectionIds.length > 0
      ? projectUniverseBundleWindowWithinBudget(
          categoryProjectedWorking,
          visibleTimelineBundleIds,
          timelineSupportBundleIds,
          timelineProjectionBudget,
          bundleWindow.eventWindowSize,
          timelineBundleEntityLimit,
        )
      : categoryProjectedWorking;
    const relatedProgressByKey = new Map<string, Set<string>>();
    working.relations.forEach((relation) => {
      const eventKey = universeNodeKey("event", relation.from_id, relation.source_id);
      const eventProgress = relatedProgressByKey.get(eventKey) ?? new Set<string>();
      eventProgress.add(`${relation.kind}:${relation.to_id}`);
      relatedProgressByKey.set(eventKey, eventProgress);
      if (relation.kind !== "mentions") return;
      const entityKey = universeNodeKey("entity", relation.to_id, relation.source_id);
      const entityProgress = relatedProgressByKey.get(entityKey) ?? new Set<string>();
      entityProgress.add(relation.from_id);
      relatedProgressByKey.set(entityKey, entityProgress);
    });
    const relationsByEvent = new Map<string, UniverseWorkingSet["relations"]>();
    const relationsByEntity = new Map<string, UniverseWorkingSet["relations"]>();
    const relationSourceByEvent = new Map<string, string>();
    const relationSourceByEntity = new Map<string, string>();
    projectedWorking.relations.forEach((relation) => {
      const eventKey = universeNodeKey("event", relation.from_id, relation.source_id);
      const eventRelations = relationsByEvent.get(eventKey) ?? [];
      eventRelations.push(relation);
      relationsByEvent.set(eventKey, eventRelations);
      relationSourceByEvent.set(relation.from_id, relation.source_id);
      if (relation.kind === "mentions") {
        const entityKey = universeNodeKey("entity", relation.to_id, relation.source_id);
        const entityRelations = relationsByEntity.get(entityKey) ?? [];
        entityRelations.push(relation);
        relationsByEntity.set(entityKey, entityRelations);
        relationSourceByEntity.set(relation.to_id, relation.source_id);
      }
    });
    const workingNodeByKey = new Map(
      projectedWorking.nodes.map((node) => [
        universeNodeKey(node.kind, node.id, node.source_id),
        node,
      ]),
    );
    const visibleNodes = projectedWorking.nodes.map((node) => workingNodeByKey.get(
      universeNodeKey(node.kind, node.id, node.source_id),
    ) ?? node);
    const timelineEventPlacementByKey = new Map<string, {
      bundleId: string;
      index: number;
      total: number;
    }>();
    const timelineBrowseActive = Boolean(browseSessionSourceId);
    const projectedBundleIds = new Set(projectedWorking.bundle_order);
    const temporalBundleByEventKey = new Map<string, string>();
    const temporalOrdinalByBundleId = new Map<string, number>();
    visibleTimelineBundleIds.forEach((bundleId) => {
      const workingBundle = working.bundles[bundleId];
      workingBundle?.node_keys.forEach((key) => {
        const workingNode = workingNodeByKey.get(key);
        if (workingNode?.kind !== "event") return;
        temporalBundleByEventKey.set(key, bundleId);
        if (Number.isInteger(workingBundle.ordinal)) {
          temporalOrdinalByBundleId.set(bundleId, workingBundle.ordinal as number);
        }
      });
    });
    // The axis is the source's snapshot-stable exploration order, never the
    // visible window: an event's depth may not move because the cache did.
    // Every browsed source carries an axis; the spiral remains only for
    // expansion bundles, which explore off the timeline on purpose.
    const browseSession = sourceSessionRef.current;
    const temporalAxis = browseSessionSourceId
      && browseSession?.sourceId === browseSessionSourceId
      ? createUniverseTemporalAxis(browseSession.timeline.totalEvents ?? 0)
      : null;
    const temporalAxisDepth = universeTemporalAxisDepth(
      temporalAxis,
      TEMPORAL_AXIS_UNITS_PER_EVENT,
    );
    const temporalProjectionByBundleId = new Map(
      (temporalAxis
        ? projectUniverseTemporalAxis(
            visibleTimelineBundleIds.flatMap((bundleId) => {
              const ordinal = temporalOrdinalByBundleId.get(bundleId);
              return ordinal === undefined ? [] : [{ bundleId, ordinal }];
            }),
            temporalAxis,
            {
              angularPhase: stableUnit(`${browseSessionSourceId}:timeline-phase`)
                * Math.PI * 2,
            },
          )
        : []
      ).map((projection) => [projection.bundleId, projection]),
    );
    const temporalBundleByEntityKey = new Map<string, string>();
    const visibleTimelineNodeKeys = new Set(
      visibleTimelineBundleIds
        .filter((bundleId) => projectedBundleIds.has(bundleId))
        .flatMap((bundleId) => working.bundles[bundleId]?.node_keys ?? []),
    );
    const placementBundleByEvent = new Map<string, string>();
    const appendPlacement = (eventKey: string, bundleId: string) => {
      if (workingNodeByKey.get(eventKey)?.kind !== "event") return;
      // A recent explicit support owner promotes the same canonical event to
      // the current exploration focus without creating a second scene node.
      placementBundleByEvent.delete(eventKey);
      placementBundleByEvent.set(eventKey, bundleId);
    };
    visibleTimelineBundleIds.forEach((bundleId) => {
      if (!projectedBundleIds.has(bundleId)) return;
      working.bundles[bundleId]?.node_keys.forEach((key) =>
        appendPlacement(key, bundleId));
    });
    projectedWorking.bundle_order.forEach((bundleId) => {
      const bundle = projectedWorking.bundles[bundleId];
      if (bundle?.origin !== "expansion") return;
      bundle.node_keys.forEach((key) => appendPlacement(key, bundleId));
    });
    const placementEntries = [...placementBundleByEvent.entries()];
    placementEntries.forEach(([eventKey, bundleId], index) => {
      timelineEventPlacementByKey.set(eventKey, {
        bundleId,
        index,
        total: placementEntries.length,
      });
    });
    const isVisualRoot = (node: (typeof visibleNodes)[number]) => {
      if (!timelineBrowseActive) return node.root;
      const key = universeNodeKey(node.kind, node.id, node.source_id);
      if (node.kind === "event") return timelineEventPlacementByKey.has(key);
      return visibleTimelineNodeKeys.has(
        key,
      );
    };
    const projectedEventIdentity = visibleNodes
      .filter((node) => node.kind === "event")
      .map((node) => universeNodeKey(node.kind, node.id, node.source_id))
      .sort();
    const sceneWindowRevision = visibleTimelineBundleIds.length > 0
      ? Math.floor(stableUnit(
          [
            "timeline",
            browseSessionSourceId ?? activePartition ?? "none",
            visibleTimelineBundleIds.join("|"),
            projectedEventIdentity.join("|"),
          ].join(":"),
        ) * 0x7fffffff)
      : 0;
    const resolvedSource = (kind: UniverseNodeKind, id: string, sourceId: string) => {
      if (sourceId) return sourceId;
      return (kind === "event"
        ? relationSourceByEvent.get(id)
        : relationSourceByEntity.get(id)) || browseSessionSourceId || activePartition || "";
    };
    const exactByRaw = new Map<string, string>();
    const positionByRaw = new Map<string, Position3D>();
    const satelliteSlotByEntityKey = new Map<string, {
      index: number;
      total: number;
      phaseKey: string;
    }>();

    const addExactNode = (
      node: (typeof visibleNodes)[number],
      anchor?: Position3D,
      rootEventPlacement?: { index: number; total: number },
      satelliteSlot?: { index: number; total: number; phaseKey: string },
    ) => {
      const sourceId = resolvedSource(node.kind, node.id, node.source_id);
      const key = universeNodeKey(node.kind, node.id, sourceId);
      const partition = sourceById.get(sourceId);
      const center = accumulationMode && !anchor
        ? { x: 0, y: 0, z: 0 }
        : anchor ?? {
        x: partition?.x ?? 0,
        y: partition?.y ?? 0,
        z: partition?.z ?? 0,
      };
      const radius = anchor
        ? LOCAL_ENTITY_SPREAD_MIN
          + stableUnit(`${key}:local-radius`) * LOCAL_ENTITY_SPREAD_RANGE
        : Math.max(72, (visualRadiusBySource.get(sourceId) ?? 72) * 1.02);
      const timelinePlacement = timelineEventPlacementByKey.get(key);
      const temporalBundleId = node.kind === "event"
        ? temporalBundleByEventKey.get(key)
        : temporalBundleByEntityKey.get(key);
      const temporalProjection = temporalBundleId
        ? temporalProjectionByBundleId.get(temporalBundleId)
        : undefined;
      const relatedProgress = relatedProgressByKey.get(key)?.size ?? 0;
      const relatedTotal = node.related_count === undefined
        ? null
        : Math.max(relatedProgress, node.related_count);
      const expansionExhausted = expandedAnchorsRef.current.has(key)
        && !cursorsRef.current.has(key);
      const offset = accumulationMode
        ? anchor
          ? stableSatelliteOffset(key, radius, center, satelliteSlot)
          : stableAccumulationEventOffset(key)
        : timelinePlacement
        // An expansion-discovered event is placed but off the time axis: only
        // visible timeline packages carry a temporal projection.
        ? temporalProjection
          ? {
              x: temporalProjection.normalizedOffset.x * radius
                * TIMELINE_EVENT_LATERAL_SPREAD,
              y: temporalProjection.normalizedOffset.y * radius
                * TIMELINE_EVENT_LATERAL_SPREAD,
              // Every event sits one vestibule deeper than its ordinal says:
              // flight depth 0 is the hero pose in front of the intact nebula,
              // and the first event only condenses after crossing it.
              z: temporalProjection.normalizedOffset.z * temporalAxisDepth
                - UNIVERSE_TEMPORAL_AXIS_VESTIBULE_UNITS,
            }
          : stableRootEventOffset(
              sourceId,
              key,
              radius,
              timelinePlacement.index,
              timelinePlacement.total,
            )
        : rootEventPlacement
          ? stableRootEventOffset(
            sourceId,
            key,
            radius,
            rootEventPlacement.index,
            rootEventPlacement.total,
          )
          : anchor
            ? stableSatelliteOffset(key, radius, {
                x: center.x - (partition?.x ?? 0),
                y: center.y - (partition?.y ?? 0),
                z: center.z - (partition?.z ?? 0),
              }, satelliteSlot)
            : stableOffset(key, radius);
      const position = {
        x: center.x + offset.x,
        y: center.y + offset.y,
        z: center.z + offset.z,
      };
      nodes.push({
        id: key,
        kind: node.kind,
        rawId: node.id,
        sourceId,
        label: node.label,
        description: node.description ?? "",
        category: node.category ?? t(`nodeKinds.${node.kind}`),
        radius: 0,
        density: 0,
        eventCount: 0,
        entityCount: 0,
        relationCount: 0,
        relatedCount: node.related_count ?? 0,
        relatedCountKnown: node.related_count !== undefined,
        relatedProgress,
        canExploreMore: !expansionExhausted
          && (relatedTotal === null || relatedProgress < relatedTotal),
        importance: node.importance ?? 0.5,
        statsReady: true,
        state: node.state ?? "active",
        root: accumulationMode ? node.kind === "event" : isVisualRoot(node),
        // Depth presence (scale/opacity along the axis) is the camera's story
        // now: the scene computes it per frame from the flight depth, so a
        // package the camera reaches is always fully present.
        timelineBundleId: temporalBundleId,
        timelineOrder: node.kind === "event" ? timelinePlacement?.index : undefined,
        ...position,
      });
      exactByRaw.set(key, key);
      positionByRaw.set(key, position);
    };
    const preferredEntityRelation = (entityKey: string) => {
      const candidates = [...(relationsByEntity.get(entityKey) ?? [])];
      const positioned = candidates.flatMap((relation) => {
        const position = positionByRaw.get(
          universeNodeKey("event", relation.from_id, relation.source_id),
        );
        return position ? [{ relation, position }] : [];
      });
      if (positioned.length > 1) {
        return positioned
          .map((candidate) => ({
            ...candidate,
            distance: positioned.reduce((total, other) => total + Math.hypot(
              candidate.position.x - other.position.x,
              candidate.position.y - other.position.y,
              candidate.position.z - other.position.z,
            ), 0),
          }))
          .sort((left, right) => left.distance - right.distance
            || universeRelationKey(left.relation).localeCompare(
              universeRelationKey(right.relation),
            ))[0]?.relation;
      }
      return candidates.sort((left, right) => {
          const leftPlacement = timelineEventPlacementByKey.get(
            universeNodeKey("event", left.from_id, left.source_id),
          );
          const rightPlacement = timelineEventPlacementByKey.get(
            universeNodeKey("event", right.from_id, right.source_id),
          );
          return (rightPlacement?.index ?? -1) - (leftPlacement?.index ?? -1)
            || universeRelationKey(left).localeCompare(universeRelationKey(right));
        })[0];
    };

    const rootEvents = visibleNodes.filter((node) => {
      if (node.kind !== "event") return false;
      return accumulationMode || isVisualRoot(node);
    });
    rootEvents.forEach((node, index) => addExactNode(node, undefined, {
      index,
      total: rootEvents.length,
    }));
    const entityKeysByPreferredEvent = new Map<string, string[]>();
    relationsByEntity.forEach((_relations, entityKey) => {
      const relation = preferredEntityRelation(entityKey);
      if (!relation) return;
      const eventKey = universeNodeKey(
        "event",
        relation.from_id,
        relation.source_id,
      );
      const entityKeys = entityKeysByPreferredEvent.get(eventKey) ?? [];
      entityKeys.push(entityKey);
      entityKeysByPreferredEvent.set(eventKey, entityKeys);
    });
    entityKeysByPreferredEvent.forEach((entityKeys, eventKey) => {
      entityKeys.sort().forEach((entityKey, index) => {
        satelliteSlotByEntityKey.set(entityKey, {
          index,
          total: entityKeys.length,
          phaseKey: eventKey,
        });
      });
    });
    relationsByEntity.forEach((_relations, entityKey) => {
      const relation = preferredEntityRelation(entityKey);
      if (!relation) return;
      const eventBundleId = temporalBundleByEventKey.get(
        universeNodeKey("event", relation.from_id, relation.source_id),
      );
      if (eventBundleId) temporalBundleByEntityKey.set(entityKey, eventBundleId);
    });
    visibleNodes
      .filter((node) => node.kind === "entity" && isVisualRoot(node))
      .forEach((node) => {
        const relation = preferredEntityRelation(
          universeNodeKey("entity", node.id, node.source_id),
        );
        addExactNode(
          node,
          relation
            ? positionByRaw.get(universeNodeKey("event", relation.from_id, relation.source_id))
            : undefined,
          undefined,
          satelliteSlotByEntityKey.get(
            universeNodeKey("entity", node.id, node.source_id),
          ),
        );
      });
    visibleNodes
      .filter((node) => {
        const key = universeNodeKey(node.kind, node.id, node.source_id);
        return !exactByRaw.has(key);
      })
      .forEach((node) => {
        const key = universeNodeKey(node.kind, node.id, node.source_id);
        const relation = node.kind === "entity"
          ? preferredEntityRelation(key)
          : relationsByEvent.get(key)?.[0];
        const anchor = relation
          ? node.kind === "entity"
            ? positionByRaw.get(
                universeNodeKey("event", relation.from_id, relation.source_id),
              )
            : positionByRaw.get(
                universeNodeKey("entity", relation.to_id, relation.source_id),
              )
          : undefined;
        addExactNode(
          node,
          anchor,
          undefined,
          node.kind === "entity"
            ? satelliteSlotByEntityKey.get(key)
            : undefined,
        );
      });

    projectedWorking.relations.forEach((relation) => {
      const source = exactByRaw.get(
        universeNodeKey("event", relation.from_id, relation.source_id),
      );
      const targetKind = relation.kind === "subevent" ? "event" : "entity";
      const target = exactByRaw.get(
        universeNodeKey(targetKind, relation.to_id, relation.source_id),
      );
      if (!source || !target) return;
      links.push({
        id: `${relation.source_id}:${relation.kind}:${relation.from_id}:${relation.to_id}`,
        source,
        target,
        weight: relation.weight,
        virtual: false,
      });
    });
    const journeyCommit = timelineJourneyCommitRef.current;
    const windowChangeCause = timelineWindowRevision >= 0
      && journeyCommit?.session === sourceSessionRef.current
      && journeyCommit.revision === timelineWindowRevision
      && journeyCommit.cause === "journey"
      ? "journey"
      : "synchronization";
    // The visible window's depth band tells the flight when the camera is
    // running out of loaded packages and the window has to page along.
    let windowNearAge = Number.POSITIVE_INFINITY;
    let windowFarAge = Number.NEGATIVE_INFINITY;
    temporalProjectionByBundleId.forEach((projection) => {
      windowNearAge = Math.min(windowNearAge, projection.ageProgress);
      windowFarAge = Math.max(windowFarAge, projection.ageProgress);
    });
    const temporalFlight = !accumulationMode && temporalAxis && browseSessionSourceId
      ? {
          sourceId: browseSessionSourceId,
          centerZ: sourceById.get(browseSessionSourceId)?.z ?? 0,
          unitsPerEvent: TEMPORAL_AXIS_UNITS_PER_EVENT,
          vestibuleDepth: UNIVERSE_TEMPORAL_AXIS_VESTIBULE_UNITS,
          maxDepth: temporalAxisDepth + UNIVERSE_TEMPORAL_AXIS_VESTIBULE_UNITS,
          windowNearDepth: (Number.isFinite(windowNearAge)
            ? windowNearAge * temporalAxisDepth
            : 0) + UNIVERSE_TEMPORAL_AXIS_VESTIBULE_UNITS,
          windowFarDepth: (Number.isFinite(windowFarAge)
            ? windowFarAge * temporalAxisDepth
            : 0) + UNIVERSE_TEMPORAL_AXIS_VESTIBULE_UNITS,
        }
      : null;
    const candidate = {
      epoch: working.epoch,
      windowRevision: sceneWindowRevision,
      windowChangeCause,
      windowDirection: windowChangeCause === "journey"
        ? journeyCommit?.direction
        : undefined,
      temporalFlight,
      nodes,
      links,
    } satisfies UniverseSceneData;
    const signature = universeSceneDataSignature(candidate);
    const cached = graphDataCacheRef.current;
    const stableData = cached?.signature === signature
      ? cached.data
      : candidate;
    if (stableData === candidate) {
      graphDataCacheRef.current = { signature, data: candidate };
    }
    nodeByIdRef.current = new Map(stableData.nodes.map((node) => [node.id, node]));
    return stableData;
  }, [
    activePartition,
    sceneStrategy,
    browseSessionSourceId,
    manifest?.version,
    renderedSourcePartitions,
    sourceById,
    t,
    bundleWindow.eventWindowSize,
    projectedEntityTypes,
    timelineBundleEntityLimit,
    timelineProjectionBudget,
    timelineCacheBundleIds,
    timelineVisibleBundleIds,
    timelineWindowRevision,
    working,
  ]);
  React.useEffect(() => {
    const journeyCommit = timelineJourneyCommitRef.current;
    if (
      graphData.windowChangeCause !== "journey"
      || !journeyCommit
      || journeyCommit.session !== sourceSessionRef.current
      || journeyCommit.revision !== timelineWindowRevision
    ) return;
    // A journey marker describes exactly one scene delivery. Consuming it
    // prevents later expansion/filter synchronizations at the same timeline
    // revision from inheriting a stale forward/backward edge animation.
    timelineJourneyCommitRef.current = null;
  }, [graphData, timelineWindowRevision]);
  const visibleGraphCounts = React.useMemo(() => ({
    events: graphData.nodes.filter((node) => node.kind === "event").length,
    entities: graphData.nodes.filter((node) => node.kind === "entity").length,
    relations: graphData.links.length,
  }), [graphData.links.length, graphData.nodes]);
  const entityCategories = React.useMemo(
    () => [...new Set(working.nodes
      .filter((node) => node.kind === "entity")
      .map((node) => node.category?.trim())
      .filter((category): category is string => Boolean(category)))]
      .sort((left, right) => left.localeCompare(right, locale)),
    [locale, working.nodes],
  );
  React.useEffect(() => {
    if (entityCategories.length > 0) {
      publishUniverseEntityCategories(entityCategories);
    }
  }, [entityCategories]);
  const visibleTimelineEventNodes = React.useMemo(() => {
    const deque = sourceSessionRef.current?.timeline.deque;
    if (!deque || !timelineWindow) return [];
    const visibleBundleIds = new Set(timelineWindow.visibleBundleIds);
    const graphNodeById = new Map(graphData.nodes.map((node) => [node.id, node]));
    return deque.bundles
      .filter((bundle) => visibleBundleIds.has(bundle.bundle_id))
      .map((bundle) => graphNodeById.get(universeNodeKey(
        "event",
        bundle.event.id,
        bundle.event.source_id,
      )))
      .filter((node): node is UniverseConcrete3DNode => Boolean(
        node && node.kind === "event",
      ));
  }, [graphData.nodes, timelineWindow]);

  const requestExpansion = React.useCallback(
    (
      node: Universe3DNode & { kind: "event" | "entity" },
      cursor: string | null,
      snapshot: ExpansionSnapshotContext | null,
      signal: AbortSignal,
    ) => {
      const epoch = epochRef.current;
      const boundCacheKey = snapshot
        ? universeExpansionCacheKey(
            epoch,
            node.sourceId,
            snapshot.sourceRevision,
            snapshot.snapshotId,
            node.kind,
            node.rawId,
            cursor,
          )
        : null;
      if (boundCacheKey) {
        const cached = expansionCacheRef.current.get(boundCacheKey);
        if (cached) {
          expansionCacheRef.current.delete(boundCacheKey);
          expansionCacheRef.current.set(boundCacheKey, cached);
          return Promise.resolve(cached);
        }
      }
      const inflightKey = boundCacheKey ?? [
        epoch,
        node.sourceId,
        "unsigned-root",
        node.kind,
        node.rawId,
      ].join(":");
      const pending = expansionInflightRef.current.get(inflightKey);
      if (pending && !pending.signal.aborted) return pending.promise;
      if (pending) expansionInflightRef.current.delete(inflightKey);
      const request = api
        .universeExpand(
          {
            epoch,
            source_id: node.sourceId,
            node_kind: node.kind,
            node_id: node.rawId,
            limit: node.kind === "event"
              ? Math.min(
                  EVENT_ENTITY_PROJECTION_LIMIT,
                  manifest?.policy.event_entity_limit ?? budgetRef.current.nodes,
                  Math.max(1, budgetRef.current.nodes - 1),
                )
              : Math.min(
                  ENTITY_EXPANSION_EVENT_LIMIT,
                  Math.max(1, bundleWindow.eventWindowSize - 1),
                  Math.max(1, budgetRef.current.nodes - 1),
                ),
            cursor,
            snapshot_id: snapshot?.snapshotId ?? null,
          },
          signal,
        )
        .finally(() => {
          if (expansionInflightRef.current.get(inflightKey)?.promise === request) {
            expansionInflightRef.current.delete(inflightKey);
          }
        });
      expansionInflightRef.current.set(inflightKey, { promise: request, signal });
      return request;
    },
    [bundleWindow.eventWindowSize, manifest?.policy.event_entity_limit],
  );

  const expandNode = React.useCallback(
    async (node: Universe3DNode) => {
      if (!interactiveRef.current) return;
      if ((node.kind !== "event" && node.kind !== "entity") || !node.sourceId) return;
      const exactNode = node as Universe3DNode & { kind: "event" | "entity" };
      const anchorKey = universeNodeKey(exactNode.kind, exactNode.rawId, exactNode.sourceId);
      const cursor = cursorsRef.current.get(anchorKey) ?? null;
      if (
        expandedAnchorsRef.current.has(anchorKey)
        && !cursor
      ) return;
      const browseSession = sessionStateRef.current.mode === "exploration"
        ? sourceSessionRef.current
        : null;
      const requestSession = sourceSessionRef.current;
      const lineageRootKey = browseSession
        ? expansionLineageRootKey(
            workingRef.current,
            browseSession.timeline.window.cacheBundleIds,
            anchorKey,
          )
        : anchorKey;
      if (!lineageRootKey) {
        setMoreHint(t("errors.relatedLoadFailed"));
        return;
      }
      let snapshot = expansionSnapshotsRef.current.get(exactNode.sourceId) ?? null;
      if (sessionStateRef.current.mode === "exploration") {
        const timeline = browseSession?.sourceId === exactNode.sourceId
          ? browseSession.timeline
          : null;
        if (!timeline?.snapshotId || !timeline.sourceRevision || !timeline.asOf) {
          setMoreHint(t("timeline.loading"));
          return;
        }
        snapshot = {
          snapshotId: timeline.snapshotId,
          sourceRevision: timeline.sourceRevision,
          asOf: timeline.asOf,
        };
        expansionSnapshotsRef.current.set(exactNode.sourceId, snapshot);
      }
      if (cursor && !snapshot) {
        cursorsRef.current.delete(anchorKey);
        expandedAnchorsRef.current.delete(anchorKey);
        setMoreHint(t("errors.relatedLoadFailed"));
        return;
      }
      expandAbortRef.current?.abort();
      const controller = new AbortController();
      expandAbortRef.current = controller;
      expandingAnchorRef.current = anchorKey;
      const requestEpoch = epochRef.current;
      setExpandingKey(node.id);
      setMoreHint("");
      try {
        const patch = await requestExpansion(
          exactNode,
          cursor,
          snapshot,
          controller.signal,
        );
        if (
          patch.epoch !== epochRef.current
          || controller.signal.aborted
          || !interactiveRef.current
          || sourceSessionRef.current !== requestSession
          || (cursorsRef.current.get(anchorKey) ?? null) !== cursor
          || !workingRef.current.nodes.some((item) =>
            universeNodeKey(item.kind, item.id, item.source_id) === anchorKey)
        ) return;
        const admissionWorking = workingRef.current;
        const currentLineageRootKey = browseSession
          ? expansionLineageRootKey(
              admissionWorking,
              browseSession.timeline.window.cacheBundleIds,
              anchorKey,
            )
          : anchorKey;
        if (currentLineageRootKey !== lineageRootKey) return;
        const windowProtection = browseSession
          ? universeBundleWindowProtection(
              admissionWorking,
              browseSession.timeline.window.cacheBundleIds,
            )
          : { nodeKeys: [], relationKeys: [] };
        let visibleSupportBundleIds: string[] = [];
        if (browseSession) {
          const categoryProjectedAdmission = projectUniverseWorkingSet(
            admissionWorking,
            projectedEntityTypes,
          );
          const projectionIds = timelineProjectionBundleIds(
            categoryProjectedAdmission,
            browseSession.timeline.window.cacheBundleIds,
            browseSession.timeline.window.visibleBundleIds,
          );
          const visibleTimelineIds = new Set(
            browseSession.timeline.window.visibleBundleIds,
          );
          const supportIds = projectionIds.filter((id) =>
            !visibleTimelineIds.has(id));
          const visibleProjection = projectUniverseBundleWindowWithinBudget(
            categoryProjectedAdmission,
            browseSession.timeline.window.visibleBundleIds,
            [...supportIds].reverse(),
            timelineProjectionBudget,
            browseSession.timeline.window.visibleLimit,
            timelineBundleEntityLimit,
          );
          visibleSupportBundleIds = visibleProjection.bundle_order.filter((id) =>
            categoryProjectedAdmission.bundles[id]?.origin === "expansion");
        }
        const admission = admitUniverseExpansionPage(
          admissionWorking,
          patch,
          {
            epoch: requestEpoch,
            sourceId: exactNode.sourceId,
            nodeKind: exactNode.kind,
            nodeId: exactNode.rawId,
            lineageRootKey,
            requestCursor: cursor,
            snapshotId: snapshot?.snapshotId ?? null,
            sourceRevision: snapshot?.sourceRevision ?? null,
            asOf: snapshot?.asOf ?? null,
          },
          browseSession ? residentBudgetRef.current : budgetRef.current,
          Date.now(),
          {
            protectedKeys: [anchorKey, ...windowProtection.nodeKeys],
            protectedRelationKeys: windowProtection.relationKeys,
            protectedBundleIds: [
              ...(browseSession?.timeline.window.cacheBundleIds ?? []),
              ...visibleSupportBundleIds,
            ],
          },
        );
        if (!admission.accepted) {
          setMoreHint(
            admission.reason === "protected_capacity"
              || admission.reason === "over_budget"
              ? t("expansion.capacityReached")
              : t("errors.relatedLoadFailed"),
          );
          return;
        }
        const admittedBundle = admission.workingSet.bundles[patch.bundle_id];
        if (
          !admittedBundle
          || admittedBundle.lineage_root_key !== lineageRootKey
          || (
            browseSession
            && !browseSession.timeline.window.cacheBundleIds.some((id) =>
              admission.workingSet.bundles[id]?.node_keys.includes(lineageRootKey))
          )
        ) return;
        const responseSnapshot = {
          snapshotId: patch.snapshot_id,
          sourceRevision: patch.source_revision,
          asOf: patch.as_of,
        };
        const expansionActivation: UniverseActivation = {
          epoch: requestEpoch,
          origin: "browse",
          query: node.label,
          nodes: [patch.anchor, ...patch.nodes],
          relations: patch.relations,
          source_hits: accumulationMetaRef.current.sourceHits,
        };
        if (
          browseSession
          && sessionStateRef.current.mode === "exploration"
        ) {
          const retained = retainCurrentExploration();
          retained.lockedKey = null;
          retained.selectedKey = null;
          releaseReadingFocus();
          prepareContextScene(requestEpoch, "browse");
          const contextSession = contextGraphSessionRef.current;
          if (contextSession) contextSession.activationCount += 1;
          appendActivationToAccumulation(expansionActivation, "browse");
        } else if (sessionStateRef.current.mode === "accumulation") {
          releaseReadingFocus();
          appendActivationToAccumulation(expansionActivation, "browse");
        } else {
          commitWorkingSet(admission.workingSet);
          pruneExpansionState(admission.workingSet);
        }

        expansionSnapshotsRef.current.set(exactNode.sourceId, responseSnapshot);
        const cacheKey = universeExpansionCacheKey(
          requestEpoch,
          exactNode.sourceId,
          responseSnapshot.sourceRevision,
          responseSnapshot.snapshotId,
          exactNode.kind,
          exactNode.rawId,
          cursor,
        );
        expansionCacheRef.current.set(cacheKey, patch);
        while (expansionCacheRef.current.size > 24) {
          const oldest = expansionCacheRef.current.keys().next().value;
          if (typeof oldest !== "string") break;
          expansionCacheRef.current.delete(oldest);
        }
        dispatchUniversePatch(patch);
        const committedWorking = workingRef.current;
        const committedCount = universeAnchorProgress(
          committedWorking,
          exactNode.kind,
          exactNode.rawId,
          exactNode.sourceId,
        );
        const totalCount = Math.max(committedCount, patch.anchor.related_count);
        if (admission.nextCursor) {
          expandedAnchorsRef.current.add(anchorKey);
          cursorsRef.current.set(anchorKey, admission.nextCursor);
        } else {
          cursorsRef.current.delete(anchorKey);
          if (committedCount >= totalCount) expandedAnchorsRef.current.add(anchorKey);
          else expandedAnchorsRef.current.delete(anchorKey);
        }
        const relationLabel = exactNode.kind === "entity"
          ? t("nodeKinds.relatedEvents")
          : t("nodeKinds.relatedEntities");
        setMoreHint(
          patch.page.has_more || committedCount < totalCount
            ? t("expansion.pageMore", { committed: committedCount, total: totalCount, relation: relationLabel })
            : t("expansion.complete", { total: totalCount, relation: relationLabel }),
        );
      } catch (reason) {
        if (reason instanceof ApiError && reason.code === "aborted") return;
        if (
          controller.signal.aborted
          || requestEpoch !== epochRef.current
          || !interactiveRef.current
          || sourceSessionRef.current !== requestSession
        ) return;
        if (reason instanceof ApiError && reason.code === "snapshot_changed") {
          invalidateSourceSnapshot(exactNode.sourceId, requestEpoch);
          return;
        }
        setMoreHint(reason instanceof ApiError ? reason.message : t("errors.relatedLoadFailed"));
      } finally {
        if (expandAbortRef.current === controller) {
          expandAbortRef.current = null;
          expandingAnchorRef.current = null;
          if (requestEpoch === epochRef.current) setExpandingKey(null);
        }
      }
    },
    [
      appendActivationToAccumulation,
      commitWorkingSet,
      invalidateSourceSnapshot,
      prepareContextScene,
      pruneExpansionState,
      projectedEntityTypes,
      releaseReadingFocus,
      requestExpansion,
      retainCurrentExploration,
      t,
      timelineBundleEntityLimit,
      timelineProjectionBudget,
    ],
  );

  const loadSourceTimelinePage = React.useCallback(
    (
      sourceId: string,
      cause: SourceTimelineLoadCause,
      direction: UniverseTimelineDirection = "older",
    ): Promise<SourceTimelineLoadResult> => {
      if (!manifest || !interactiveRef.current) return Promise.resolve("blocked");
      const inFlight = timelineRequestRef.current;
      if (inFlight) {
        if (inFlight.sourceId === sourceId && inFlight.direction === direction) {
          return inFlight.promise;
        }
        if (cause !== "journey" && cause !== "source-entry") {
          return Promise.resolve("blocked");
        }
        // A direct journey outranks background work in the opposite direction.
        // Wait for the abort cleanup before recomputing cursors and runway; this
        // prevents an older prefetch finally-block from clearing a newer load.
        inFlight.controller.abort();
        const retry = () => timelinePageLoaderRef.current?.(
          sourceId,
          cause,
          direction,
        ) ?? Promise.resolve<SourceTimelineLoadResult>("blocked");
        return inFlight.promise.then(
          retry,
          retry,
        );
      }

      const session = sourceSessionRef.current;
      if (!session || session.sourceId !== sourceId) {
        return Promise.resolve("blocked");
      }
      const state = session.timeline;
      if (state.loading || state.pausedReason) return Promise.resolve("blocked");
      if (lockedKeyRef.current && cause !== "source-entry") {
        return Promise.resolve("blocked");
      }
      if (state.deque === null && direction !== "older") {
        return Promise.resolve("blocked");
      }
      if (
        state.deque
        && (
          (direction === "older" && !state.deque.hasOlder)
          || (direction === "newer" && !state.deque.hasNewer)
        )
      ) return Promise.resolve("blocked");

      const epoch = epochRef.current;
      const source = sourceById.get(sourceId);
      const pageBundleLimit = state.queryPageSize ?? universeTimelinePageBundleLimit(
        viewPreferences.temporalPageSize,
        Math.min(EVENT_ENTITY_PROJECTION_LIMIT, manifest.policy.event_entity_limit),
        residentBudgetRef.current,
      );
      const requestCursor = state.deque === null
        ? null
        : direction === "older"
          ? state.deque.olderCursor
          : state.deque.newerCursor;
      const controller = new AbortController();
      state.loading = true;
      if (cause !== "prefetch") {
        commitTimelineWindow(session, {
          ...state.window,
          phase: "loading",
        });
        setMoreHint(t("timeline.loading"));
      }
      refreshLoadProgress();

      let loadResult: SourceTimelineLoadResult = "blocked";
      const request = (async (): Promise<SourceTimelineLoadResult> => {
        try {
          const page = await api.universeTimeline(
            {
              epoch,
              source_id: sourceId,
              limit: pageBundleLimit,
              direction,
              cursor: requestCursor,
              snapshot_id: state.snapshotId,
            },
            controller.signal,
          );
          if (
            page.epoch !== epochRef.current
            || controller.signal.aborted
            || !interactiveRef.current
            || sourceSessionRef.current !== session
          ) return "blocked";
          if (
            page.source_id !== sourceId
            || page.request_direction !== direction
            || page.request_cursor !== requestCursor
            || (state.snapshotId !== null && page.snapshot_id !== state.snapshotId)
            || (
              state.sourceRevision !== null
              && page.source_revision !== state.sourceRevision
            )
            || (state.asOf !== null && page.as_of !== state.asOf)
          ) {
            throw new Error("timeline response does not match the active snapshot");
          }

          // Network pages populate the pure FIFO cache only. Cache admission
          // never creates scene objects or moves the camera.
          eventCacheRef.current = admitUniverseEventBundles(
            eventCacheRef.current,
            universeEventBundlesFromTimeline(page),
          ).cache;

          const previousDeque = state.deque;
          const dequeAdmission = admitUniverseTimelineDequePage(
            previousDeque,
            page,
            state.window.cacheLimit,
          );
          const synchronizedWindow = synchronizeTimelineWindowWithDeque(
            state.window,
            previousDeque,
            dequeAdmission,
          );
          if (!synchronizedWindow) {
            state.pausedReason = "capacity";
            if (cause !== "prefetch") {
              setMoreHint(t("timeline.capacityPaused", {
                count: queriedUniverseTimelineEventCount(state.window),
              }));
            }
            return "blocked";
          }

          // The deque has already decided the fixed-cache edge retirement.
          // Protect only its final retained identities so the working set can
          // release the retired edge while admitting the complete new page.
          const protectedTimelineBundleIds = synchronizedWindow.cacheBundleIds;
          const admissionWorking = workingRef.current;
          const windowProtection = universeBundleWindowProtection(
            admissionWorking,
            protectedTimelineBundleIds,
          );
          const categoryProjectedAdmission = projectUniverseWorkingSet(
            admissionWorking,
            projectedEntityTypes,
          );
          const currentProjectionIds = timelineProjectionBundleIds(
            categoryProjectedAdmission,
            synchronizedWindow.cacheBundleIds,
            synchronizedWindow.visibleBundleIds,
          );
          const visibleTimelineIds = new Set(synchronizedWindow.visibleBundleIds);
          const visibleSupportBundleIds = projectUniverseBundleWindowWithinBudget(
            categoryProjectedAdmission,
            synchronizedWindow.visibleBundleIds,
            currentProjectionIds
              .filter((id) => !visibleTimelineIds.has(id))
              .reverse(),
            timelineProjectionBudget,
            synchronizedWindow.visibleLimit,
            timelineBundleEntityLimit,
          ).bundle_order.filter(
            (id) => categoryProjectedAdmission.bundles[id]?.origin === "expansion",
          );
          const admission = admitUniverseTimelinePage(
            admissionWorking,
            page,
            residentBudgetRef.current,
            Date.now(),
            {
              roots: true,
              protectedKeys: [
                ...windowProtection.nodeKeys,
                ...(lockedKeyRef.current ? [lockedKeyRef.current] : []),
              ],
              protectedRelationKeys: windowProtection.relationKeys,
              protectedBundleIds: [
                ...protectedTimelineBundleIds,
                ...visibleSupportBundleIds,
              ],
            },
          );
          const retainedDequeIds = new Set(
            dequeAdmission.deque.bundles.map((bundle) => bundle.bundle_id),
          );
          if (
            !admission.pageAcknowledged
            || admission.evictedBundleIds.some((id) => retainedDequeIds.has(id))
          ) {
            state.pausedReason = "capacity";
            if (cause !== "prefetch") {
              setMoreHint(t("timeline.capacityPaused", {
                count: queriedUniverseTimelineEventCount(state.window),
              }));
            }
            return "blocked";
          }

          state.snapshotId = page.snapshot_id;
          state.sourceRevision = page.source_revision;
          state.asOf = page.as_of;
          state.totalEvents = page.total_events;
          state.queryPageSize ??= pageBundleLimit;
          state.deque = dequeAdmission.deque;
          state.pausedReason = null;
          expansionSnapshotsRef.current.set(sourceId, {
            snapshotId: page.snapshot_id,
            sourceRevision: page.source_revision,
            asOf: page.as_of,
          });
          commitTimelineWindow(session, synchronizedWindow);

          const retainedIds = timelineRetentionBundleIds(
            admission.workingSet,
            synchronizedWindow.cacheBundleIds,
          );
          const retainedWorking = retainUniverseWorkingSetBundles(
            admission.workingSet,
            retainedIds,
          );
          commitWorkingSet(retainedWorking);
          pruneExpansionState(retainedWorking);

          const residentKeys = new Set(retainedWorking.nodes.map((node) =>
            universeNodeKey(node.kind, node.id, node.source_id)));
          page.bundles.forEach((bundle) => {
            const eventKey = universeNodeKey("event", bundle.event.id, sourceId);
            if (!residentKeys.has(eventKey)) return;
            if (
              expandedAnchorsRef.current.has(eventKey)
              || cursorsRef.current.has(eventKey)
            ) return;
            expandedAnchorsRef.current.add(eventKey);
            if (bundle.neighbor_page.next_cursor) {
              cursorsRef.current.set(eventKey, bundle.neighbor_page.next_cursor);
            }
          });
          loadResult = "loaded";
          refreshLoadProgress();

          if (sessionStateRef.current.mode === "exploration") {
            setSummary({
              query: source?.label ?? t("timeline.defaultTitle"),
              events: queriedUniverseTimelineEventCount(synchronizedWindow),
              entities: retainedWorking.nodes.filter((node) =>
                node.kind === "entity" && node.source_id === sourceId).length,
              relations: retainedWorking.relations.length,
            });
          }
          if (cause !== "prefetch" && synchronizedWindow.phase === "complete") {
            setMoreHint(t("timeline.explorationComplete", {
              source: source?.label ?? t("timeline.thisGalaxy"),
            }));
          } else if (cause !== "prefetch") {
            setMoreHint(t("timeline.windowReady", {
              visible: synchronizedWindow.visibleBundleIds.length,
            }));
          }
          return loadResult;
        } catch (reason) {
          if (reason instanceof ApiError && reason.code === "aborted") {
            return "blocked";
          }
          if (
            reason instanceof ApiError
            && reason.code === "snapshot_changed"
            && sourceSessionRef.current === session
          ) {
            invalidateSourceSnapshot(sourceId, epoch);
            return "blocked";
          }
          if (cause !== "prefetch") {
            setMoreHint(
              reason instanceof ApiError ? reason.message : t("errors.timelineLoadFailed"),
            );
          }
          return "blocked";
        } finally {
          state.loading = false;
          if (sourceSessionRef.current === session) {
            commitTimelineWindow(session, settleUniverseTimelineWindow(state.window));
          }
          refreshLoadProgress();
        }
      })();
      const requestState: SourceTimelineRequest = {
        sourceId,
        cause,
        direction,
        controller,
        promise: request,
      };
      timelineRequestRef.current = requestState;
      void request.finally(() => {
        if (timelineRequestRef.current === requestState) timelineRequestRef.current = null;
      });
      return request;
    },
    [
      commitTimelineWindow,
      commitWorkingSet,
      invalidateSourceSnapshot,
      manifest,
      pruneExpansionState,
      projectedEntityTypes,
      refreshLoadProgress,
      sourceById,
      t,
      timelineBundleEntityLimit,
      timelineProjectionBudget,
      viewPreferences.temporalPageSize,
    ],
  );
  timelinePageLoaderRef.current = loadSourceTimelinePage;

  React.useEffect(() => {
    const session = sourceSessionRef.current;
    if (!session || session.timeline.loading) return;
    const current = session.timeline.window;
    if (current.phase === "transitioning") return;

    let next = current;
    const deque = session.timeline.deque;
    if (deque && current.activeIndex >= 0) {
      const activeBundleId = current.cacheBundleIds[current.activeIndex];
      const pageSize = session.timeline.queryPageSize
        ?? viewPreferences.temporalPageSize;
      const resized = activeBundleId
        ? resizeUniverseTimelineDeque(
            deque,
            bundleWindow.cacheCapacity,
            activeBundleId,
            bundleWindow.eventWindowSize,
            pageSize,
          )
        : null;
      const anchor = resized && activeBundleId
        ? syncUniverseTimelineWindowToDeque(resized.deque, {
            activeBundleId,
            activeIndex: current.activeIndex,
            visibleLimit: bundleWindow.eventWindowSize,
          })
        : null;
      if (resized && anchor) {
        session.timeline.deque = resized.deque;
        const cacheStartOffset = current.cacheStartOffset
          + resized.evictedNewerBundleIds.length;
        const networkExhausted = !resized.deque.hasOlder;
        const phase = networkExhausted
          && anchor.activeIndex === anchor.cacheBundleIds.length - 1
          ? "complete" as const
          : "idle" as const;
        const changed = current.visibleLimit !== bundleWindow.eventWindowSize
          || current.cacheLimit !== bundleWindow.cacheCapacity
          || current.activeIndex !== anchor.activeIndex
          || current.phase !== phase
          || current.cacheBundleIds.length !== anchor.cacheBundleIds.length
          || current.cacheBundleIds.some(
            (id, index) => id !== anchor.cacheBundleIds[index],
          )
          || current.visibleBundleIds.length !== anchor.visibleBundleIds.length
          || current.visibleBundleIds.some(
            (id, index) => id !== anchor.visibleBundleIds[index],
          );
        if (changed) {
          next = {
            ...current,
            cacheBundleIds: anchor.cacheBundleIds,
            activeIndex: anchor.activeIndex,
            visibleBundleIds: anchor.visibleBundleIds,
            visitedCount: Math.max(
              current.visitedCount,
              cacheStartOffset + anchor.activeIndex + 1,
            ),
            networkExhausted,
            phase,
            revision: current.revision + 1,
            visibleLimit: bundleWindow.eventWindowSize,
            cacheLimit: bundleWindow.cacheCapacity,
            cacheStartOffset,
          };
        }
      }
    } else {
      next = reconfigureUniverseTimelineWindow(
        current,
        bundleWindow.eventWindowSize,
        bundleWindow.cacheCapacity,
      );
    }
    if (next !== current) commitTimelineWindow(session, next);

    const resident = workingRef.current;
    const retainedIds = timelineRetentionBundleIds(
      resident,
      next.cacheBundleIds,
    );
    const retained = retainUniverseWorkingSetBundles(
      resident,
      retainedIds,
    );
    if (session.timeline.pausedReason === "capacity") {
      session.timeline.pausedReason = null;
      setMoreHint("");
      refreshLoadProgress();
    }
    const bundleOrderChanged = retained.bundle_order.length
      !== resident.bundle_order.length
      || retained.bundle_order.some((id, index) => id !== resident.bundle_order[index]);
    if (!bundleOrderChanged) return;
    commitWorkingSet(retained);
    pruneExpansionState(retained);
  }, [
    bundleWindow,
    commitTimelineWindow,
    commitWorkingSet,
    pruneExpansionState,
    refreshLoadProgress,
    timelineWindow,
    viewPreferences.temporalPageSize,
  ]);

  const activateSource = React.useCallback(
    (sourceId: string) => {
      if (!sourceById.has(sourceId)) return;
      retainedExplorationRef.current = null;
      contextGraphSessionRef.current = null;
      contextProjectionClosedRef.current = false;
      setContextActivationCount(0);
      resetAccumulationKernel(true);
      dispatchUniverseResume();
      // Explicit source navigation starts a timeline browse session even when
      // the user arrived from search or an assistant-provided activation.
      evidenceOriginRef.current = "browse";
      setEvidenceOrigin("browse");
      dispatchSession({ type: "ENTER_EXPLORATION", sourceId });
      setSourceHits([]);
      clearCameraSchedule();
      expandAbortRef.current?.abort();
      expandAbortRef.current = null;
      expandingAnchorRef.current = null;
      expansionCacheRef.current.clear();
      expansionInflightRef.current.clear();
      dispatchUniversePatchReset();
      expansionSnapshotsRef.current.clear();
      cursorsRef.current.clear();
      expandedAnchorsRef.current.clear();
      const previousSession = sourceSessionRef.current;
      if (previousSession) previousSession.timeline.loading = false;
      timelineRequestRef.current?.controller.abort();
      timelineRequestRef.current = null;
      if (snapshotReloadTimerRef.current !== null) {
        window.clearTimeout(snapshotReloadTimerRef.current);
        snapshotReloadTimerRef.current = null;
      }
      snapshotReloadAttemptsRef.current.clear();
      clearTimelineSettle();
      timelineJourneyCommitRef.current = null;
      setExpandingKey(null);
      graphRef.current?.unlockNode();
      setLockedKey(null);
      setSelectedKey(null);

      if (epochRef.current === 0) epochRef.current = 1;
      const session = emptySourceBrowseSession(
        epochRef.current,
        sourceId,
        bundleWindow.eventWindowSize,
        bundleWindow.cacheCapacity,
      );
      sourceSessionRef.current = session;
      completedSourcesRef.current.clear();

      commitWorkingSet(session.working);
      pruneExpansionState(session.working);
      setTimelineWindow(session.timeline.window);
      refreshLoadProgress();
      setActivePartition(sourceId);
      void loadSourceTimelinePage(sourceId, "source-entry");
    },
    [
      bundleWindow,
      clearCameraSchedule,
      clearTimelineSettle,
      commitWorkingSet,
      dispatchSession,
      loadSourceTimelinePage,
      pruneExpansionState,
      refreshLoadProgress,
      resetAccumulationKernel,
      setLockedKey,
      setSelectedKey,
      sourceById,
    ],
  );

  React.useEffect(() => {
    const onSourceFocus = (event: Event) => {
      const sourceId = (
        event as CustomEvent<{ source_id: string }>
      ).detail?.source_id;
      if (sourceId) activateSource(sourceId);
    };
    window.addEventListener(UNIVERSE_SOURCE_FOCUS_EVENT, onSourceFocus);
    return () => window.removeEventListener(UNIVERSE_SOURCE_FOCUS_EVENT, onSourceFocus);
  }, [activateSource]);

  const activatePartition = React.useCallback(
    (node: Universe3DNode) => activateSource(node.sourceId),
    [activateSource],
  );

  const handleSourceLod = React.useCallback(
    (sourceId: string, level: 0 | 1 | 2 | 3) => {
      if (!interactiveRef.current) return;
      if (level < 1) return;
      // Camera LOD is visual feedback, not source navigation. Once a browse
      // session owns the timeline, a nearby partition must not replace its
      // active source and unmount the pager for the loaded session.
      const sessionSourceId = sourceSessionRef.current?.sourceId;
      if (sessionSourceId && sessionSourceId !== sourceId) return;
      setActivePartition(sourceId);
    },
    [],
  );

  const handleSourceWheel = React.useCallback(
    (sourceId: string) => {
      if (!interactiveRef.current) return;
      // The source may already be loading because a prior wheel tick or a
      // click started the same dive. Keep that in-flight session intact so a
      // trackpad cannot restart the timeline on every native wheel event.
      if (sourceSessionRef.current?.sourceId === sourceId) return;
      activateSource(sourceId);
    },
    [activateSource],
  );

  const handleTimelineIntent = React.useCallback(
    async (direction: "next" | "previous"): Promise<UniverseTimelineIntentResult> => {
      const session = sourceSessionRef.current;
      if (
        !session
        || sessionStateRef.current.mode !== "exploration"
      ) return "blocked";
      if (lockedKeyRef.current) {
        setMoreHint(t("timeline.unlockToContinue"));
        return "blocked";
      }

      const queryPageSize = session.timeline.queryPageSize ?? universeTimelinePageBundleLimit(
        viewPreferences.temporalPageSize,
        Math.min(
          EVENT_ENTITY_PROJECTION_LIMIT,
          manifest?.policy.event_entity_limit ?? EVENT_ENTITY_PROJECTION_LIMIT,
        ),
        residentBudgetRef.current,
      );
      // Network pages remain large for efficient bidirectional prefetch. The
      // visual page is capped by the configured window so every queried event
      // is eventually shown instead of being skipped by a wider stride.
      const pageStride = Math.min(
        queryPageSize,
        session.timeline.window.visibleLimit,
      );
      const requestDirection: UniverseTimelineDirection =
        direction === "next" ? "older" : "newer";
      session.timeline.preferredDirection = requestDirection;

      for (let attempt = 0; attempt < 2; attempt += 1) {
        const current = session.timeline.window;
        const rewindFloor = universeTimelineRewindStartActiveIndex(current);
        const localRunway = direction === "next"
          ? current.cacheBundleIds.length - current.activeIndex - 1
          : current.activeIndex - rewindFloor;
        if (localRunway >= pageStride) break;
        const edgeAvailable = direction === "next"
          ? session.timeline.deque?.hasOlder
          : session.timeline.deque?.hasNewer;
        if (!edgeAvailable) break;
        await loadSourceTimelinePage(
          session.sourceId,
          "journey",
          requestDirection,
        );
        if (sourceSessionRef.current !== session) return "blocked";
      }

      const current = session.timeline.window;
      const rewindFloor = universeTimelineRewindStartActiveIndex(current);
      const localRunway = direction === "next"
        ? current.cacheBundleIds.length - current.activeIndex - 1
        : current.activeIndex - rewindFloor;
      const edgeAvailable = direction === "next"
        ? session.timeline.deque?.hasOlder
        : session.timeline.deque?.hasNewer;
      const completeTerminalPage = localRunway > 0 && !edgeAvailable;
      if (localRunway < pageStride && !completeTerminalPage) {
        if (session.timeline.pausedReason === "capacity") {
          setMoreHint(t("timeline.capacityPaused", {
            count: queriedUniverseTimelineEventCount(current),
          }));
        }
        return "blocked";
      }
      const next = advanceUniverseTimelineWindow(
        current,
        direction,
        pageStride,
      );
      if (next === current || next.revision === current.revision) {
        if (direction === "next" && !session.timeline.deque?.hasOlder) {
          const complete = markUniverseTimelineNetworkExhausted(current);
          commitTimelineWindow(session, complete);
          setMoreHint(t("timeline.explorationComplete", {
            source: sourceById.get(session.sourceId)?.label ?? t("timeline.thisGalaxy"),
          }));
          return "complete";
        }
        if (session.timeline.pausedReason === "capacity") {
          setMoreHint(t("timeline.capacityPaused", {
            count: queriedUniverseTimelineEventCount(current),
          }));
        }
        return "blocked";
      }

      timelineJourneyCommitRef.current = {
        session,
        revision: next.revision,
        cause: "journey",
        direction,
      };
      commitTimelineWindow(session, next);
      scheduleTimelineSettle(session, next);
      if (next.phase === "complete") {
        setMoreHint(t("timeline.explorationComplete", {
          source: sourceById.get(session.sourceId)?.label ?? t("timeline.thisGalaxy"),
        }));
      } else {
        setMoreHint(t(direction === "next"
          ? "timeline.movedEarlier"
          : "timeline.movedLater"));
      }
      return "advanced";
    },
    [
      commitTimelineWindow,
      loadSourceTimelinePage,
      manifest?.policy.event_entity_limit,
      scheduleTimelineSettle,
      sourceById,
      t,
      viewPreferences.temporalPageSize,
    ],
  );

  React.useEffect(() => {
    const session = sourceSessionRef.current;
    if (
      !interactive
      || sessionState.mode !== "exploration"
      || !session
      || session.timeline.loading
      || session.timeline.pausedReason
      || lockedKey
      || timelineWindow?.phase !== "idle"
      || !session.timeline.deque
    ) return;
    const current = session.timeline.window;
    const nextPageSize = session.timeline.queryPageSize ?? universeTimelinePageBundleLimit(
      viewPreferences.temporalPageSize,
      Math.min(
        EVENT_ENTITY_PROJECTION_LIMIT,
        manifest?.policy.event_entity_limit ?? EVENT_ENTITY_PROJECTION_LIMIT,
      ),
      residentBudget,
    );
    const visibleStart = Math.max(
      0,
      current.activeIndex - current.visibleLimit + 1,
    );
    const plan = planUniversePrefetch({
      newerEvents: visibleStart,
      olderEvents: Math.max(
        0,
        current.cacheBundleIds.length - current.activeIndex - 1,
      ),
      hasOlder: session.timeline.deque.hasOlder,
      hasNewer: session.timeline.deque.hasNewer,
      pageSize: nextPageSize,
      pagesPerSide: viewPreferences.temporalPrefetchPages,
      preferredDirection: session.timeline.preferredDirection,
      inFlight: Boolean(timelineRequestRef.current),
    });
    if (!plan.direction) return;
    void loadSourceTimelinePage(
      session.sourceId,
      "prefetch",
      plan.direction,
    );
  }, [
    interactive,
    loadSourceTimelinePage,
    lockedKey,
    manifest?.policy.event_entity_limit,
    residentBudget,
    sessionState.mode,
    timelineWindow,
    viewPreferences.temporalPageSize,
    viewPreferences.temporalPrefetchPages,
  ]);

  const timelineJourney = React.useMemo<UniverseTimelineJourney>(() => {
    const enabled = Boolean(
      interactive
      && browseSessionSourceId
      && timelineWindow
      && timelineWindow.cacheBundleIds.length > 0,
    );
    const activeIndex = timelineWindow?.activeIndex ?? -1;
    const cacheLength = timelineWindow?.cacheBundleIds.length ?? 0;
    const startActiveIndex = timelineWindow
      ? universeTimelineRewindStartActiveIndex(timelineWindow)
      : -1;
    const activeDeque = browseSessionSourceId
      ? sourceSessionRef.current?.timeline.deque ?? null
      : null;
    const hasOlder = activeDeque?.hasOlder ?? false;
    const hasNewer = activeDeque?.hasNewer ?? false;
    const networkExhausted = !hasOlder;
    return {
      enabled,
      phase: timelineWindow?.phase ?? "idle",
      hasNext: enabled
        && (activeIndex < cacheLength - 1 || hasOlder),
      hasPrevious: enabled && (activeIndex > startActiveIndex || hasNewer),
      networkExhausted,
      revision: timelineWindow?.revision ?? 0,
    };
  }, [
    browseSessionSourceId,
    interactive,
    timelineWindow,
  ]);

  // Keep the pager mounted for the lifetime of an active browse session.
  // Journey readiness only controls the individual buttons: coupling the
  // whole toolbar to a populated cache made it disappear during page loads,
  // deque synchronization, and other short-lived timeline transitions.
  const timelineControlsVisible = Boolean(
    interactive
    && browseSessionSourceId
    && timelineWindow,
  );
  const contextualWorkspaceActive = Boolean(
    interactive
    && sessionState.mode === "accumulation",
  );

  const handleTimelineSettled = React.useCallback((revision: number) => {
    const session = sourceSessionRef.current;
    if (
      !session
      || session.timeline.window.revision !== revision
      || session.timeline.window.phase !== "transitioning"
    ) return;
    clearTimelineSettle();
    commitTimelineWindow(
      session,
      settleUniverseTimelineWindow(session.timeline.window),
    );
  }, [clearTimelineSettle, commitTimelineWindow]);

  const visibleTimelineRange = React.useMemo(() => {
    const session = sourceSessionRef.current;
    if (!session?.timeline.deque || !timelineWindow || timelineWindow.activeIndex < 0) {
      return t("controls.unknownTime");
    }
    const visibleIds = new Set(timelineWindow.visibleBundleIds);
    const visibleBundles = session.timeline.deque.bundles
      .filter((bundle) => visibleIds.has(bundle.bundle_id));
    const values = visibleBundles
      .map((bundle) => bundle.event.start_time)
      .filter((value): value is string => typeof value === "string")
      .map((value) => ({ value, timestamp: Date.parse(value) }))
      .filter((item) => Number.isFinite(item.timestamp));
    const oldest = Math.min(...values.map((item) => item.timestamp));
    const newest = Math.max(...values.map((item) => item.timestamp));
    // A window whose clock collapsed to one instant (an imported book) reads
    // as exploration position, not as a meaningless repeated date.
    const ordinals = visibleBundles
      .map((bundle) => bundle.ordinal)
      .filter((ordinal): ordinal is number => Number.isInteger(ordinal));
    const total = session.timeline.totalEvents;
    if (
      total !== null
      && ordinals.length === visibleBundles.length
      && ordinals.length > 0
      && (values.length === 0 || (oldest === newest && visibleBundles.length > 1))
    ) {
      const from = Math.min(...ordinals) + 1;
      const to = Math.max(...ordinals) + 1;
      return from === to
        ? t("controls.countPosition", { position: from, total })
        : t("controls.countRange", { from, to, total });
    }
    if (values.length === 0) return t("controls.unknownTime");
    const includesClock = values.some(({ value }) =>
      !/T00:00(?::00(?:\.\d+)?)?(?:Z|[+-]\d\d:\d\d)?$/.test(value));
    const formatter = new Intl.DateTimeFormat(locale, includesClock
      ? { dateStyle: "medium", timeStyle: "short" }
      : { dateStyle: "medium" });
    const oldestLabel = formatter.format(new Date(oldest));
    const newestLabel = formatter.format(new Date(newest));
    const range = oldest === newest ? oldestLabel : `${oldestLabel} – ${newestLabel}`;
    return values.length < visibleBundles.length
      ? t("controls.partialTime", { time: range })
      : range;
  }, [locale, t, timelineWindow]);

  const handleSceneViewChange = React.useCallback(
    (view: UniverseSceneView) => {
      if (!interactiveRef.current) return;
      dispatchUniverseView({
        mode: view.mode,
        source_id: view.sourceId,
        progress: view.progress,
      });
      if (viewportSourceRef.current === view.sourceId) return;
      viewportSourceRef.current = view.sourceId;
      setViewportSourceId(view.sourceId);
    },
    [],
  );

  const clearSelection = React.useCallback((options?: UniverseSelectionClearOptions) => {
    releaseReadingFocus();
    if (options?.dismissWorkspace !== false) dispatchUniverseInteraction();
  }, [releaseReadingFocus]);

  const timelineNavigationForNode = React.useCallback(
    (node: UniverseConcrete3DNode) => {
      if (node.kind !== "event") return undefined;
      const index = visibleTimelineEventNodes.findIndex((item) => item.id === node.id);
      if (index < 0) return undefined;
      return {
        items: visibleTimelineEventNodes.map((item) => ({
          kind: "event" as const,
          id: item.rawId,
          source_id: item.sourceId,
        })),
        index,
      };
    },
    [visibleTimelineEventNodes],
  );

  const lockNodeForReading = React.useCallback(
    (node: UniverseConcrete3DNode) => {
      setTimelinePlaying(false);
      // Locking is presentation-only. The mini workspace reads through the
      // detail endpoint and expansion stays behind the explicit action.
      if (timelineRequestRef.current?.cause !== "source-entry") {
        timelineRequestRef.current?.controller.abort();
      }
      setLockedKey(node.id);
      setSelectedKey(node.id);
      graphRef.current?.lockNode(node.id);
    },
    [setLockedKey, setSelectedKey],
  );

  const handleNodeClick = React.useCallback(
    (node: UniverseSceneNode) => {
      setTimelinePlaying(false);
      if (node.kind === "source") {
        activatePartition(node as Universe3DNode);
        return;
      }
      const concreteNode = node as UniverseConcrete3DNode;
      if (lockedKeyRef.current === concreteNode.id) {
        clearSelection();
        return;
      }
      // Primary card activation only locks the reading focus. Opening the
      // contextual workspace belongs to the explicit "View details" action.
      lockNodeForReading(concreteNode);
    },
    [activatePartition, clearSelection, lockNodeForReading],
  );

  const handleViewDetails = React.useCallback(
    (node: UniverseSceneNode) => {
      if (node.kind === "source") return;
      const concreteNode = node as UniverseConcrete3DNode;
      lockNodeForReading(concreteNode);
      dispatchUniverseDetail(
        concreteNode.kind,
        concreteNode.rawId,
        concreteNode.sourceId,
        timelineNavigationForNode(concreteNode),
      );
      dispatchSession({ type: "OPEN_DETAIL", key: concreteNode.id });
    },
    [dispatchSession, lockNodeForReading, timelineNavigationForNode],
  );

  const moveTimelineManually = React.useCallback((direction: "next" | "previous") => {
    setTimelinePlaying(false);
    if (lockedKeyRef.current) clearSelection();
    else dispatchUniverseInteraction();
    window.requestAnimationFrame(() => {
      void graphRef.current?.moveTimeline(direction);
    });
  }, [clearSelection]);

  const returnToTimelineOrigin = React.useCallback(() => {
    setTimelinePlaying(false);
    clearSelection();
    if (timelineRequestRef.current?.cause !== "source-entry") {
      timelineRequestRef.current?.controller.abort();
      timelineRequestRef.current = null;
    }
    clearTimelineSettle();
    const sourceId = sourceSessionRef.current?.sourceId;
    if (sourceId) graphRef.current?.returnToSourceOrigin(sourceId);
    else graphRef.current?.focusOverview();
  }, [clearSelection, clearTimelineSettle]);

  const handleSceneInteraction = React.useCallback(() => {
    setTimelinePlaying(false);
    dispatchUniverseInteraction();
  }, []);

  const handleSceneBackgroundClick = React.useCallback(() => {
    clearSelection();
  }, [clearSelection]);

  const handleExploreMore = React.useCallback((node: UniverseSceneNode) => {
    if (node.kind === "source") return;
    clearSelection();
    void expandNode(node as UniverseConcrete3DNode);
  }, [clearSelection, expandNode]);

  const handleAskNode = React.useCallback((node: UniverseSceneNode) => {
    if (node.kind === "source") return;
    setTimelinePlaying(false);
    releaseReadingFocus();
    dispatchUniverseAsk(node as UniverseConcrete3DNode);
  }, [releaseReadingFocus]);

  const timelinePlaybackPlan = React.useMemo(() => planUniverseTimelinePlayback({
    enabled: timelinePlaying && interactive && timelineJourney.enabled,
    order: timelinePlaybackOrder,
    hasOlder: timelineJourney.hasNext,
    hasNewer: timelineJourney.hasPrevious,
    documentHidden,
    reducedMotion: Boolean(reducedMotion),
    locked: Boolean(lockedKey),
    loading: timelineJourney.phase === "loading",
    transitioning: timelineJourney.phase === "transitioning",
  }), [
    documentHidden,
    interactive,
    lockedKey,
    reducedMotion,
    timelineJourney,
    timelinePlaybackOrder,
    timelinePlaying,
  ]);

  React.useEffect(() => {
    if (timelinePlaybackTimerRef.current !== null) {
      window.clearTimeout(timelinePlaybackTimerRef.current);
      timelinePlaybackTimerRef.current = null;
    }
    if (timelinePlaybackPlan.status === "paused") {
      if (
        timelinePlaying
        && ["boundary", "document-hidden", "locked", "reduced-motion"]
          .includes(timelinePlaybackPlan.reason)
      ) {
        setTimelinePlaying(false);
        if (timelinePlaybackPlan.reason === "boundary") {
          setMoreHint(t("controls.playbackComplete"));
        }
      }
      return;
    }
    timelinePlaybackTimerRef.current = window.setTimeout(() => {
      timelinePlaybackTimerRef.current = null;
      void Promise.resolve(
        graphRef.current?.moveTimeline(timelinePlaybackPlan.sceneDirection),
      ).then((result) => {
        if (result === "advanced") return;
        setTimelinePlaying(false);
        if (result === "complete" || result === "blocked") {
          setMoreHint(t("controls.playbackComplete"));
        }
      }).catch(() => {
        setTimelinePlaying(false);
      });
    }, timelinePlaybackPlan.delayMs);
    return () => {
      if (timelinePlaybackTimerRef.current !== null) {
        window.clearTimeout(timelinePlaybackTimerRef.current);
        timelinePlaybackTimerRef.current = null;
      }
    };
  }, [t, timelinePlaybackPlan, timelinePlaying]);

  const toggleTimelinePlayback = React.useCallback(() => {
    if (timelinePlaying) {
      setTimelinePlaying(false);
      return;
    }
    if (lockedKeyRef.current) clearSelection();
    setTimelinePlaying(true);
  }, [clearSelection, timelinePlaying]);

  const toggleTimelineOrder = React.useCallback(() => {
    setTimelinePlaying(false);
    setTimelinePlaybackOrder((current) =>
      toggleUniverseTimelinePlaybackOrder(current));
  }, []);

  const handleSceneUnavailable = React.useCallback((reason: UniverseSceneUnavailableReason) => {
    // Rendering failure must not invalidate the already loaded cache/window.
    // Retaining it makes a renderer retry a visual remount instead of a data reload.
    setSceneUnavailableReason((current) => current ?? reason);
  }, []);

  const retryScene = React.useCallback(() => {
    if (sceneRetryFrameRef.current !== null) return;
    setSceneUnavailableReason(null);
    setWebglAvailable(null);
    // Keep one paint with no canvas so Chrome can retire the previous context
    // before the renderer asks for another one.
    sceneRetryFrameRef.current = window.requestAnimationFrame(() => {
      sceneRetryFrameRef.current = null;
      setSceneAttempt((current) => current + 1);
    });
  }, []);

  const resetUniversePresentation = React.useCallback(() => {
    setTimelinePlaying(false);
    graphRef.current?.resetOverview();
    viewportSourceRef.current = null;
    setViewportSourceId(null);
    setActivePartition(null);
    setLockedKey(null);
    setSelectedKey(null);
  }, [setLockedKey, setSelectedKey]);

  const returnToUniverseHome = React.useCallback(() => {
    setTimelinePlaying(false);
    // The galaxy overview is a navigation boundary, not a camera shortcut.
    // Leave the current source session and reveal the complete knowledge universe.
    resetScene(epochRef.current + 1);
  }, [resetScene]);

  const requestSourceBack = React.useCallback(() => {
    const sourceId = sourceSessionRef.current?.sourceId;
    if (!sourceId) return;
    setTimelinePlaying(false);
    clearSelection();
    if (timelineRequestRef.current?.cause !== "source-entry") {
      timelineRequestRef.current?.controller.abort();
      timelineRequestRef.current = null;
    }
    clearTimelineSettle();
    const result = graphRef.current?.returnToSourceOrigin(sourceId)
      ?? "already-at-origin";
    if (result === "already-at-origin") returnToUniverseHome();
  }, [clearSelection, clearTimelineSettle, returnToUniverseHome]);

  React.useEffect(() => {
    const visibleNodeIds = new Set(graphData.nodes.map((node) => node.id));
    const selectedMissing = Boolean(selectedKey && !visibleNodeIds.has(selectedKey));
    const lockedMissing = Boolean(lockedKey && !visibleNodeIds.has(lockedKey));
    if (!selectedMissing && !lockedMissing) return;
    clearSelection();
  }, [clearSelection, graphData.nodes, lockedKey, selectedKey]);

  // Transient hover is rendered entirely inside the scene. Promoting it into
  // React state mounted the reading panel and re-rendered this large controller on
  // every hover transition, which made a lightweight highlight feel like a
  // mode change. Click/keyboard selection still owns the persistent detail view.
  const handleSceneHover = React.useCallback(() => undefined, []);

  React.useEffect(() => {
    if (!interactive || !manifest || !webglAvailable) return;
    const frame = window.requestAnimationFrame(() => {
      focusOverview();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [focusOverview, interactive, manifest, webglAvailable]);

  React.useEffect(() => {
    if (interactive) return;
    // Settle the authoritative window before cancelling its scene/timer. If
    // suspension happens mid-animation, re-entry must expose an idle pager
    // instead of inheriting a transition that can no longer complete.
    settleTimelineBeforeSuspend();
    clearTimelineSettle();
    resetUniversePresentation();
    graphRef.current?.pause();
    expandAbortRef.current?.abort();
    timelineRequestRef.current?.controller.abort();
    if (snapshotReloadTimerRef.current !== null) {
      window.clearTimeout(snapshotReloadTimerRef.current);
      snapshotReloadTimerRef.current = null;
    }
    rebuildAbortRef.current?.abort();
    clearCameraSchedule();
  }, [
    clearCameraSchedule,
    clearTimelineSettle,
    interactive,
    resetUniversePresentation,
    settleTimelineBeforeSuspend,
  ]);

  React.useEffect(() => {
    if (!viewportLoadProgress) return;
    if (!viewportLoadProgress.allDone) {
      completedSourcesRef.current.delete(viewportLoadProgress.sourceId);
      return;
    }
    if (viewportLoadProgress.loading) return;
    if (completedSourcesRef.current.has(viewportLoadProgress.sourceId)) return;
    completedSourcesRef.current.add(viewportLoadProgress.sourceId);
    setMoreHint(
      t("loadProgress.sourceComplete", {
        source: viewportLoadProgress.label,
        events: viewportLoadProgress.events.loaded,
        entities: viewportLoadProgress.entities.loaded,
      }),
    );
  }, [t, viewportLoadProgress]);

  React.useEffect(() => {
    const onVisibility = () => {
      const hidden = document.visibilityState !== "visible";
      setDocumentHidden(hidden);
      if (hidden) setTimelinePlaying(false);
      const graph = graphRef.current;
      if (!graph) return;
      if (interactive && !hidden) graph.resume();
      else graph.pause();
    };
    onVisibility();
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [interactive]);

  React.useEffect(
    () => () => {
      expandAbortRef.current?.abort();
      expansionCacheRef.current.clear();
      expansionInflightRef.current.clear();
      rebuildAbortRef.current?.abort();
      timelineRequestRef.current?.controller.abort();
      if (snapshotReloadTimerRef.current !== null) {
        window.clearTimeout(snapshotReloadTimerRef.current);
        snapshotReloadTimerRef.current = null;
      }
      snapshotReloadAttemptsRef.current.clear();
      clearCameraSchedule();
      clearAccumulationSettle();
      clearTimelineSettle();
      if (timelinePlaybackTimerRef.current !== null) {
        window.clearTimeout(timelinePlaybackTimerRef.current);
        timelinePlaybackTimerRef.current = null;
      }
    },
    [clearAccumulationSettle, clearCameraSchedule, clearTimelineSettle],
  );

  const rebuild = React.useCallback(async () => {
    if (!interactiveRef.current) return;
    rebuildAbortRef.current?.abort();
    const controller = new AbortController();
    rebuildAbortRef.current = controller;
    setRebuilding(true);
    setError("");
    try {
      const queued = await api.rebuildUniverse(controller.signal);
      if (controller.signal.aborted || !interactiveRef.current) return;
      setMoreHint(t("rebuild.queued"));
      for (let attempt = 0; attempt < 80; attempt += 1) {
        await waitForAbortableDelay(750, controller.signal);
        if (controller.signal.aborted || !interactiveRef.current) return;
        const job = await api.getJob(queued.id, controller.signal);
        if (job.status === "failed") {
          throw new ApiError(0, "rebuild_failed", job.error || t("rebuild.failed"));
        }
        if (job.status !== "succeeded") continue;
        setManifest(await api.universeManifest());
        setMoreHint(t("rebuild.complete"));
        return;
      }
      throw new ApiError(0, "rebuild_timeout", t("rebuild.timeout"));
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === "aborted") return;
      setError(reason instanceof ApiError ? reason.message : t("rebuild.failed"));
    } finally {
      if (rebuildAbortRef.current === controller) {
        rebuildAbortRef.current = null;
        setRebuilding(false);
      }
    }
  }, [t]);

  React.useEffect(() => {
    if (
      !interactive
      || !manifest?.stale
      || manifest.status !== "stale"
      || autoRebuildAttemptedRef.current
    ) return;
    autoRebuildAttemptedRef.current = true;
    void rebuild();
  }, [interactive, manifest, rebuild]);

  return (
    <div
      ref={containerRef}
      className={cn(
        "sag-knowledge-universe absolute inset-0 z-[2] origin-center overflow-hidden transition-[opacity,transform,filter] duration-700 ease-smooth will-change-[opacity,transform,filter]",
        interactive
          ? "scale-100 opacity-100 blur-0"
          : "pointer-events-none scale-[0.76] opacity-0 blur-[10px]",
      )}
      aria-label={t("aria")}
      aria-hidden={!interactive}
      style={activeSourceAccent
        ? { "--universe-source-accent": activeSourceAccent } as React.CSSProperties
        : undefined}
      data-universe-suspended={!interactive}
      data-universe-mode={interactive ? "explore" : "normal"}
      data-universe-view={viewportSourceId ? "detail" : "overview"}
      data-universe-evidence-origin={evidenceOrigin}
      data-universe-scene-strategy={sceneStrategy}
      data-universe-engine={
        sceneUnavailableReason
        ?? (webglAvailable === true ? "ready" : webglAvailable === false ? "api-unavailable" : "probing")
      }
      data-universe-node-budget={budget.nodes}
      data-universe-projection-node-budget={timelineProjectionBudget.nodes}
      data-universe-resident-node-budget={residentBudget.nodes}
      data-universe-timeline-phase={timelineJourney.phase}
      data-universe-visible-bundle-limit={bundleWindow.eventWindowSize}
      data-universe-transition-headroom={timelineWindowPlan.transitionHeadroomPackages}
      data-universe-visible-bundles={timelineWindow?.visibleBundleIds.length ?? 0}
      data-universe-cached-bundles={timelineWindow?.cacheBundleIds.length ?? 0}
    >
      {webglAvailable === true && sceneUnavailableReason === null && manifest ? (
        <div className="sag-universe-graph absolute inset-0">
          <UniverseScene
            key={`universe-scene:${sceneAttempt}`}
            ref={graphRef}
            data={graphData}
            strategy={sceneStrategy}
            policy={manifest.policy}
            sourceHits={sourceHits}
            selectedId={selectedKey}
            darkTheme={darkTheme}
            interactive={interactive}
            reducedMotion={Boolean(reducedMotion)}
            viewPreferences={viewPreferences}
            timelineJourney={timelineJourney}
            onNodeClick={handleNodeClick}
            onHover={handleSceneHover}
            onTimelineIntent={handleTimelineIntent}
            onTimelineSettled={handleTimelineSettled}
            onViewChange={handleSceneViewChange}
            onSourceLod={handleSourceLod}
            onSourceWheel={handleSourceWheel}
            onSelectionClear={clearSelection}
            onBackRequest={requestSourceBack}
            onBackgroundClick={handleSceneBackgroundClick}
            actionLabels={{
              viewDetails: t("nodeActions.viewDetails"),
              exploreMore: t("nodeActions.exploreMore"),
              askAi: t("nodeActions.askAi"),
            }}
            onViewDetails={handleViewDetails}
            onExploreMore={handleExploreMore}
            onAskNode={handleAskNode}
            onUserInteraction={handleSceneInteraction}
            onUnavailable={handleSceneUnavailable}
          />
        </div>
      ) : webglAvailable === false || sceneUnavailableReason !== null ? (
        <div className="absolute inset-0 grid place-items-center p-8">
          <div className="max-w-sm rounded-lg border border-border/70 bg-background/75 p-5 text-center shadow-soft backdrop-blur-md">
            <p className="text-sm font-medium">
              {t(
                sceneUnavailableReason === "context-disabled"
                  ? "webgl.contextDisabledTitle"
                  : sceneUnavailableReason === "context-creation"
                    ? "webgl.contextCreationTitle"
                    : sceneUnavailableReason === "dynamic-import"
                      ? "webgl.moduleTitle"
                      : sceneUnavailableReason
                        ? "webgl.sceneTitle"
                        : "webgl.title",
              )}
            </p>
            <p className="mt-2 text-xs leading-5 text-muted-foreground">
              {t(
                sceneUnavailableReason === "context-lost"
                  ? "webgl.contextLostDescription"
                  : sceneUnavailableReason === "context-disabled"
                    ? "webgl.contextDisabledDescription"
                    : sceneUnavailableReason === "context-creation"
                      ? "webgl.contextCreationDescription"
                      : sceneUnavailableReason === "dynamic-import"
                        ? "webgl.moduleDescription"
                        : sceneUnavailableReason
                          ? "webgl.sceneDescription"
                          : "webgl.description",
              )}
            </p>
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="mt-4"
              onClick={retryScene}
            >
              <RefreshCw className="size-3.5" />
              {t("webgl.retry")}
            </Button>
          </div>
        </div>
      ) : null}

      <div className="pointer-events-none absolute left-3 top-3 z-20 flex max-w-[calc(100vw-1.5rem)] flex-col items-start gap-2 sm:left-5 sm:top-5">
        <div className="flex max-w-full items-center gap-2">
          {showReturnHomeControl && (
            <Button
              type="button"
              variant="outline"
              size="icon"
              className="group pointer-events-auto size-9 shrink-0 bg-background/72 text-muted-foreground shadow-soft backdrop-blur-md hover:text-foreground"
              data-universe-home-control="true"
              aria-label={t("controls.home")}
              title={t("controls.homeHint")}
              onClick={requestSourceBack}
              disabled={!browseSessionSourceId && !working.nodes.length}
            >
              <ChevronLeft className="size-4 transition-transform group-hover:-translate-x-0.5" />
            </Button>
          )}
          <div
            data-universe-summary="true"
            className={cn(
              "flex items-center gap-2 overflow-hidden rounded-md border border-border/60 bg-background/62 px-2.5 py-2 text-[11px] text-muted-foreground shadow-soft backdrop-blur-md sm:gap-3 sm:px-3",
              showReturnHomeControl
                ? "max-w-[calc(100vw-7.25rem)]"
                : "max-w-[calc(100vw-1.5rem)]",
            )}
          >
          <AnimatePresence initial={false} mode="wait">
            {viewportSource ? (
              <motion.div
                key={`detail:${viewportSource.source_id}`}
                className="flex min-w-0 items-center gap-2"
                initial={reducedMotion ? false : { opacity: 0, y: -4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={reducedMotion ? { opacity: 0 } : { opacity: 0, y: 3 }}
                transition={{
                  duration: reducedMotion ? 0 : 0.18,
                  ease: [0.22, 1, 0.36, 1],
                }}
              >
                <span
                  className="size-1.5 shrink-0 rounded-full"
                  style={{
                    backgroundColor: "var(--universe-source-accent)",
                    boxShadow: "0 0 10px color-mix(in srgb, var(--universe-source-accent), transparent 34%)",
                  }}
                />
                <span
                  className="min-w-0 max-w-72 truncate font-medium text-foreground/90"
                  title={viewportSource.label}
                >
                  {viewportSource.label}
                </span>
                <span
                  className="min-w-0 max-w-52 truncate border-l border-border/70 pl-2 tabular-nums sm:pl-3"
                  title={manifest?.version
                    ? t("stats.source", { events: viewportSource.event_count, entities: viewportSource.entity_count })
                    : t("stats.sourceBuilding", { events: viewportSource.event_count })}
                >
                  {manifest?.version
                    ? t("stats.source", {
                        events: compactCount(viewportSource.event_count, locale),
                        entities: compactCount(viewportSource.entity_count, locale),
                      })
                    : t("stats.sourceBuilding", {
                        events: compactCount(viewportSource.event_count, locale),
                      })}
                </span>
              </motion.div>
            ) : (
              <motion.div
                key="overview"
                className="flex min-w-0 items-center gap-2 sm:gap-3"
                initial={reducedMotion ? false : { opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={reducedMotion ? { opacity: 0 } : { opacity: 0, y: -3 }}
                transition={{
                  duration: reducedMotion ? 0 : 0.18,
                  ease: [0.22, 1, 0.36, 1],
                }}
              >
                <span className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap">
                  <CircleDot className="size-3.5 text-[#a3c0ff]" />
                  <span className="hidden sm:inline">{t("legend.entities")}</span>
                </span>
                <span className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap">
                  <Sparkles className="size-3.5 text-amber-300" />
                  <span className="hidden sm:inline">{t("legend.events")}</span>
                </span>
                {summary ? (
                  <span className="min-w-0 max-w-64 truncate border-l border-border/70 pl-2 tabular-nums sm:pl-3" title={summary.query}>
                    {t("stats.result", visibleGraphCounts)}
                  </span>
                ) : manifest ? (
                  <span className="min-w-0 truncate border-l border-border/70 pl-2 tabular-nums sm:pl-3">
                    {manifest.version
                      ? t("stats.overview", {
                          sources: manifest.counts.sources ?? 0,
                          entities: compactCount(manifest.counts.entities ?? 0, locale),
                          events: compactCount(manifest.counts.events ?? 0, locale),
                        })
                      : t("stats.overviewBuilding", {
                          sources: manifest.counts.sources ?? 0,
                          events: compactCount(manifest.counts.events ?? 0, locale),
                        })}
                    {(manifest.counts.sources ?? 0) > renderedSourcePartitions.length
                      ? t("stats.visibleContours", { count: renderedSourcePartitions.length })
                      : ""}
                  </span>
                ) : null}
                {(manifest?.status === "failed" || manifest?.stale || manifest?.status === "building") && (
                  <TooltipProvider delayDuration={220}>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span
                          className={cn(
                            "size-2 shrink-0 rounded-full",
                            manifest.status === "failed"
                              ? "bg-rose-400 shadow-[0_0_8px_rgb(251_113_133_/_0.5)]"
                              : "bg-amber-300 shadow-[0_0_8px_rgb(252_211_77_/_0.45)]",
                          )}
                          role="status"
                          aria-label={manifest.status === "failed"
                            ? t("rebuild.failed")
                            : manifest.status === "building" || rebuilding
                              ? t("rebuild.running")
                              : t("rebuild.ready")}
                        />
                      </TooltipTrigger>
                      <TooltipContent side="bottom">
                        {manifest.status === "failed"
                          ? t("rebuild.failed")
                          : manifest.status === "building" || rebuilding
                            ? t("rebuild.running")
                            : t("rebuild.ready")}
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                )}
              </motion.div>
            )}
          </AnimatePresence>
          {expandingKey && <Loader2 className="size-3 animate-spin" />}
          </div>
        </div>

        <AnimatePresence initial={false}>
          {interactive && viewportLoadProgress && (
            <UniverseLoadProgressPanel
              key={viewportLoadProgress.sourceId}
              progress={viewportLoadProgress}
              reducedMotion={Boolean(reducedMotion)}
              explicitOnly={sessionState.mode !== "exploration"}
            />
          )}
        </AnimatePresence>
      </div>

      {(moreHint || error) && (
        <div className={cn(
          "pointer-events-none absolute bottom-5 left-1/2 z-10 max-w-md -translate-x-1/2 rounded-md border border-border/60 bg-background/72 px-3 py-2 text-[11px] text-muted-foreground shadow-soft backdrop-blur-md",
          timelineJourney.enabled && "bottom-20",
        )} role="status" aria-live="polite">
          {error || moreHint}
        </div>
      )}

      {(loading || webglAvailable === null) && (
        <div className="pointer-events-none absolute inset-0 z-10 grid place-items-center">
          <Loader2 className="size-5 animate-spin text-muted-foreground" />
        </div>
      )}

      {contextualWorkspaceActive && (
        <div
          className="absolute bottom-5 left-1/2 z-20 flex max-w-[calc(100vw-2rem)] -translate-x-1/2 items-center gap-2 rounded-full border border-amber-400/25 bg-background/82 p-1.5 pl-3 shadow-soft backdrop-blur-xl"
          data-universe-context-return="true"
        >
          <span className="min-w-0 truncate whitespace-nowrap text-[10px] text-muted-foreground">
            {contextWorkspace.section === "answer"
              ? t("controls.answerContext", {
                  count: Math.max(
                    contextActivationCount,
                    sessionState.evidenceBatchCount,
                  ),
                  events: contextGraphSessionRef.current ? summary?.events ?? 0 : 0,
                  entities: contextGraphSessionRef.current ? summary?.entities ?? 0 : 0,
                })
              : contextWorkspace.section === "search"
                ? t("controls.searchContext")
                : t("controls.evidenceContext", {
                    count: sessionState.evidenceBatchCount,
                    events: summary?.events ?? 0,
                    entities: summary?.entities ?? 0,
                  })}
          </span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 shrink-0 gap-1.5 rounded-full border-amber-400/30 bg-amber-400/10 px-3 text-[11px] text-amber-700 hover:bg-amber-400/15 hover:text-amber-800 dark:text-amber-300 dark:hover:text-amber-200"
            onClick={dispatchUniverseResume}
          >
            <Orbit className="size-3.5" />
            {t("controls.returnToExplore")}
          </Button>
        </div>
      )}

      {timelineControlsVisible && !contextualWorkspaceActive && (
        <TooltipProvider delayDuration={200}>
          <div
            className="absolute bottom-5 left-1/2 z-20 flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-border/65 bg-background/76 p-1.5 shadow-soft backdrop-blur-xl"
            data-universe-timeline-controls="true"
          >
            <IconControl
              label={t("controls.previousTimePage")}
              onClick={() => moveTimelineManually("previous")}
              disabled={!timelineJourney.hasPrevious
                || timelineJourney.phase === "loading"
                || timelineJourney.phase === "transitioning"}
            >
              <ChevronLeft className="size-3.5" />
            </IconControl>
            <IconControl
              label={t(timelinePlaying
                ? "controls.pauseTimeline"
                : "controls.playTimeline")}
              onClick={toggleTimelinePlayback}
              disabled={Boolean(reducedMotion)
                || (timelinePlaybackOrder === "reverse"
                  ? !timelineJourney.hasNext
                  : !timelineJourney.hasPrevious)}
            >
              {timelinePlaying
                ? <Pause className="size-3.5" />
                : <Play className="size-3.5" />}
            </IconControl>
            <span className="min-w-32 px-2 text-center text-[10px] tabular-nums text-muted-foreground">
              {t(timelineJourney.phase === "complete"
                ? "controls.completedTimePosition"
                : "controls.timePosition", {
                  time: visibleTimelineRange,
                })}
            </span>
            <IconControl
              label={t("controls.nextTimePage")}
              onClick={() => moveTimelineManually("next")}
              disabled={!timelineJourney.hasNext
                || timelineJourney.phase === "loading"
                || timelineJourney.phase === "transitioning"}
            >
              <ChevronRight className="size-3.5" />
            </IconControl>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-8 gap-1.5 rounded-full border-border/70 bg-background/75 px-2.5 text-[10px] text-muted-foreground shadow-soft backdrop-blur-md hover:bg-background hover:text-foreground"
                  aria-label={t(timelinePlaybackOrder === "reverse"
                    ? "controls.orderOlder"
                    : "controls.orderNewer")}
                  onClick={toggleTimelineOrder}
                >
                  <ArrowDownUp className="size-3" />
                  {t(timelinePlaybackOrder === "reverse"
                    ? "controls.reverse"
                    : "controls.chronological")}
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top">
                {t(timelinePlaybackOrder === "reverse"
                  ? "controls.orderOlder"
                  : "controls.orderNewer")}
              </TooltipContent>
            </Tooltip>
          </div>
        </TooltipProvider>
      )}

      {interactive && (
        <TooltipProvider delayDuration={240}>
          <div
            className="absolute bottom-5 right-5 z-10 flex flex-col gap-1.5"
            data-universe-controls="true"
          >
            <IconControl
              label={t("controls.currentResult")}
              onClick={focusResult}
              disabled={!summary && !selectedKey}
            >
              <LocateFixed className="size-3.5" />
            </IconControl>
            <IconControl
              label={t("controls.origin")}
              onClick={returnToTimelineOrigin}
              disabled={!manifest}
            >
              <Focus className="size-3.5" />
            </IconControl>
            <IconControl
              label={t("controls.reset")}
              onClick={() => resetScene(epochRef.current + 1)}
              disabled={!working.nodes.length}
            >
              <RotateCcw className="size-3.5" />
            </IconControl>
            <IconControl label={t("controls.refresh")} onClick={() => void rebuild()} disabled={rebuilding}>
              <RefreshCw className={cn("size-3.5", rebuilding && "animate-spin")} />
            </IconControl>
          </div>
        </TooltipProvider>
      )}
    </div>
  );
}
