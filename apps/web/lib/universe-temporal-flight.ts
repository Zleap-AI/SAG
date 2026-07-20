/**
 * Temporal flight along a source's counting axis.
 *
 * Pure state: the scene feeds wheel samples and frame ticks, and reads back one
 * number — how deep into the past the camera sits, in world units, 0 being the
 * source's newest moment. Wheel gestures add velocity and inertia carries it;
 * button flights glide to a target; both respect the same clamps, so there is
 * exactly one notion of "where the camera is in time".
 *
 * The camera rig applies depth *deltas* (translating camera and orbit target
 * together along the axis), so flight composes with OrbitControls rotate/pan/
 * pinch instead of competing with them — no gesture classifier needed.
 */

export interface UniverseTemporalFlightState {
  /** World units along the axis. 0 = newest. */
  depth: number;
  /** World units per second toward the past. */
  velocity: number;
  /** Glide destination for impulse flights; null while free-flying. */
  targetDepth: number | null;
}

export interface UniverseTemporalFlightWheelInput {
  deltaY: number;
  deltaMode: number;
  viewportHeight: number;
  reducedMotion?: boolean;
}

/**
 * Deliberate second-stage retreat at the source entrance. Reaching depth zero
 * first restores the intact nebula; only a fresh outward wheel gesture exits
 * to the universe overview. The arm delay absorbs trackpad inertia from the
 * gesture that merely returned to the entrance.
 */
export interface UniverseSourceExitGate {
  armedAt: number | null;
  lastWheelAt: number | null;
  outwardPixels: number;
}

export interface UniverseSourceExitWheelInput extends UniverseTemporalFlightWheelInput {
  now: number;
}

export interface UniverseSourceExitWheelResult {
  gate: UniverseSourceExitGate;
  exitRequested: boolean;
}

export interface UniverseTemporalFlightStepInput {
  /** Milliseconds since the previous step. */
  elapsedMs: number;
  /** Axis length; depth clamps to [0, maxDepth]. */
  maxDepth: number;
  reducedMotion?: boolean;
}

export interface UniverseTemporalFlightStepResult {
  state: UniverseTemporalFlightState;
  /** True while the flight still needs animation frames. */
  moving: boolean;
}

export interface UniverseTemporalFlightFollowInput {
  depth: number;
  /** Depth of the newest and oldest packages in the visible window. */
  windowNearDepth: number;
  windowFarDepth: number;
  /** How close to a window edge the camera may fly before paging. */
  marginUnits: number;
  /**
   * Signed flight velocity in units/s; positive flies older. Fast flight pages
   * ahead of arrival so data lands before the camera does.
   */
  velocity?: number;
  busy: boolean;
  hasNext: boolean;
  hasPrevious: boolean;
}

export interface UniverseTemporalFlightPresence {
  scale: number;
  opacity: number;
  /**
   * Readable-card presence is a narrower camera-relative band than star
   * presence. It slides continuously by event and never limits which nodes
   * belong to the resident graph window.
   */
  card: number;
}

/** One 120px wheel notch flies roughly this share of two packages. */
export const UNIVERSE_FLIGHT_UNITS_PER_WHEEL_PIXEL = 0.9;
/** How far ahead (in seconds of current velocity) the window follow leads. */
export const UNIVERSE_FLIGHT_FOLLOW_LEAD_S = 0.5;
/**
 * Camera-relative reading band ahead of the traveller. The complete resident
 * window remains in the scene as stars and links; only the nearest few moments
 * resolve into readable cards. This is continuous depth LOD, not a second
 * count-based preview window, so every moment naturally becomes readable when
 * the camera reaches it.
 */
const PRESENCE_AHEAD_FULL_EVENTS = 1.25;
const PRESENCE_AHEAD_FAR_EVENTS = 6;
/** Atmosphere behind the camera: passed packages fade out fast. */
const PRESENCE_BEHIND_FULL_EVENTS = 0.75;
const PRESENCE_BEHIND_GONE_EVENTS = 2.5;
/** Five chronological moments form the readable core; two more begin as previews. */
const CARD_AHEAD_FULL_EVENTS = 3.15;
const CARD_AHEAD_GONE_EVENTS = 6.15;
const CARD_BEHIND_FULL_EVENTS = 0.65;
const CARD_BEHIND_GONE_EVENTS = 1.8;
const PRESENCE_FAR_SCALE = 0.5;
const PRESENCE_FAR_OPACITY = 0.25;
/**
 * Passed packages keep a faint ember instead of going black: looking back
 * shows the travelled road, and the warm event stars read as cooling embers.
 */
