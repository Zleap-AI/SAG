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
    key.startsWith("responses") || ["saveCard", "cardSaved", "cardSaveFailed", "modelRequired", "dimensionsInvalid", "refreshFailed"].includes(key) || key === "separateEmbeddingAddress" || key === "embeddingInheritedAddressIndependentKey"
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
function saveButton(target?: string): HTMLButtonElement {
  if (target) return container.querySelector<HTMLButtonElement>(`[aria-label="Save ${target}"]`)!;
  const buttons = [...container.querySelectorAll<HTMLButtonElement>('button[aria-label^="Save "]')];
  return buttons.find(button => !button.disabled) ?? saveButton("embeddingTitle");
}
async function save(target?: string) { await act(async () => saveButton(target).click()); }
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
  jev_endpoint: null, jev_model: "jev-latest", jev_api_key_set: false, jev_ranking_timeout_seconds: 30, jev_failure_policy: "error",
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

describe("provider preset compatibility", () => {
  it.each([
    ["qwen3.6-flash", "native-model"],
    ["qwen3.8-flash", "native-model"],
    ["team-custom-model", "team-custom-model"],
  ])("keeps %s saved until a provider switch, then uses %s", async (savedModel, expectedModel) => {
    const persisted = { ...embeddingConfig, llm_model: savedModel };
    mocks.getModelConfig.mockResolvedValue(persisted);
    mocks.getModelProviders.mockResolvedValue(embeddingProviders.map(provider =>
      provider.id === "openai" ? { ...provider, default_model: "qwen3.8-flash" } : provider));
    await mount();
    expect(container.querySelector<HTMLInputElement>("#llm-model")!.value).toBe(savedModel);
    expect(mocks.saveModelConfig).not.toHaveBeenCalled();
    const scrollIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => undefined });
    try {
      await act(async () => container.querySelector("#llm-provider")!.dispatchEvent(
        new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }),
      ));
      const option = [...document.querySelectorAll<HTMLElement>('[role="option"]')]
        .find(node => node.textContent === "anthropic")!;
      await act(async () => option.click());
    } finally {
      if (scrollIntoView) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", scrollIntoView);
      else delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
    }
    expect(container.querySelector<HTMLInputElement>("#llm-model")!.value).toBe(expectedModel);
    expect(mocks.saveModelConfig).not.toHaveBeenCalled();
  });
});

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

  it("uses the saved generation address until its independent save succeeds", async () => {
    const config = { ...embeddingConfig, embedding_base_url: null };
    mocks.getModelConfig.mockResolvedValue(config);
    mocks.saveModelConfig.mockImplementation(async patch => ({ config: { ...config, ...patch } }));
    await mount();
    await edit("#llm-model", "different-model");
    expect(fieldDescription("#emb-url")).toContain("Currently used: https://llm.invalid/v1 (generation model URL)");
    await edit("#llm-model", config.llm_model);
    await edit("#llm-url", "  https://new-generation.invalid/v1  ");
    expect(fieldDescription("#emb-url")).toContain("Currently used: https://llm.invalid/v1 (generation model URL)");
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

  it("inherits a new generation key only after its independent save", async () => {
    mocks.getModelConfig.mockResolvedValue({ ...embeddingConfig, embedding_base_url: null,
      llm_api_key_set: false, embedding_api_key_set: false });
    await mount();
    expect(fieldDescription("#emb-key")).toContain("No embedding key is configured. Configure a separate key.");
    await edit("#llm-key", "generation-secret-draft");
    expect(fieldDescription("#emb-key")).toContain("No embedding key is configured. Configure a separate key.");
    expect(fieldDescription("#emb-key")).not.toContain("generation-secret-draft");
    expect(container.textContent).not.toContain("generation-secret-draft");
    expect(fieldDescription("#emb-url")).toContain("Currently used: https://llm.invalid/v1 (generation model URL)");
    await save();
    expect(mocks.saveModelConfig).toHaveBeenCalledWith({ llm_api_key: "generation-secret-draft" });
    expect(fieldDescription("#emb-key")).toContain("Uses the generation model key.");
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
    expect(fieldDescription("#emb-url")).toContain("Currently used: SDK default URL");
    expect(fieldDescription("#emb-key")).toContain("No embedding key is configured");
    await save();
    expect(mocks.saveModelConfig).toHaveBeenCalledWith({ llm_provider: "openai" });
    expect(fieldDescription("#emb-key")).toContain("Uses the generation model key.");
  });

  it("requires a replacement generation key for embedding inheritance after leaving Responses", async () => {
    mocks.getModelConfig.mockResolvedValue({ ...embeddingConfig, llm_provider: "responses",
      llm_responses_provider: "openai", llm_responses_endpoint: "https://responses.invalid/v1/responses",
      llm_responses_api_version: null, embedding_base_url: null, embedding_api_key_set: false });
    await mount();
    await selectFormat("openai");
    expect(fieldDescription("#emb-key")).toContain("No embedding key is configured. Configure a separate key.");
    await edit("#llm-key", "new-chat-completions-key");
    expect(fieldDescription("#emb-key")).toContain("No embedding key is configured. Configure a separate key.");
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
    expect(container.querySelector<HTMLInputElement>("#emb-url")!.placeholder).toBe("Leave blank to use default URL");
    expect(fieldDescription("#emb-url")).toContain("Currently used: SDK default URL.");
    await edit("#llm-url", "   ");
    expect(container.querySelector<HTMLInputElement>("#emb-url")!.placeholder).toBe("Leave blank to use default URL");
    expect(fieldDescription("#emb-url")).toContain("Currently used: SDK default URL.");
  });

  it("localizes the inherited saved endpoint while generation has an unsaved draft", async () => {
    modelMessages = chinese.ModelConfig;
    mocks.getModelConfig.mockResolvedValue({ ...embeddingConfig, embedding_base_url: null });
    await mount();
    expect(container.querySelector<HTMLInputElement>("#emb-url")!.placeholder).toBe("留空使用生成模型地址");
    expect(fieldDescription("#emb-url")).toContain("当前使用：https://llm.invalid/v1（来自生成模型地址）");
    expect(fieldDescription("#emb-url")).toContain("将使用生成模型地址，仍使用独立向量密钥。如向量模型使用其他服务，请填写对应的服务地址。");
    await edit("#llm-url", "https://changed-generation.invalid/v1");
    expect(fieldDescription("#emb-url")).toContain("当前使用：https://llm.invalid/v1（来自生成模型地址）");
    await edit("#llm-url", "   ");
    expect(container.querySelector<HTMLInputElement>("#emb-url")!.placeholder).toBe("留空使用生成模型地址");
    expect(fieldDescription("#emb-url")).toContain("当前使用：https://llm.invalid/v1（来自生成模型地址）");
  });
});

