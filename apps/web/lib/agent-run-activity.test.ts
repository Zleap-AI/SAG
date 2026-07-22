import { describe, expect, it } from "vitest";

import type { AgentEvent, AgentEventType } from "./sse";
import {
  citationsFromArtifacts,
  mergeCitations,
  reduceAgentRunSteps,
  toolArgumentsPreview,
  toolScope,
  type LiveStep,
} from "./agent-run-activity";

function event(
  type: AgentEventType,
  payload: Record<string, unknown> = {},
  turn = 1,
  sequence = 1,
): AgentEvent {
  return {
    version: 1,
    type,
    run_id: "run-1",
    sequence,
    timestamp: "2026-07-12T00:00:00Z",
    turn,
    payload,
  };
}

function reduce(events: AgentEvent[]): LiveStep[] {
  return events.reduce(
    (steps, item, index) => reduceAgentRunSteps(steps, item, 1_000 + index * 100),
    [] as LiveStep[],
  );
}

describe("agent run activity", () => {
  it("represents a direct answer without inventing a knowledge lookup", () => {
    const steps = reduce([
      event("turn.started"),
      event("message.delta", { role: "assistant", delta: "你好" }, 1, 2),
      event(
        "message.completed",
        {
          message: { role: "assistant" },
          duration_ms: 240,
          has_tool_calls: false,
        },
        1,
        3,
      ),
    ]);

    expect(steps).toHaveLength(1);
    expect(steps[0]).toMatchObject({
      id: "answer-1",
      kind: "answer",
      status: "done",
      ms: 240,
    });
    expect(steps[0].name).toBeUndefined();
  });

  it("keeps the server tool label, arguments, details, and duration", () => {
    const steps = reduce([
      event("turn.started"),
      event(
        "message.completed",
        { message: { role: "assistant" }, duration_ms: 80, has_tool_calls: true },
        1,
        2,
      ),
      event(
        "tool.started",
        {
          tool_call_id: "time-1",
          name: "get_time",
          label: "查询时间",
          arguments: { timezone: "Asia/Shanghai" },
        },
        1,
        3,
      ),
      event(
        "tool.progress",
        { tool_call_id: "time-1", message: "读取时区", details: { count: 1 } },
        1,
        4,
      ),
      event(
        "tool.completed",
        {
          tool_call_id: "time-1",
          name: "get_time",
          duration_ms: 37,
          details: { count: 1, output_preview: "2026-07-12 08:00" },
        },
        1,
        5,
      ),
    ]);

    expect(steps).toHaveLength(2);
    expect(steps[1]).toMatchObject({
      id: "time-1",
      kind: "tool",
      name: "get_time",
      label: "查询时间",
      arguments: { timezone: "Asia/Shanghai" },
      status: "done",
      ms: 37,
      count: 1,
      details: { count: 1, output_preview: "2026-07-12 08:00" },
      progress: "读取时区",
    });
  });

  it("records observable tool failures", () => {
    const steps = reduce([
      event("tool.started", {
        tool_call_id: "remote-1",
        name: "remote_weather",
        label: "查询天气",
        arguments: { city: "上海" },
      }),
      event(
        "tool.failed",
        {
          tool_call_id: "remote-1",
          name: "remote_weather",
          label: "查询天气",
          duration_ms: 410,
          error: { message: "上游超时" },
        },
        1,
        2,
      ),
    ]);

    expect(steps[0]).toMatchObject({
      label: "查询天气",
      status: "error",
      error: "上游超时",
      ms: 410,
    });
  });

  it("keeps parallel tools active and accepts out-of-order completion", () => {
    let steps = reduceAgentRunSteps(
      [],
      event("tool.started", {
        tool_call_id: "tool-a",
        name: "alpha",
        label: "工具 A",
      }),
      1_000,
    );
    steps = reduceAgentRunSteps(
      steps,
      event(
        "tool.started",
        { tool_call_id: "tool-b", name: "beta", label: "工具 B" },
        1,
        2,
      ),
      1_010,
    );

    expect(steps.map((step) => [step.id, step.status])).toEqual([
      ["tool-a", "active"],
      ["tool-b", "active"],
    ]);

    steps = reduceAgentRunSteps(
      steps,
      event(
        "tool.completed",
        { tool_call_id: "tool-b", name: "beta", duration_ms: 20, details: {} },
        1,
        3,
      ),
      1_030,
    );
    expect(steps.find((step) => step.id === "tool-a")?.status).toBe("active");
    expect(steps.find((step) => step.id === "tool-b")?.status).toBe("done");

    steps = reduceAgentRunSteps(
      steps,
      event(
        "tool.completed",
        { tool_call_id: "tool-a", name: "alpha", duration_ms: 45, details: {} },
        1,
        4,
      ),
      1_045,
    );
    expect(steps.every((step) => step.status === "done")).toBe(true);
  });

  it("marks active work as stopped when a run is cancelled", () => {
    const steps = reduce([
      event("turn.started"),
      event("run.cancelled", { error: { message: "Run cancelled" } }, 1, 2),
    ]);
    expect(steps[0]).toMatchObject({ status: "error", error: "已停止" });
  });

  it("normalizes and merges internal and external citation artifacts", () => {
    const first = {
      n: 1,
      chunk_id: "chunk-1",
      heading: "旧标题",
      snippet: "旧内容",
      score: 0.8,
      source_id: "source-1",
      event_refs: [
        {
          id: "event-1",
          title: "真实事件",
          content: "真实事件正文",
          summary: "真实事件摘要",
          category: "发布",
        },
        { id: "invalid", title: "" },
      ],
    };
    const replacement = { ...first, heading: "新标题" };
    const second = { ...first, n: 2, chunk_id: "chunk-2" };

    expect(citationsFromArtifacts({ citations: [first, null, { n: "bad" }] })).toEqual([
      {
        ...first,
        kind: "internal",
        event_refs: [first.event_refs[0]],
      },
    ]);
    expect(mergeCitations([first], [second, replacement])).toEqual([replacement, second]);

    const external = {
      n: 1,
      kind: "external",
      url: "https://example.com/report",
      title: "行业报告",
      source: "Example Research",
      summary: "报告摘要",
      mapped: false,
      claim_level: "run",
    };
    expect(
      citationsFromArtifacts({
        citations: [external, { ...external, n: 2, url: "javascript:alert(1)" }],
      }),
    ).toEqual([
      {
        ...external,
        chunk_id: null,
        heading: "行业报告",
        snippet: "",
        score: 0,
        source_id: null,
        source_name: "Example Research",
      },
    ]);

    const normalizedExternal = citationsFromArtifacts({ citations: [external] });
    expect(mergeCitations([first], normalizedExternal)).toMatchObject([
      { n: 1, source_id: "source-1" },
      { kind: "external", n: 1, url: "https://example.com/report" },
    ]);

    expect(
      citationsFromArtifacts({
        citations: [{ ...first, event_refs: undefined, summary: "旧版片段摘要" }],
      })[0],
    ).not.toHaveProperty("summary");
  });

  it("formats bounded tool arguments", () => {
    expect(toolArgumentsPreview({ query: "现在几点", top_k: 8 })).toBe(
      "query=现在几点; top_k=8",
    );
    expect(toolArgumentsPreview({ query: "一段很长的查询文本" }, 12)).toHaveLength(13);
  });

  it("treats web search as internet scope even for legacy traces with mounted sources", () => {
    expect(
      toolScope({
        kind: "tool",
        step: 1,
        name: "web_search",
        details: {
          sources: [
            { id: "source-1", name: "西游记" },
            { id: "source-2", name: "SAG" },
          ],
        },
      }),
    ).toEqual({ kind: "internet", sources: [] });

    expect(
      toolScope({
        kind: "tool",
        step: 1,
        name: "search_context",
        details: { sources: [{ id: "source-1", name: "西游记" }] },
      }),
    ).toEqual({
      kind: "knowledge",
      sources: [{ id: "source-1", name: "西游记" }],
    });

    expect(
      toolScope({
        kind: "tool",
        step: 1,
        name: "get_time",
        details: { sources: [{ id: "source-1", name: "西游记" }] },
      }),
    ).toEqual({ kind: null, sources: [] });
  });
});
