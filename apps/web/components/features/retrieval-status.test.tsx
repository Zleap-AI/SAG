/** @vitest-environment jsdom */
import * as React from "react";
import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { RetrievalStatus } from "./retrieval-status";
import { AgentActivityTimeline } from "./chat/agent-activity-timeline";
vi.mock("next-intl", () => ({ useTranslations: () => (key: string, values?: Record<string, unknown>) => `${key}${values?.mode ? ` ${values.mode}` : ""}${values?.count !== undefined ? ` ${values.count}` : ""}` }));

it("reports completed JEV only when the backend confirms JEV ranking", () => {
  render(<RetrievalStatus stats={{ requested_strategy: "multi_jev", effective_strategy: "multi_jev", ranking: { method: "jev", fallback_reason: null, scored_chunks: 2, query_chunk_fallback_chunks: 0 } }} />);
  expect(screen.getByRole("status")).toHaveTextContent("jevComplete 2");
});
it("does not infer completed scoring from the native method when counts are absent", () => {
  render(<RetrievalStatus stats={{ requested_strategy: "multi_jev", effective_strategy: "multi_jev", ranking: { method: "jev", fallback_reason: null } }} />);
  expect(screen.queryByText(/jevComplete/)).toBeNull();
});
it("shows no-event results as unscored vector candidates", () => {
  render(<RetrievalStatus stats={{ requested_strategy: "multi_jev", effective_strategy: "multi_jev", ranking: { method: "jev", fallback_reason: null, scored_chunks: 0, query_chunk_fallback_chunks: 3 } }} />);
  expect(screen.getByRole("status")).toHaveTextContent("unscoredCandidates 3");
  expect(screen.getByRole("status")).toHaveTextContent("actual unscored");
  expect(screen.getByRole("status")).not.toHaveTextContent("jevComplete");
  expect(screen.getByRole("status")).not.toHaveTextContent("jevFallback");
});
it("shows scored and unscored candidate counts without double-counting source stats", () => {
  render(<RetrievalStatus stats={{ requested_strategy: "multi_jev", effective_strategy: "multi_jev",
    ranking: { method: "jev", fallback_reason: null, scored_chunks: 2, query_chunk_fallback_chunks: 1 },
    source_stats: [{ ranking: { method: "jev", fallback_reason: null, scored_chunks: 2, query_chunk_fallback_chunks: 1 } }],
  }} />);
  expect(screen.getByRole("status")).toHaveTextContent("jevComplete 2");
  expect(screen.getByRole("status")).toHaveTextContent("unscoredCandidates 1");
});
it("uses source counts when no aggregate ranking is provided", () => {
  render(<RetrievalStatus stats={{ requested_strategy: "multi_jev", source_stats: [
    { ranking: { method: "jev", fallback_reason: null, scored_chunks: 1, query_chunk_fallback_chunks: 0 } },
    { ranking: { method: "jev", fallback_reason: null, scored_chunks: 0, query_chunk_fallback_chunks: 2 } },
  ] }} />);
  expect(screen.getByRole("status")).toHaveTextContent("jevComplete 1");
  expect(screen.getByRole("status")).toHaveTextContent("unscoredCandidates 2");
});
it("reports a source fallback and its cause without claiming complete JEV", () => {
  render(<RetrievalStatus stats={{ requested_strategy: "multi_jev", effective_strategy: "multi_jev", source_stats: [{ source_config_id: "a", ranking: { method: "jev", fallback_reason: "timeout" } }] }} />);
  expect(screen.getByRole("status")).toHaveTextContent("jevFallback");
  expect(screen.getByRole("status")).toHaveTextContent("fallbackTimeout");
  expect(screen.getByRole("status")).not.toHaveTextContent("jevComplete");
});
it("does not claim JEV success from a requested strategy alone", () => {
  render(<RetrievalStatus requestedStrategy="multi_jev" stats={{}} />);
  expect(screen.queryByText(/jevComplete/)).toBeNull();
});
it("keeps ordinary vector results free of extra status", () => {
  render(<RetrievalStatus stats={{ requested_strategy: "vector", effective_strategy: "vector" }} />);
  expect(screen.queryByRole("status")).toBeNull();
});
it("shows retrieval fallback even while the agent tool timeline is collapsed", () => {
  render(<AgentActivityTimeline collapsed steps={[{ kind: "tool", step: 1, name: "search_context", details: { retrieval: { requested_strategy: "multi_jev", fallback_used: true } } }]} />);
  expect(screen.getByRole("status")).toHaveTextContent("jevFallback");
});
it("shows unscored vector candidates in the collapsed agent timeline", () => {
  render(<AgentActivityTimeline collapsed steps={[{ kind: "tool", step: 1, name: "search_context", details: { retrieval: {
    requested_strategy: "multi_jev", effective_strategy: "multi_jev", ranking: { method: "jev", fallback_reason: null, scored_chunks: 0, query_chunk_fallback_chunks: 1 },
  } } }]} />);
  expect(screen.getByRole("status")).toHaveTextContent("unscoredCandidates 1");
  expect(screen.getByRole("status")).not.toHaveTextContent("jevComplete");
});
