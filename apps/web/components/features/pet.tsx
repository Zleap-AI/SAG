"use client";

import * as React from "react";
import { usePathname, useRouter } from "next/navigation";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useTranslations } from "next-intl";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Hand,
  Library,
  Loader2,
  MessageCircle,
  Music2,
  Rocket,
  RotateCcw,
  Route,
  Search,
  SlidersHorizontal,
  Sparkles,
  TriangleAlert,
  WandSparkles,
  X,
} from "lucide-react";

import { DEFAULT_AGENT_AVATAR, DEFAULT_AGENT_NAME } from "@/lib/branding";
import {
  APP_INITIALIZATION_DEFAULTS,
  persistPetCollapsed,
  readInitialPetCollapsed,
  shouldShowPet,
} from "@/lib/app-initialization";
import type { ConversationSessionSnapshot } from "@/lib/conversation-runtime";
import {
  PetAgent,
  type PetAgentActivity,
  type PetAgentFacing,
} from "@/lib/pet-agent";
import {
  resolvePetFace,
  usePetAppearancePreferences,
} from "@/lib/pet-appearance-preferences";
import {
  clampPetPosition,
  resolveExplorePetPosition,
} from "@/lib/pet-placement";
import { usePetPresence } from "@/lib/pet-preferences";
import {
  UNIVERSE_ASK_EVENT,
  UNIVERSE_DETAIL_EVENT,
  UNIVERSE_INTERACTION_EVENT,
  UNIVERSE_RESET_EVENT,
  UNIVERSE_RESUME_EVENT,
  UNIVERSE_SOURCE_FOCUS_EVENT,
  dispatchUniverseContext,
  readUniverseContext,
} from "@/lib/universe-events";
import { cn } from "@/lib/utils";
import type { WorkspaceSection } from "@/lib/workspace";
import { useApp } from "@/components/features/app-shell";
import {
  useOptionalConversationIndex,
  useOptionalConversationSession,
} from "@/components/features/chat/conversation-provider";
import {
  PetMiniWorkspace,
  type PetMiniView,
} from "@/components/features/pet-mini-workspace";
import { petFaceStyle } from "@/components/features/pet-head-avatar";

type PetVisualMode = PetAgentActivity | "jumping" | "flying" | "roaming" | "dancing";
type PetFormTransition = "idle" | "bursting" | "falling" | "launching";

interface PetActivity {
  streaming: boolean;
  mode: Exclude<PetAgentActivity, "done">;
  label: string;
  threadId: string | null;
  runKey: string | null;
  failed: boolean;
}

const IDLE_EXPRESSIONS = ["^_^", "-_-", "o_o", "._.", "u_u"] as const;
const PET_VIEWPORT_MARGIN = 24;
const PET_BUBBLE_POP_DURATION = 240;
const PET_BUBBLE_FALL_DURATION = 460;
const PET_BUBBLE_LAUNCH_DURATION = 1_250;
const PET_BUBBLE_DROP_DISTANCE = 28;

function visiblePlacementAvoidRects() {
  return [...document.querySelectorAll<HTMLElement>("[data-universe-controls]")]
    .filter((element) => element.offsetParent !== null)
    .map((element) => {
      const bounds = element.getBoundingClientRect();
      return {
        x: bounds.left,
        y: bounds.top,
        width: bounds.width,
        height: bounds.height,
      };
    });
}

const MODE_EXPRESSIONS: Partial<Record<PetVisualMode, string>> = {
  thinking: "◔_◔",
  searching: "o_o",
  working: ">_<",
  answering: "^_^",
  done: "^o^",
  error: "x_x",
  jumping: "^_^",
  flying: "^O^",
  roaming: "^o^",
  dancing: "^3^",
};

function nameplateStyle(value: string): React.CSSProperties {
  const length = Array.from(value).length;
  if (length <= 3) return { fontSize: 7 };
  if (length <= 5) return { fontSize: 6 };
  return { fontSize: 5 };
}

function deriveActivity(
  state: ConversationSessionSnapshot | null,
  labels: { thinking: string; answering: string; working: string },
): PetActivity {
  const steps = state?.run?.steps ?? [];
  const active = [...steps].reverse().find((step) => step.status === "active");
  const failed = Boolean(state?.error) && !state?.run;

  if (!state?.run) {
    return {
      streaming: false,
      mode: failed ? "error" : "idle",
      label: "",
      threadId: state?.threadId ?? null,
      runKey: null,
      failed,
    };
  }
  if (!active || active.kind === "thinking") {
    return {
      streaming: true,
      mode: "thinking",
      label: labels.thinking,
      threadId: state.threadId,
      runKey: `${state.sessionId}:${state.run.requestId}`,
      failed,
    };
  }
  if (active.kind === "answer") {
    return {
      streaming: true,
      mode: "answering",
      label: labels.answering,
      threadId: state.threadId,
      runKey: `${state.sessionId}:${state.run.requestId}`,
      failed,
    };
  }
  return {
    streaming: true,
    mode: "working",
    label: active.label || active.name || labels.working,
    threadId: state.threadId,
    runKey: `${state.sessionId}:${state.run.requestId}`,
    failed,
  };
}

function usePetActivity() {
  const t = useTranslations("Pet");
  const index = useOptionalConversationIndex();
  const session = useOptionalConversationSession(
    index.activeRunSessionId ?? index.activeSessionId,
  );
  return React.useMemo(() => deriveActivity(session, {
    thinking: t("status.thinking"),
    answering: t("status.answering"),
    working: t("status.working"),
  }), [session, t]);
}

interface PetProps {
  ambient?: boolean;
  character?: PetAgent;
  syncIdentity?: boolean;
  visible?: boolean;
}

interface PetRoamPath {
  x: number[];
  y: number[];
}

export function PetWithPreference(props: Omit<PetProps, "visible">) {
  const { appMode } = useApp();
  const [presence] = usePetPresence();
  const visible = shouldShowPet(appMode, presence);
  return <Pet {...props} visible={visible} />;
}

interface PetActionButtonProps {
  children: React.ReactNode;
  disabled?: boolean;
  label: string;
  onClick: () => void;
  slot:
    | "ask"
    | "search"
    | "answer"
    | "knowledge"
    | "actions"
    | "appearance"
    | "wave"
    | "fly"
    | "roam"
    | "dance"
    | "collapse"
    | "expand";
}

function PetActionButton({
  children,
  disabled,
  label,
  onClick,
  slot,
}: PetActionButtonProps) {
  return (
    <button
      type="button"
      data-slot={slot}
      disabled={disabled}
      aria-label={label}
      title={label}
      className="sag-pet__action"
      onPointerDown={(event) => event.stopPropagation()}
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
    >
      {children}
    </button>
  );
}

