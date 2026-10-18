/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import messages from "@/messages/en-US.json";
import { KnowledgeConfigForm } from "./knowledge-config-form";
import { api } from "@/lib/api";
import type { Capabilities } from "@/lib/types";

const state = vi.hoisted(() => {
  const capabilities: Partial<Capabilities> = {};
  return { capabilities, refreshCapabilities: vi.fn() };
});
vi.mock("@/components/features/app-shell", () => ({ useApp: () => state }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
const config = {
  document_chunk_max_tokens: 1000, document_chunk_mode: "standard", document_extract_concurrency: 30,
  search_strategy: "vector", search_top_k: 8, sag_language: "zh", jev_endpoint: null, jev_model: "jev-latest",
  jev_api_key_set: true, jev_ranking_timeout_seconds: 30, jev_failure_policy: "error",
};
const provider302Endpoint = "https://api.302.ai/typesafeai/v1/systemone";
const officialEndpoint = "https://api.typesafe.ai/v1/systemone";
let root: Root;
let container: HTMLDivElement;
let saved: Record<string, unknown> | undefined;
let tested: Record<string, unknown> | undefined;
let testedUrl: string | undefined;
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  saved = tested = undefined; testedUrl = undefined;
  state.capabilities = { vector_provider: "lancedb", search_strategies_disabled: { multi_jev: { reason: "jev_not_configured", message: "Configure JEV" } } };
  state.refreshCapabilities.mockClear();
  vi.spyOn(api, "getModelConfig").mockResolvedValue(config as never);
  let persistedConfig: Awaited<ReturnType<typeof api.getModelConfig>> | undefined;
  vi.spyOn(api, "saveModelConfig").mockImplementation(async patch => {
    saved = patch;
    persistedConfig = { ...(persistedConfig ?? await api.getModelConfig()), ...patch };
    return { config: persistedConfig, capabilities: {} } as never;
  });
  vi.stubGlobal("fetch", vi.fn(async (_url, init) => {
    testedUrl = String(_url); tested = JSON.parse(init.body);
    return new Response(JSON.stringify({ ok: true, message: "Draft connected" }), { headers: { "Content-Type": "application/json" } });
  }));
  Object.defineProperties(HTMLElement.prototype, {
    hasPointerCapture: { configurable: true, value: () => false }, releasePointerCapture: { configurable: true, value: () => undefined },
    setPointerCapture: { configurable: true, value: () => undefined }, scrollIntoView: { configurable: true, value: () => undefined },
  });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
async function mount() { await act(async () => root.render(<NextIntlClientProvider locale="en-US" messages={messages}><KnowledgeConfigForm /></NextIntlClientProvider>)); }
async function edit(id: string, value: string) {
  const input = container.querySelector<HTMLInputElement>(id)!;
  expect(input, `missing ${id}`).not.toBeNull();
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
function button(text: string) { return [...container.querySelectorAll("button")].find(item => item.textContent?.includes(text))!; }
function pointerEvent(type: string) {
  const event = new MouseEvent(type, { bubbles: true, button: 0 });
  Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: "mouse" } });
  return event;
}
async function select(id: string, label: string) {
  const trigger = container.querySelector<HTMLElement>(id);
  expect(trigger, `missing selector ${id}`).not.toBeNull();
  await act(async () => trigger!.dispatchEvent(pointerEvent("pointerdown")));
  const option = [...document.body.querySelectorAll<HTMLElement>('[role="option"]')].find(item => item.textContent?.includes(label))!;
  expect(option).toBeDefined();
  expect(option.getAttribute("aria-disabled")).not.toBe("true");
  await act(async () => {
    option.dispatchEvent(pointerEvent("pointerdown"));
    option.dispatchEvent(pointerEvent("pointerup"));
  });
}

