/** @vitest-environment jsdom */
import * as React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import english from "@/messages/en-US.json";
import { SearchStrategyControl } from "./search-strategy-control";

let capabilities: Record<string, unknown>;
vi.mock("@/components/features/app-shell", () => ({ useApp: () => ({ capabilities }) }));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => (english.SearchStrategies as Record<string, string>)[key] ?? key }));
beforeEach(() => { capabilities = { search_strategies: ["vector", "multi", "multi_jev"] }; });
function open() { fireEvent.keyDown(screen.getByTestId("search-strategy"), { key: "ArrowDown" }); }
it("shows a disabled JEV mode with the backend reason", () => {
  capabilities.search_strategies_disabled = { multi_jev: { reason: "jev_connection_not_configured", message: "Configure an independent key first" } };
  const change = vi.fn(); render(<SearchStrategyControl value="vector" defaultValue="vector" onValueChange={change} />); open();
  const item = screen.getByRole("menuitem", { name: /JEV/ });
  expect(item).toHaveAttribute("data-disabled");
  expect(item).toHaveTextContent("Configure an independent key first");
  fireEvent.click(item); expect(change).not.toHaveBeenCalled();
});
it("does not enable JEV when an older backend omits capabilities", () => {
  capabilities = {}; render(<SearchStrategyControl value="vector" defaultValue="vector" onValueChange={vi.fn()} />); open();
  expect(screen.getByRole("menuitem", { name: /JEV/ })).toHaveAttribute("data-disabled");
});
it("emits the JEV strategy only after explicit selection", () => {
  const change = vi.fn(); render(<SearchStrategyControl value="vector" defaultValue="vector" onValueChange={change} />); open();
  expect(change).not.toHaveBeenCalled(); fireEvent.click(screen.getByRole("menuitem", { name: /JEV/ }));
  expect(change).toHaveBeenCalledWith("multi_jev");
});