const PRESENCE_BEHIND_EMBER = 0.1;
/** Coasting velocity halves this often. */
export const UNIVERSE_FLIGHT_VELOCITY_HALF_LIFE_MS = 160;
/** Glides cover half their remaining distance this often. */
export const UNIVERSE_FLIGHT_GLIDE_HALF_LIFE_MS = 140;
/** Distances below this settle instantly instead of easing forever. */
export const UNIVERSE_FLIGHT_SETTLE_EPSILON = 0.5;
/** Ignore the inertia tail that delivered the camera to the nebula entrance. */
export const UNIVERSE_SOURCE_EXIT_ARM_DELAY_MS = 260;
/** Trackpad samples farther apart than this form a new deliberate gesture. */
export const UNIVERSE_SOURCE_EXIT_GESTURE_GAP_MS = 520;
/** One ordinary mouse notch, or a short trackpad pull, exits the source. */
export const UNIVERSE_SOURCE_EXIT_WHEEL_PX = 72;
/** Velocities below this stop instead of easing forever. */
const VELOCITY_REST_EPSILON = 2;
/** Frames longer than this (tab switches) step as if one frame passed. */
const MAX_STEP_MS = 64;
const WHEEL_LINE_PX = 16;

function finite(value: number, fallback = 0) {
  return Number.isFinite(value) ? value : fallback;
}

export function createUniverseTemporalFlightState(
  depth = 0,
): UniverseTemporalFlightState {
  return { depth: Math.max(0, finite(depth)), velocity: 0, targetDepth: null };
}

function normalizedWheelPixels(input: UniverseTemporalFlightWheelInput) {
  const delta = finite(input.deltaY);
  if (input.deltaMode === 1) return delta * WHEEL_LINE_PX;
  if (input.deltaMode === 2) {
    return delta * Math.max(1, finite(input.viewportHeight, 800));
  }
  return delta;
}

export function createUniverseSourceExitGate(
  armedAt: number | null = null,
): UniverseSourceExitGate {
  return { armedAt, lastWheelAt: null, outwardPixels: 0 };
}

export function armUniverseSourceExitGate(now: number): UniverseSourceExitGate {
  return createUniverseSourceExitGate(Math.max(0, finite(now)));
}

/**
 * Consumes wheel samples only while the camera is already at the source
 * entrance. Inward motion cancels the exit intent; outward motion must happen
 * after the entry has visibly settled and cross a small gesture threshold.
 */
export function advanceUniverseSourceExitGate(
  gate: UniverseSourceExitGate,
  input: UniverseSourceExitWheelInput,
): UniverseSourceExitWheelResult {
  const now = Math.max(0, finite(input.now));
  const outwardPixels = normalizedWheelPixels(input);
  if (outwardPixels <= 0) {
    return { gate: createUniverseSourceExitGate(), exitRequested: false };
  }
  if (gate.armedAt === null) {
    return { gate: armUniverseSourceExitGate(now), exitRequested: false };
  }
  if (now - gate.armedAt < UNIVERSE_SOURCE_EXIT_ARM_DELAY_MS) {
    return {
      gate: { ...gate, lastWheelAt: now, outwardPixels: 0 },
      exitRequested: false,
    };
  }
  const continuingGesture = gate.lastWheelAt !== null
    && now - gate.lastWheelAt <= UNIVERSE_SOURCE_EXIT_GESTURE_GAP_MS;
  const accumulated = (continuingGesture ? gate.outwardPixels : 0)
    + Math.abs(outwardPixels);
  if (accumulated < UNIVERSE_SOURCE_EXIT_WHEEL_PX) {
    return {
      gate: { ...gate, lastWheelAt: now, outwardPixels: accumulated },
      exitRequested: false,
    };
  }
  return {
    gate: createUniverseSourceExitGate(),
    exitRequested: true,
  };
}

/**
 * A wheel sample becomes a velocity impulse sized so its coasted distance is
 * the gesture's travel; reduced motion applies the travel directly instead.
 * Scrolling up (negative deltaY) flies deeper — the same hand motion that
 * zooms in everywhere else pulls the corridor's depths toward you.
 */
export function applyUniverseTemporalFlightWheel(
  state: UniverseTemporalFlightState,
  input: UniverseTemporalFlightWheelInput,
): UniverseTemporalFlightState {
  const travel = -normalizedWheelPixels(input) * UNIVERSE_FLIGHT_UNITS_PER_WHEEL_PIXEL;
  if (travel === 0) return state;
  if (input.reducedMotion) {
    return {
      depth: Math.max(0, state.depth + travel),
      velocity: 0,
      targetDepth: null,
    };
  }
  // Coasted distance of v0 under exponential decay is v0 × halfLife / ln2.
  const impulse = travel * (Math.LN2 * 1000) / UNIVERSE_FLIGHT_VELOCITY_HALF_LIFE_MS;
  return {
    depth: state.depth,
    velocity: state.velocity + impulse,
    // A live gesture overrides any glide in progress.
    targetDepth: null,
  };
}

/** Button flights glide; the step clamps the destination to the axis. */
export function flyUniverseTemporalFlightTo(
  state: UniverseTemporalFlightState,
  targetDepth: number,
): UniverseTemporalFlightState {
  return {
    depth: state.depth,
    velocity: 0,
    targetDepth: Math.max(0, finite(targetDepth)),
  };
}

/** Grabbing the scene brakes: a deliberate drag owns the camera immediately. */
export function brakeUniverseTemporalFlight(
  state: UniverseTemporalFlightState,
): UniverseTemporalFlightState {
  if (state.velocity === 0 && state.targetDepth === null) return state;
  return { depth: state.depth, velocity: 0, targetDepth: null };
}