/** 视口级知识宇航员：角色对象负责状态与动作，组件只处理输入和渲染。 */
export function Pet({
  ambient = false,
  character: providedCharacter,
  syncIdentity,
  visible = true,
}: PetProps = {}) {
  const t = useTranslations("Pet");
  const {
    agent,
    threads,
    appMode,
    workspaceSection,
    enterExploreMode,
  } = useApp();
  const router = useRouter();
  const pathname = usePathname();
  const reduceMotion = useReducedMotion();
  const activity = usePetActivity();
  const ownedCharacter = React.useMemo(
    () => new PetAgent({ name: DEFAULT_AGENT_NAME, avatar: DEFAULT_AGENT_AVATAR, size: 1 }),
    [],
  );
  const character = providedCharacter ?? ownedCharacter;
  const shouldSyncIdentity = syncIdentity ?? !providedCharacter;
  const characterState = React.useSyncExternalStore(
    character.subscribe,
    character.getSnapshot,
    character.getSnapshot,
  );
  const [alignRight, setAlignRight] = React.useState(false);
  const [panelAbove, setPanelAbove] = React.useState(true);
  const [open, setOpen] = React.useState(false);
  const [miniView, setMiniView] = React.useState<PetMiniView>("workspace");
  const [transientDetailPreview, setTransientDetailPreview] = React.useState(false);
  const [collapsed, setCollapsed] = React.useState<boolean>(
    APP_INITIALIZATION_DEFAULTS.petCollapsed,
  );
  const [formTransition, setFormTransition] = React.useState<PetFormTransition>("idle");
  const [revealing, setRevealing] = React.useState(false);
  const [curious, setCurious] = React.useState(false);
  const [petOverlay, setPetOverlay] = React.useState<"none" | "actions">("none");
  const [roamPath, setRoamPath] = React.useState<PetRoamPath | null>(null);
  const { preferences: appearance } = usePetAppearancePreferences();
  const dragRef = React.useRef<{
    dx: number;
    dy: number;
    startX: number;
    startY: number;
    lastX: number;
    facing: PetAgentFacing;
    moved: boolean;
    startedOnForm: boolean;
  } | null>(null);
  const lastPosRef = React.useRef<{ x: number; y: number } | null>(null);
  const freePositionRef = React.useRef<{ x: number; y: number } | null>(null);
  const explorePositionedRef = React.useRef(false);
  const expandActionTimerRef = React.useRef<number | null>(null);
  const commandCloseTimerRef = React.useRef<number | null>(null);
  const wasStreamingRef = React.useRef(activity.streaming);
  const activeRunKeyRef = React.useRef<string | null>(activity.runKey);
  const notifiedRunKeysRef = React.useRef(new Set<string>());
  const exploreSourceRef = React.useRef<string | null>(null);
  const pointerFrameRef = React.useRef<number | null>(null);
  const pointerRef = React.useRef({ x: 0, y: 0 });
  const elRef = React.useRef<HTMLDivElement>(null);
  const visualRef = React.useRef<HTMLDivElement>(null);
  const agentFace = agent?.avatar || DEFAULT_AGENT_AVATAR;
  const identityFace = shouldSyncIdentity ? agentFace : characterState.identity.avatar;
  const displayFace = resolvePetFace(appearance, identityFace);
  const motionReduced = Boolean(reduceMotion || appearance.reduceMotion);

  React.useEffect(
    () => () => {
      if (!providedCharacter) ownedCharacter.destroy();
    },
    [ownedCharacter, providedCharacter],
  );

  React.useEffect(() => {
    if (!shouldSyncIdentity) return;
    character.configure({
      name: agent?.name || DEFAULT_AGENT_NAME,
      avatar: displayFace,
      size: appearance.size,
    });
  }, [agent?.name, appearance.size, character, displayFace, shouldSyncIdentity]);

  React.useEffect(() => {
    try {
      const saved = window.localStorage.getItem("sag:pet-pos");
      if (saved) {
        const parsed = JSON.parse(saved) as { x?: unknown; y?: unknown };
        if (typeof parsed.x === "number" && typeof parsed.y === "number") {
          const width = 94 * character.getSnapshot().identity.size;
          const height = 118 * character.getSnapshot().identity.size;
          const next = {
            x: Math.min(
              Math.max(PET_VIEWPORT_MARGIN, parsed.x),
              Math.max(PET_VIEWPORT_MARGIN, window.innerWidth - width - PET_VIEWPORT_MARGIN),
            ),
            y: Math.min(
              Math.max(PET_VIEWPORT_MARGIN, parsed.y),
              Math.max(PET_VIEWPORT_MARGIN, window.innerHeight - height - PET_VIEWPORT_MARGIN),
            ),
          };
          character.moveTo(next);
          lastPosRef.current = next;
          setAlignRight(next.x + width / 2 > window.innerWidth / 2);
          setPanelAbove(next.y + height / 2 > window.innerHeight / 2);
        }
      }
      const savedFacing = window.localStorage.getItem("sag:pet-facing");
      if (savedFacing === "left" || savedFacing === "right") character.face(savedFacing);
      if (!ambient) setCollapsed(readInitialPetCollapsed(window.localStorage));
    } catch {
      /* ignore */
    }
  }, [ambient, character]);

  React.useEffect(() => {
    if (appMode === "explore") {
      // Exploration owns the canvas. Keep the mini workspace out of the way
      // until the user explicitly opens it or asks to inspect a node.
      exploreSourceRef.current = null;
      setOpen(false);
      setTransientDetailPreview(false);
      setPetOverlay("none");
      return;
    }

    setOpen(false);
    setTransientDetailPreview(false);

    // Exiting exploration removes a hovered child without always
    // producing pointerleave on the pet shell. Clear that latched state so
    // commands return to their hover/focus-only presentation.
    setCurious(false);
    setPetOverlay("none");
    setMiniView("workspace");
    if (commandCloseTimerRef.current !== null) {
      window.clearTimeout(commandCloseTimerRef.current);
      commandCloseTimerRef.current = null;
    }
  }, [appMode]);

  React.useEffect(() => {
    if (appMode !== "explore") return;
    const reopenDetailPreview = () => {
      // A graph-node click is a reversible reading preview. If no contextual
      // search/answer session was already active, opening the detail must not
      // create one merely because the underlying workspace has such a section.
      setTransientDetailPreview(!readUniverseContext().active);
      setMiniView("workspace");
      setPetOverlay("none");
      setOpen(true);
    };
    const reopenAnswerWorkspace = () => {
      setTransientDetailPreview(false);
      setMiniView("workspace");
      setPetOverlay("none");
      setOpen(true);
    };
    window.addEventListener(UNIVERSE_DETAIL_EVENT, reopenDetailPreview);
    window.addEventListener(UNIVERSE_ASK_EVENT, reopenAnswerWorkspace);
    return () => {
      window.removeEventListener(UNIVERSE_DETAIL_EVENT, reopenDetailPreview);
      window.removeEventListener(UNIVERSE_ASK_EVENT, reopenAnswerWorkspace);
    };
  }, [appMode]);

  React.useEffect(() => {
    const section = workspaceSection === "search" || workspaceSection === "answer"
      ? workspaceSection
      : null;
    const active = appMode === "explore"
      && open
      && miniView === "workspace"
      && !transientDetailPreview
      && section !== null;
    dispatchUniverseContext({ active, section: active ? section : null });
    return () => {
      if (active) dispatchUniverseContext({ active: false, section: null });
    };
  }, [appMode, miniView, open, transientDetailPreview, workspaceSection]);

  React.useEffect(() => {
    if (appMode !== "explore") return;
    const closeForCanvasGesture = () => {
      // This event is emitted only for a real pointer/wheel gesture on the
      // graph surface. Yield the screen to that gesture without dispatching a
      // context resume: answer/search graph updates themselves never emit it.
      setTransientDetailPreview(false);
      setOpen(false);
      setPetOverlay("none");
    };
    const closeForResume = () => {
      setTransientDetailPreview(false);
      setOpen(false);
      setPetOverlay("none");
    };
    const closeForReset = (event: Event) => {
      const owner = (event as CustomEvent<{ owner?: string }>).detail?.owner;
      // Search is a contextual workspace inside exploration. Its lifecycle
      // must not dismiss the panel that is presenting the search itself.
      if (owner?.startsWith("search")) return;
      closeForCanvasGesture();
    };
    const closeOnSourceChange = (event: Event) => {
      const detail = (event as CustomEvent<{
        mode?: string;
        source_id?: string | null;
      }>).detail;
      const sourceId = detail?.source_id ?? null;
      const changed = sourceId !== exploreSourceRef.current;
      if (changed) {
        exploreSourceRef.current = sourceId;
        setTransientDetailPreview(false);
        setOpen(false);
        setPetOverlay("none");
      }
    };
    window.addEventListener(UNIVERSE_INTERACTION_EVENT, closeForCanvasGesture);
    window.addEventListener(UNIVERSE_RESET_EVENT, closeForReset);
    window.addEventListener(UNIVERSE_RESUME_EVENT, closeForResume);
    window.addEventListener(UNIVERSE_SOURCE_FOCUS_EVENT, closeOnSourceChange);
    return () => {
      window.removeEventListener(UNIVERSE_INTERACTION_EVENT, closeForCanvasGesture);
      window.removeEventListener(UNIVERSE_RESET_EVENT, closeForReset);
      window.removeEventListener(UNIVERSE_RESUME_EVENT, closeForResume);
      window.removeEventListener(UNIVERSE_SOURCE_FOCUS_EVENT, closeOnSourceChange);
    };
  }, [appMode]);

  React.useEffect(() => {
    if (collapsed && petOverlay === "actions") setPetOverlay("none");
  }, [collapsed, petOverlay]);

  React.useEffect(() => {
    lastPosRef.current = characterState.position;
  }, [characterState.position]);

  React.useEffect(() => {
    if (ambient) return;
    const frame = window.requestAnimationFrame(() => {
      if (appMode === "explore") {
        if (explorePositionedRef.current) return;
        const visual = visualRef.current;
        if (!visual) return;
        const rect = visual.getBoundingClientRect();
        freePositionRef.current = lastPosRef.current ?? { x: rect.left, y: rect.top };
        const next = resolveExplorePetPosition({
          viewport: { width: window.innerWidth, height: window.innerHeight },
          pet: { width: rect.width, height: rect.height },
          avoidRects: visiblePlacementAvoidRects(),
          margin: PET_VIEWPORT_MARGIN,
        });
        explorePositionedRef.current = true;
        lastPosRef.current = next;
        character.moveTo(next);
        setAlignRight(true);
        setPanelAbove(true);
        return;
      }

      if (!explorePositionedRef.current) return;
      explorePositionedRef.current = false;
      const restore = freePositionRef.current;
      freePositionRef.current = null;
      if (!restore) return;
      const width = visualRef.current?.offsetWidth ?? 94;
      const height = visualRef.current?.offsetHeight ?? 118;
      const next = clampPetPosition(
        restore,
        { width: window.innerWidth, height: window.innerHeight },
        { width, height },
        PET_VIEWPORT_MARGIN,
      );
      lastPosRef.current = next;
      character.moveTo(next);
      setAlignRight(next.x + width / 2 > window.innerWidth / 2);
      setPanelAbove(next.y + height / 2 > window.innerHeight / 2);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [ambient, appMode, character, visible]);

  React.useEffect(() => {
    const keepVisible = () => {
      if (appMode === "explore" && !explorePositionedRef.current) return;
      const current = lastPosRef.current;
      if (!current) return;
      const width = visualRef.current?.offsetWidth ?? 94;
      const height = visualRef.current?.offsetHeight ?? 118;
      const next = clampPetPosition(
        current,
        { width: window.innerWidth, height: window.innerHeight },
        { width, height },
        PET_VIEWPORT_MARGIN,
      );
      lastPosRef.current = next;
      character.moveTo(next);
      setAlignRight(next.x + width / 2 > window.innerWidth / 2);
      setPanelAbove(next.y + height / 2 > window.innerHeight / 2);
    };
    keepVisible();
    window.addEventListener("resize", keepVisible);
    return () => window.removeEventListener("resize", keepVisible);
  }, [appMode, character, collapsed]);

  const activeThread = activity.threadId
    ? threads.find((thread) => thread.id === activity.threadId) ?? null
    : null;
  const answerVisible =
    (appMode === "normal" && (pathname === "/chat" || pathname.startsWith("/chat/")))
    || (appMode === "explore" && workspaceSection === "answer");
  const canAct = !motionReduced && !activity.streaming && characterState.motion === "idle";

  const triggerWave = React.useCallback(() => {
    if (!canAct) return;
    character.wave();
  }, [canAct, character]);

  const triggerFlight = React.useCallback(() => {
    if (!canAct) return;
    const top = visualRef.current?.getBoundingClientRect().top ?? 140;
    const availableLift = top - 10;
    setPetOverlay("none");
    if (availableLift < 42) character.jump();
    else character.fly({ height: Math.min(300, availableLift) });
  }, [canAct, character]);

  const triggerDance = React.useCallback(() => {
    if (!canAct) return;
    setPetOverlay("none");
    character.dance();
  }, [canAct, character]);

  const triggerRoam = React.useCallback(() => {
    if (!canAct) return;
    const visual = visualRef.current;
    if (!visual) return;
    const rect = visual.getBoundingClientRect();
    const margin = 14;
    const left = margin - rect.left;
    const right = window.innerWidth - rect.width - margin - rect.left;
    const top = margin - rect.top;
    const bottom = window.innerHeight - rect.height - margin - rect.top;
    const startsBelowCenter = rect.top + rect.height / 2 > window.innerHeight / 2;
    setRoamPath(
      startsBelowCenter
        ? {
            x: [0, left, left, right, right, left, 0],
            y: [0, bottom, top, top, bottom, bottom, 0],
          }
        : {
            x: [0, left, left, right, right, left, 0],
            y: [0, top, bottom, bottom, top, top, 0],
          },
    );
    setPetOverlay("none");
    character.roam();
  }, [canAct, character]);

  React.useEffect(() => {
    if (!revealing) return;
    const timer = window.setTimeout(() => setRevealing(false), 560);
    return () => window.clearTimeout(timer);
  }, [revealing]);

  React.useEffect(() => {
    if (formTransition !== "launching" || characterState.motion === "fly") return;
    setFormTransition("idle");
  }, [characterState.motion, formTransition]);

  React.useEffect(
    () => () => {
      if (expandActionTimerRef.current !== null) {
        window.clearTimeout(expandActionTimerRef.current);
      }
    },
    [],
  );

  React.useEffect(() => {
    if (activity.streaming) {
      activeRunKeyRef.current = activity.runKey;
      const options = {
        threadId: activity.threadId ?? undefined,
        detail: activeThread?.title,
        duration: null,
      } as const;
      if (activity.mode === "thinking") character.think(activity.label, options);
      else if (activity.mode === "searching") character.search(activity.label, options);
      else if (activity.mode === "answering") character.answer(activity.label, options);
      else character.work(activity.label, options);
    } else if (wasStreamingRef.current) {
      const completedRunKey = activeRunKeyRef.current;
      const options = {
        threadId: activity.threadId ?? undefined,
        detail: activeThread?.title,
      };
      if (answerVisible) {
        // The answer is already in front of the user. Returning to idle is
        // feedback enough; a foreground completion bubble is notification spam.
        character.idle();
      } else if (
        !completedRunKey
        || !notifiedRunKeysRef.current.has(completedRunKey)
      ) {
        if (completedRunKey) {
          notifiedRunKeysRef.current.add(completedRunKey);
          if (notifiedRunKeysRef.current.size > 64) {
            const oldest = notifiedRunKeysRef.current.values().next().value;
            if (typeof oldest === "string") notifiedRunKeysRef.current.delete(oldest);
          }
        }
        if (activity.failed) character.fail(t("status.failed"), { ...options, duration: 3_600 });
        else character.complete(t("status.complete"), { ...options, duration: 3_200 });
      }
      activeRunKeyRef.current = null;
    }
    wasStreamingRef.current = activity.streaming;
  }, [
    activeThread?.title,
    answerVisible,
    activity.failed,
    activity.label,
    activity.mode,
    activity.runKey,
    activity.streaming,
    activity.threadId,
    character,
    t,
  ]);

  React.useEffect(() => {
    if (
      answerVisible
      && !activity.streaming
      && (
        characterState.activity === "done"
        || characterState.speech?.tone === "active"
      )
    ) {
      character.idle();
    }
  }, [
    activity.streaming,
    answerVisible,
    character,
    characterState.activity,
    characterState.speech?.tone,
  ]);

  React.useEffect(() => {
    if (
      motionReduced ||
      !visible ||
      collapsed ||
      open ||
      appearance.actionRate <= 0.05 ||
      characterState.activity !== "idle" ||
      characterState.motion !== "idle"
    ) {
      return;
    }
    const timer = window.setTimeout(() => {
      const roll = Math.random();
      if (roll < 0.2) triggerDance();
      else if (roll < 0.44) triggerFlight();
      else triggerWave();
    }, (16_000 + Math.round(Math.random() * 12_000)) / appearance.actionRate);
    return () => window.clearTimeout(timer);
  }, [
    characterState.activity,
    characterState.motion,
    appearance.actionRate,
    collapsed,
    open,
    motionReduced,
    triggerDance,
    triggerFlight,
    triggerWave,
    visible,
  ]);

  React.useEffect(() => {
    if (
      motionReduced ||
      !visible ||
      open ||
      curious ||
      displayFace.length === 0 ||
      characterState.activity !== "idle" ||
      characterState.motion !== "idle" ||
      characterState.expression
    ) {
      return;
    }
    const timer = window.setTimeout(() => {
      const choices = IDLE_EXPRESSIONS.filter((expression) => expression !== displayFace);
      const expression = choices[Math.floor(Math.random() * choices.length)] ?? "^_^";
      character.emote(expression, 1_650 + Math.round(Math.random() * 900));
    }, (7_500 + Math.round(Math.random() * 8_500)) * appearance.expressionDelay);
    return () => window.clearTimeout(timer);
  }, [
    character,
    characterState.activity,
    characterState.expression,
    characterState.motion,
    curious,
    displayFace,
    appearance.expressionDelay,
    open,
    motionReduced,
    visible,
  ]);

  React.useEffect(() => {
    if (motionReduced) return;
    const updateLook = () => {
      pointerFrameRef.current = null;
      const visual = visualRef.current;
      if (!visual) return;
      const rect = visual.getBoundingClientRect();
      const dx = Math.max(-1, Math.min(1, (pointerRef.current.x - (rect.left + rect.width / 2)) / 340));
      const dy = Math.max(-1, Math.min(1, (pointerRef.current.y - (rect.top + rect.height / 2)) / 260));
      character.lookAt(dx, dy);
    };
    const onPointerMove = (event: PointerEvent) => {
      pointerRef.current = { x: event.clientX, y: event.clientY };
      if (pointerFrameRef.current === null) {
        pointerFrameRef.current = requestAnimationFrame(updateLook);
      }
    };
    window.addEventListener("pointermove", onPointerMove, { passive: true });
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      if (pointerFrameRef.current !== null) cancelAnimationFrame(pointerFrameRef.current);
    };
  }, [character, motionReduced]);

  const showCommands = React.useCallback(() => {
    if (commandCloseTimerRef.current !== null) {
      window.clearTimeout(commandCloseTimerRef.current);
      commandCloseTimerRef.current = null;
    }
    setCurious(true);
  }, []);

  const scheduleCommandClose = React.useCallback(() => {
    if (commandCloseTimerRef.current !== null) {
      window.clearTimeout(commandCloseTimerRef.current);
    }
    commandCloseTimerRef.current = window.setTimeout(() => {
      commandCloseTimerRef.current = null;
      setCurious(false);
    }, 520);
  }, []);

  React.useEffect(
    () => () => {
      if (commandCloseTimerRef.current !== null) {
        window.clearTimeout(commandCloseTimerRef.current);
      }
    },
    [],
  );

  const visualMode: PetVisualMode =
    characterState.motion === "fly"
      ? "flying"
      : characterState.motion === "roam"
        ? "roaming"
        : characterState.motion === "dance"
          ? "dancing"
      : characterState.motion === "jump"
          ? "jumping"
          : characterState.activity;
  const statusText = characterState.speech?.text ?? "";
  const statusThread = characterState.speech?.threadId
    ? threads.find((thread) => thread.id === characterState.speech?.threadId) ?? null
    : activeThread;
  const isBusy = ["thinking", "searching", "working", "answering"].includes(
    characterState.activity,
  );
  const renderedFace =
    (characterState.motion === "wave" ? "^_^" : null) ??
    MODE_EXPRESSIONS[visualMode] ??
    (curious && !open && visualMode === "idle" ? "?_?" : null) ??
    characterState.expression ??
    displayFace;
  const petName = Array.from(characterState.identity.name || DEFAULT_AGENT_NAME)
    .slice(0, 7)
    .join("");

  function setFacing(direction: PetAgentFacing) {
    character.face(direction);
    window.localStorage.setItem("sag:pet-facing", direction);
  }

  function resetPetPosition() {
    const width = 94 * character.getSnapshot().identity.size;
    const height = 118 * character.getSnapshot().identity.size;
    const next = {
      x: Math.max(PET_VIEWPORT_MARGIN, window.innerWidth - width - 32),
      y: Math.max(PET_VIEWPORT_MARGIN, window.innerHeight - height - 88),
    };
    lastPosRef.current = next;
    character.moveTo(next);
    setAlignRight(true);
    setPanelAbove(true);
    window.localStorage.setItem("sag:pet-pos", JSON.stringify(next));
  }

  function openMiniWorkspace(section: WorkspaceSection) {
    setCurious(false);
    setPetOverlay("none");
    setMiniView("workspace");
    setTransientDetailPreview(false);
    setOpen(true);
    enterExploreMode(section);
  }

  function openAssistantSettings() {
    setCurious(false);
    setPetOverlay("none");
    setMiniView("assistant-settings");
    setTransientDetailPreview(false);
    setOpen(true);
    enterExploreMode(workspaceSection);
  }

  function switchToSimpleForm() {
    if (ambient) return;
    setPetOverlay("none");
    setCurious(false);
    setRevealing(false);
    if (expandActionTimerRef.current !== null) {
      window.clearTimeout(expandActionTimerRef.current);
      expandActionTimerRef.current = null;
    }
    setFormTransition("idle");
    setCollapsed(true);
    persistPetCollapsed(window.localStorage, true);
  }

  function expandFromHead() {
    if (!collapsed || formTransition !== "idle") return;
    setPetOverlay("none");
    setCurious(false);

    const revealFullForm = () => {
      setCollapsed(false);
      setRevealing(true);
      persistPetCollapsed(window.localStorage, false);
    };

    if (motionReduced) {
      revealFullForm();
      return;
    }

    setFormTransition("bursting");
    expandActionTimerRef.current = window.setTimeout(() => {
      setFormTransition("falling");

      expandActionTimerRef.current = window.setTimeout(() => {
        expandActionTimerRef.current = null;
        revealFullForm();
        character.emote("^o^", PET_BUBBLE_LAUNCH_DURATION);
        const current = character.getSnapshot();
        if (current.activity !== "idle" || current.motion !== "idle") {
          setFormTransition("idle");
          return;
        }

        setFormTransition("launching");
        character.fly({
          height: PET_BUBBLE_DROP_DISTANCE,
          duration: PET_BUBBLE_LAUNCH_DURATION,
        });
      }, PET_BUBBLE_FALL_DURATION);
    }, PET_BUBBLE_POP_DURATION);
  }

  function togglePetForm() {
    if (ambient) {
      triggerWave();
      return;
    }
    if (collapsed) expandFromHead();
    else switchToSimpleForm();
  }

  function onPointerDown(event: React.PointerEvent) {
    if (event.button !== 0) return;
    setCurious(false);
    const visual = visualRef.current;
    const el = elRef.current;
    if (!visual || !el) return;
    const rect = visual.getBoundingClientRect();
    dragRef.current = {
      dx: event.clientX - rect.left,
      dy: event.clientY - rect.top,
      startX: event.clientX,
      startY: event.clientY,
      lastX: event.clientX,
      facing: characterState.facing,
      moved: false,
      startedOnForm:
        event.target instanceof Element
        && event.target.closest("[data-pet-form-toggle='true']") !== null,
    };
    el.setPointerCapture(event.pointerId);
  }

  function onPointerMove(event: React.PointerEvent) {
    const drag = dragRef.current;
    if (!drag) {
      showCommands();
      return;
    }
    if (!drag.moved && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 8) return;
    drag.moved = true;
    const horizontalDelta = event.clientX - drag.lastX;
    if (Math.abs(horizontalDelta) >= 8) {
      drag.facing = horizontalDelta < 0 ? "left" : "right";
      character.face(drag.facing);
      drag.lastX = event.clientX;
    }
    const width = visualRef.current?.offsetWidth ?? 94;
    const height = visualRef.current?.offsetHeight ?? 118;
    const next = {
      x: Math.min(
        Math.max(PET_VIEWPORT_MARGIN, event.clientX - drag.dx),
        window.innerWidth - width - PET_VIEWPORT_MARGIN,
      ),
      y: Math.min(
        Math.max(PET_VIEWPORT_MARGIN, event.clientY - drag.dy),
        window.innerHeight - height - PET_VIEWPORT_MARGIN,
      ),
    };
    lastPosRef.current = next;
    character.moveTo(next);
    setAlignRight(next.x + width / 2 > window.innerWidth / 2);
    setPanelAbove(next.y + height / 2 > window.innerHeight / 2);
  }

  function onPointerUp() {
    const drag = dragRef.current;
    dragRef.current = null;
    if (drag?.moved && lastPosRef.current) {
      if (appMode === "normal") {
        window.localStorage.setItem("sag:pet-pos", JSON.stringify(lastPosRef.current));
      }
      window.localStorage.setItem("sag:pet-facing", drag.facing);
    }
    if (drag?.startedOnForm && !drag.moved) {
      togglePetForm();
    }
  }

  function onPointerCancel() {
    dragRef.current = null;
  }

  const style: React.CSSProperties = characterState.position
    ? { left: characterState.position.x, top: characterState.position.y, right: "auto", bottom: "auto" }
    : {};
  const panelSide = alignRight ? "right-0" : "left-0";
  const panelVertical = panelAbove ? "bottom-full mb-2" : "top-full mt-2";
  const statusPanelVertical = panelAbove
    ? "bottom-[calc(100%+3rem)]"
    : "top-full mt-2";
  const scale = characterState.identity.size;
  const visualStyle = {
    width: (collapsed ? 82 : 94) * scale,
    height: (collapsed ? 82 : 118) * scale,
  } as React.CSSProperties;
  const characterStyle = {
    "--pet-scale": scale,
    "--pet-look-x": `${(characterState.look.x * 2.4).toFixed(2)}px`,
    "--pet-look-y": `${(characterState.look.y * 1.7).toFixed(2)}px`,
    "--pet-look-rotate": `${(characterState.look.x * 1.2).toFixed(2)}deg`,
  } as React.CSSProperties;
  const visualAnimate = motionReduced
    ? undefined
    : formTransition === "falling"
      ? {
          x: [0, 0.2, 0.7, 0],
          y: [0, 3, 13, PET_BUBBLE_DROP_DISTANCE],
          rotate: [0, 0.2, 0.9, 0],
        }
    : collapsed
      ? {
          x: [0, 0.8 * appearance.floatStrength, 0],
          y: [0, -2.2 * appearance.floatStrength, 0],
          rotate: [0, 0.65 * appearance.floatStrength, 0],
        }
    : characterState.motion === "fly"
      ? formTransition === "launching"
        ? {
            x: [0, 0.8, -0.7, 0.45, -0.2, 0],
            y: [
              PET_BUBBLE_DROP_DISTANCE,
              PET_BUBBLE_DROP_DISTANCE + 2,
              21,
              7,
              -3,
              0,
            ],
            rotate: [0, 0.8, -1.4, 0.8, -0.25, 0],
          }
        : {
          x: [0, 1.8, -1.2, 0.8, -0.5, 0.3, -0.2, 0],
          y: [
            0,
            -characterState.flightLift * 0.18,
            -characterState.flightLift,
            -characterState.flightLift * 1.03,
            -characterState.flightLift * 0.97,
            -characterState.flightLift * 0.76,
            -characterState.flightLift * 0.38,
            0,
          ],
          rotate: [0, -3.2, 1.8, -1.4, 0.8, -0.5, 0.2, 0],
        }
      : characterState.motion === "roam"
        ? roamPath
          ? {
              x: roamPath.x,
              y: roamPath.y,
              rotate: [0, -2.4, 1.2, 2.2, -1.4, -2, 0],
            }
          : { x: 0, y: 0, rotate: 0 }
      : characterState.motion === "dance"
        ? {
            x: [0, -5, 5, -6, 6, -5, 5, 0],
            y: [0, -8, 0, -12, 0, -9, 0, 0],
            rotate: [0, -7, 7, -9, 9, -7, 7, 0],
          }
      : characterState.motion === "jump"
        ? { x: [0, 0.5, 0], y: [0, -8, 0], rotate: [0, -2, 0] }
        : visualMode === "idle" && curious
          ? { x: 0, y: -1.5, rotate: 0 }
        : visualMode === "idle"
          ? {
              x: [0, 4.2, 1.4, -4.6, -1.5, 0].map(
                (value) => value * appearance.floatStrength,
              ),
              y: [0, -5.8, -2.4, 3.8, 1.2, 0].map(
                (value) => value * appearance.floatStrength,
              ),
              rotate: [0, 0.62, 0.2, -0.56, -0.14, 0].map(
                (value) => value * appearance.floatStrength,
              ),
            }
          : { x: [0, 0.45, 0], y: [0, -1, 0], rotate: [0, 0, 0] };
  const visualTransition = motionReduced
    ? undefined
    : formTransition === "falling"
      ? {
          duration: PET_BUBBLE_FALL_DURATION / 1_000,
          times: [0, 0.22, 0.58, 1],
          ease: "easeIn" as const,
        }
    : collapsed
      ? { duration: 4.6, repeat: Infinity, ease: "easeInOut" as const }
    : characterState.motion === "fly"
      ? formTransition === "launching"
        ? {
          duration: PET_BUBBLE_LAUNCH_DURATION / 1_000,
            times: [0, 0.08, 0.3, 0.58, 0.84, 1],
            ease: "easeOut" as const,
          }
        : {
          duration: 6.8,
          times: [0, 0.04, 0.18, 0.28, 0.42, 0.6, 0.82, 1],
          ease: "easeInOut" as const,
        }
      : characterState.motion === "roam"
        ? {
            duration: roamPath ? 14 : 0,
            times: roamPath ? [0, 0.08, 0.27, 0.51, 0.73, 0.91, 1] : undefined,
            ease: "easeInOut" as const,
          }
      : characterState.motion === "dance"
        ? {
            duration: 5.2,
            times: [0, 0.1, 0.22, 0.36, 0.5, 0.64, 0.8, 1],
            ease: "easeInOut" as const,
          }
      : characterState.motion === "jump"
        ? { duration: 0.76, times: [0, 0.46, 1], ease: "easeOut" as const }
        : visualMode === "idle" && curious
          ? { duration: 0.32, ease: "easeOut" as const }
        : {
            duration: visualMode === "idle" ? 9.2 : 3.5,
            repeat: Infinity,
            ease: "easeInOut" as const,
          };

  if (!visible) return null;

  return (
    <div
      ref={elRef}
      className={cn(
        "sag-pet-shell group/pet fixed z-40 block select-none",
        ambient ? "bottom-16 right-7" : "bottom-24 left-5",
      )}
      data-facing={characterState.facing}
      data-collapsed={collapsed ? "true" : "false"}
      data-pet-mode={appMode}
      style={style}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
      onPointerEnter={showCommands}
      onPointerLeave={scheduleCommandClose}
    >
      <AnimatePresence>
        {!ambient && !open && statusText && (
          <motion.button
            type="button"
            initial={{ opacity: 0, scale: 0.96 }}
            animate={
              motionReduced
                ? { opacity: 1, scale: 1 }
                : { ...visualAnimate, opacity: 1, scale: 1 }
            }
            exit={{
              opacity: 0,
              scale: 0.97,
              transition: { duration: 0.16, ease: "easeOut" },
            }}
            transition={visualTransition}
            onPointerDown={(event) => event.stopPropagation()}
            onClick={() =>
              characterState.speech?.threadId &&
              router.push(`/chat/${characterState.speech.threadId}`)
            }
            className={cn(
              "absolute flex w-60 max-w-[calc(100vw-2rem)] items-center gap-2 rounded-lg border bg-popover px-3 py-2 text-left shadow-lift",
              statusPanelVertical,
              panelSide,
            )}
            data-pet-status="true"
            aria-live="polite"
          >
            {isBusy ? (
              <Loader2 className="size-3.5 shrink-0 animate-spin text-muted-foreground" />
            ) : characterState.speech?.tone === "success" ? (
              <Check className="size-3.5 shrink-0 text-success" />
            ) : characterState.speech?.tone === "error" ? (
              <TriangleAlert className="size-3.5 shrink-0 text-destructive" />
            ) : (
              <Sparkles className="size-3.5 shrink-0 text-muted-foreground" />
            )}
            <span className="min-w-0 flex-1">
              <span className="block truncate text-xs font-medium">{statusText}</span>
              {statusThread && (
                <span className="mt-0.5 block truncate text-[11px] text-muted-foreground">
                  {statusThread.title}
                </span>
              )}
            </span>
          </motion.button>
        )}


        {!ambient && appMode === "explore" && open && (
          <PetMiniWorkspace
            character={character}
            panelClassName={cn(panelVertical, panelSide)}
            alignRight={alignRight}
            panelAbove={panelAbove}
            panelView={miniView}
            onPanelViewChange={setMiniView}
            onClose={() => {
              setTransientDetailPreview(false);
              setOpen(false);
            }}
          />
        )}

        {!ambient && petOverlay === "actions" && (
          <motion.div
            initial={{ opacity: 0, scale: 0.94, y: 6 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.96, y: 4 }}
            transition={{ type: "spring", stiffness: 380, damping: 28 }}
            className={cn(
              "absolute w-72 max-w-[calc(100vw-2rem)] overflow-hidden rounded-lg border bg-popover/96 shadow-2xl backdrop-blur-xl",
              panelVertical,
              panelSide,
            )}
            onPointerDown={(event) => event.stopPropagation()}
          >
            <div className="flex h-11 items-center border-b px-3">
              <Hand className="size-3.5 text-muted-foreground" />
              <span className="ml-2 flex-1 text-sm font-medium">{t("actions.title")}</span>
              <button
                type="button"
                onClick={() => setPetOverlay("none")}
                aria-label={t("actions.collapseAria")}
                title={t("actions.collapse")}
                className="grid size-7 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
              >
                <X className="size-3.5" />
              </button>
            </div>
            <div className="grid grid-cols-2 gap-1.5 p-2.5">
              {[
                { label: t("actions.wave"), icon: Hand, run: triggerWave },
                { label: t("actions.fly"), icon: Rocket, run: triggerFlight },
                { label: t("actions.roam"), icon: Route, run: triggerRoam },
                { label: t("actions.dance"), icon: Music2, run: triggerDance },
              ].map(({ label, icon: Icon, run }) => (
                <button
                  key={label}
                  type="button"
                  disabled={!canAct}
                  onClick={() => {
                    setPetOverlay("none");
                    run();
                  }}
                  className="flex h-9 items-center gap-2 rounded-md border px-2.5 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-35"
                >
                  <Icon className="size-3.5" />
                  {label}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-1.5 border-t px-2.5 py-2">
              <span className="mr-auto text-[11px] text-muted-foreground">{t("facing.title")}</span>
              <button
                type="button"
                onClick={() => setFacing("left")}
                aria-label={t("facing.left")}
                title={t("facing.left")}
                className={cn(
                  "grid size-7 place-items-center rounded-md border text-muted-foreground hover:bg-muted hover:text-foreground",
                  characterState.facing === "left" && "bg-muted text-foreground",
                )}
              >
                <ArrowLeft className="size-3.5" />
              </button>
              <button
                type="button"
                onClick={() => setFacing("right")}
                aria-label={t("facing.right")}
                title={t("facing.right")}
                className={cn(
                  "grid size-7 place-items-center rounded-md border text-muted-foreground hover:bg-muted hover:text-foreground",
                  characterState.facing === "right" && "bg-muted text-foreground",
                )}
              >
                <ArrowRight className="size-3.5" />
              </button>
              <button
                type="button"
                onClick={resetPetPosition}
                className="ml-1 inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground"
              >
                <RotateCcw className="size-3" />
                {t("facing.reset")}
              </button>
            </div>
          </motion.div>
        )}

      </AnimatePresence>

      <motion.div
        ref={visualRef}
        animate={visualAnimate}
        transition={visualTransition}
        style={visualStyle}
        data-facing={characterState.facing}
        data-collapsed={collapsed ? "true" : "false"}
        className="sag-pet-rig relative"
      >
        {collapsed && !open && petOverlay === "none" && (
          <div
            className="sag-pet__actions sag-pet__actions--commands sag-pet__actions--collapsed"
            data-align={alignRight ? "right" : "left"}
            data-visible={appMode === "normal" ? "false" : curious ? "true" : "false"}
            data-pet-toolbar="true"
            data-visibility-policy={appMode === "normal" ? "hover-focus" : "contextual"}
            role="toolbar"
            aria-label={t("toolbar.aria")}
            onPointerEnter={showCommands}
            onPointerLeave={scheduleCommandClose}
          >
            <PetActionButton
              slot="search"
              label={t("toolbar.search")}
              onClick={() => {
                setCurious(false);
                openMiniWorkspace("search");
              }}
            >
              <Search />
            </PetActionButton>
            <PetActionButton
              slot="answer"
              label={t("toolbar.answer")}
              onClick={() => {
                setCurious(false);
                openMiniWorkspace("answer");
              }}
            >
              <MessageCircle />
            </PetActionButton>
            <PetActionButton
              slot="knowledge"
              label={t("toolbar.knowledge")}
              onClick={() => {
                setCurious(false);
                openMiniWorkspace("knowledge");
              }}
            >
              <Library />
            </PetActionButton>
            <PetActionButton
              slot="appearance"
              label={t("toolbar.settings")}
              onClick={openAssistantSettings}
            >
              <SlidersHorizontal />
            </PetActionButton>
          </div>
        )}

        {!collapsed && !open && petOverlay === "none" && (
          <div
            className={cn(
              "sag-pet__actions",
              !ambient && "sag-pet__actions--commands",
            )}
            data-align={alignRight ? "right" : "left"}
            data-visible={appMode === "normal" ? "false" : curious ? "true" : "false"}
            data-pet-toolbar="true"
            data-visibility-policy={appMode === "normal" ? "hover-focus" : "contextual"}
            role="toolbar"
            aria-label={t("toolbar.aria")}
            onPointerEnter={showCommands}
            onPointerLeave={scheduleCommandClose}
          >
            {ambient ? (
              <PetActionButton slot="wave" label={t("actions.wave")} onClick={triggerWave} disabled={!canAct}>
                <Hand />
              </PetActionButton>
            ) : (
              <>
                <PetActionButton
                  slot="search"
                  label={t("toolbar.search")}
                  onClick={() => {
                    setCurious(false);
                    setPetOverlay("none");
                    openMiniWorkspace("search");
                  }}
                >
                  <Search />
                </PetActionButton>
                <PetActionButton
                  slot="answer"
                  label={t("toolbar.answer")}
                  onClick={() => {
                    setCurious(false);
                    setPetOverlay("none");
                    openMiniWorkspace("answer");
                  }}
                >
                  <MessageCircle />
                </PetActionButton>
                <PetActionButton
                  slot="knowledge"
                  label={t("toolbar.knowledge")}
                  onClick={() => {
                    setCurious(false);
                    setPetOverlay("none");
                    openMiniWorkspace("knowledge");
                  }}
                >
                  <Library />
                </PetActionButton>
                <PetActionButton
                  slot="appearance"
                  label={t("toolbar.settings")}
                  onClick={openAssistantSettings}
                >
                  <SlidersHorizontal />
                </PetActionButton>
                <PetActionButton
                  slot="actions"
                  label={t("toolbar.actions")}
                  onClick={() => setPetOverlay("actions")}
                >
                  <WandSparkles />
                </PetActionButton>
              </>
            )}
          </div>
        )}

        {collapsed ? (
          <div
            role="button"
            tabIndex={0}
            data-pet-form-toggle="true"
            data-mode={visualMode}
            data-facing={characterState.facing}
            data-curious={curious && visualMode === "idle" ? "true" : "false"}
            data-form-transition={formTransition}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                togglePetForm();
              }
            }}
            aria-label={t("form.simpleAria", {
              name: characterState.identity.name,
              status: statusText ? t("form.statusSuffix", { status: statusText }) : "",
            })}
            title={t("form.switchToFull")}
            style={characterStyle}
            className="sag-pet-collapsed relative cursor-grab outline-none active:cursor-grabbing"
          >
            <span className="sag-pet__bubble" aria-hidden />
            <span className="sag-pet__bubble-fragments" aria-hidden>
              <i />
              <i />
              <i />
              <i />
              <i />
            </span>
            <div className="sag-pet__helmet" aria-hidden>
              <span className="sag-pet__antenna" />
              <div className="sag-pet__visor">
                <span className="sag-pet__glass" />
                <span
                  className="sag-pet__face"
                  style={visualMode === "thinking" ? undefined : petFaceStyle(renderedFace)}
                >
                  {visualMode === "thinking" ? (
                    <span className="sag-pet__signal-meter">
                      <span />
                      <span />
                      <span />
                      <span />
                    </span>
                  ) : (
                    <AnimatePresence initial={false} mode="wait">
                      <motion.span
                        key={renderedFace}
                        initial={{ opacity: 0, scale: 0.82, y: 1 }}
                        animate={{ opacity: 1, scale: 1, y: 0 }}
                        exit={{ opacity: 0, scale: 0.88, y: -1 }}
                        transition={{ duration: 0.16 }}
                        className="sag-pet__face-glyph"
                      >
                        {renderedFace}
                      </motion.span>
                    </AnimatePresence>
                  )}
                </span>
              </div>
              <span className="sag-pet__shine" />
            </div>
          </div>
        ) : (
          <div
            role="button"
            tabIndex={0}
            data-pet-form-toggle="true"
            data-mode={visualMode}
            data-wave={characterState.motion === "wave" ? "true" : "false"}
            data-facing={characterState.facing}
            data-curious={curious && !open && visualMode === "idle" ? "true" : "false"}
            data-revealing={revealing ? "true" : "false"}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                setCurious(false);
                togglePetForm();
              }
            }}
            aria-label={t("form.fullAria", {
              name: characterState.identity.name,
              status: statusText ? t("form.statusSuffix", { status: statusText }) : "",
            })}
            title={t("form.switchToSimple")}
            style={characterStyle}
            className="sag-pet-astronaut relative h-full w-full cursor-grab outline-none active:cursor-grabbing"
          >
          <div className="sag-pet__scale" aria-hidden>
            <div className="sag-pet__stage">
              <span className="sag-pet__orbit" />
              <span className="sag-pet__shadow" />
              <span className="sag-pet__celebrate">
                <Sparkles />
                <Sparkles />
                <Sparkles />
              </span>
              <div className="sag-pet__backpack">
                <span className="sag-pet__pack-lid" />
                <span className="sag-pet__found-card" />
                <span className="sag-pet__beacon" />
                <span className="sag-pet__pack-status" />
                <span className="sag-pet__knowledge">
                  <span />
                  <span />
                  <span />
                </span>
                <span className="sag-pet__thrusters">
                  <span />
                  <span />
                </span>
              </div>
              <div className="sag-pet__body">
                <span className="sag-pet__arm sag-pet__arm--left" />
                <span className="sag-pet__arm sag-pet__arm--right" />
                <span className="sag-pet__harness" />
                <span className="sag-pet__panel">
                  <span className="sag-pet__nameplate-text" style={nameplateStyle(petName)}>
                    {petName}
                  </span>
                  <span className="sag-pet__panel-light" />
                </span>
                <span className="sag-pet__leg sag-pet__leg--left" />
                <span className="sag-pet__leg sag-pet__leg--right" />
              </div>
              <div className="sag-pet__helmet">
                {visualMode === "thinking" && <span className="sag-pet__antenna" />}
                <div className="sag-pet__visor">
                  <span className="sag-pet__glass" />
                  <span
                    className="sag-pet__face"
                    style={visualMode === "thinking" ? undefined : petFaceStyle(renderedFace)}
                  >
                    {visualMode === "thinking" ? (
                      <span className="sag-pet__signal-meter">
                        <span />
                        <span />
                        <span />
                        <span />
                      </span>
                    ) : (
                      <AnimatePresence initial={false} mode="wait">
                        <motion.span
                          key={renderedFace}
                          initial={{ opacity: 0, scale: 0.82, y: 1 }}
                          animate={{ opacity: 1, scale: 1, y: 0 }}
                          exit={{ opacity: 0, scale: 0.88, y: -1 }}
                          transition={{ duration: 0.16 }}
                          className="sag-pet__face-glyph"
                        >
                          {renderedFace}
                        </motion.span>
                      </AnimatePresence>
                    )}
                  </span>
                </div>
                <span className="sag-pet__shine" />
              </div>
            </div>
          </div>
          </div>
        )}
      </motion.div>
    </div>
  );
}