describe("generation connection tests", () => {
  it.each(["openai", "responses"] as const)("tests only the %s generation draft while other sections are unfinished", async provider => {
    mocks.getModelConfig.mockResolvedValue({ ...embeddingConfig, llm_provider: provider,
      llm_responses_provider: "azure", llm_responses_endpoint: "https://saved.invalid/openai/responses",
      llm_responses_api_version: "2025-04-01-preview", llm_responses_send_temperature: true });
    mocks.getModelProviders.mockResolvedValue(embeddingProviders);
    await mount();
    await edit("#llm-model", "  qwen3.8-flash  ");
    await edit("#llm-maxtok", "20000");
    await edit("#llm-key", "  synthetic-generation-key  ");
    if (provider === "responses") {
      await edit("#original-responses-endpoint", "  https://draft.invalid/openai/responses  ");
    } else {
      await edit("#llm-url", "  https://api.302ai.cn/v1  ");
    }
    await edit("#emb-model", "");
    await edit("#emb-dims", "0");
    await edit("#emb-key", "synthetic-embedding-key");
    await edit("#mineru-key", "synthetic-mineru-key");
    const button = [...container.querySelectorAll<HTMLButtonElement>("button")]
      .find(button => button.textContent === "Test generation model")!;
    await act(async () => button.click());
    expect(mocks.testModelConfig).toHaveBeenCalledExactlyOnceWith({
      llm_provider: provider, llm_model: "qwen3.8-flash", llm_api_key: "synthetic-generation-key",
      llm_base_url: provider === "responses" ? "https://llm.invalid/v1" : "https://api.302ai.cn/v1",
      llm_temperature: 0.3, llm_max_tokens: 20000, llm_context_window: 128000,
      llm_timeout_ms: 60000, llm_max_retries: 2,
      ...(provider === "responses" ? {
        llm_responses_provider: "azure", llm_responses_endpoint: "https://draft.invalid/openai/responses",
        llm_responses_api_version: "2025-04-01-preview", llm_responses_send_temperature: true,
      } : {}),
    });
    expect(button.closest("section")!.textContent).toContain("Draft connected");
    expect(container.querySelector<HTMLInputElement>("#emb-model")!.value).toBe("");
    expect(container.querySelector<HTMLInputElement>("#emb-dims")!.value).toBe("0");
    expect(container.querySelector<HTMLInputElement>("#emb-key")!.value).toBe("synthetic-embedding-key");
    expect(container.querySelector<HTMLInputElement>("#mineru-key")!.value).toBe("synthetic-mineru-key");
    expect(mocks.saveModelConfig).not.toHaveBeenCalled();
    expect(writes()).toHaveLength(0);
  });
});