export function stepUniverseTemporalFlight(
  state: UniverseTemporalFlightState,
  input: UniverseTemporalFlightStepInput,
): UniverseTemporalFlightStepResult {
  const elapsed = Math.min(MAX_STEP_MS, Math.max(0, finite(input.elapsedMs)));
  const maxDepth = Math.max(0, finite(input.maxDepth));
  let { depth, velocity, targetDepth } = state;

  if (targetDepth !== null) {
    const target = Math.min(maxDepth, targetDepth);
    const remaining = target - depth;
    if (input.reducedMotion || Math.abs(remaining) <= UNIVERSE_FLIGHT_SETTLE_EPSILON) {
      depth = target;
      targetDepth = null;
    } else {
      depth += remaining
        * (1 - Math.exp(-(Math.LN2 * elapsed) / UNIVERSE_FLIGHT_GLIDE_HALF_LIFE_MS));
    }
  } else if (velocity !== 0) {
    depth += velocity * (elapsed / 1000);
    velocity *= Math.exp(-(Math.LN2 * elapsed) / UNIVERSE_FLIGHT_VELOCITY_HALF_LIFE_MS);
    if (Math.abs(velocity) < VELOCITY_REST_EPSILON) velocity = 0;
  }

  // The axis ends are walls, not springs: hitting one stops the flight.
  if (depth <= 0) {
    depth = 0;
    if (velocity < 0) velocity = 0;
    if (targetDepth !== null && targetDepth <= 0) targetDepth = null;
  } else if (depth >= maxDepth) {
    depth = maxDepth;
    if (velocity > 0) velocity = 0;
    if (targetDepth !== null && targetDepth >= maxDepth) targetDepth = null;
  }

  const next = depth === state.depth
    && velocity === state.velocity
    && targetDepth === state.targetDepth
    ? state
    : { depth, velocity, targetDepth };
  return { state: next, moving: velocity !== 0 || targetDepth !== null };
}

/**
 * Decides whether the visible window must page to keep the camera inside it.
 * "next" pages older (deeper), "previous" pages newer, matching the buttons.
 */
export function planUniverseTemporalFlightFollow(
  input: UniverseTemporalFlightFollowInput,
): "next" | "previous" | null {
  if (input.busy) return null;
  const span = Math.max(0, input.windowFarDepth - input.windowNearDepth);
  // A margin wider than a third of the window would let both edges trigger.
  const margin = Math.max(0, Math.min(finite(input.marginUnits), span / 3));
  // The lead is velocity-gated, so only the edge being flown toward moves its
  // threshold: at rest both leads vanish and the static hysteresis holds.
  const lead = finite(input.velocity ?? 0) * UNIVERSE_FLIGHT_FOLLOW_LEAD_S;
  const forwardLead = Math.max(0, lead);
  const backwardLead = Math.max(0, -lead);
  if (
    input.hasNext
    && input.depth > input.windowFarDepth - margin - forwardLead
  ) return "next";
  if (
    input.hasPrevious
    && input.depth < input.windowNearDepth - margin + backwardLead
  ) {
    return "previous";
  }
  return null;
}

function easedRange(value: number, from: number, to: number) {
  const t = Math.max(0, Math.min(1, (value - from) / (to - from)));
  return t * t * (3 - 2 * t);
}

/**
 * Camera-relative presence of a package on the axis: how large and how opaque
 * it renders given its depth distance from the camera, in world units
 * (positive = ahead of the camera, deeper into the past).
 *
 * Whatever the camera reaches is fully present — this replaces any static
 * age-based dimming, which under a moving camera would leave a reached package
 * forever small and dark. Ahead, atmospheric perspective thins packages toward
 * a floor (still visible: the corridor keeps promising more). Behind, passed
 * packages fade out quickly so the view is always about what is being reached.
 */
export function universeTemporalFlightPresence(
  deltaUnits: number,
  unitsPerEvent: number,
): UniverseTemporalFlightPresence {
  const unit = Math.max(1, finite(unitsPerEvent, 1));
  const events = finite(deltaUnits) / unit;
  if (events < 0) {
    const kept = 1 - easedRange(
      -events,
      PRESENCE_BEHIND_FULL_EVENTS,
      PRESENCE_BEHIND_GONE_EVENTS,
    );
    const card = 1 - easedRange(
      -events,
      CARD_BEHIND_FULL_EVENTS,
      CARD_BEHIND_GONE_EVENTS,
    );
    return {
      scale: 1,
      opacity: PRESENCE_BEHIND_EMBER + (1 - PRESENCE_BEHIND_EMBER) * kept,
      card,
    };
  }
  const fade = easedRange(
    events,
    PRESENCE_AHEAD_FULL_EVENTS,
    PRESENCE_AHEAD_FAR_EVENTS,
  );
  const card = 1 - easedRange(
    events,
    CARD_AHEAD_FULL_EVENTS,
    CARD_AHEAD_GONE_EVENTS,
  );
  return {
    scale: 1 - (1 - PRESENCE_FAR_SCALE) * fade,
    opacity: 1 - (1 - PRESENCE_FAR_OPACITY) * fade,
    card,
  };
}
