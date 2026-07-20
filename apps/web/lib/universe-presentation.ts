export interface UniverseDetailLatchInput {
  currentSourceId: string | null;
  currentRadiusPx: number | null;
  candidateSourceId: string | null;
  candidateRadiusPx: number | null;
  explicitSourceId?: string | null;
  enterRadiusPx: number;
  exitRadiusPx: number;
}

export interface UniverseCardMorph {
  reveal: number;
  scale: number;
}

export type UniverseNodeEmergenceKind = "event" | "entity";

export interface UniverseNodeEmergence {
  /** Gathering grains: rises first, then settles to a restrained halo. */
  grain: number;
  /** Star visibility after the source particle has condensed. */
  star: number;
  /** Whole-card visibility after the star has settled. */
  card: number;
  /** Scale of the gathering cloud that carries both halo and core. */
  cloudScale: number;
  /** Monotonic world-space star scale for symmetric forward/reverse travel. */
  starScale: number;
  /** Whole-card scale while it approaches from the distance. */
  cardScale: number;
  /** Presentation blur in CSS pixels; zero means fully resolved. */
  blur: number;
}

function clamp01(value: number) {
  return Math.max(0, Math.min(1, value));
}

function smoothstep(value: number) {
  const clamped = clamp01(value);
  return clamped * clamped * (3 - 2 * clamped);
}

function phase(progress: number, start: number, end: number) {
  return smoothstep((progress - start) / Math.max(0.001, end - start));
}

function easeOutCubic(value: number) {
  return 1 - Math.pow(1 - clamp01(value), 3);
}

/**
 * Converts one reversible journey progress into particle → star → card
 * presentation. The complete card is always one visual object: title,
 * metadata and summary share the same opacity and scale. Events lead entities
 * by a short beat, while `stagger` spreads peers without preventing the last
 * item from resolving at progress 1.
 */
export function universeNodeEmergence(
  progress: number,
  kind: UniverseNodeEmergenceKind,
  stagger = 0,
  target?: UniverseNodeEmergence,
): UniverseNodeEmergence {
  const value = progress === Number.POSITIVE_INFINITY
    ? 1
    : clamp01(Number.isFinite(progress) ? progress : 0);
  const staggerValue = clamp01(Number.isFinite(stagger) ? stagger : 0);
  const kindDelay = kind === "entity" ? 0.06 : 0;
  const staggerDelay = staggerValue * 0.14;
  const delay = kindDelay + staggerDelay;
  const grainIn = phase(value, 0.005 + delay * 0.25, 0.15 + delay * 0.6);
  const grainOut = phase(value, 0.18 + delay, 0.5 + delay);
  const star = phase(value, 0.08 + delay, 0.34 + delay);
  // Let the complete card begin while the star finishes condensing. This
  // overlap is the visual hand-off: a central point becomes a small card,
  // rather than a full card suddenly appearing after it has reached an edge.
  // The maximum kind + peer delay still resolves exactly at progress 1.
  const card = phase(value, 0.26 + delay, 0.68 + delay);
  const result = target ?? {
    grain: 0,
    star: 0,
    card: 0,
    cloudScale: 0.22,
    starScale: 0.08,
    cardScale: 0.28,
    blur: 7,
  };

  result.grain = grainIn * (1 - grainOut * 0.72);
  result.star = star;
  result.card = card;
  result.cloudScale = 0.22
    + easeOutCubic(phase(value, 0.005 + delay * 0.25, 0.46 + delay)) * 0.78;
  result.starScale = 0.08 + easeOutCubic(star) * 0.92;
  result.cardScale = 0.28 + easeOutCubic(card) * 0.72;
  result.blur = 7 * Math.pow(1 - card, 1.3);
  return result;
}

/**
 * Keeps one source in detail mode until the camera crosses the overview
 * boundary. Candidate changes caused by panning or orbiting are deliberately
 * ignored while the current source remains above that boundary.
 */
export function resolveUniverseDetailSource({
  currentSourceId,
  currentRadiusPx,
  candidateSourceId,
  candidateRadiusPx,
  explicitSourceId,
  enterRadiusPx,
  exitRadiusPx,
}: UniverseDetailLatchInput) {
  if (explicitSourceId) return explicitSourceId;

  if (
    currentSourceId
    && currentRadiusPx !== null
    && Number.isFinite(currentRadiusPx)
    && currentRadiusPx > exitRadiusPx
  ) {
    return currentSourceId;
  }

  if (
    candidateSourceId
    && candidateRadiusPx !== null
    && Number.isFinite(candidateRadiusPx)
    && candidateRadiusPx >= enterRadiusPx
  ) {
    return candidateSourceId;
  }

  return null;
}

/**
 * Converts deep zoom into coarse, monotonic loading milestones. Re-entering a
 * milestone that was already visited must not request another page.
 */
export function universeDeepLoadMilestone(
  radiusPx: number,
  deepRadiusPx: number,
  hysteresisPx: number,
) {
  if (!Number.isFinite(radiusPx) || radiusPx < deepRadiusPx) return 0;
  const step = Math.max(96, hysteresisPx * 4, deepRadiusPx * 0.34);
  return 1 + Math.floor((radiusPx - deepRadiusPx) / step);
}

/**
 * Maps projected source size to a continuous visual morph. `nearRadiusPx`
 * remains the semantic detail latch, but lands halfway through the visual
 * transition; the card reaches full size before deep-loading begins.
 */
export function universeVisualDetailProgress(
  radiusPx: number | null,
  orbitRadiusPx: number,
  nearRadiusPx: number,
  deepRadiusPx: number,
) {
  if (radiusPx === null || !Number.isFinite(radiusPx)) return 0;
  const start = Math.max(0, orbitRadiusPx);
  const near = Math.max(start + 1, nearRadiusPx);
  const deep = Math.max(near + 1, deepRadiusPx);
  const full = near + (deep - near) * 0.6;
  return smoothstep((radiusPx - start) / Math.max(1, full - start));
}

/** Controls the complete card as one object; internal fields never stage. */
export function universeCardMorph(progress: number): UniverseCardMorph {
  const value = clamp01(progress);
  return {
    reveal: smoothstep((value - 0.02) / 0.5),
    scale: 0.72 + smoothstep(value) * 0.28,
  };
}