describe("independent model cards", () => {
  it("renders five card-owned save actions and four connection tests", async () => {
    await mount();
    const buttons = [...container.querySelectorAll<HTMLButtonElement>('button[aria-label^="Save "]')];
    expect(buttons.map(button => button.getAttribute("aria-label"))).toEqual([
      "Save generationTitle", "Save embeddingTitle", "Save parserTitle",
      "Save Chatbot LLM (optional)", "Save Query embedding (optional)",
    ]);
    expect(buttons.every(button => button.closest("section") && button.disabled)).toBe(true);
    expect([...container.querySelectorAll("section button")].filter(button => button.textContent?.startsWith("Test "))).toHaveLength(4);
  });

  it.each([
    ["generationTitle", { llm_model: "new-generation", llm_api_key: "generation-key" }, "#llm-key", "#emb-key", "embedding-key"],
    ["embeddingTitle", { embedding_model: "new-embedding", embedding_dimensions: null, embedding_api_key: "embedding-key" }, "#emb-key", "#mineru-key", "parser-key"],
    ["parserTitle", { mineru_api_key: "parser-key" }, "#mineru-key", "#llm-key", "generation-key"],
  ] as const)("saves %s without submitting or clearing sibling drafts", async (target, patch, cleared, retained, secret) => {
    await mount();
    await edit("#llm-model", "new-generation"); await edit("#llm-key", "generation-key");
    await edit("#emb-model", "new-embedding"); await edit("#emb-dims", ""); await edit("#emb-key", "embedding-key");
    await edit("#mineru-key", "parser-key");
    await edit('[aria-label="llm API Key"]', "chat-key");
    await save(target);
    expect(mocks.saveModelConfig).toHaveBeenCalledExactlyOnceWith(patch);
    expect(container.querySelector<HTMLInputElement>(cleared)!.value).toBe("");
    expect(container.querySelector<HTMLInputElement>(retained)!.value).toBe(secret);
    expect(container.querySelector<HTMLInputElement>('[aria-label="llm API Key"]')!.value).toBe("chat-key");
    expect(writes()).toHaveLength(0);
    expect(saveButton(target).disabled).toBe(true);
  });

  it.each(["generationTitle", "embeddingTitle", "parserTitle"])("keeps failed %s drafts and keys for retry", async target => {
    await mount();
    await edit("#llm-key", "generation-key"); await edit("#emb-key", "embedding-key"); await edit("#mineru-key", "parser-key");
    mocks.saveModelConfig.mockRejectedValueOnce(new Error("database unavailable"));
    await save(target);
    expect(saveButton(target).closest("section")!.querySelector('[role="alert"]')!.textContent).toContain("database unavailable");
    expect(["#llm-key", "#emb-key", "#mineru-key"].map(selector => container.querySelector<HTMLInputElement>(selector)!.value))
      .toEqual(["generation-key", "embedding-key", "parser-key"]);
    expect(saveButton(target).disabled).toBe(false);
    await save(target);
    expect(mocks.saveModelConfig).toHaveBeenCalledTimes(2);
    expect(saveButton(target).closest("section")!.querySelector('[role="alert"]')).toBeNull();
  });

  it("allows concurrent generation and embedding saves without restoring stale sibling config", async () => {
    let finishGeneration!: (value: unknown) => void;
    mocks.saveModelConfig.mockImplementationOnce(() => new Promise(resolve => { finishGeneration = resolve; }));
    await mount(); await edit("#llm-model", "saved-generation"); await edit("#llm-key", "generation-key");
    await edit("#emb-model", "saved-embedding"); await edit("#emb-key", "embedding-key");
    await save("generationTitle");
    expect(container.querySelector("#llm-model")!.matches(":disabled")).toBe(true);
    expect(container.querySelector("#emb-model")!.matches(":disabled")).toBe(false);
    expect(saveButton("embeddingTitle").disabled).toBe(false);
    await save("embeddingTitle");
    await edit("#emb-model", "newer-embedding-draft");
    await edit("#emb-key", "newer-embedding-key");
    await act(async () => finishGeneration({ config: { ...original, llm_model: "saved-generation", llm_api_key_set: true } }));
    expect(container.querySelector<HTMLInputElement>("#emb-model")!.value).toBe("newer-embedding-draft");
    expect(container.querySelector<HTMLInputElement>("#emb-key")!.value).toBe("newer-embedding-key");
    await edit("#emb-model", "saved-embedding"); await edit("#emb-key", "");
    expect(saveButton("embeddingTitle").disabled).toBe(true);
  });

  it.each([
    ["", "3", "Enter a model name"], ["embedding", "0", "whole number"],
    ["embedding", "2.5", "whole number"], ["embedding", "8193", "whole number"],
  ])("locates invalid embedding model=%s dimensions=%s without a generic API failure", async (model, dimensions, message) => {
    await mount(); await edit("#emb-model", model); await edit("#emb-dims", dimensions);
    await save("embeddingTitle");
    const section = saveButton("embeddingTitle").closest("section")!;
    expect(section.querySelector('[role="alert"]')!.textContent).toContain(message);
    expect(document.activeElement?.id).toBe(model ? "emb-dims" : "emb-model");
    const test = [...section.querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === "Test embedding model")!;
    await act(async () => test.click());
    expect(section.querySelector('[role="status"]')!.textContent).toContain(message);
    expect(mocks.testEmbeddingModelConfig).not.toHaveBeenCalled(); expect(mocks.saveModelConfig).not.toHaveBeenCalled();
  });

  it("preserves deployment locks for Responses fields while allowing embedding saves", async () => {
    mocks.getModelConfig.mockResolvedValue({ ...embeddingConfig, llm_provider: "responses", locked_fields: ["llm_responses_endpoint"] });
    mocks.getModelProviders.mockResolvedValue(embeddingProviders);
    await mount();
    expect(container.querySelector<HTMLInputElement>("#original-responses-endpoint")!.disabled).toBe(true);
    await edit("#emb-url", "https://new-embedding.invalid/v1"); await save("embeddingTitle");
    expect(mocks.saveModelConfig).toHaveBeenCalledExactlyOnceWith({ embedding_base_url: "https://new-embedding.invalid/v1" });
  });
  it("saves generation alone while preserving invalid embedding and parser secret drafts", async () => {
    await mount();
    await edit("#llm-model", "new-generation");
    await edit("#llm-key", "synthetic-generation-key");
    await edit("#emb-model", "");
    await edit("#emb-dims", "0");
    await edit("#emb-key", "synthetic-embedding-key");
    await edit("#mineru-key", "synthetic-parser-key");
    const button = container.querySelector<HTMLButtonElement>('[aria-label="Save generationTitle"]');
    expect(button).not.toBeNull();
    await act(async () => button!.click());
    expect(mocks.saveModelConfig).toHaveBeenCalledExactlyOnceWith({
      llm_model: "new-generation", llm_api_key: "synthetic-generation-key",
    });
    expect(container.querySelector<HTMLInputElement>("#llm-key")!.value).toBe("");
    expect(container.querySelector<HTMLInputElement>("#emb-model")!.value).toBe("");
    expect(container.querySelector<HTMLInputElement>("#emb-dims")!.value).toBe("0");
    expect(container.querySelector<HTMLInputElement>("#emb-key")!.value).toBe("synthetic-embedding-key");
    expect(container.querySelector<HTMLInputElement>("#mineru-key")!.value).toBe("synthetic-parser-key");
  });

  it("tests embedding without sending unsaved generation credentials or unrelated invalid fields", async () => {
    await mount();
    await edit("#llm-model", "");
    await edit("#llm-url", "https://unsaved-generation.invalid/v1");
    await edit("#llm-key", "synthetic-generation-secret");
    await edit("#emb-model", "draft-embedding");
    await edit("#emb-url", "");
    await edit("#emb-key", "synthetic-embedding-secret");
    const button = [...container.querySelectorAll<HTMLButtonElement>("button")]
      .find(node => node.textContent === "Test embedding model")!;
    await act(async () => button.click());
    expect(mocks.testEmbeddingModelConfig).toHaveBeenCalledExactlyOnceWith({
      embedding_model: "draft-embedding", embedding_base_url: "",
      embedding_api_key: "synthetic-embedding-secret",
    });
  });

  it.each([
    ["unchanged schema dimension", 1024, null, {}],
    ["changed dimension", 1024, "2048", { embedding_dimensions: 2048 }],
    ["explicitly cleared dimension", 1024, "", { embedding_dimensions: null }],
    ["unchanged model-default dimension", null, null, {}],
  ] as const)("tests embedding with %s while preserving the saved request policy", async (_name, savedDimensions, draftDimensions, expectedDimensions) => {
    mocks.getModelConfig.mockResolvedValue({ ...original, embedding_dimensions: savedDimensions });
    await mount();
    await edit("#emb-model", "draft-embedding");
    await edit("#emb-url", "https://draft-embedding.invalid/v1");
    await edit("#emb-key", "synthetic-embedding-key");
    if (draftDimensions !== null) await edit("#emb-dims", draftDimensions);
    const button = [...container.querySelectorAll<HTMLButtonElement>("button")]
      .find(node => node.textContent === "Test embedding model")!;
    await act(async () => button.click());
    expect(mocks.testEmbeddingModelConfig).toHaveBeenCalledExactlyOnceWith({
      embedding_model: "draft-embedding", embedding_base_url: "https://draft-embedding.invalid/v1",
      embedding_api_key: "synthetic-embedding-key", ...expectedDimensions,
    });
  });
});

describe("individual card format compatibility", () => {
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
    expect(draft.llm_provider).toBeUndefined();
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

});
