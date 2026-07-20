import type { SearchEvent } from "./types";

export type SearchResultSort = "relevance" | "time";

function timestamp(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * Keep the API's relevance order by default. Time sorting uses the event's
 * own start time, puts undated events last, and preserves relevance order for
 * ties so switching modes never makes the result list jump unpredictably.
 */
export function sortSearchEvents(
  events: SearchEvent[],
  sort: SearchResultSort,
): SearchEvent[] {
  if (sort === "relevance" || events.length < 2) return events;
  return events
    .map((event, index) => ({ event, index, timestamp: timestamp(event.start_time) }))
    .sort((left, right) => {
      if (left.timestamp === null && right.timestamp === null) {
        return left.index - right.index;
      }
      if (left.timestamp === null) return 1;
      if (right.timestamp === null) return -1;
      return right.timestamp - left.timestamp || left.index - right.index;
    })
    .map(({ event }) => event);
}
