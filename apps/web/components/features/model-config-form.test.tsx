/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import english from "@/messages/en-US.json";
import { ModelConfigForm } from "./model-config-form";

const mocks = vi.hoisted(() => ({ translate: (key: string) => key === "testGeneration" ? "Test generation model" : key === "testing" ? "Testing…" : key,
  getModelConfig: vi.fn(), getModelProviders: vi.fn(), saveModelConfig: vi.fn(),
  getChatbotConfig: vi.fn(), saveChatbotConfig: vi.fn(), testChatbotConfig: vi.fn(),
  refreshCapabilities: vi.fn(), success: vi.fn(), order: [] as string[] }));
const chatbotTranslate = (key: string, values?: Record<string, string>) => {
  let text = english.ChatbotConfig[key as keyof typeof english.ChatbotConfig] ?? key;
  for (const [name, value] of Object.entries(values ?? {})) text = text.replace(`{${name}}`, value);
  return text;
};
vi.mock("next-intl", () => ({ useLocale: () => "en-US", useTranslations: (namespace: string) => namespace === "ChatbotConfig" ? chatbotTranslate : mocks.translate }));
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
  vi.clearAllMocks(); mocks.order = []; failChatbot = false; saved = structuredClone(initial);
  mocks.getModelConfig.mockResolvedValue(original);
  mocks.getModelProviders.mockResolvedValue([{ id: "openai", default_model: "m", temperature_configurable: true,
    default_temperature: 0.3, can_reuse_embedding_credentials: true }]);
  mocks.saveModelConfig.mockImplementation(async (patch) => {
    mocks.order.push("original"); return { config: { ...original, ...patch } };
  });
  mocks.refreshCapabilities.mockResolvedValue(undefined);
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

describe("one page-wide model Save", () => {
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
      [...section[target].querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === (target === "llm" ? "Test generation model" : "Test embedding connection"))!,
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
