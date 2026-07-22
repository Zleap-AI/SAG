import { describe, expect, it } from "vitest";

import {
  beginUniversePrefetchRequest,
  createUniversePrefetchRequestState,
  finishUniversePrefetchRequest,
  planUniversePrefetch,
  resetUniversePrefetchRequests,
} from "./universe-prefetch-controller";

describe("universe temporal prefetch controller", () => {
  it("targets three pages on both sides and chooses the preferred deficit", () => {
    expect(planUniversePrefetch({
      newerEvents: 20,
      olderEvents: 40,
      pageSize: 20,
      pagesPerSide: 3,
      hasNewer: true,
      hasOlder: true,
      preferredDirection: "older",
      inFlight: false,
    })).toMatchObject({
      direction: "older",
      targetEventsPerSide: 60,
      newerDeficit: 40,
      olderDeficit: 20,
    });
  });

  it("never schedules a second concurrent page", () => {
    expect(planUniversePrefetch({
      newerEvents: 0,
      olderEvents: 0,
      pageSize: 20,
      pagesPerSide: 3,
      hasNewer: true,
      hasOlder: true,
      preferredDirection: "newer",
      inFlight: true,
    }).reason).toBe("in-flight");
  });

  it("falls back to the only remaining timeline edge", () => {
    expect(planUniversePrefetch({
      newerEvents: 0,
      olderEvents: 0,
      pageSize: 20,
      pagesPerSide: 3,
      hasNewer: false,
      hasOlder: true,
      preferredDirection: "newer",
      inFlight: false,
    }).direction).toBe("older");
  });

  it("invalidates late request completions after a scope reset", () => {
    const initial = createUniversePrefetchRequestState();
    const started = beginUniversePrefetchRequest(initial, "older", "cursor-1");
    expect(started?.inFlight?.cursor).toBe("cursor-1");

    const reset = resetUniversePrefetchRequests(started ?? initial);
    const late = finishUniversePrefetchRequest(reset, initial.generation);
    expect(late).toBe(reset);
    expect(reset.generation).toBe(1);
  });
});
