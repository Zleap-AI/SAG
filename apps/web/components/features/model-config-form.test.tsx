/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import english from "@/messages/en-US.json";
import chinese from "@/messages/zh-CN.json";
import type { ModelConfig, ModelProviderSpec } from "@/lib/types";
import { ModelConfigForm } from "./model-config-form";

const mocks = vi.hoisted(() => ({ translate: (key: string) => key === "testGeneration" ? "Test generation model" : key === "testEmbedding" ? "Test embedding model" : key === "testing" ? "Testing…" : key,
  getModelConfig: vi.fn(), getModelProviders: vi.fn(), saveModelConfig: vi.fn(), testModelConfig: vi.fn(), testEmbeddingModelConfig: vi.fn(),
  getChatbotConfig: vi.fn(), saveChatbotConfig: vi.fn(), testChatbotConfig: vi.fn(),
  refreshCapabilities: vi.fn(), success: vi.fn(), order: [] as string[] }));
const chatbotTranslate = (key: string, values?: Record<string, string>) => {
  let text = english.ChatbotConfig[key as keyof typeof english.ChatbotConfig] ?? key;
  for (const [name, value] of Object.entries(values ?? {})) text = text.replace(`{${name}}`, value);
  return text;
};
let modelMessages: Record<string, unknown> = english.ModelConfig;
const modelTranslate = (key: string, values?: Record<string, string | number>) => {
  const message = key.startsWith("embeddingAddress") || key.startsWith("embeddingKey") ||
    key.startsWith("responses") || key === "separateEmbeddingAddress" || key === "embeddingInheritedAddressIndependentKey"
    ? modelMessages[key] : mocks.translate(key);
  let text = typeof message === "string" ? message : key;
  for (const [name, value] of Object.entries(values ?? {})) text = text.replaceAll(`{${name}}`, String(value));
  return text;
};
vi.mock("next-intl", () => ({ useLocale: () => "en-US", useTranslations: (namespace: string) => namespace === "ChatbotConfig" ? chatbotTranslate : modelTranslate }));
vi.mock("@/components/features/app-shell", () => ({ useApp: () => ({ refreshCapabilities: mocks.refreshCapabilities }) }));
vi.mock("@/lib/auth", () => ({ getToken: () => "test-token" }));
vi.mock("@/lib/api", () => ({ API_BASE: "https://api.invalid", ApiError: class extends Error {}, api: mocks }));
vi.mock("@/lib/diagnostics", () => ({ getDiagnosticsStore: () => ({ record: vi.fn() }) }));
vi.mock("sonner", () => ({ toast: { success: mocks.success } }));

const original = { llm_provider: "openai", llm_model: "original-model", llm_base_url: "https://llm.invalid/v1",
  llm_temperature: 0.3, llm_max_tokens: 1000, embedding_model: "shared-model", embedding_base_url: "https://original.invalid/v1",
  embedding_dimensions: 3, document_parser: "auto", mineru_provider: "302", mineru_version: "2.5",
  mineru_official_model: "vlm", locked_fields: [] };
const initial = { locked: false, encryption_configured: true,
  llm: { enabled: false, provider: "openai", model: "chat-model", base_url: "", api_key_set: true },
  embedding: { enabled: false, model: "shared-model", base_url: "https://query.invalid/v1", api_key_set: true,
    schema_dimensions: 3, request_dimensions: null } };
