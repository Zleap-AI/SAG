/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import english from "@/messages/en-US.json";
import { ChatbotConfigSections } from "./chatbot-config-sections";

vi.mock("@/components/features/app-shell", () => ({ useApp: () => ({ refreshCapabilities: vi.fn() }) }));
const chatbotTranslate = (key: string, values?: Record<string, string>) => {
  let text = english.ChatbotConfig[key as keyof typeof english.ChatbotConfig] ?? key;
  for (const [name, value] of Object.entries(values ?? {})) text = text.replace(`{${name}}`, value);
  return text;
};
const modelTranslate = (key: string) => key === "testGeneration" ? "Test generation model" : "Testing…";
vi.mock("next-intl", () => ({ useTranslations: (namespace: string) => namespace === "ChatbotConfig" ? chatbotTranslate : modelTranslate }));
vi.mock("@/lib/auth", () => ({ getToken: () => "test-token" }));
vi.mock("sonner", () => ({ toast: { success: vi.fn() } }));

const initial = {
  locked: false, encryption_configured: true,
  llm: { enabled: false, provider: "openai", model: "m", base_url: "", api_key_set: true, credential_source: "environment" },
  embedding: { enabled: false, model: "shared", base_url: "https://embed.invalid/v1", api_key_set: true, credential_source: "environment", schema_dimensions: 3, request_dimensions: null },
};
let container: HTMLDivElement;
let root: Root;
let fetcher: ReturnType<typeof vi.fn>;
async function mount(config = initial) {
  fetcher = vi.fn(async (url: string) => Response.json(url.endsWith("/model-providers")
    ? [{ id: "openai", display_name: "OpenAI-compatible" }] : { config }));
  vi.stubGlobal("fetch", fetcher);
  container = document.createElement("div"); document.body.append(container);
  root = createRoot(container);
  await act(async () => { root.render(<ChatbotConfigSections />); });
}
beforeEach(() => { (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true; });
afterEach(async () => { if (root) await act(async () => root.unmount()); container?.remove(); vi.unstubAllGlobals(); });

describe("native chatbot settings", () => {
  it("shows shared embedding identity and empty password inputs", async () => {
    await mount();
    expect(container.textContent).toContain("shared");
    expect(container.textContent).toContain("Schema dimensions: 3");
    expect(container.textContent).toContain("Request dimensions: omitted");
    const inputs = container.querySelectorAll<HTMLInputElement>('input[type="password"]');
    expect(inputs.length).toBe(2); expect([...inputs].every(input => input.value === "")).toBe(true);
    expect(fetcher.mock.calls[0][1].headers.Authorization).toBe("Bearer test-token");
  });
  it("deployment lock disables every control independently of stock locks", async () => {
    await mount({ ...initial, locked: true });
    const controls = [...container.querySelectorAll<HTMLInputElement | HTMLButtonElement | HTMLSelectElement>("input, button, select")];
    expect(controls.length).toBeGreaterThan(5);
    expect(controls.every(control => control.disabled)).toBe(true);
    expect(container.textContent).toContain("locked by the deployment");
  });
  it("offers optional API keys and tests a blank key without requiring encryption", async () => {
    await mount({ ...initial, encryption_configured: false,
      llm: { ...initial.llm, api_key_set: false }, embedding: { ...initial.embedding, api_key_set: false } });
    expect(container.textContent).toContain("Keyless connections can be saved without it");
    const keys = [...container.querySelectorAll<HTMLInputElement>('input[type="password"]')];
    expect(keys.every(input => !input.required && input.value === "")).toBe(true);
    expect(keys.every(input => input.placeholder === "Only if the endpoint requires authentication")).toBe(true);
    expect(keys.every(input => input.closest("label")!.textContent === "API Key (optional)")).toBe(true);
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Enable chatbot llm"]')!.click());
    fetcher.mockImplementation(async () => Response.json({ message: "Connection successful" }));
    const test = [...container.querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === "Test generation model")!;
    await act(async () => test.click());
    expect(JSON.parse(fetcher.mock.calls.at(-1)![1].body).llm.api_key).toBe("");
    expect(container.textContent).toContain("Connection successful");
  });
  it("tests the unsaved embedding connection without sending its model/dimensions", async () => {
    await mount();
    const toggle = container.querySelector<HTMLButtonElement>('[aria-label="Enable chatbot embedding"]')!;
    await act(async () => toggle.click());
    fetcher.mockImplementation(async () => Response.json({ ok: true, message: "Connection successful" }));
    const tests = [...container.querySelectorAll<HTMLButtonElement>("button")].filter(button => button.textContent?.startsWith("Test "));
    await act(async () => tests[1].click());
    const [url, request] = fetcher.mock.calls.at(-1)!;
    expect(url).toContain("/chatbot-config/test");
    expect(request.method).toBe("POST");
    const body = JSON.parse(request.body);
    expect(body.target).toBe("embedding"); expect(body.embedding.enabled).toBe(true);
    expect(body.embedding.api_key).toBe(""); expect(body.embedding.model).toBeUndefined();
    expect(body.embedding.schema_dimensions).toBeUndefined();
    expect(container.textContent).toContain("Connection successful");
  });
  it("uses the native provider catalog and official endpoint option", async () => {
    await mount();
    const provider = container.querySelector<HTMLSelectElement>('[aria-label="Chatbot provider"]')!;
    expect([...provider.options].map(option => option.value)).toEqual(["openai"]);
    expect(container.querySelector('[aria-label="Endpoint (blank for official endpoint)"]')).toBeTruthy();
  });
  it("explains missing encryption while retaining draft Test controls", async () => {
    await mount({ ...initial, encryption_configured: false });
    expect(container.textContent).toContain("Unsaved connections can still be tested");
    const button = [...container.querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === "Test generation model")!;
    expect(button.disabled).toBe(false);
  });
  it("refreshes shared identity after a stock save while retaining connection drafts", async () => {
    await mount();
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Enable chatbot llm"]')!.click());
    fetcher.mockImplementation(async () => Response.json({
      config: { ...initial, embedding: { ...initial.embedding, model: "updated-model", schema_dimensions: 8 } },
    }));
    await act(async () => window.dispatchEvent(new Event("sag:stock-model-saved")));
    expect(container.textContent).toContain("updated-model");
    expect(container.textContent).toContain("Schema dimensions: 8");
    expect(container.querySelector('[aria-label="Enable chatbot llm"]')!.getAttribute("aria-checked")).toBe("true");
  });
});
