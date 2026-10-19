/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { beforeEach, afterEach, expect, it, vi } from "vitest";
import english from "@/messages/en-US.json";
import { KnowledgeConfigForm } from "./knowledge-config-form";

const mocks = vi.hoisted(() => ({ getModelConfig: vi.fn(), saveModelConfig: vi.fn(), testJevModelConfig: vi.fn(), refreshCapabilities: vi.fn(), error: vi.fn() }));
const translations = new Map<string, (key: string) => string>();
vi.mock("next-intl", () => ({ useTranslations: (namespace: string) => {
  if (!translations.has(namespace)) translations.set(namespace, key => String((english as unknown as Record<string, Record<string, unknown>>)[namespace]?.[key] ?? key));
  return translations.get(namespace);
} }));
let capabilities: Record<string, unknown>;
vi.mock("@/components/features/app-shell", () => ({ useApp: () => ({ capabilities, refreshCapabilities: mocks.refreshCapabilities }) }));
vi.mock("@/lib/api", () => ({ ApiError: class extends Error {}, api: mocks }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: mocks.error } }));

const original = { document_chunk_max_tokens: 1000, document_chunk_mode: "standard", document_extract_concurrency: 30,
  search_strategy: "vector", search_top_k: 8, sag_language: "zh", jev_endpoint: null,
  jev_model: "jev-latest", jev_api_key_set: false, jev_ranking_timeout_seconds: 30, jev_failure_policy: "error" };
let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.clearAllMocks();
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  HTMLElement.prototype.scrollIntoView = () => undefined;
  capabilities = { search_strategies: ["vector", "multi"], search_strategies_disabled: { multi_jev: { reason: "jev_connection_not_configured", message: "Configure JEV first" } } };
  mocks.getModelConfig.mockResolvedValue(original);
  mocks.saveModelConfig.mockImplementation(async patch => ({ config: { ...original, ...patch, jev_api_key_set: Boolean(patch.jev_api_key) } }));
  mocks.testJevModelConfig.mockResolvedValue({ ok: true, message: "Connected" });
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });
async function mount() { await act(async () => root.render(<KnowledgeConfigForm />)); }
function input(id: string) { const node = container.querySelector<HTMLInputElement>(id); expect(node).not.toBeNull(); return node!; }
async function edit(id: string, value: string) { await act(async () => {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input(id), value);
  input(id).dispatchEvent(new Event("input", { bubbles: true }));
}); }
function button(text: string) { return [...container.querySelectorAll<HTMLButtonElement>("button")].find(node => node.textContent === text)!; }
async function select(id: string, text: string) {
  await act(async () => container.querySelector(id)!.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })));
  const node = [...document.querySelectorAll<HTMLElement>('[role="option"]')].find(option => option.textContent?.includes(text));
  expect(node).toBeDefined(); await act(async () => node!.click());
}
const jevTest = () => button("Test JEV connection");
const save = () => button(english.KnowledgeConfig.save);

it("shows preset drafts without saving a connection or enabling JEV", async () => {
  await mount();
  expect(input("#kb-jev-endpoint").value).toBe("https://api.302.ai/typesafeai/v1/systemone");
  expect(jevTest()).toBeDisabled();
  await act(async () => save().click());
  expect(mocks.saveModelConfig.mock.calls[0][0]).not.toHaveProperty("jev_endpoint");
  expect(mocks.saveModelConfig.mock.calls[0][0].search_strategy).not.toBe("multi_jev");
});

it("tests the unsaved independent key and saves the connection while keeping vector", async () => {
  await mount(); await edit("#kb-jev-api-key", "  draft-secret  ");
  await act(async () => jevTest().click());
  expect(mocks.testJevModelConfig).toHaveBeenCalledWith({ jev_endpoint: "https://api.302.ai/typesafeai/v1/systemone", jev_model: "jev-latest", jev_api_key: "draft-secret", jev_ranking_timeout_seconds: 30, jev_failure_policy: "error" });
  expect(mocks.saveModelConfig).not.toHaveBeenCalled();
  expect(container.textContent).not.toContain("draft-secret");
  await act(async () => save().click());
  expect(mocks.saveModelConfig.mock.calls[0][0]).toMatchObject({ jev_api_key: "draft-secret" });
  expect(mocks.saveModelConfig.mock.calls[0][0].search_strategy).not.toBe("multi_jev");
  expect(input("#kb-jev-api-key").value).toBe("");
});

it("retains a saved key only at the same endpoint and clears a key draft on provider switch", async () => {
  mocks.getModelConfig.mockResolvedValue({ ...original, jev_endpoint: "https://api.302.ai/typesafeai/v1/systemone", jev_api_key_set: true });
  await mount(); await act(async () => jevTest().click());
  expect(mocks.testJevModelConfig.mock.calls[0][0]).not.toHaveProperty("jev_api_key");
  await edit("#kb-jev-api-key", "new-secret");
  await select("#kb-jev-provider", "TypeSafe");
  expect(input("#kb-jev-api-key").value).toBe("");
  expect(jevTest()).toBeDisabled();
  expect(input("#kb-jev-api-key").placeholder).not.toContain("keep");
});

it("requires a key to enable JEV and does not let old backends enable it", async () => {
  await mount();
  await edit("#kb-jev-api-key", "independent-secret");
  await select("#kb-search-strategy", "JEV");
  await act(async () => save().click());
  expect(mocks.saveModelConfig.mock.calls[0][0]).toMatchObject({ search_strategy: "multi_jev", jev_api_key: "independent-secret" });
  capabilities = {};
  await act(async () => root.render(<KnowledgeConfigForm />));
  expect(jevTest()).toBeDisabled();
});

it("permits switching a broken saved JEV configuration back to vector", async () => {
  mocks.getModelConfig.mockResolvedValue({ ...original, search_strategy: "multi_jev", jev_endpoint: "invalid", jev_api_key_set: false });
  await mount(); await select("#kb-search-strategy", english.SearchStrategies.vectorLabel);
  expect(save()).toBeEnabled(); await act(async () => save().click());
  expect(mocks.saveModelConfig.mock.calls[0][0]).toMatchObject({ search_strategy: "vector" });
  expect(mocks.saveModelConfig.mock.calls[0][0]).not.toHaveProperty("jev_endpoint");
});

it("rejects credentials or fragments in a custom endpoint and reports test failure without exposing a key", async () => {
  await mount(); await select("#kb-jev-provider", "Custom");
  await edit("#kb-jev-api-key", "private-key");
  await edit("#kb-jev-endpoint", "https://user:pass@example.com/score");
  expect(jevTest()).toBeDisabled();
  await edit("#kb-jev-endpoint", "https://example.com/score#fragment");
  expect(jevTest()).toBeDisabled();
  await edit("#kb-jev-endpoint", "https://example.com/score");
  await edit("#kb-jev-api-key", "private-key");
  mocks.testJevModelConfig.mockRejectedValue(new Error("network failure private-key"));
  await act(async () => jevTest().click());
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("JEV connection test failed");
  expect(container.textContent).not.toContain("private-key");
});