let container: HTMLDivElement;
let root: Root;
type FetchResponse = {
  ok: boolean; json: () => Promise<{ config?: typeof initial; message?: string; error?: { message: string } }>;
};
let fetcher: ReturnType<typeof vi.fn<(url: string, request: { method: string; body?: string }) => Promise<FetchResponse>>>;
let saved = structuredClone(initial);
let failChatbot = false;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks(); mocks.order = []; failChatbot = false; saved = structuredClone(initial); modelMessages = english.ModelConfig;
  mocks.getModelConfig.mockResolvedValue(original);
  mocks.getModelProviders.mockResolvedValue([{ id: "openai", default_model: "m", temperature_configurable: true,
    default_temperature: 0.3, can_reuse_embedding_credentials: true }]);
  mocks.saveModelConfig.mockImplementation(async (patch) => {
    mocks.order.push("original"); return { config: { ...original, ...patch } };
  });
  mocks.refreshCapabilities.mockResolvedValue(undefined);
  mocks.testModelConfig.mockResolvedValue({ ok: true, message: "Draft connected" });
  mocks.testEmbeddingModelConfig.mockResolvedValue({ ok: true, message: "Embedding connected" });
  fetcher = vi.fn(async (_url: string, request: { method: string; body?: string }) => {
    if (request.method === "PUT") {
      mocks.order.push("chatbot");
      if (failChatbot) return { ok: false, json: async () => ({ error: { message: "Credential encryption is not configured" } }) };
      const changes = JSON.parse(request.body!);
      saved = { ...saved, llm: { ...saved.llm, ...changes.llm }, embedding: { ...saved.embedding, ...changes.embedding } };
    }
    return { ok: true, json: async () => ({ config: saved }) };
  });
  const call = async (url: string, request: { method: string; body?: string }) => {
    const result = await fetcher(url, request);
    const body = await result.json();
    if (!result.ok) throw new Error(body.error?.message);
    return body;
  };
  mocks.getChatbotConfig.mockImplementation(() => call("/chatbot-config", { method: "GET" }));
  mocks.saveChatbotConfig.mockImplementation(body => call("/chatbot-config", { method: "PUT", body: JSON.stringify(body) }));
  mocks.testChatbotConfig.mockImplementation(body => call("/chatbot-config/test", { method: "POST", body: JSON.stringify(body) }));
  vi.stubGlobal("fetch", fetcher);
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals(); });
async function mount() { await act(async () => root.render(<ModelConfigForm />)); }
function saveButton() { return [...container.querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === "save" || button.textContent === "saving")!; }
async function save() { await act(async () => saveButton().click()); }
async function edit(selector: string, value: string) {
  await act(async () => {
    const input = container.querySelector<HTMLInputElement>(selector)!;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
function writes() { return fetcher.mock.calls.filter(([, request]) => request.method === "PUT"); }
async function selectFormat(value: "openai" | "responses") {
  const scrollIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => undefined });
  try {
    await act(async () => container.querySelector("#llm-api-format")!.dispatchEvent(
      new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }),
    ));
    const label = value === "responses" ? "Responses API" : "Chat Completions";
    const option = [...document.querySelectorAll<HTMLElement>('[role="option"]')].find(node => node.textContent === label)!;
    await act(async () => option.click());
  } finally {
    if (scrollIntoView) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", scrollIntoView);
    else delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
  }
}

function fieldDescription(selector: string) {
  const input = container.querySelector<HTMLInputElement>(selector)!;
  const ids = input.getAttribute("aria-describedby")?.split(/\s+/) ?? [];
  return ids.map(id => document.getElementById(id)?.textContent ?? "").join(" ");
}

