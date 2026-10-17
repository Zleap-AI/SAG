/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import english from "@/messages/en-US.json";
import { clientErrorMessage } from "@/i18n/client-errors";
import { ChatbotConfigSections } from "./chatbot-config-sections";

vi.mock("@/components/features/app-shell", () => ({ useApp: () => ({ refreshCapabilities: vi.fn() }) }));
const chatbotTranslate = (key: string, values?: Record<string, string>) => {
  let text = english.ChatbotConfig[key as keyof typeof english.ChatbotConfig] ?? key;
  for (const [name, value] of Object.entries(values ?? {})) text = text.replace(`{${name}}`, value);
  return text;
};
const modelTranslate = (key: string, values?: Record<string, string>) => {
  const message = english.ModelConfig[key as keyof typeof english.ModelConfig];
  let text = typeof message === "string" ? message : key;
  for (const [name, value] of Object.entries(values ?? {})) text = text.replace(`{${name}}`, value);
  return text;
};
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
async function mount(config = initial, providers = [{ id: "openai", display_name: "OpenAI-compatible" }]) {
  fetcher = vi.fn(async (url: string) => Response.json(url.endsWith("/model-providers")
    ? providers : { config }));
  vi.stubGlobal("fetch", fetcher);
  container = document.createElement("div"); document.body.append(container);
  root = createRoot(container);
  await act(async () => { root.render(<ChatbotConfigSections />); });
}
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});
afterEach(async () => { if (root) await act(async () => root.unmount()); container?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function select(selector: string, label: string) {
  const scrollIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => undefined });
  try {
    await act(async () => container.querySelector(selector)!.dispatchEvent(
      new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }),
    ));
    const options = [...document.querySelectorAll<HTMLElement>('[role="option"]')];
    const option = options.find(node => node.textContent === label)!;
    await act(async () => option.click());
    return options.map(node => node.textContent);
  } finally {
    if (scrollIntoView) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", scrollIntoView);
    else delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
  }
}

