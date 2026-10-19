/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import english from "@/messages/en-US.json";
import chinese from "@/messages/zh-CN.json";
import type { ModelConfig, ModelProviderSpec } from "@/lib/types";
import { ModelConfigForm } from "./model-config-form";

const mocks = vi.hoisted(() => ({
  getModelConfig: vi.fn(), getModelProviders: vi.fn(), saveModelConfig: vi.fn(),
  refreshCapabilities: vi.fn(),
}));
let modelMessages: Record<string, unknown> = english.ModelConfig;
const modelTranslate = (key: string, values?: Record<string, string | number>) => {
  let message = typeof modelMessages[key] === "string" ? String(modelMessages[key]) : key;
  for (const [name, value] of Object.entries(values ?? {})) message = message.replaceAll(`{${name}}`, String(value));
  return message;
};
vi.mock("next-intl", () => ({ useTranslations: () => modelTranslate }));
vi.mock("@/components/features/app-shell", () => ({ useApp: () => ({ refreshCapabilities: mocks.refreshCapabilities }) }));
vi.mock("@/lib/api", () => ({ ApiError: class extends Error {}, api: mocks }));
vi.mock("@/lib/diagnostics", () => ({ getDiagnosticsStore: () => ({ record: vi.fn() }) }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const original = { llm_provider: "openai", llm_model: "original-model", llm_base_url: "https://llm.invalid/v1",
  llm_temperature: 0.3, llm_max_tokens: 1000, embedding_model: "shared-model", embedding_base_url: "https://original.invalid/v1",
  embedding_dimensions: 3, document_parser: "auto", mineru_provider: "302", mineru_version: "2.5",
  mineru_official_model: "vlm" };
let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  modelMessages = english.ModelConfig;
  mocks.refreshCapabilities.mockResolvedValue(undefined);
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });
async function mount() { await act(async () => root.render(<ModelConfigForm />)); }
function saveButton() {
  return [...container.querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === english.ModelConfig.save)!;
}
async function save() { await act(async () => saveButton().click()); }
async function edit(selector: string, value: string) {
  await act(async () => {
    const input = container.querySelector<HTMLInputElement>(selector)!;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
function fieldDescription(selector: string) {
  const input = container.querySelector<HTMLInputElement>(selector)!;
  const ids = input.getAttribute("aria-describedby")?.split(/\s+/) ?? [];
  return ids.map(id => document.getElementById(id)?.textContent ?? "").join(" ");
}

const embeddingConfig: ModelConfig = {
  ...original, llm_provider: "openai", llm_context_window: 128000, llm_timeout_ms: 60000,
  llm_max_retries: 2, llm_api_key_set: true, embedding_api_key_set: true,
  document_parser: "auto", mineru_provider: "302", mineru_base_url: null,
  mineru_version: "2.5", mineru_official_model: "vlm", mineru_api_key_set: false,
  effective_document_parser: "markitdown", document_extract_concurrency: 5,
  document_chunk_max_tokens: 512, document_chunk_mode: "standard",
  search_strategy: "vector", search_top_k: 5, sag_language: "zh",
};
const embeddingProviders: ModelProviderSpec[] = [
  { id: "openai", display_name: "OpenAI-compatible", protocol: "openai", default_model: "m",
    default_base_url: "https://api.302ai.cn/v1", default_context_window: 128000,
    default_temperature: 0.3, temperature_configurable: true,
    can_reuse_embedding_credentials: true, api_key_placeholder: "sk-…" },
  ...(["anthropic", "gemini"] as const).map(id => ({
    id, display_name: id, protocol: id, default_model: "native-model",
    default_base_url: null, default_context_window: 128000, default_temperature: 0.3,
    temperature_configurable: true, can_reuse_embedding_credentials: false,
    api_key_placeholder: "native-key",
  })),
];

describe("embedding connection hints", () => {
  beforeEach(() => {
    mocks.getModelConfig.mockResolvedValue(embeddingConfig);
    mocks.getModelProviders.mockResolvedValue(embeddingProviders);
    mocks.saveModelConfig.mockImplementation(async patch => ({ config: { ...embeddingConfig, ...patch } }));
  });

  it("keeps a cleared address inherited and its saved independent key after save and reload", async () => {
    const persisted = { ...embeddingConfig, embedding_base_url: null };
    mocks.saveModelConfig.mockResolvedValue({ config: persisted });
    await mount();
    expect(fieldDescription("#emb-url")).toContain("Currently used: https://original.invalid/v1 (separate embedding URL)");
    await edit("#emb-url", "");
    expect(container.querySelector<HTMLInputElement>("#emb-url")!.value).toBe("");
    expect(container.querySelector<HTMLInputElement>("#emb-url")!.placeholder).toBe("Leave blank to use generation URL");
    expect(fieldDescription("#emb-url")).toContain("Used after saving: https://llm.invalid/v1 (generation model URL)");
    expect(fieldDescription("#emb-key")).toContain("A separate embedding key is already saved. Leave blank to keep it.");
    expect(fieldDescription("#emb-url")).toContain("The generation model URL will be used with the separate embedding key");
    await save();
    expect(mocks.saveModelConfig).toHaveBeenCalledWith(expect.objectContaining({ embedding_base_url: "" }));
    expect(mocks.saveModelConfig.mock.calls[0][0]).not.toHaveProperty("embedding_api_key");
    expect(mocks.saveModelConfig.mock.calls[0][0]).not.toHaveProperty("llm_api_key");
    expect(container.querySelector<HTMLInputElement>("#emb-url")!.value).toBe("");
    expect(fieldDescription("#emb-url")).toContain("Currently used: https://llm.invalid/v1 (generation model URL)");
    mocks.getModelConfig.mockResolvedValue(persisted);
    await act(async () => root.unmount());
    root = createRoot(container);
    await mount();
    expect(container.querySelector<HTMLInputElement>("#emb-url")!.value).toBe("");
    expect(fieldDescription("#emb-url")).toContain("Currently used: https://llm.invalid/v1 (generation model URL)");
    expect(fieldDescription("#emb-key")).toContain("A separate embedding key is already saved. Leave blank to keep it.");
    expect(fieldDescription("#emb-url")).toContain("The generation model URL will be used with the separate embedding key");
  });

  it("restores explicit address behavior without copying the inherited URL into the field", async () => {
    await mount();
    await edit("#emb-url", "");
    await edit("#emb-url", "  https://original.invalid/v1  ");
    expect(fieldDescription("#emb-url")).toContain("Currently used: https://original.invalid/v1 (separate embedding URL)");
    expect(fieldDescription("#emb-url")).not.toContain("with the separate embedding key");
    await edit("#emb-url", "  https://new-embedding.invalid/v1  ");
    expect(fieldDescription("#emb-url")).toContain("Used after saving: https://new-embedding.invalid/v1 (separate embedding URL)");
    await save();
    expect(mocks.saveModelConfig).toHaveBeenCalledWith(expect.objectContaining({ embedding_base_url: "https://new-embedding.invalid/v1" }));
    expect(container.querySelector<HTMLInputElement>("#emb-url")!.value).toBe("https://new-embedding.invalid/v1");
    expect(fieldDescription("#emb-key")).toContain("A separate embedding key is already saved");
  });

  it("tracks generation address drafts live only when the embedding address is inherited", async () => {
    const config = { ...embeddingConfig, embedding_base_url: null };
    mocks.getModelConfig.mockResolvedValue(config);
    mocks.saveModelConfig.mockImplementation(async patch => ({ config: { ...config, ...patch } }));
    await mount();
    await edit("#llm-model", "different-model");
    expect(fieldDescription("#emb-url")).toContain("Currently used: https://llm.invalid/v1 (generation model URL)");
    await edit("#llm-model", config.llm_model);
    await edit("#llm-url", "  https://new-generation.invalid/v1  ");
    expect(fieldDescription("#emb-url")).toContain("Used after saving: https://new-generation.invalid/v1 (generation model URL)");
    expect(container.querySelector<HTMLInputElement>("#emb-url")!.value).toBe("");
    await edit("#llm-url", "  https://llm.invalid/v1  ");
    expect(fieldDescription("#emb-url")).toContain("Currently used: https://llm.invalid/v1 (generation model URL)");
    await edit("#llm-url", "https://new-generation.invalid/v1");
    await save();
    expect(mocks.saveModelConfig).toHaveBeenCalledWith(expect.objectContaining({ llm_base_url: "https://new-generation.invalid/v1" }));
    expect(fieldDescription("#emb-url")).toContain("Currently used: https://new-generation.invalid/v1 (generation model URL)");
  });

  it("keeps an unchanged separate address current when generation settings change", async () => {
    await mount();
    await edit("#llm-url", "https://new-generation.invalid/v1");
    expect(fieldDescription("#emb-url")).toContain("Currently used: https://original.invalid/v1 (separate embedding URL)");
    expect(fieldDescription("#emb-url")).not.toContain("new-generation.invalid");
  });

  it("shows a new separate key draft without leaking it or changing the address source", async () => {
    mocks.getModelConfig.mockResolvedValue({ ...embeddingConfig, embedding_base_url: null, embedding_api_key_set: false });
    await mount();
    await edit("#emb-key", "  secret-draft-embedding-key  ");
    expect(fieldDescription("#emb-key")).toContain("After saving, the new separate embedding key will be used.");
    expect(fieldDescription("#emb-key")).not.toContain("secret-draft-embedding-key");
    expect(container.textContent).not.toContain("secret-draft-embedding-key");
    expect(fieldDescription("#emb-url")).toContain("Currently used: https://llm.invalid/v1 (generation model URL)");
    expect(fieldDescription("#emb-url")).toContain("with the separate embedding key");
    await save();
    expect(mocks.saveModelConfig).toHaveBeenCalledWith(expect.objectContaining({ embedding_api_key: "secret-draft-embedding-key" }));
  });

  it("reuses a configured generation key only when no separate embedding key exists", async () => {
    mocks.getModelConfig.mockResolvedValue({ ...embeddingConfig, embedding_base_url: null, embedding_api_key_set: false });
    await mount();
    expect(fieldDescription("#emb-key")).toContain("Uses the generation model key.");
    expect(fieldDescription("#emb-url")).not.toContain("with the separate embedding key");
    await edit("#emb-key", "new-independent-key");
    expect(fieldDescription("#emb-key")).toContain("new separate embedding key");
    await edit("#emb-key", "   ");
    expect(fieldDescription("#emb-key")).toContain("Uses the generation model key.");
  });

  it("recognizes a new generation key draft without showing its secret", async () => {
    mocks.getModelConfig.mockResolvedValue({ ...embeddingConfig, embedding_base_url: null,
      llm_api_key_set: false, embedding_api_key_set: false });
    await mount();
    expect(fieldDescription("#emb-key")).toContain("No embedding key is configured. Configure a separate key.");
    await edit("#llm-key", "generation-secret-draft");
    expect(fieldDescription("#emb-key")).toContain("Uses the generation model key.");
    expect(fieldDescription("#emb-key")).not.toContain("generation-secret-draft");
    expect(container.textContent).not.toContain("generation-secret-draft");
    expect(fieldDescription("#emb-url")).toContain("Currently used: https://llm.invalid/v1 (generation model URL)");
    await save();
    expect(mocks.saveModelConfig).toHaveBeenCalledWith(expect.objectContaining({ llm_api_key: "generation-secret-draft" }));
  });

  it.each([
    ["anthropic", false, "No embedding key is configured. Configure a separate key."],
    ["anthropic", true, "A separate embedding key is already saved. Leave blank to keep it."],
    ["gemini", false, "No embedding key is configured. Configure a separate key."],
    ["gemini", true, "A separate embedding key is already saved. Leave blank to keep it."],
  ] as const)("shows SDK default URL for %s with saved independent key=%s without inheriting generation credentials", async (llm_provider, embedding_api_key_set, keyDescription) => {
    mocks.getModelConfig.mockResolvedValue({ ...embeddingConfig, llm_provider, embedding_base_url: null, embedding_api_key_set });
    await mount();
    expect(container.querySelector<HTMLInputElement>("#emb-url")!.value).toBe("");
    expect(container.querySelector<HTMLInputElement>("#emb-url")!.placeholder).toBe("Configure a separate embedding endpoint");
    expect(fieldDescription("#emb-url")).toContain("Currently used: SDK default URL. The generation model URL is not inherited; enter your embedding service’s URL.");
    expect(fieldDescription("#emb-url")).not.toContain("llm.invalid");
    expect(fieldDescription("#emb-url")).not.toContain("with the separate embedding key");
    expect(fieldDescription("#emb-key")).toContain(keyDescription);
    expect(fieldDescription("#emb-key")).not.toContain("Uses the generation model key");
    await edit("#emb-url", "https://native-embedding.invalid/v1");
    await edit("#emb-key", "separate-native-key");
    expect(fieldDescription("#emb-url")).toContain("Used after saving: https://native-embedding.invalid/v1 (separate embedding URL)");
    expect(fieldDescription("#emb-url")).not.toContain("with the separate embedding key");
    await save();
    expect(mocks.saveModelConfig).toHaveBeenCalledWith(expect.objectContaining({ embedding_base_url: "https://native-embedding.invalid/v1", embedding_api_key: "separate-native-key" }));
  });

  it("inherits the retained generation key when switching from a native provider to OpenAI-compatible", async () => {
    mocks.getModelConfig.mockResolvedValue({ ...embeddingConfig, llm_provider: "anthropic", embedding_base_url: null, embedding_api_key_set: false });
    await mount();
    expect(fieldDescription("#emb-key")).toContain("No embedding key is configured");
    const scrollIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => undefined });
    try {
      await act(async () => container.querySelector("#llm-provider")!.dispatchEvent(
        new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }),
      ));
      const option = [...document.querySelectorAll<HTMLElement>('[role="option"]')].find(node => node.textContent === "OpenAI-compatible")!;
      await act(async () => option.click());
    } finally {
      if (scrollIntoView) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", scrollIntoView);
      else delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
    }
    expect(container.querySelector<HTMLInputElement>("#llm-key")!.value).toBe("");
    expect(fieldDescription("#emb-url")).toContain("Used after saving: https://llm.invalid/v1 (generation model URL)");
    expect(fieldDescription("#emb-key")).toContain("Uses the generation model key.");
    await save();
    expect(mocks.saveModelConfig).toHaveBeenCalledWith(expect.objectContaining({ llm_provider: "openai" }));
  });

  it("uses SDK-default wording instead of guessing the catalog URL when both raw addresses are empty", async () => {
    mocks.getModelConfig.mockResolvedValue({ ...embeddingConfig, llm_base_url: null, embedding_base_url: null });
    await mount();
    expect(container.querySelector<HTMLInputElement>("#emb-url")!.value).toBe("");
    expect(container.querySelector<HTMLInputElement>("#emb-url")!.placeholder).toBe("Leave blank to use default URL");
    expect(fieldDescription("#emb-url")).toContain("Currently used: SDK default URL.");
    expect(fieldDescription("#emb-url")).not.toContain("302");
    expect(fieldDescription("#emb-url")).not.toContain("with the separate embedding key");
    await edit("#llm-url", "https://explicit-generation.invalid/v1");
    expect(container.querySelector<HTMLInputElement>("#emb-url")!.placeholder).toBe("Leave blank to use generation URL");
    expect(fieldDescription("#emb-url")).toContain("Used after saving: https://explicit-generation.invalid/v1 (generation model URL)");
    await edit("#llm-url", "   ");
    expect(container.querySelector<HTMLInputElement>("#emb-url")!.placeholder).toBe("Leave blank to use default URL");
    expect(fieldDescription("#emb-url")).toContain("Currently used: SDK default URL.");
  });

  it("shows the localized inheritance hint with the actual Chinese draft endpoint", async () => {
    modelMessages = chinese.ModelConfig;
    mocks.getModelConfig.mockResolvedValue({ ...embeddingConfig, embedding_base_url: null });
    await mount();
    expect(container.querySelector<HTMLInputElement>("#emb-url")!.placeholder).toBe("留空使用生成模型地址");
    expect(fieldDescription("#emb-url")).toContain("当前使用：https://llm.invalid/v1（来自生成模型地址）");
    expect(fieldDescription("#emb-url")).toContain("将使用生成模型地址，仍使用独立向量密钥。如向量模型使用其他服务，请填写对应的服务地址。");
    await edit("#llm-url", "https://changed-generation.invalid/v1");
    expect(fieldDescription("#emb-url")).toContain("保存后使用：https://changed-generation.invalid/v1（来自生成模型地址）");
    await edit("#llm-url", "   ");
    expect(container.querySelector<HTMLInputElement>("#emb-url")!.placeholder).toBe("留空使用默认向量服务地址");
    expect(fieldDescription("#emb-url")).toContain("保存后使用：SDK 默认地址。");
  });
});
