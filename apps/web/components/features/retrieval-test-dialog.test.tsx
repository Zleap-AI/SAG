/** @vitest-environment jsdom */
import * as React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import english from "@/messages/en-US.json";
import { RetrievalTestDialog } from "./retrieval-test-dialog";
const mocks = vi.hoisted(() => ({ globalSearch: vi.fn() }));
vi.mock("@/lib/api", () => ({ ApiError: class extends Error {}, api: mocks }));
vi.mock("sonner", () => ({ toast: { error: vi.fn() } }));
vi.mock("@/components/features/app-shell", () => ({ useApp: () => ({ capabilities: { search_strategies: ["vector", "multi", "multi_jev"] } }) }));
vi.mock("next-intl", () => ({ useTranslations: (namespace: string) => (key: string) => (english as unknown as Record<string, Record<string, string>>)[namespace]?.[key] ?? key }));
beforeEach(() => {
  vi.clearAllMocks(); HTMLElement.prototype.scrollIntoView = () => undefined;
  mocks.globalSearch.mockResolvedValue({ sections: [], stats: { requested_strategy: "multi_jev", effective_strategy: "multi_jev", fallback_used: true, ranking: { method: "event_score", fallback_reason: "timeout" } } });
});
it("inherits the global strategy until a strategy is explicitly selected", async () => {
  render(<RetrievalTestDialog sourceId="source-a" sourceName="A" open onOpenChange={vi.fn()} />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "question" } });
  fireEvent.click(screen.getByRole("button", { name: english.RetrievalTest.run }));
  await waitFor(() => expect(mocks.globalSearch).toHaveBeenCalledWith({ query: "question", source_ids: ["source-a"], top_k: 8 }));
  expect(screen.getByRole("status")).toHaveTextContent("coarse results");
  fireEvent.keyDown(screen.getByRole("combobox", { name: "Retrieval strategy" }), { key: "ArrowDown" });
  fireEvent.click(await screen.findByRole("option", { name: /JEV/ }));
  fireEvent.click(screen.getByRole("button", { name: english.RetrievalTest.run }));
  await waitFor(() => expect(mocks.globalSearch).toHaveBeenLastCalledWith({ query: "question", source_ids: ["source-a"], top_k: 8, strategy: "multi_jev" }));
});
