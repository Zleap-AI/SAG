import { describe, expect, it } from "vitest";

import {
  createUniverseSessionState,
  reduceUniverseSession,
} from "./universe-session-state";

describe("knowledge universe session state", () => {
  it("moves explicitly from home to exploration to accumulation and back", () => {
    const exploration = reduceUniverseSession(
      createUniverseSessionState(),
      { type: "ENTER_EXPLORATION", sourceId: "history" },
    );
    const accumulation = reduceUniverseSession(exploration, {
      type: "ENTER_ACCUMULATION",
      origin: "assistant",
    });
    const appended = reduceUniverseSession(accumulation, {
      type: "APPEND_EVIDENCE",
      origin: "assistant",
      addedEvents: 4,
    });
    const restored = reduceUniverseSession(appended, {
      type: "RETURN_TO_EXPLORATION",
    });

    expect(accumulation).toMatchObject({
      mode: "accumulation",
      sourceId: "history",
      explorationSnapshotAvailable: true,
    });
    expect(appended.evidenceBatchCount).toBe(1);
    expect(restored).toMatchObject({
      mode: "exploration",
      sourceId: "history",
      explorationSnapshotAvailable: false,
    });
  });

  it("returns to home when accumulation has no exploration snapshot", () => {
    const accumulation = reduceUniverseSession(
      createUniverseSessionState(),
      {
        type: "ENTER_ACCUMULATION",
        origin: "search",
        snapshotAvailable: false,
      },
    );
    expect(reduceUniverseSession(accumulation, {
      type: "RETURN_TO_EXPLORATION",
    }).mode).toBe("home");
  });

  it("keeps the workspace when an evidence batch contains only duplicates", () => {
    const accumulation = reduceUniverseSession(
      createUniverseSessionState(),
      {
        type: "ENTER_ACCUMULATION",
        origin: "assistant",
        snapshotAvailable: false,
      },
    );
    const duplicate = reduceUniverseSession(accumulation, {
      type: "APPEND_EVIDENCE",
      origin: "assistant",
      addedEvents: 0,
    });
    expect(duplicate.evidenceBatchCount).toBe(0);
    expect(duplicate.revision).toBe(accumulation.revision);
  });

  it("toggles the same card and clears its detail in one transition", () => {
    const selected = reduceUniverseSession(
      createUniverseSessionState(),
      { type: "OPEN_DETAIL", key: "event:1" },
    );
    const cleared = reduceUniverseSession(selected, {
      type: "TOGGLE_LOCK",
      key: "event:1",
    });
    expect(cleared).toMatchObject({
      selectedKey: null,
      lockedKey: null,
      detailKey: null,
    });
  });

  it("blank-focus clearing never changes the workspace mode", () => {
    const exploration = reduceUniverseSession(
      createUniverseSessionState(),
      { type: "ENTER_EXPLORATION", sourceId: "history" },
    );
    const locked = reduceUniverseSession(exploration, {
      type: "LOCK",
      key: "event:1",
    });
    const cleared = reduceUniverseSession(locked, { type: "CLEAR_FOCUS" });
    expect(cleared.mode).toBe("exploration");
    expect(cleared.sourceId).toBe("history");
  });
});