const embeddingConfig: ModelConfig = {
  ...original, llm_provider: "openai", llm_context_window: 128000, llm_timeout_ms: 60000,
  llm_max_retries: 2, llm_api_key_set: true, embedding_api_key_set: true,
  llm_responses_provider: "openai", llm_responses_endpoint: "https://api.openai.com/v1/responses",
  llm_responses_api_version: "", llm_responses_send_temperature: false,
  document_parser: "auto", mineru_provider: "302", mineru_base_url: null,
  mineru_version: "2.5", mineru_official_model: "vlm", mineru_api_key_set: false,
  effective_document_parser: "markitdown", document_extract_concurrency: 5,
  document_chunk_max_tokens: 512, document_chunk_mode: "standard",
  search_strategy: "vector", search_top_k: 5, sag_language: "zh", sources: {},
};
const embeddingProviders: ModelProviderSpec[] = [
  { id: "openai", display_name: "OpenAI-compatible", protocol: "openai", default_model: "m",
    default_base_url: "https://api.302ai.cn/v1", default_context_window: 128000,
    default_temperature: 0.3, temperature_configurable: true,
    can_reuse_embedding_credentials: true, api_key_placeholder: "sk-…" },
  { id: "responses", display_name: "Responses API", protocol: "responses", default_model: "",
    default_base_url: null, default_context_window: 128000,
    default_temperature: 0.3, temperature_configurable: true,
    can_reuse_embedding_credentials: false, api_key_placeholder: "Responses key" },
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
    expect(mocks.saveModelConfig).toHaveBeenCalledWith({ embedding_base_url: "" });
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
    expect(saveButton().disabled).toBe(true);
    await edit("#emb-url", "  https://new-embedding.invalid/v1  ");
    expect(fieldDescription("#emb-url")).toContain("Used after saving: https://new-embedding.invalid/v1 (separate embedding URL)");
    await save();
    expect(mocks.saveModelConfig).toHaveBeenCalledWith({ embedding_base_url: "https://new-embedding.invalid/v1" });
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
    expect(mocks.saveModelConfig).toHaveBeenCalledWith({ llm_base_url: "https://new-generation.invalid/v1" });
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
    expect(mocks.saveModelConfig).toHaveBeenCalledWith({ embedding_api_key: "secret-draft-embedding-key" });
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
    expect(saveButton().disabled).toBe(true);
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
    expect(mocks.saveModelConfig).toHaveBeenCalledWith({ llm_api_key: "generation-secret-draft" });
  });

  it.each([
    ["anthropic", false, "No embedding key is configured. Configure a separate key."],
    ["anthropic", true, "A separate embedding key is already saved. Leave blank to keep it."],
    ["gemini", false, "No embedding key is configured. Configure a separate key."],
    ["gemini", true, "A separate embedding key is already saved. Leave blank to keep it."],
    ["responses", false, "No embedding key is configured. Configure a separate key."],
    ["responses", true, "A separate embedding key is already saved. Leave blank to keep it."],
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
    expect(mocks.saveModelConfig).toHaveBeenCalledWith({ embedding_base_url: "https://native-embedding.invalid/v1", embedding_api_key: "separate-native-key" });
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
    expect(mocks.saveModelConfig).toHaveBeenCalledWith({ llm_provider: "openai" });
  });

  it("requires a replacement generation key for embedding inheritance after leaving Responses", async () => {
    mocks.getModelConfig.mockResolvedValue({ ...embeddingConfig, llm_provider: "responses",
      llm_responses_provider: "openai", llm_responses_endpoint: "https://responses.invalid/v1/responses",
      llm_responses_api_version: null, embedding_base_url: null, embedding_api_key_set: false });
    await mount();
    await selectFormat("openai");
    expect(fieldDescription("#emb-key")).toContain("No embedding key is configured. Configure a separate key.");
    await edit("#llm-key", "new-chat-completions-key");
    expect(fieldDescription("#emb-key")).toContain("Uses the generation model key.");
    expect(container.textContent).not.toContain("new-chat-completions-key");
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

describe("one page-wide model Save", () => {
  it("tests the unsaved original embedding draft without saving and reports dimensions", async () => {
    let finish!: (result: { ok: boolean; message: string; dimensions: number }) => void;
    mocks.testEmbeddingModelConfig.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    await mount();
    await edit("#emb-model", "draft-embedding"); await edit("#emb-url", "https://draft.invalid/v1");
    await edit("#emb-dims", "4"); await edit("#emb-key", "draft-embedding-key");
    const button = [...container.querySelectorAll<HTMLButtonElement>("button")]
      .find(button => button.textContent === "Test embedding model")!;
    await act(async () => button.click());
    expect(mocks.testEmbeddingModelConfig).toHaveBeenCalledWith({
      embedding_model: "draft-embedding", embedding_base_url: "https://draft.invalid/v1",
      embedding_api_key: "draft-embedding-key", embedding_dimensions: 4,
      llm_provider: "openai", llm_base_url: original.llm_base_url,
    });
    expect(button.disabled).toBe(true); expect(button.textContent).toBe("Testing…");
    expect(saveButton().disabled).toBe(true);
    expect([...container.querySelectorAll<HTMLButtonElement>("button")]
      .find(button => button.textContent === "Test generation model")!.disabled).toBe(false);
    await act(async () => finish({ ok: true, message: "Embedding connection successful · 4 dimensions", dimensions: 4 }));
    expect(button.closest("section")!.textContent).toContain("4 dimensions");
    expect(button.disabled).toBe(false); expect(saveButton().disabled).toBe(false);
    expect(container.querySelector<HTMLInputElement>("#emb-key")!.value).toBe("draft-embedding-key");
    expect(mocks.saveModelConfig).not.toHaveBeenCalled(); expect(writes()).toHaveLength(0);
  });

  it("keeps Responses credentials out of the original embedding test", async () => {
    mocks.getModelConfig.mockResolvedValue({ ...original, llm_provider: "responses" });
    mocks.getModelProviders.mockResolvedValue([
      { id: "openai", display_name: "OpenAI-compatible", temperature_configurable: true, default_temperature: 0.3 },
      { id: "responses", display_name: "Responses API", temperature_configurable: true, default_temperature: 0.3,
        can_reuse_embedding_credentials: false },
    ]);
    await mount(); await edit("#llm-key", "responses-private-key");
    const button = [...container.querySelectorAll<HTMLButtonElement>("button")]
      .find(button => button.textContent === "Test embedding model")!;
    await act(async () => button.click());
    const draft = mocks.testEmbeddingModelConfig.mock.calls[0][0];
    expect(draft.llm_provider).toBe("responses");
    expect(draft.llm_api_key).toBeUndefined(); expect(draft.llm_base_url).toBeUndefined();
    expect(JSON.stringify(draft)).not.toContain("responses-private-key");
  });

  it("saves the original Responses endpoint and service fields from the native form", async () => {
    const responses = { ...original, llm_provider: "responses", llm_responses_provider: "openai",
      llm_responses_endpoint: "https://original.invalid/v1/responses", llm_responses_api_version: "",
      llm_responses_send_temperature: false };
    mocks.getModelConfig.mockResolvedValue(responses);
    mocks.saveModelConfig.mockImplementation(async patch => ({ config: { ...responses, ...patch } }));
    mocks.getModelProviders.mockResolvedValue([
      { id: "openai", display_name: "OpenAI-compatible" },
      { id: "responses", display_name: "Responses API", default_model: "", temperature_configurable: true,
        default_temperature: 0.3, can_reuse_embedding_credentials: false },
    ]);
    await mount();
    expect(container.querySelector("#llm-provider")!.textContent).toBe("OpenAI-compatible");
    expect(container.querySelector("#llm-api-format")!.textContent).toBe("Responses API");
    expect(container.querySelector("#llm-url")).toBeNull();
    expect(container.querySelector("#original-responses-service")).toBeNull();
    expect(container.querySelector("#original-responses-auth")!.closest("details")!.open).toBe(false);
    await edit("#original-responses-endpoint", "https://edited.invalid/v1/responses");
    await save();
    expect(mocks.saveModelConfig.mock.calls[0][0]).toEqual({
      llm_responses_endpoint: "https://edited.invalid/v1/responses",
    });
  });

  it("preserves saved Azure authentication and version when only the original temperature switch changes", async () => {
    const responses = { ...embeddingConfig, llm_provider: "responses", llm_responses_provider: "azure",
      llm_responses_endpoint: "https://resource.invalid/openai/responses",
      llm_responses_api_version: "2025-04-01-preview", llm_responses_send_temperature: false };
    mocks.getModelConfig.mockResolvedValue(responses);
    mocks.saveModelConfig.mockImplementation(async patch => ({ config: { ...responses, ...patch } }));
    mocks.getModelProviders.mockResolvedValue(embeddingProviders);
    await mount();
    expect(container.querySelector("#original-responses-service")).toBeNull();
    expect(container.querySelector("#original-responses-auth")!.textContent).toBe("API key header (Azure)");
    expect(container.querySelector<HTMLInputElement>("#original-responses-version")!.value)
      .toBe(responses.llm_responses_api_version);
    await act(async () => container.querySelector<HTMLButtonElement>("#original-responses-temperature")!.click());
    await save();
    expect(mocks.saveModelConfig).toHaveBeenCalledWith({ llm_responses_send_temperature: true });
    expect(container.querySelector<HTMLInputElement>("#llm-key")!.value).toBe("");
    expect(container.querySelector<HTMLInputElement>("#llm-key")!.placeholder).toBe("keyConfigured");
  });

  it("selects Responses under OpenAI-compatible and preserves drafts when switching API formats", async () => {
    mocks.getModelProviders.mockResolvedValue([
      { id: "openai", display_name: "OpenAI-compatible", default_model: "m", default_base_url: null,
        default_context_window: 128000, temperature_configurable: true, default_temperature: 0.3 },
      { id: "responses", display_name: "Responses API", default_model: "", default_base_url: null,
        default_context_window: 128000, temperature_configurable: true, default_temperature: 0.3 },
    ]);
    await mount();
    expect(container.querySelector("#llm-api-format")!.textContent).toBe("Chat Completions");
    await selectFormat("responses");
    expect(container.querySelector("#llm-provider")!.textContent).toBe("OpenAI-compatible");
    expect(container.querySelector("#llm-url")).toBeNull();
    expect(container.querySelector<HTMLInputElement>("#llm-model")!.value).toBe(original.llm_model);
    await edit("#original-responses-endpoint", "https://api.deepseek.com/responses");
    const test = [...container.querySelectorAll<HTMLButtonElement>("button")]
      .find(button => button.textContent === "Test generation model")!;
    await act(async () => test.click());
    expect(mocks.testModelConfig).toHaveBeenLastCalledWith(expect.objectContaining({
      llm_provider: "responses", llm_model: original.llm_model,
      llm_responses_endpoint: "https://api.deepseek.com/responses",
    }));
    await selectFormat("openai");
    expect(container.querySelector<HTMLInputElement>("#llm-url")!.value).toBe(original.llm_base_url);
    expect(container.querySelector("#original-responses-endpoint")).toBeNull();
    expect(saveButton().disabled).toBe(true);
    await act(async () => test.click());
    expect(mocks.testModelConfig).toHaveBeenLastCalledWith(expect.objectContaining({
      llm_provider: "openai", llm_base_url: original.llm_base_url,
    }));
    expect(mocks.testModelConfig.mock.calls.at(-1)![0].llm_responses_endpoint).toBeUndefined();
    await selectFormat("responses");
    expect(container.querySelector<HTMLInputElement>("#original-responses-endpoint")!.value)
      .toBe("https://api.deepseek.com/responses");
    await save();
    expect(mocks.saveModelConfig).toHaveBeenLastCalledWith(expect.objectContaining({
      llm_provider: "responses", llm_responses_endpoint: "https://api.deepseek.com/responses",
    }));
  });

  it("locks the nested API format with deployment-managed generation settings", async () => {
    mocks.getModelConfig.mockResolvedValue({ ...original, locked_fields: ["llm_provider"] });
    mocks.getModelProviders.mockResolvedValue([
      { id: "openai", display_name: "OpenAI-compatible", temperature_configurable: true, default_temperature: 0.3 },
      { id: "responses", display_name: "Responses API" },
    ]);
    await mount();
    expect(container.querySelector<HTMLButtonElement>("#llm-api-format")!.disabled).toBe(true);
  });

  it("renders one Save after every section and retains per-section Tests", async () => {
    await mount();
    const saves = [...container.querySelectorAll<HTMLButtonElement>("button")].filter(button => button.textContent === "save");
    expect(saves).toHaveLength(1); expect(saves[0].closest("section")).toBeNull();
    expect([...container.querySelectorAll("button")].at(-1)).toBe(saves[0]);
    expect(container.querySelectorAll("section button").length).toBeGreaterThanOrEqual(3);
  });
  it.each(["llm", "embedding"] as const)("keeps the other optional endpoint editable and testable while %s is testing", async (first) => {
    saved.llm.enabled = true; saved.embedding.enabled = true;
    await mount();
    type Target = "llm" | "embedding";
    const other: Target = first === "llm" ? "embedding" : "llm";
    const sections = [...container.querySelectorAll<HTMLElement>("section")].slice(-2);
    const section = { llm: sections[0], embedding: sections[1] };
    const controls = (target: Target) => [...section[target].querySelectorAll<HTMLInputElement | HTMLButtonElement | HTMLSelectElement>("input, button, select")];
    const buttons = Object.fromEntries((["llm", "embedding"] as const).map(target => [target,
      [...section[target].querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === (target === "llm" ? "Test generation model" : "Test embedding model"))!,
    ])) as Record<Target, HTMLButtonElement>;
    const finish: Partial<Record<Target, (response: FetchResponse) => void>> = {};
    fetcher.mockImplementation((_url, request) => new Promise(resolve => {
      finish[JSON.parse(request.body!).target as Target] = resolve;
    }));

    await act(async () => buttons[first].click());
    expect(buttons[first].textContent).toBe("Testing…");
    expect(controls(first).every(control => control.disabled)).toBe(true);
    expect(controls(other).every(control => !control.disabled)).toBe(true);
    expect(saveButton().disabled).toBe(true);
    await edit(`[aria-label="${other} API Key"]`, "independent-draft-key");
    await act(async () => buttons[other].click());
    const requests = fetcher.mock.calls.filter(([, request]) => request.method === "POST").map(([, request]) => JSON.parse(request.body!));
    expect(requests.map(request => request.target)).toEqual([first, other]);
    expect(requests[1][other].api_key).toBe("independent-draft-key");
    expect(controls(other).every(control => control.disabled)).toBe(true);

    await act(async () => finish[first]!({ ok: true, json: async () => ({ message: `${first} connected` }) }));
    expect(controls(first).every(control => !control.disabled)).toBe(true);
    expect(controls(other).every(control => control.disabled)).toBe(true);
    expect(section[first].querySelector('[role="status"]')!.textContent).toBe(`${first} connected`);
    expect(buttons[other].textContent).toBe("Testing…");
    expect(saveButton().disabled).toBe(true);

    await act(async () => finish[other]!({ ok: false, json: async () => ({ error: { message: `${other} unavailable` } }) }));
    expect(section[other].querySelector('[role="status"]')!.textContent).toBe(`${other} unavailable`);
    expect(section[first].querySelector('[role="status"]')!.textContent).toBe(`${first} connected`);
    expect(controls(other).every(control => !control.disabled)).toBe(true);
    expect(saveButton().disabled).toBe(false);
    expect(writes()).toHaveLength(0); expect(mocks.saveModelConfig).not.toHaveBeenCalled();
  });
  it("saves only changed original fields and both edited chatbot connections independently", async () => {
    await mount();
    await edit("#llm-model", "edited-original"); await edit('[aria-label="Model"]', "edited-chat");
    await edit('[aria-label="embedding API Key"]', "new-query-key");
    await save();
    expect(mocks.order).toEqual(["original", "chatbot"]);
    expect(mocks.saveModelConfig.mock.calls[0][0]).toEqual({ llm_model: "edited-original" });
    expect(writes()).toHaveLength(1);
    const body = JSON.parse(writes()[0][1].body!);
    expect(body.llm.model).toBe("edited-chat"); expect(body.llm.api_key).toBe("");
    expect(body.embedding.api_key).toBe("new-query-key"); expect(body.embedding.model).toBeUndefined();
    expect(body.embedding.schema_dimensions).toBeUndefined();
    expect(container.querySelector<HTMLInputElement>('[aria-label="embedding API Key"]')!.value).toBe("");
    expect(mocks.success).toHaveBeenCalledTimes(2); expect(mocks.refreshCapabilities).toHaveBeenCalledTimes(2);
    expect(saveButton().disabled).toBe(true);
  });
  it.each([['[aria-label="Model"]', "new-chat-model"], ['[aria-label="Query endpoint"]', "https://new-query.invalid/v1"]])(
    "saves optional changes to %s without calling the extraction settings save", async (selector, value) => {
      await mount(); await edit(selector, value); await save();
      expect(mocks.saveModelConfig).not.toHaveBeenCalled(); expect(writes()).toHaveLength(1);
      expect(mocks.success).toHaveBeenCalledWith("Chatbot connections saved.");
      expect(saveButton().disabled).toBe(true);
    });
  it("skips unchanged original settings, including reverted drafts and blank keys", async () => {
    await mount(); expect(saveButton().disabled).toBe(true);
    await edit("#llm-model", "temporary-model"); expect(saveButton().disabled).toBe(false);
    await edit("#llm-model", original.llm_model);
    await edit("#llm-key", "temporary-key"); await edit("#llm-key", "");
    expect(saveButton().disabled).toBe(true);
    await edit('[aria-label="Model"]', "edited-chat"); await save();
    expect(mocks.saveModelConfig).not.toHaveBeenCalled();
  });
  it("preserves explicit endpoint and dimension clears in the original delta", async () => {
    await mount(); await edit("#emb-url", ""); await edit("#emb-dims", ""); await edit("#emb-key", "new-index-key"); await save();
    expect(mocks.saveModelConfig).toHaveBeenCalledWith({ embedding_base_url: "", embedding_dimensions: null, embedding_api_key: "new-index-key" });
  });
  it("skips unchanged chatbot connections instead of creating UI overrides", async () => {
    await mount(); await edit("#emb-url", "https://edited-original.invalid/v1"); await save();
    expect(mocks.saveModelConfig).toHaveBeenCalledTimes(1); expect(writes()).toHaveLength(0);
    expect(mocks.success).toHaveBeenCalledTimes(1);
  });
  it("retains failed chatbot drafts and keys for a successful retry after partial save", async () => {
    await mount(); await edit("#llm-model", "edited-original"); await edit('[aria-label="llm API Key"]', "retained-chat-key");
    failChatbot = true; await save();
    expect(container.textContent).toContain("Original model settings saved.");
    expect(container.querySelector('[role="alert"]')!.textContent).toContain("Chatbot connections could not be saved");
    expect(container.querySelector<HTMLInputElement>('[aria-label="llm API Key"]')!.value).toBe("retained-chat-key");
    expect(mocks.success).toHaveBeenCalledTimes(1); expect(saveButton().disabled).toBe(false);
    failChatbot = false; await save();
    expect(JSON.parse(writes()[1][1].body!).llm.api_key).toBe("retained-chat-key");
    expect(mocks.saveModelConfig).toHaveBeenCalledTimes(1);
    expect(mocks.success).toHaveBeenCalledTimes(2); expect(container.querySelector('[role="alert"]')).toBeNull();
  });
  it("saves chatbot changes even if original settings fail, then retries only the failed draft", async () => {
    await mount(); await edit("#llm-model", "edited-original"); await edit('[aria-label="Model"]', "saved-chat");
    mocks.saveModelConfig.mockRejectedValueOnce(new Error("database unavailable")); await save();
    expect(writes()).toHaveLength(1); expect(mocks.success).toHaveBeenCalledWith("Chatbot connections saved.");
    expect(container.querySelector('[role="alert"]')!.textContent).toContain("Original model settings could not be saved");
    expect(container.querySelector<HTMLInputElement>("#llm-model")!.value).toBe("edited-original");
    await save(); expect(writes()).toHaveLength(1); expect(mocks.saveModelConfig).toHaveBeenCalledTimes(2);
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });
  it("retains failed drafts even if the post-save identity refresh also fails", async () => {
    await mount(); await edit("#llm-model", "edited-original"); await edit('[aria-label="Model"]', "retained-model");
    const normalFetch = fetcher.getMockImplementation()!;
    let failRefresh = true;
    fetcher.mockImplementation((url, request) => request.method === "GET" && failRefresh
      ? Promise.resolve({ ok: false, json: async () => ({ error: { message: "Shared identity refresh failed" } }) })
      : normalFetch(url, request));
    failChatbot = true; await save();
    expect(container.querySelector<HTMLInputElement>('[aria-label="Model"]')!.value).toBe("retained-model");
    failRefresh = false;
    const retry = [...container.querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === "Retry")!;
    await act(async () => retry.click());
    expect(container.querySelector<HTMLInputElement>('[aria-label="Model"]')!.value).toBe("retained-model");
    failChatbot = false; await save();
    expect(JSON.parse(writes()[1][1].body!).llm.model).toBe("retained-model");
  });
  it("allows original settings to save while chatbot deployment settings are locked", async () => {
    saved.locked = true; await mount(); await edit("#llm-model", "edited-original"); expect(saveButton().disabled).toBe(false); await save();
    expect(mocks.saveModelConfig).toHaveBeenCalledTimes(1); expect(writes()).toHaveLength(0);
  });
  it("allows original saves while chatbot settings fail to load and optional saves after retry", async () => {
    const normalFetch = fetcher.getMockImplementation()!;
    let failLoad = true;
    fetcher.mockImplementation((url, request) => request.method === "GET" && failLoad
      ? Promise.resolve({ ok: false, json: async () => ({ error: { message: "Unable to load" } }) })
      : normalFetch(url, request));
    await mount(); expect(saveButton().disabled).toBe(true);
    await edit("#llm-model", "edited-original"); expect(saveButton().disabled).toBe(false); await save();
    expect(mocks.saveModelConfig).toHaveBeenCalledTimes(1); expect(writes()).toHaveLength(0);
    failLoad = false;
    const retry = [...container.querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === "Retry")!;
    await act(async () => retry.click()); await edit('[aria-label="Model"]', "edited-chat"); await save();
    expect(writes()).toHaveLength(1);
  });
  it("keeps chatbot changes editable, testable and saveable while an original write waits for extraction", async () => {
    let finish!: (value: unknown) => void;
    mocks.saveModelConfig.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    await mount(); await edit("#llm-model", "edited-original"); await edit('[aria-label="Model"]', "saved-chat"); await save();
    expect(saveButton().disabled).toBe(true); expect(container.querySelector("fieldset")!.disabled).toBe(true);
    expect(container.querySelectorAll("fieldset")[1].disabled).toBe(false);
    expect(writes()).toHaveLength(1); expect(container.textContent).toContain("Chatbot connections saved.");
    expect(container.textContent).toContain("Saving original model settings");
    const optionalTest = [...container.querySelectorAll<HTMLButtonElement>("button")].filter(button => button.textContent === "Test generation model").at(-1)!;
    expect(optionalTest.disabled).toBe(false);
    await edit('[aria-label="Model"]', "second-chat-change"); expect(saveButton().disabled).toBe(false); await save();
    expect(JSON.parse(writes()[1][1].body!).llm.model).toBe("second-chat-change");
    expect(mocks.saveModelConfig).toHaveBeenCalledTimes(1);
    await act(async () => finish({ config: { ...original, llm_model: "edited-original" } }));
    expect(saveButton().disabled).toBe(true); expect(container.querySelector("fieldset")!.disabled).toBe(false);
  });
  it("allows chatbot saves when original settings fail to load", async () => {
    mocks.getModelConfig.mockRejectedValueOnce(new Error("original load failed"));
    await mount(); await edit('[aria-label="Model"]', "edited-chat"); await save();
    expect(mocks.saveModelConfig).not.toHaveBeenCalled(); expect(writes()).toHaveLength(1);
  });
  it("initializes the chatbot baseline when an original save recovers its failed first load", async () => {
    fetcher.mockResolvedValueOnce({ ok: false, json: async () => ({ error: { message: "Unable to load" } }) });
    await mount(); await edit("#llm-model", "edited-original"); await save();
    expect(container.querySelector('[aria-label="Model"]')).toBeTruthy();
    await edit('[aria-label="Model"]', "edited-chat"); await save();
    expect(JSON.parse(writes()[0][1].body!).llm.model).toBe("edited-chat");
    expect(mocks.saveModelConfig).toHaveBeenCalledTimes(1);
  });
  it("omits deployment-locked original fields while saving editable embedding changes", async () => {
    mocks.getModelConfig.mockResolvedValueOnce({ ...original, locked_fields: ["llm_model"] });
    await mount(); await edit("#llm-model", "locked-draft"); expect(saveButton().disabled).toBe(true);
    await edit("#emb-url", "https://new-index.invalid/v1"); await save();
    expect(mocks.saveModelConfig).toHaveBeenCalledWith({ embedding_base_url: "https://new-index.invalid/v1" });
  });
  it("refreshes shared embedding identity after both independently saved drafts settle", async () => {
    let finish!: () => void;
    const normalFetch = fetcher.getMockImplementation()!;
    fetcher.mockImplementation((url, request) => request.method === "PUT"
      ? new Promise(resolve => { finish = () => resolve({ ok: true, json: async () => ({ config: structuredClone(initial) }) }); })
      : normalFetch(url, request));
    await mount(); await edit("#llm-model", "edited-original"); await edit('[aria-label="Model"]', "chat-model");
    await edit('[aria-label="embedding API Key"]', "new-key"); await save();
    saved.embedding.schema_dimensions = 7;
    await act(async () => window.dispatchEvent(new Event("sag:stock-model-saved")));
    expect(container.textContent).toContain("Schema dimensions: 7");
    await act(async () => finish());
    expect(container.textContent).toContain("Schema dimensions: 7");
  });
  it("reports a capability refresh failure as saved settings rather than a failed write", async () => {
    await mount(); await edit('[aria-label="Model"]', "edited-chat"); mocks.refreshCapabilities.mockRejectedValueOnce(new Error("refresh unavailable")); await save();
    expect(mocks.success).toHaveBeenCalledTimes(1);
    expect(container.querySelector('[role="alert"]')!.textContent).toContain("Settings saved, but model information could not refresh");
  });
});
