/**
 * Playback order is presentation state only. The resident timeline remains in
 * its canonical newest-to-oldest order at all times.
 *
 * `chronological` deliberately starts at the current position and moves toward
 * newer events. It never seeks to, or pretends that the client owns, the true
 * oldest event.
 */
export type UniverseTimelinePlaybackOrder = "reverse" | "chronological";

export type UniverseTimelinePlaybackDirection = "older" | "newer";

export type UniverseTimelinePlaybackSceneDirection = "next" | "previous";

export interface UniverseTimelinePlaybackBounds {
  hasOlder: boolean;
  hasNewer: boolean;
}

export const UNIVERSE_TIMELINE_AUTOPLAY_DELAY_MS = 3_200;

export type UniverseTimelinePlaybackPauseReason =
  | "disabled"
  | "document-hidden"
  | "reduced-motion"
  | "locked"
  | "loading"
  | "transitioning"
  | "boundary";

export interface UniverseTimelinePlaybackPlanInput
  extends UniverseTimelinePlaybackBounds {
  enabled: boolean;
  order: UniverseTimelinePlaybackOrder;
  documentHidden: boolean;
  reducedMotion: boolean;
  locked: boolean;
  loading: boolean;
  transitioning: boolean;
}

export type UniverseTimelinePlaybackPlan =
  | {
      status: "ready";
      reason: null;
      direction: UniverseTimelinePlaybackDirection;
      sceneDirection: UniverseTimelinePlaybackSceneDirection;
      delayMs: typeof UNIVERSE_TIMELINE_AUTOPLAY_DELAY_MS;
    }
  | {
      status: "paused";
      reason: UniverseTimelinePlaybackPauseReason;
      direction: UniverseTimelinePlaybackDirection;
      sceneDirection: UniverseTimelinePlaybackSceneDirection;
      delayMs: null;
    };

/** Returns the only valid direction for the selected presentation order. */
export function universeTimelinePlaybackDirection(
  order: UniverseTimelinePlaybackOrder,
): UniverseTimelinePlaybackDirection {
  return order === "reverse" ? "older" : "newer";
}

/** Maps data-axis direction onto the existing scene pager contract. */
export function universeTimelinePlaybackSceneDirection(
  direction: UniverseTimelinePlaybackDirection,
): UniverseTimelinePlaybackSceneDirection {
  return direction === "older" ? "next" : "previous";
}

export function canAdvanceUniverseTimelinePlayback(
  direction: UniverseTimelinePlaybackDirection,
  bounds: UniverseTimelinePlaybackBounds,
) {
  return direction === "older" ? bounds.hasOlder : bounds.hasNewer;
}

export function toggleUniverseTimelinePlaybackOrder(
  order: UniverseTimelinePlaybackOrder,
): UniverseTimelinePlaybackOrder {
  return order === "reverse" ? "chronological" : "reverse";
}

/**
 * Produces one timer decision without reading browser or React state. A caller
 * should discard its existing timer whenever the returned plan is paused and
 * re-plan after the relevant state changes.
 */
export function planUniverseTimelinePlayback(
  input: UniverseTimelinePlaybackPlanInput,
): UniverseTimelinePlaybackPlan {
  const direction = universeTimelinePlaybackDirection(input.order);
  const sceneDirection = universeTimelinePlaybackSceneDirection(direction);
  const pause = (
    reason: UniverseTimelinePlaybackPauseReason,
  ): UniverseTimelinePlaybackPlan => ({
    status: "paused",
    reason,
    direction,
    sceneDirection,
    delayMs: null,
  });

  if (!input.enabled) return pause("disabled");
  if (input.documentHidden) return pause("document-hidden");
  if (input.reducedMotion) return pause("reduced-motion");
  if (input.locked) return pause("locked");
  if (input.loading) return pause("loading");
  if (input.transitioning) return pause("transitioning");
  if (!canAdvanceUniverseTimelinePlayback(direction, input)) {
    return pause("boundary");
  }

  return {
    status: "ready",
    reason: null,
    direction,
    sceneDirection,
    delayMs: UNIVERSE_TIMELINE_AUTOPLAY_DELAY_MS,
  };
}