async function edit(selector: string, value: string) {
  await act(async () => {
    const input = container.querySelector<HTMLInputElement>(selector)!;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
const saveButton = (target: "llm" | "embedding") => container.querySelector<HTMLButtonElement>(
  `[aria-label="Save ${target === "llm" ? "Chatbot LLM (optional)" : "Query embedding (optional)"}"]`,
)!;
const testButton = (target: "llm" | "embedding") => [...saveButton(target).closest("section")!.querySelectorAll<HTMLButtonElement>("button")]
  .find(button => button.textContent === (target === "llm" ? "Test generation model" : "Test embedding model"))!;
async function enable(target: "llm" | "embedding") {
  await act(async () => container.querySelector<HTMLButtonElement>(`[aria-label="Enable chatbot ${target}"]`)!.click());
}

describe("native chatbot settings", () => {
  it.each(["focus", "visible"] as const)("refreshes shared metadata on %s without discarding either card's drafts", async trigger => {
    await mount();
    await enable("llm"); await enable("embedding");
    await edit("#chatbot-llm-model", "draft-chat-model");
    await edit("#chatbot-llm-base_url", "https://draft-chat.invalid/v1");
    await edit("#chatbot-llm-api-key", "synthetic-chat-key");
    await edit("#chatbot-embedding-base_url", "https://draft-query.invalid/v1");
    await edit("#chatbot-embedding-api-key", "synthetic-query-key");
    const inputs = () => [...container.querySelectorAll<HTMLInputElement>("input")].map(input => [input.id, input.value]);
    const before = inputs();
    const initialRequests = fetcher.mock.calls.length;
    fetcher.mockImplementation(async () => Response.json({ config: { ...initial,
      embedding: { ...initial.embedding, model: "updated-shared-model", schema_dimensions: 1024, request_dimensions: 512 },
    } }));
    await act(async () => trigger === "focus"
      ? window.dispatchEvent(new Event("focus")) : document.dispatchEvent(new Event("visibilitychange")));
    expect(fetcher.mock.calls).toHaveLength(initialRequests + 1);
    expect(container.textContent).toContain("updated-shared-model");
    expect(container.textContent).toContain("Schema dimensions: 1024");
    expect(container.textContent).toContain("Request dimensions: 512");
    expect(inputs()).toEqual(before);
    expect(container.querySelector('[aria-label="Enable chatbot llm"]')!.getAttribute("aria-checked")).toBe("true");
    expect(container.querySelector('[aria-label="Enable chatbot embedding"]')!.getAttribute("aria-checked")).toBe("true");
  });

  it("skips refreshes while hidden and removes lifecycle listeners on unmount", async () => {
    await mount();
    const originalRequests = fetcher.mock.calls.length;
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      document.dispatchEvent(new Event("visibilitychange"));
      window.dispatchEvent(new Event("sag:stock-model-saved"));
    });
    expect(fetcher.mock.calls).toHaveLength(originalRequests);
    visibility.mockReturnValue("visible");
    await act(async () => document.dispatchEvent(new Event("visibilitychange")));
    expect(fetcher.mock.calls).toHaveLength(originalRequests + 1);
    await act(async () => root.unmount());
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      document.dispatchEvent(new Event("visibilitychange"));
      window.dispatchEvent(new Event("sag:stock-model-saved"));
    });
    expect(fetcher.mock.calls).toHaveLength(originalRequests + 1);
    visibility.mockRestore();
  });

  it.each([
    { model: "changed-model" }, { schema_dimensions: 1024 }, { request_dimensions: 16 },
  ])("clears only the embedding test result when shared identity changes by %o", async changed => {
    await mount();
    await enable("llm"); await enable("embedding");
    fetcher.mockImplementation(async (_url: string, request: RequestInit) => Response.json({
      ok: true, message: JSON.parse(String(request.body)).target === "llm" ? "Chat connected" : "Query connected",
    }));
    await act(async () => testButton("llm").click());
    await act(async () => testButton("embedding").click());
    expect(container.textContent).toContain("Query connected");
    fetcher.mockImplementation(async () => Response.json({ config: { ...initial,
      embedding: { ...initial.embedding, ...changed },
    } }));
    await act(async () => window.dispatchEvent(new Event("sag:stock-model-saved")));
    expect(container.textContent).not.toContain("Query connected");
    expect(container.textContent).toContain("Chat connected");
  });

  it("retains a successful embedding test when refreshed shared identity is unchanged", async () => {
    await mount(); await enable("embedding");
    fetcher.mockImplementationOnce(async () => Response.json({ ok: true, message: "Query connected" }));
    await act(async () => testButton("embedding").click());
    await act(async () => window.dispatchEvent(new Event("sag:stock-model-saved")));
    expect(container.textContent).toContain("Query connected");
  });

  it.each(["success", "failure"] as const)("ignores an embedding test %s from before a shared identity refresh", async outcome => {
    await mount(); await enable("embedding");
    let finish!: (response: Response) => void;
    fetcher.mockImplementationOnce(() => new Promise<Response>(resolve => { finish = resolve; }));
    const button = testButton("embedding");
    await act(async () => button.click());
    fetcher.mockImplementationOnce(async () => Response.json({ config: { ...initial,
      embedding: { ...initial.embedding, schema_dimensions: 1024 },
    } }));
    await act(async () => window.dispatchEvent(new Event("sag:stock-model-saved")));
    expect(button.disabled).toBe(true);
    expect(container.querySelector<HTMLInputElement>("#chatbot-embedding-api-key")!.disabled).toBe(true);
    await act(async () => finish(outcome === "success"
      ? Response.json({ ok: true, message: "Stale query connected" })
      : Response.json({ error: { message: "Stale query failure" } }, { status: 503 })));
    expect(button.closest("section")!.querySelector('[role="status"]')!.textContent).toBe("");
    expect(button.disabled).toBe(false);
    expect(container.textContent).toContain("Schema dimensions: 1024");
  });

  it.each(["llm", "embedding"] as const)("allows saving a disabled %s connection and its draft configuration", async target => {
    await mount({ ...initial, [target]: { ...initial[target], enabled: true } });
    await act(async () => container.querySelector<HTMLButtonElement>(`[aria-label="Enable chatbot ${target}"]`)!.click());
    expect(testButton(target).disabled).toBe(true);
    const key = container.querySelector<HTMLInputElement>(`#chatbot-${target}-api-key`)!;
    expect(key.disabled).toBe(false);
    await edit(`#chatbot-${target}-api-key`, "synthetic-disabled-key");
    fetcher.mockImplementationOnce(async () => Response.json({ config: initial }));
    await act(async () => saveButton(target).click());
    const body = JSON.parse(fetcher.mock.calls.at(-1)![1].body);
    expect(body[target].enabled).toBe(false);
    expect(body[target].api_key).toBe("synthetic-disabled-key");
  });

  it.each(["llm", "embedding"] as const)("requires enabling the optional %s connection before testing while preserving draft saves", async target => {
    await mount();
    const button = testButton(target);
    expect(button.disabled).toBe(true);
    expect(button.closest("section")!.textContent).toContain(target === "llm"
      ? "When disabled, uses the saved generation model above" : "When disabled, uses the saved embedding connection above");
    const before = fetcher.mock.calls.length;
    await act(async () => button.click());
    expect(fetcher.mock.calls).toHaveLength(before);
    await edit(`#chatbot-${target}-api-key`, "synthetic-draft-key");
    expect(saveButton(target).disabled).toBe(false);
    fetcher.mockImplementationOnce(async () => Response.json({ config: initial }));
    await act(async () => saveButton(target).click());
    expect(JSON.parse(fetcher.mock.calls.at(-1)![1].body)[target].enabled).toBe(false);
    await enable(target);
    expect(button.disabled).toBe(false);
    fetcher.mockImplementationOnce(async () => Response.json({ ok: true, message: "Connected" }));
    await act(async () => button.click());
    expect(JSON.parse(fetcher.mock.calls.at(-1)![1].body)[target].enabled).toBe(true);
  });
  it.each(["llm", "embedding"] as const)("saves %s while the other connection is invalid and retains failed draft keys for retry", async target => {
    await mount();
    await edit("#chatbot-llm-model", "");
    if (target === "embedding") await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Enable chatbot llm"]')!.click());
    await edit("#chatbot-llm-api-key", "chat-key");
    await edit("#chatbot-embedding-api-key", "query-key");
    await edit("#chatbot-embedding-base_url", "https://new-query.invalid/v1");
    fetcher.mockImplementationOnce(async () => Response.json({ error: { message: "Database unavailable" } }, { status: 503 }));
    await act(async () => saveButton(target).click());
    const section = saveButton(target).closest("section")!;
    expect(section.querySelector('[role="alert"]')!.textContent).toContain("Database unavailable");
    expect(container.querySelector<HTMLInputElement>("#chatbot-llm-api-key")!.value).toBe("chat-key");
    expect(container.querySelector<HTMLInputElement>("#chatbot-embedding-api-key")!.value).toBe("query-key");
    fetcher.mockImplementationOnce(async () => Response.json({ config: { ...initial,
      llm: { ...initial.llm, model: "" }, embedding: { ...initial.embedding, base_url: "https://new-query.invalid/v1" },
    } }));
    await act(async () => saveButton(target).click());
    const body = JSON.parse(fetcher.mock.calls.at(-1)![1].body);
    expect(Object.keys(body)).toEqual([target]);
    expect(body[target].api_key).toBe(target === "llm" ? "chat-key" : "query-key");
    if (target === "embedding") {
      expect(body.embedding).toEqual({ enabled: false, base_url: "https://new-query.invalid/v1", api_key: "query-key" });
      expect(container.querySelector<HTMLInputElement>("#chatbot-llm-model")!.value).toBe("");
    }
    expect(container.querySelector<HTMLInputElement>(`#chatbot-${target}-api-key`)!.value).toBe("");
    expect(section.querySelector('[role="alert"]')).toBeNull();
  });

  it("locates an empty enabled chatbot model while still allowing the query card to save", async () => {
    await mount();
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Enable chatbot llm"]')!.click());
    await edit("#chatbot-llm-model", "   ");
    const originalRequests = fetcher.mock.calls.length;
    await act(async () => saveButton("llm").click());
    expect(saveButton("llm").closest("section")!.querySelector('[role="alert"]')!.textContent).toContain("Enter a model name");
    expect(document.activeElement?.id).toBe("chatbot-llm-model");
    expect(fetcher.mock.calls).toHaveLength(originalRequests);
    await edit("#chatbot-embedding-api-key", "query-key");
    fetcher.mockImplementationOnce(async () => Response.json({ config: initial }));
    await act(async () => saveButton("embedding").click());
    expect(Object.keys(JSON.parse(fetcher.mock.calls.at(-1)![1].body))).toEqual(["embedding"]);
    expect(container.querySelector<HTMLInputElement>("#chatbot-llm-model")!.value).toBe("   ");
  });

  it.each(["llm", "embedding"] as const)("allows an independent sibling test while %s is testing", async target => {
    await mount();
    await enable("llm"); await enable("embedding");
    await edit("#chatbot-llm-api-key", "chat-key"); await edit("#chatbot-embedding-api-key", "query-key");
    const other = target === "llm" ? "embedding" : "llm";
    const buttons = Object.fromEntries((["llm", "embedding"] as const).map(name => [name,
      [...saveButton(name).closest("section")!.querySelectorAll<HTMLButtonElement>("button")]
        .find(button => button.textContent?.startsWith("Test "))!,
    ])) as Record<"llm" | "embedding", HTMLButtonElement>;
    const finishes: Array<(response: Response) => void> = [];
    fetcher.mockImplementation(() => new Promise<Response>(resolve => { finishes.push(resolve); }));
    await act(async () => buttons[target].click());
    expect(buttons[target].disabled).toBe(true); expect(buttons[other].disabled).toBe(false);
    expect(saveButton(other).disabled).toBe(false);
    await act(async () => buttons[other].click());
    const bodies = fetcher.mock.calls.slice(-2).map(([, request]) => JSON.parse(request.body));
    expect(bodies.map(body => Object.keys(body))).toEqual([[target, "target"], [other, "target"]]);
    await act(async () => finishes[0](Response.json({ ok: true, message: "Connected" })));
    expect(buttons[target].disabled).toBe(false); expect(buttons[other].disabled).toBe(true);
    await act(async () => finishes[1](Response.json({ ok: false, message: "Unavailable" })));
    expect(buttons[other].disabled).toBe(false);
    expect(container.querySelector<HTMLInputElement>("#chatbot-llm-api-key")!.value).toBe("chat-key");
    expect(container.querySelector<HTMLInputElement>("#chatbot-embedding-api-key")!.value).toBe("query-key");
  });

  it.each(["llm", "embedding"] as const)("keeps the sibling card editable, testable and saveable during a %s save", async target => {
    await mount();
    await enable("llm"); await enable("embedding");
    const other = target === "llm" ? "embedding" : "llm";
    await edit(`#chatbot-${target}-api-key`, "saving-key");
    let finish!: (response: Response) => void;
    fetcher.mockImplementationOnce(() => new Promise<Response>(resolve => { finish = resolve; }));
    await act(async () => saveButton(target).click());
    expect(container.querySelector<HTMLInputElement>(`#chatbot-${target}-api-key`)!.disabled).toBe(true);
    expect(container.querySelector<HTMLInputElement>(`#chatbot-${other}-api-key`)!.disabled).toBe(false);
    await edit(`#chatbot-${other}-api-key`, "other-key");
    expect(saveButton(other).disabled).toBe(false);
    const otherTest = [...saveButton(other).closest("section")!.querySelectorAll<HTMLButtonElement>("button")]
      .find(button => button.textContent?.startsWith("Test "))!;
    expect(otherTest.disabled).toBe(false);
    fetcher.mockImplementationOnce(async () => Response.json({ config: initial }));
    await act(async () => saveButton(other).click());
    expect(Object.keys(JSON.parse(fetcher.mock.calls.at(-1)![1].body))).toEqual([other]);
    await edit(`#chatbot-${other}-api-key`, "newer-other-draft");
    await act(async () => finish(Response.json({ config: initial })));
    expect(container.querySelector<HTMLInputElement>(`#chatbot-${other}-api-key`)!.value).toBe("newer-other-draft");
    expect(container.querySelector<HTMLInputElement>(`#chatbot-${target}-api-key`)!.value).toBe("");
  });

  it("keeps the latest shared vector identity when refresh responses arrive out of order", async () => {
    await mount();
    await edit("#chatbot-embedding-api-key", "query-draft-key");
    const finishes: Array<(response: Response) => void> = [];
    fetcher.mockImplementation(() => new Promise<Response>(resolve => { finishes.push(resolve); }));
    await act(async () => window.dispatchEvent(new Event("sag:stock-model-saved")));
    await act(async () => window.dispatchEvent(new Event("sag:stock-model-saved")));
    await act(async () => finishes[1](Response.json({ config: { ...initial,
      embedding: { ...initial.embedding, model: "latest-model", schema_dimensions: 8 },
    } })));
    await act(async () => finishes[0](Response.json({ config: initial })));
    expect(container.textContent).toContain("latest-model");
    expect(container.textContent).toContain("Schema dimensions: 8");
    expect(container.querySelector<HTMLInputElement>("#chatbot-embedding-api-key")!.value).toBe("query-draft-key");
  });

  it("preserves freshly refreshed shared identity when an older optional save response completes", async () => {
    await mount();
    await edit("#chatbot-embedding-api-key", "query-key");
    let finish!: (response: Response) => void;
    fetcher.mockImplementationOnce(() => new Promise<Response>(resolve => { finish = resolve; }));
    await act(async () => saveButton("embedding").click());
    fetcher.mockImplementationOnce(async () => Response.json({ config: { ...initial,
      embedding: { ...initial.embedding, model: "updated-model", schema_dimensions: 12 },
    } }));
    await act(async () => window.dispatchEvent(new Event("sag:stock-model-saved")));
    await act(async () => finish(Response.json({ config: initial })));
    expect(container.textContent).toContain("updated-model");
    expect(container.textContent).toContain("Schema dimensions: 12");
    expect(saveButton("embedding").disabled).toBe(true);
  });
  it("saves only the selected optional card and keeps the other card's secret draft", async () => {
    await mount();
    await act(async () => {
      for (const [selector, value] of [["#chatbot-llm-model", "new-chat-model"],
        ["#chatbot-llm-api-key", "synthetic-chat-key"], ["#chatbot-embedding-api-key", "synthetic-query-key"]]) {
        const input = container.querySelector<HTMLInputElement>(selector)!;
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
    });
    const button = container.querySelector<HTMLButtonElement>('[aria-label="Save Chatbot LLM (optional)"]');
    expect(button).not.toBeNull();
    fetcher.mockImplementation(async () => Response.json({ config: { ...initial, llm: {
      ...initial.llm, model: "new-chat-model", credential_source: "database",
    } } }));
    await act(async () => button!.click());
    const [, request] = fetcher.mock.calls.at(-1)!;
    expect(JSON.parse(request.body)).toEqual({ llm: {
      enabled: false, provider: "openai", base_url: "", model: "new-chat-model", api_key: "synthetic-chat-key",
    } });
    expect(container.querySelector<HTMLInputElement>("#chatbot-llm-api-key")!.value).toBe("");
    expect(container.querySelector<HTMLInputElement>("#chatbot-embedding-api-key")!.value).toBe("synthetic-query-key");
  });
  it.each(["llm", "embedding"] as const)("shows green success and red failures for optional %s tests", async target => {
    await mount();
    await enable(target);
    const button = [...container.querySelectorAll<HTMLButtonElement>("button")].find(button =>
      button.textContent === (target === "llm" ? "Test generation model" : "Test embedding model"))!;
    const message = target === "llm" ? "连接成功 · openai / m" : "Embedding connection successful · 3 dimensions";
    fetcher.mockImplementation(async () => Response.json({ ok: true, message }));
    await act(async () => button.click());
    const status = [...container.querySelectorAll<HTMLElement>('[role="status"]')].find(status => status.textContent === message)!;
    expect(status.querySelector("span")!.classList.contains("text-success")).toBe(true);
    expect(status.querySelector("svg.lucide-check")).toBeTruthy();

    let finish: (response: Response) => void = () => undefined;
    fetcher.mockImplementationOnce(() => new Promise<Response>(resolve => { finish = resolve; }));
    await act(async () => button.click());
    expect(status.textContent).toBe("");
    expect(button.disabled).toBe(true);
    await act(async () => finish(Response.json({ ok: false, message: "Connection test failed" })));
    expect(status.textContent).toBe("Connection test failed");
    expect(status.querySelector("span")!.classList.contains("text-destructive")).toBe(true);
    expect(status.querySelector("svg.lucide-x")).toBeTruthy();

    fetcher.mockRejectedValueOnce(new Error("Network unavailable"));
    await act(async () => button.click());
    expect(status.textContent).toBe(clientErrorMessage("network"));
    expect(status.querySelector("span")!.classList.contains("text-destructive")).toBe(true);
    expect(status.querySelector("svg.lucide-x")).toBeTruthy();
  });
  it("uses a single Responses connection and tests an unsaved advanced Azure authentication draft", async () => {
    await mount({ ...initial, llm: { ...initial.llm, provider: "responses" } },
      [{ id: "openai", display_name: "OpenAI-compatible" }, { id: "responses", display_name: "Responses API" }]);
    await enable("llm");
    expect(container.querySelector('[aria-label="Chatbot provider"]')!.textContent).toBe("OpenAI-compatible");
    expect(await select('[aria-label="Chatbot provider"]', "OpenAI-compatible")).toEqual(["OpenAI-compatible"]);
    expect(container.querySelector("#chatbot-api-format")!.textContent).toBe("Responses API");
    expect(container.querySelector('[aria-label="Endpoint (blank for official endpoint)"]')).toBeNull();
    expect(container.querySelector("#chatbot-responses-service")).toBeNull();
    const advanced = container.querySelector("#chatbot-responses-auth")!.closest("details")!;
    expect(advanced.open).toBe(false);
    await act(async () => advanced.querySelector("summary")!.click());
    expect(advanced.open).toBe(true);
    expect(await select("#chatbot-responses-auth", "API key header (Azure)")).toEqual([
      "Bearer (OpenAI-compatible)", "API key header (Azure)",
    ]);
    expect(container.querySelector("#chatbot-responses-version")).toBeTruthy();
    const endpoint = container.querySelector<HTMLInputElement>("#chatbot-responses-endpoint")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(endpoint,
        "https://draft.invalid/openai/v1/responses");
      endpoint.dispatchEvent(new Event("input", { bubbles: true }));
      const version = container.querySelector<HTMLInputElement>("#chatbot-responses-version")!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(version, "preview");
      version.dispatchEvent(new Event("input", { bubbles: true }));
    });
    fetcher.mockImplementation(async () => Response.json({ message: "Connection successful" }));
    const button = [...container.querySelectorAll<HTMLButtonElement>("button")]
      .find(button => button.textContent === "Test generation model")!;
    await act(async () => button.click());
    const body = JSON.parse(fetcher.mock.calls.at(-1)![1].body);
    expect(body.llm.responses_provider).toBe("azure");
    expect(body.llm.responses_endpoint).toBe("https://draft.invalid/openai/v1/responses");
    expect(body.llm.responses_api_version).toBe("preview");
    expect(body.llm.responses_send_temperature).toBe(false);
    expect(Object.keys(body)).toEqual(["llm", "target"]);
  });

  it("retains saved Bedrock connection overrides while displaying standard Bearer authentication", async () => {
    const config = { ...initial, llm: { ...initial.llm, provider: "responses",
      responses_provider: "bedrock_runtime", responses_endpoint: "https://runtime.invalid/openai/v1/responses",
      responses_api_version: "", responses_send_temperature: false } };
    await mount(config, [{ id: "openai", display_name: "OpenAI-compatible" },
      { id: "responses", display_name: "Responses API" }]);
    await enable("llm");
    expect(container.querySelector("#chatbot-responses-service")).toBeNull();
    expect(container.querySelector("#chatbot-responses-auth")!.textContent).toBe("Bearer (OpenAI-compatible)");
    await act(async () => container.querySelector<HTMLButtonElement>("#chatbot-responses-temperature")!.click());
    fetcher.mockImplementation(async () => Response.json({ message: "Connection successful" }));
    const test = [...container.querySelectorAll<HTMLButtonElement>("button")]
      .find(button => button.textContent === "Test generation model")!;
    await act(async () => test.click());
    expect(JSON.parse(fetcher.mock.calls.at(-1)![1].body).llm).toMatchObject({
      responses_provider: config.llm.responses_provider, responses_endpoint: config.llm.responses_endpoint,
      responses_api_version: "", responses_send_temperature: true, api_key: "",
    });
  });

  it("switches chatbot API formats under OpenAI-compatible while keeping model and endpoints", async () => {
    await mount(initial, [{ id: "openai", display_name: "OpenAI-compatible" },
      { id: "responses", display_name: "Responses API" }, { id: "anthropic", display_name: "Anthropic" }]);
    await enable("llm");
    await select("#chatbot-api-format", "Responses API");
    expect(container.querySelector<HTMLInputElement>('[aria-label="Model"]')!.value).toBe(initial.llm.model);
    const endpoint = container.querySelector<HTMLInputElement>("#chatbot-responses-endpoint")!;
    expect(endpoint.value).toBe("https://api.openai.com/v1/responses");
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(endpoint,
        "https://api.deepseek.com/responses");
      endpoint.dispatchEvent(new Event("input", { bubbles: true }));
    });
    fetcher.mockImplementation(async () => Response.json({ message: "Connection successful" }));
    const test = [...container.querySelectorAll<HTMLButtonElement>("button")]
      .find(button => button.textContent === "Test generation model")!;
    await act(async () => test.click());
    expect(JSON.parse(fetcher.mock.calls.at(-1)![1].body).llm).toMatchObject({
      provider: "responses", model: initial.llm.model, responses_endpoint: "https://api.deepseek.com/responses",
    });
    await select("#chatbot-api-format", "Chat Completions");
    expect(container.querySelector<HTMLInputElement>('[aria-label="Endpoint (blank for official endpoint)"]')!.value)
      .toBe(initial.llm.base_url);
    expect(container.querySelector("#chatbot-responses-endpoint")).toBeNull();
    await act(async () => test.click());
    const chatDraft = JSON.parse(fetcher.mock.calls.at(-1)![1].body).llm;
    expect(chatDraft.provider).toBe("openai");
    expect(chatDraft.responses_endpoint).toBeUndefined();
    await select("#chatbot-api-format", "Responses API");
    expect(container.querySelector<HTMLInputElement>("#chatbot-responses-endpoint")!.value)
      .toBe("https://api.deepseek.com/responses");
    await select('[aria-label="Chatbot provider"]', "Anthropic");
    expect(container.querySelector("#chatbot-api-format")).toBeNull();
    expect(container.querySelector("#chatbot-responses-endpoint")).toBeNull();
  });

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
    await mount({ ...initial, locked: true },
      [{ id: "openai", display_name: "OpenAI-compatible" }, { id: "responses", display_name: "Responses API" }]);
    expect(container.querySelector<HTMLButtonElement>("#chatbot-api-format")!.disabled).toBe(true);
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
    expect(keys.every(input => input.labels?.[0]?.textContent === "API Key (optional)")).toBe(true);
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
    expect(Object.keys(body)).toEqual(["embedding", "target"]);
    expect(container.textContent).toContain("Connection successful");
  });
  it("uses the native provider catalog and official endpoint option", async () => {
    await mount();
    expect(await select('[aria-label="Chatbot provider"]', "OpenAI-compatible")).toEqual(["OpenAI-compatible"]);
    expect(container.querySelector('[aria-label="Endpoint (blank for official endpoint)"]')).toBeTruthy();
  });
  it("explains missing encryption while retaining draft Test controls", async () => {
    await mount({ ...initial, encryption_configured: false });
    expect(container.textContent).toContain("Unsaved connections can still be tested");
    await enable("llm");
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