describe("JEV knowledge settings", () => {
  it("prefills 302.AI and the stable latest model without saving or changing the selected strategy", async () => {
    await mount();
    expect(container.querySelector<HTMLInputElement>("#kb-jev-endpoint")!.value).toBe(provider302Endpoint);
    expect(container.querySelector<HTMLInputElement>("#kb-jev-model")!.value).toBe("jev-latest");
    expect(container.querySelector<HTMLInputElement>("#kb-jev-api-key")!.value).toBe("");
    expect(container.querySelector("#kb-jev-provider")?.textContent).toContain("302.AI");
    expect(container.querySelector("#kb-search-strategy")?.textContent).toContain("Fast (vector)");
    expect(container.querySelector<HTMLInputElement>("#kb-jev-endpoint")!.readOnly).toBe(true);
    expect(saved).toBeUndefined();
    expect(tested).toBeUndefined();
    expect(container.textContent).toContain("Knowledge processing language");
    expect(container.textContent).toContain("Chinese is the default for document extraction, query processing, and chat prompts");
    expect(container.textContent).toContain("Documents are not translated");
    expect(container.textContent).toContain("does not change JEV's scoring language");
    expect(container.textContent).toContain("latest stable JEV release");
  });
  it.each(["", "   "])("prefills the latest alias when the stored model is blank (%j)", async model => {
    vi.mocked(api.getModelConfig).mockResolvedValue({ ...config, jev_model: model } as never);
    await mount();
    expect(container.querySelector<HTMLInputElement>("#kb-jev-model")!.value).toBe("jev-latest");
  });
  it("keeps an existing custom endpoint and model when loading", async () => {
    vi.mocked(api.getModelConfig).mockResolvedValue({
      ...config, jev_endpoint: "https://private.invalid/rerank", jev_model: "deployed-jev-version",
    } as never);
    await mount();
    expect(container.querySelector("#kb-jev-provider")?.textContent).toContain("Custom");
    expect(container.querySelector<HTMLInputElement>("#kb-jev-endpoint")!.value).toBe("https://private.invalid/rerank");
    expect(container.querySelector<HTMLInputElement>("#kb-jev-model")!.value).toBe("deployed-jev-version");
    expect(container.querySelector<HTMLInputElement>("#kb-jev-api-key")!.value).toBe("");
    expect(saved).toBeUndefined();
  });
  it.each([
    [provider302Endpoint, "302.AI"],
    [officialEndpoint, "TypeSafe official"],
  ])("keeps a saved model for the recognized provider %s", async (endpoint, provider) => {
    vi.mocked(api.getModelConfig).mockResolvedValue({ ...config, jev_endpoint: endpoint, jev_model: "jev-pinned" } as never);
    await mount();
    expect(container.querySelector("#kb-jev-provider")?.textContent).toContain(provider);
    expect(container.querySelector<HTMLInputElement>("#kb-jev-model")!.value).toBe("jev-pinned");
  });
  it.each([
    { label: "unconfigured", jev_endpoint: null, search_strategy: "vector" },
    { label: "configured but inactive", jev_endpoint: officialEndpoint, search_strategy: "vector" },
    { label: "already enabled", jev_endpoint: officialEndpoint, search_strategy: "multi_jev" },
  ])("saves document settings without changing $label JEV configuration", async stored => {
    vi.mocked(api.getModelConfig).mockResolvedValue({ ...config, ...stored } as never);
    await mount();
    await edit("#kb-extract-concurrency", "12");
    await act(async () => button("Save and apply").click());
    expect(saved).toMatchObject({ document_extract_concurrency: 12 });
    expect(saved).not.toHaveProperty("search_strategy");
    expect(Object.keys(saved!).filter(key => key.startsWith("jev_"))).toEqual([]);
  });
  it("saves untouched provider defaults only when the user enables JEV", async () => {
    await mount();
    await select("#kb-search-strategy", "JEV");
    await act(async () => button("Save and apply").click());
    expect(saved).toMatchObject({ jev_endpoint: provider302Endpoint, jev_model: "jev-latest", search_strategy: "multi_jev", jev_ranking_timeout_seconds: 30, jev_failure_policy: "error" });
    expect(saved).not.toHaveProperty("jev_api_key");
  });
  it("does not persist provider defaults after the user cancels enabling JEV", async () => {
    await mount();
    await select("#kb-search-strategy", "JEV");
    await select("#kb-search-strategy", "Fast (vector)");
    await act(async () => button("Save and apply").click());
    expect(saved).not.toHaveProperty("search_strategy");
    expect(Object.keys(saved!).filter(key => key.startsWith("jev_"))).toEqual([]);
  });
  it("does not persist a tested but unedited default JEV draft", async () => {
    await mount();
    await act(async () => button("Test JEV connection").click());
    expect(tested).toMatchObject({ jev_endpoint: provider302Endpoint, jev_model: "jev-latest" });
    await edit("#kb-extract-concurrency", "12");
    await act(async () => button("Save and apply").click());
    expect(Object.keys(saved!).filter(key => key.startsWith("jev_"))).toEqual([]);
  });
  it("treats reverted JEV edits as an unchanged draft", async () => {
    await mount();
    await edit("#kb-jev-model", "jev-pinned");
    await edit("#kb-jev-model", "jev-latest");
    await act(async () => button("Save and apply").click());
    expect(Object.keys(saved!).filter(key => key.startsWith("jev_"))).toEqual([]);
  });
  it("selects the official full URL and latest alias, clearing the previous destination's draft key and result", async () => {
    await mount();
    await edit("#kb-jev-model", "custom-pinned");
    await edit("#kb-jev-api-key", "synthetic-302-key");
    await act(async () => button("Test JEV connection").click());
    expect(container.textContent).toContain("Draft connected");
    await select("#kb-jev-provider", "TypeSafe official");
    expect(container.querySelector<HTMLInputElement>("#kb-jev-endpoint")!.value).toBe(officialEndpoint);
    expect(container.querySelector<HTMLInputElement>("#kb-jev-model")!.value).toBe("jev-latest");
    expect(container.querySelector<HTMLInputElement>("#kb-jev-api-key")!.value).toBe("");
    expect(container.textContent).not.toContain("Draft connected");
    await act(async () => button("Test JEV connection").click());
    expect(tested).toMatchObject({ jev_endpoint: officialEndpoint, jev_model: "jev-latest" });
    expect(tested).not.toHaveProperty("jev_api_key");
    expect(tested).not.toHaveProperty("llm_api_key");
    await select("#kb-jev-provider", "302.AI");
    await act(async () => button("Save and apply").click());
    expect(saved).not.toHaveProperty("search_strategy");
    expect(Object.keys(saved!).filter(key => key.startsWith("jev_"))).toEqual([]);
    expect(saved).not.toHaveProperty("jev_api_key");
    expect(saved).not.toHaveProperty("jev_provider");
  });
  it("allows a custom full URL and custom model while keeping a blank independent key", async () => {
    await mount();
    await select("#kb-jev-provider", "Custom");
    await edit("#kb-jev-endpoint", "https://gateway.invalid/custom/score");
    await edit("#kb-jev-model", "gateway-jev");
    await act(async () => button("Test JEV connection").click());
    expect(tested).toMatchObject({ jev_endpoint: "https://gateway.invalid/custom/score", jev_model: "gateway-jev" });
    expect(tested).not.toHaveProperty("jev_api_key");
  });
  it("clears a draft key and test result when a custom destination changes", async () => {
    await mount();
    await select("#kb-jev-provider", "Custom");
    expect(container.querySelector<HTMLInputElement>("#kb-jev-endpoint")!.readOnly).toBe(false);
    await edit("#kb-jev-api-key", "synthetic-private-key");
    await act(async () => button("Test JEV connection").click());
    expect(container.textContent).toContain("Draft connected");
    await edit("#kb-jev-endpoint", "https://other.invalid/score");
    expect(container.querySelector<HTMLInputElement>("#kb-jev-api-key")!.value).toBe("");
    expect(container.textContent).not.toContain("Draft connected");
  });
  it("saves a service draft and JEV mode together while leaving a blank key untouched", async () => {
    await mount();
    await select("#kb-jev-provider", "Custom");
    await edit("#kb-jev-endpoint", "https://jev.invalid/score");
    await edit("#kb-jev-model", "gateway-model");
    await select("#kb-search-strategy", "JEV");
    await act(async () => button("Save and apply").click());
    expect(saved).toMatchObject({ jev_endpoint: "https://jev.invalid/score", jev_model: "gateway-model", search_strategy: "multi_jev", jev_ranking_timeout_seconds: 30, jev_failure_policy: "error" });
    expect(saved).not.toHaveProperty("jev_api_key");
    expect(state.refreshCapabilities).toHaveBeenCalled();
  });
  it.each([
    { label: "model", id: "#kb-jev-model", value: "jev-pinned", patch: { jev_model: "jev-pinned" } },
    { label: "timeout", id: "#kb-jev-timeout", value: "45", patch: { jev_ranking_timeout_seconds: 45 } },
    { label: "key rotation", id: "#kb-jev-api-key", value: "synthetic-rotated-key", patch: { jev_api_key: "synthetic-rotated-key" } },
  ])("saves the complete JEV group after editing $label and clears the dirty state", async change => {
    await mount();
    await edit(change.id, change.value);
    await act(async () => button("Save and apply").click());
    expect(saved).toMatchObject({ jev_endpoint: provider302Endpoint, jev_model: "jev-latest", jev_ranking_timeout_seconds: 30, jev_failure_policy: "error", ...change.patch });
    expect(container.querySelector<HTMLInputElement>("#kb-jev-api-key")!.value).toBe("");
    await edit("#kb-extract-concurrency", "12");
    await act(async () => button("Save and apply").click());
    expect(saved).toMatchObject({ document_extract_concurrency: 12 });
    expect(Object.keys(saved!).filter(key => key.startsWith("jev_"))).toEqual([]);
  });
  it("saves an edited failure policy without requiring a strategy change", async () => {
    await mount();
    await select("#kb-jev-failure-policy", "Allow coarse results");
    await act(async () => button("Save and apply").click());
    expect(saved).toMatchObject({ jev_endpoint: provider302Endpoint, jev_model: "jev-latest", jev_ranking_timeout_seconds: 30, jev_failure_policy: "fallback_to_score" });
    expect(saved).not.toHaveProperty("search_strategy");
  });
  it("starts clean after saving JEV and switching away from it", async () => {
    await mount();
    await select("#kb-search-strategy", "JEV");
    await act(async () => button("Save and apply").click());
    await act(async () => button("Save and apply").click());
    expect(saved).not.toHaveProperty("search_strategy");
    expect(Object.keys(saved!).filter(key => key.startsWith("jev_"))).toEqual([]);
    await select("#kb-search-strategy", "Fast (vector)");
    await act(async () => button("Save and apply").click());
    expect(saved).toMatchObject({ search_strategy: "vector" });
    expect(Object.keys(saved!).filter(key => key.startsWith("jev_"))).toEqual([]);
  });
  it("tests the current unsaved service draft without saving it", async () => {
    await mount();
    await select("#kb-jev-provider", "Custom");
    await edit("#kb-jev-endpoint", "https://jev.invalid/custom/ranking");
    await edit("#kb-jev-api-key", "synthetic-draft-key");
    await act(async () => button("Test JEV connection").click());
    expect(testedUrl).toMatch(/\/api\/v1\/system\/model-config\/jev\/test$/);
    expect(tested).toMatchObject({ jev_endpoint: "https://jev.invalid/custom/ranking", jev_api_key: "synthetic-draft-key", jev_model: "jev-latest" });
    expect(saved).toBeUndefined();
    expect(container.textContent).toContain("Draft connected");
  });
  it("blocks invalid service drafts before either network action", async () => {
    await mount();
    await select("#kb-jev-provider", "Custom");
    await edit("#kb-jev-endpoint", "javascript:alert(1)");
    expect(button("Test JEV connection").disabled).toBe(true);
    expect(button("Save and apply").disabled).toBe(true);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("valid full HTTP(S) URL");
    expect(saved).toBeUndefined();
    expect(tested).toBeUndefined();
  });
  it.each([
    ["pgvector", "PostgreSQL (pgvector)"],
    ["oceanbase", "OceanBase"],
  ])("explains the %s JEV limitation without blocking ordinary settings or rewriting the stored strategy", async (provider, displayName) => {
    state.capabilities = {
      vector_provider: provider,
      search_strategies: ["vector", "multi"],
      search_strategies_disabled: {
        multi_jev: { reason: "vector_provider_lacks_lexical", message: "Lexical search required" },
        multi_es_fast: { reason: "vector_provider_lacks_lexical", message: "Lexical search required" },
      },
    };
    vi.mocked(api.getModelConfig).mockResolvedValue({ ...config, search_strategy: "multi_es_fast", jev_endpoint: "invalid-persisted-endpoint", jev_ranking_timeout_seconds: -1 } as never);
    await mount();
    expect(container.textContent).toContain(displayName);
    expect(container.textContent).toContain("requires lexical search");
    expect(container.textContent).toContain("Your existing retrieval settings remain unchanged");
    expect(container.textContent).toContain("No database or model changes are needed");
    expect(container.querySelector("#kb-search-strategy")?.parentElement?.textContent).toContain("Currently using vector retrieval. You can keep using it or select another available strategy.");
    expect(container.querySelector("#kb-jev-provider")).toBeNull();
    expect(container.querySelector("#kb-jev-endpoint")).toBeNull();
    expect(button("Test JEV connection")).toBeUndefined();
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(button("Save and apply").disabled).toBe(false);
    await edit("#kb-extract-concurrency", "12");
    await act(async () => button("Save and apply").click());
    expect(saved).toMatchObject({ document_extract_concurrency: 12 });
    expect(saved).not.toHaveProperty("search_strategy");
    expect(Object.keys(saved!).filter(key => key.startsWith("jev_"))).toEqual([]);
    await select("#kb-search-strategy", "Precise");
    expect(container.querySelector("#kb-legacy-vector-fallback")).toBeNull();
  });
  it("lets a PostgreSQL user switch an unsupported stored JEV mode back to vector", async () => {
    state.capabilities = {
      vector_provider: "pgvector", search_strategies: ["vector", "multi"],
      search_strategies_disabled: { multi_jev: { reason: "vector_provider_lacks_lexical", message: "Lexical search required" } },
    };
    vi.mocked(api.getModelConfig).mockResolvedValue({ ...config, search_strategy: "multi_jev", jev_endpoint: "invalid", jev_model: "", jev_ranking_timeout_seconds: -1 } as never);
    await mount();
    expect(container.textContent).toContain("Choose Fast (vector) or Precise above and save");
    expect(container.textContent).not.toContain("Your existing retrieval settings remain unchanged");
    await select("#kb-search-strategy", "Fast (vector)");
    expect(button("Save and apply").disabled).toBe(false);
    await act(async () => button("Save and apply").click());
    expect(saved).toMatchObject({ search_strategy: "vector" });
    expect(Object.keys(saved!).filter(key => key.startsWith("jev_"))).toEqual([]);
  });
  it("disables the JEV option and rejects a draft enable when storage capabilities change", async () => {
    await mount();
    await select("#kb-search-strategy", "JEV");
    state.capabilities = {
      vector_provider: "pgvector", search_strategies: ["vector", "multi"],
      search_strategies_disabled: { multi_jev: { reason: "vector_provider_lacks_lexical", message: "Lexical search required" } },
    };
    await mount();
    expect(button("Save and apply").disabled).toBe(true);
    const trigger = container.querySelector<HTMLElement>("#kb-search-strategy")!;
    await act(async () => trigger.dispatchEvent(pointerEvent("pointerdown")));
    const jevOption = [...document.body.querySelectorAll<HTMLElement>('[role="option"]')].find(item => item.textContent?.includes("JEV"))!;
    expect(jevOption.getAttribute("aria-disabled")).toBe("true");
    expect(saved).toBeUndefined();
  });
  it.each([["lancedb", "LanceDB"], ["es", "Elasticsearch"]])("keeps the JEV connection controls available with %s", async (provider, displayName) => {
    state.capabilities.vector_provider = provider;
    vi.mocked(api.getModelConfig).mockResolvedValue({ ...config, search_strategy: "multi_es_fast" } as never);
    await mount();
    expect(container.textContent).toContain(displayName);
    expect(container.textContent).not.toContain("Currently using vector retrieval");
    expect(container.querySelector("#kb-jev-provider")).not.toBeNull();
    expect(button("Test JEV connection").disabled).toBe(false);
    await select("#kb-search-strategy", "JEV");
    await act(async () => button("Save and apply").click());
    expect(saved).toMatchObject({ search_strategy: "multi_jev", jev_endpoint: provider302Endpoint });
  });
  it("keeps ordinary settings editable against an older API without JEV capability", async () => {
    state.capabilities = { search_strategies: ["vector", "multi"] };
    vi.mocked(api.getModelConfig).mockResolvedValue({ ...config, jev_endpoint: "invalid" } as never);
    await mount();
    expect(container.textContent).toContain("This server does not advertise JEV support");
    expect(button("Test JEV connection").disabled).toBe(true);
    expect(button("Save and apply").disabled).toBe(false);
    await act(async () => button("Save and apply").click());
    expect(saved).not.toHaveProperty("search_strategy");
    expect(Object.keys(saved!).filter(key => key.startsWith("jev_"))).toEqual([]);
  });
});
