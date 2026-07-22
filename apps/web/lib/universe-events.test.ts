import { afterEach, describe, expect, it, vi } from "vitest";

import type { SearchResponse } from "./types";
import {
  activationFromSearch,
  dispatchUniverseContext,
  dispatchUniverseDetail,
  dispatchUniverseFocus,
  dispatchUniverseInteraction,
  dispatchUniversePresentation,
  dispatchUniverseResume,
  dispatchUniversePatchReset,
  dispatchUniverseSourceFocus,
  dispatchUniverseView,
  readUniverseContext,
  readUniversePresentation,
  readUniverseView,
  UNIVERSE_CONTEXT_EVENT,
  UNIVERSE_INTERACTION_EVENT,
  UNIVERSE_DETAIL_EVENT,
  UNIVERSE_FOCUS_EVENT,
  UNIVERSE_PATCH_RESET_EVENT,
  UNIVERSE_PRESENTATION_EVENT,
  UNIVERSE_RESUME_EVENT,
  UNIVERSE_SOURCE_FOCUS_EVENT,
} from "./universe-events";

afterEach(() => {
  vi.unstubAllGlobals();
});

function searchResponse(): SearchResponse {
  return {
    query: "跨来源实体",
    sections: [],
    events: [
      {
        id: "event-a",
        source_id: "source-a",
        source_name: "来源 A",
        document_id: null,
        title: "事件 A",
        summary: "",
        category: "event",
        rank: 1,
        parent_id: null,
        chunk_id: null,
        start_time: null,
        score: 1,
      },
      {
        id: "event-b",
        source_id: "source-b",
        source_name: "来源 B",
        document_id: null,
        title: "事件 B",
        summary: "",
        category: "event",
        rank: 1,
        parent_id: null,
        chunk_id: null,
        start_time: null,
        score: 0.9,
      },
    ],
    entities: [
      { id: "entity-shared", name: "共享实体", type: "topic", description: "", heat: 2 },
    ],
    relations: ["event-a", "event-b"].map((eventId) => ({
      source_id: eventId,
      source_kind: "event" as const,
      target_id: "entity-shared",
      target_kind: "entity" as const,
      kind: "mentions" as const,
      weight: 1,
      description: "",
    })),
    source_hits: [],
    summary: "",
    exploration_id: null,
    stats: {},
  };
}

describe("universe search activation", () => {
  it("projects a shared entity into every source that supports it", () => {
    const activation = activationFromSearch(searchResponse());
    const projections = activation.nodes
      .filter((node) => node.kind === "entity" && node.id === "entity-shared")
      .map((node) => node.source_id)
      .sort();

    expect(projections).toEqual(["source-a", "source-b"]);
    expect(activation.origin).toBe("search");
    expect(activation.relations.map((relation) => relation.source_id).sort()).toEqual([
      "source-a",
      "source-b",
    ]);
  });
});

describe("universe view state", () => {
  it("publishes accumulation presentation independently from browse view state", () => {
    const target = new EventTarget();
    vi.stubGlobal("window", target);
    const modes: unknown[] = [];
    target.addEventListener(UNIVERSE_PRESENTATION_EVENT, (event) => {
      modes.push((event as CustomEvent).detail);
    });

    dispatchUniversePresentation("accumulation");

    expect(readUniversePresentation()).toBe("accumulation");
    expect(modes).toEqual(["accumulation"]);
    expect(readUniverseView()).toEqual({
      mode: "overview",
      source_id: null,
      progress: 0,
    });

    dispatchUniversePresentation("exploration");
  });

  it("keeps the camera-derived progress bounded and clears source identity in overview", () => {
    dispatchUniverseView({ mode: "detail", source_id: "source-a", progress: 1.4 });
    expect(readUniverseView()).toEqual({
      mode: "detail",
      source_id: "source-a",
      progress: 1,
    });

    dispatchUniverseView({ mode: "overview", source_id: "source-a", progress: -0.2 });
    expect(readUniverseView()).toEqual({
      mode: "overview",
      source_id: null,
      progress: 0,
    });
  });

  it("dispatches an explicit source focus when the compact workspace switches sources", () => {
    const target = new EventTarget();
    vi.stubGlobal("window", target);
    let sourceId = "";
    target.addEventListener(UNIVERSE_SOURCE_FOCUS_EVENT, (event) => {
      sourceId = (event as CustomEvent<{ source_id: string }>).detail.source_id;
    });

    dispatchUniverseSourceFocus("source-b");

    expect(sourceId).toBe("source-b");
  });

  it("signals real canvas gestures so contextual overlays can close", () => {
    const target = new EventTarget();
    vi.stubGlobal("window", target);
    let gestures = 0;
    target.addEventListener(UNIVERSE_INTERACTION_EVENT, () => {
      gestures += 1;
    });

    dispatchUniverseInteraction();

    expect(gestures).toBe(1);
  });

  it("tracks contextual workspaces and resumes the retained graph explicitly", () => {
    const target = new EventTarget();
    vi.stubGlobal("window", target);
    const contexts: unknown[] = [];
    let resumes = 0;
    target.addEventListener(UNIVERSE_CONTEXT_EVENT, (event) => {
      contexts.push((event as CustomEvent).detail);
    });
    target.addEventListener(UNIVERSE_RESUME_EVENT, () => {
      resumes += 1;
    });

    dispatchUniverseContext({ active: true, section: "answer" });
    expect(readUniverseContext()).toEqual({ active: true, section: "answer" });

    dispatchUniverseResume();

    expect(readUniverseContext()).toEqual({ active: false, section: null });
    expect(contexts).toEqual([
      { active: true, section: "answer" },
      { active: false, section: null },
    ]);
    expect(resumes).toBe(1);
  });

  it("carries a stable event sequence into contextual detail navigation", () => {
    const target = new EventTarget();
    vi.stubGlobal("window", target);
    let detail: unknown = null;
    target.addEventListener(UNIVERSE_DETAIL_EVENT, (event) => {
      detail = (event as CustomEvent).detail;
    });
    const navigation = {
      items: [
        { kind: "event" as const, id: "event-a", source_id: "source-a" },
        { kind: "event" as const, id: "event-b", source_id: "source-a" },
      ],
      index: 0,
    };

    dispatchUniverseDetail("event", "event-a", "source-a", navigation);

    expect(detail).toEqual({
      kind: "event",
      id: "event-a",
      source_id: "source-a",
      navigation,
    });
  });

  it("marks detail navigation focus as a lock request", () => {
    const target = new EventTarget();
    vi.stubGlobal("window", target);
    let detail: unknown = null;
    target.addEventListener(UNIVERSE_FOCUS_EVENT, (event) => {
      detail = (event as CustomEvent).detail;
    });

    dispatchUniverseFocus("event", "event-b", "source-a", { lock: true });

    expect(detail).toEqual({
      kind: "event",
      id: "event-b",
      source_id: "source-a",
      lock: true,
    });
  });

  it("invalidates snapshot-bound detail patches without changing graph state", () => {
    const target = new EventTarget();
    vi.stubGlobal("window", target);
    let resets = 0;
    target.addEventListener(UNIVERSE_PATCH_RESET_EVENT, () => {
      resets += 1;
    });

    dispatchUniversePatchReset();

    expect(resets).toBe(1);
  });
});
