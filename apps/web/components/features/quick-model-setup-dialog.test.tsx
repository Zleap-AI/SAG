/** @vitest-environment jsdom */

import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { Capabilities, ModelConfig } from "@/lib/types";
import zh from "@/messages/zh-CN.json";
import en from "@/messages/en-US.json";
import { QuickModelSetupDialog } from "./quick-model-setup-dialog";

const capabilities: Capabilities = {
  llm_configured: true, llm_provider: "openai", llm_model: "deepseek-flash",
  embedding_model: "Qwen/Qwen3-Embedding-4B", vector_provider: "pgvector",
  language: "zh", search_strategy: "vector", document_parser: "auto",
  effective_document_parser: "anydoc", mineru_configured: false,
  max_upload_mb: 100, timezone: "Asia/Shanghai",
};
const config: ModelConfig = {
  llm_provider: "openai", llm_base_url: "https://api.deepseek.com", llm_model: "deepseek-flash",
  llm_context_window: 131072, llm_temperature: 0.3, llm_max_tokens: 4096,
  llm_timeout_ms: 60000, llm_max_retries: 3, llm_api_key_set: true,
  embedding_model: "Qwen/Qwen3-Embedding-4B", embedding_base_url: "https://api.302.ai/v1",
  embedding_dimensions: 1024, embedding_api_key_set: true, document_parser: "auto",
  mineru_provider: "302", mineru_base_url: null, mineru_version: "2.5",
  mineru_official_model: "vlm", mineru_api_key_set: false, effective_document_parser: "anydoc",
  document_extract_concurrency: 4, document_chunk_max_tokens: 512, document_chunk_mode: "standard",
  search_strategy: "vector", search_top_k: 10, sag_language: "zh", sources: {}, locked_fields: [],
};

function successResponse() {
  return new Response(JSON.stringify({ config, capabilities }), {
    status: 200, headers: { "Content-Type": "application/json" },
  });
}

let root: Root;
let container: HTMLDivElement;
let fetcher: ReturnType<typeof vi.fn<typeof fetch>>;
let configured: ReturnType<typeof vi.fn<(value: Capabilities) => void>>;
let onOpenChange: ReturnType<typeof vi.fn<(value: boolean) => void>>;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  fetcher = vi.fn<typeof fetch>().mockImplementation(async () => successResponse());
  vi.stubGlobal("fetch", fetcher);
  configured = vi.fn();
  onOpenChange = vi.fn();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function mount(locale: "zh-CN" | "en-US" = "zh-CN") {
  await act(async () => root.render(
    <NextIntlClientProvider locale={locale} timeZone="UTC" messages={locale === "zh-CN" ? zh : en}>
      <QuickModelSetupDialog open onOpenChange={onOpenChange} onConfigured={configured} />
    </NextIntlClientProvider>,
  ));
}

function input(id = "quick-setup-api-key") {
  const element = document.getElementById(id);
  expect(element, `input ${id}`).toBeInstanceOf(HTMLInputElement);
  return element as HTMLInputElement;
}

async function edit(value: string, id?: string) {
  await act(async () => {
    const element = input(id);
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function provider() {
  const element = document.getElementById("quick-setup-provider");
  expect(element, "generation provider select").toBeInstanceOf(HTMLSelectElement);
  return element as HTMLSelectElement;
}

async function choose(value: "302" | "deepseek") {
  await selectOption(provider(), value);
}

function embeddingProvider() {
  const element = document.getElementById("quick-setup-embedding-provider");
  expect(element, "embedding provider select").toBeInstanceOf(HTMLSelectElement);
  return element as HTMLSelectElement;
}

async function chooseEmbedding(value: "302" | "zhipu" | "bailian") {
  await selectOption(embeddingProvider(), value);
}

async function selectOption(element: HTMLSelectElement, value: string) {
  await act(async () => {
    element.value = value;
    element.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

function submitButton() {
  return document.querySelector<HTMLButtonElement>('button[type="submit"]')!;
}

async function submit() {
  await act(async () => submitButton().click());
}

function postedRequest(index = 0) {
  const [url, request] = fetcher.mock.calls[index];
  return { url, method: request?.method, body: JSON.parse(String(request?.body)) };
}

describe("first-run quick model setup", () => {
  it("focuses the API key without scrolling the title and close button out of view", async () => {
    await mount();
    const keyInput = input();
    const focus = vi.spyOn(keyInput, "focus");
    await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 120)); });
    expect(document.activeElement).toBe(keyInput);
    expect(focus).toHaveBeenCalledWith({ preventScroll: true });
  });

  it.each(["outside click", "Escape"] as const)("keeps the dialog open after %s", async (action) => {
    await mount();
    await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 0)); });
    await act(async () => {
      if (action === "outside click") {
        document.body.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, cancelable: true, button: 0 }));
      } else {
        input().dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
      }
    });
    expect(onOpenChange).not.toHaveBeenCalled();
  });

  it.each(["close", "skip"] as const)("allows explicit %s before saving", async (action) => {
    await mount();
    const label = action === "close" ? zh.Common.close : zh.QuickSetup.skip;
    const button = [...document.querySelectorAll<HTMLButtonElement>("button")].find(item => item.textContent === label);
    expect(button).toBeDefined();
    await act(async () => button!.click());
    expect(onOpenChange).toHaveBeenCalledOnce();
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("preserves the existing 302 single-key request", async () => {
    await mount();
    await edit("  sk-302  ");
    await submit();
    expect(postedRequest()).toEqual({
      url: expect.stringContaining("/api/v1/system/model-setup/302"),
      method: "POST", body: { api_key: "sk-302" },
    });
    expect(configured).toHaveBeenCalledWith(capabilities);
    expect(input().value).toBe("");
  });

  it("posts official generation and 302 embedding keys as separate credentials", async () => {
    await mount();
    await choose("deepseek");
    await edit("  sk-deepseek  ");
    await edit("  sk-embedding-302  ", "quick-setup-embedding-api-key");
    await submit();
    expect(postedRequest()).toEqual({
      url: expect.stringContaining("/api/v1/system/model-setup/deepseek"),
      method: "POST", body: { api_key: "sk-deepseek", embedding_api_key: "sk-embedding-302" },
    });
    expect(configured).toHaveBeenCalledWith(capabilities);
    expect(input().value).toBe("");
    expect(input("quick-setup-embedding-api-key").value).toBe("");
  });

  it.each([
    ["302", "zhipu", "embedding-3"],
    ["302", "bailian", "text-embedding-v4"],
    ["deepseek", "zhipu", "embedding-3"],
    ["deepseek", "bailian", "text-embedding-v4"],
  ] as const)("configures %s generation with %s embeddings", async (generation, embedding, model) => {
    await mount();
    await choose(generation);
    await edit(`  sk-generation-${generation}  `);
    await chooseEmbedding(embedding);
    expect(embeddingProvider().selectedOptions[0]?.textContent).toContain(model);
    expect(submitButton().disabled).toBe(true);
    await edit("  ", "quick-setup-embedding-api-key");
    expect(submitButton().disabled).toBe(true);
    await edit(`  sk-embedding-${embedding}  `, "quick-setup-embedding-api-key");
    await submit();
    expect(postedRequest()).toEqual({
      url: expect.stringContaining(`/api/v1/system/model-setup/${generation}`),
      method: "POST",
      body: {
        api_key: `sk-generation-${generation}`,
        embedding_provider: embedding,
        embedding_api_key: `sk-embedding-${embedding}`,
      },
    });
    expect(configured).toHaveBeenCalledWith(capabilities);
    expect(input("quick-setup-embedding-api-key").value).toBe("");
  });

  it("keeps each embedding vendor key separate through generation and embedding switches", async () => {
    await mount();
    await edit("sk-generation-302");
    await chooseEmbedding("zhipu");
    await edit("sk-zhipu", "quick-setup-embedding-api-key");
    await chooseEmbedding("bailian");
    expect(input("quick-setup-embedding-api-key").value).toBe("");
    expect(submitButton().disabled).toBe(true);
    await edit("sk-bailian", "quick-setup-embedding-api-key");
    await choose("deepseek");
    expect(embeddingProvider().value).toBe("bailian");
    expect(embeddingProvider().selectedOptions[0]?.textContent).toContain("text-embedding-v4");
    expect(input("quick-setup-embedding-api-key").value).toBe("sk-bailian");
    await edit("sk-deepseek");
    await chooseEmbedding("302");
    expect(input("quick-setup-embedding-api-key").value).toBe("");
    await edit("sk-embedding-302", "quick-setup-embedding-api-key");
    await choose("302");
    expect(input().value).toBe("sk-generation-302");
    expect(document.getElementById("quick-setup-embedding-api-key")).toBeNull();
    await chooseEmbedding("zhipu");
    expect(input("quick-setup-embedding-api-key").value).toBe("sk-zhipu");
    await submit();
    expect(postedRequest().body).toEqual({
      api_key: "sk-generation-302", embedding_provider: "zhipu", embedding_api_key: "sk-zhipu",
    });
  });

  it.each(["zh-CN", "en-US"] as const)("identifies each key provider and non-thinking mode in %s", async (locale) => {
    await mount(locale);
    await choose("deepseek");
    expect(document.querySelector('label[for="quick-setup-provider"]')?.textContent)
      .toBe(locale === "zh-CN" ? "对话模型" : "Chat model");
    expect(document.querySelector('label[for="quick-setup-embedding-provider"]')?.textContent)
      .toBe(locale === "zh-CN" ? "资料检索模型" : "Document search model");
    expect(document.getElementById(provider().getAttribute("aria-describedby")!)?.textContent)
      .toBe(locale === "zh-CN" ? "理解问题，生成回答" : "Understands questions and writes answers");
    expect(document.getElementById(embeddingProvider().getAttribute("aria-describedby")!)?.textContent)
      .toBe(locale === "zh-CN" ? "从资料中查找与问题相关的内容" : "Finds content relevant to your question");
    expect(provider().value).toBe("deepseek");
    expect(provider().selectedOptions[0]?.textContent).toContain("deepseek-flash");
    expect(document.body.textContent).toContain("deepseek-flash");
    expect(document.body.textContent).toContain(locale === "zh-CN" ? "非思考" : "Non-thinking");
    const labels = [...document.querySelectorAll("label")].map(label => label.textContent);
    expect(labels).toContain("DeepSeek API Key");
    expect(labels).toContain("302.AI API Key");
    expect(document.querySelector('a[href="https://platform.deepseek.com/api_keys"]')).not.toBeNull();
    expect(document.querySelector('a[href="https://dash.302.ai/"]')).not.toBeNull();
    expect(document.body.textContent).not.toContain("MinerU 2.5");
  });

  it("requires both nonblank credentials before enabling DeepSeek setup", async () => {
    await mount();
    await choose("deepseek");
    expect(submitButton().disabled).toBe(true);
    await edit("sk-deepseek");
    expect(submitButton().disabled).toBe(true);
    await edit("   ", "quick-setup-embedding-api-key");
    expect(submitButton().disabled).toBe(true);
    await edit("sk-302", "quick-setup-embedding-api-key");
    expect(submitButton().disabled).toBe(false);
    await edit(" ");
    expect(submitButton().disabled).toBe(true);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("keeps credentials isolated when switching providers", async () => {
    await mount();
    await edit("sk-original-302");
    await choose("deepseek");
    expect(input().value).toBe("");
    expect(input("quick-setup-embedding-api-key").value).toBe("");
    await edit("sk-deepseek");
    await edit("sk-embedding-302", "quick-setup-embedding-api-key");
    await choose("302");
    expect(input().value).not.toBe("sk-deepseek");
    await edit("sk-original-302");
    await submit();
    expect(postedRequest().body).toEqual({ api_key: "sk-original-302" });
    expect(postedRequest().url).toEqual(expect.stringContaining("/model-setup/302"));
  });

  it("locks credentials, provider switching, closing and duplicate submission while saving", async () => {
    let finish!: (response: Response) => void;
    fetcher.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    await mount();
    await choose("deepseek");
    await edit("sk-deepseek");
    await edit("sk-302", "quick-setup-embedding-api-key");
    await act(async () => {
      const form = document.querySelector("form")!;
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(provider().disabled).toBe(true);
    expect(embeddingProvider().disabled).toBe(true);
    expect(input().disabled).toBe(true);
    expect(input("quick-setup-embedding-api-key").disabled).toBe(true);
    expect(submitButton().disabled).toBe(true);
    expect(provider().value).toBe("deepseek");
    expect(embeddingProvider().value).toBe("302");
    const close = [...document.querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === zh.Common.close);
    expect(close).toBeDefined();
    await act(async () => close!.click());
    expect(onOpenChange).not.toHaveBeenCalled();
    await act(async () => finish(successResponse()));
    expect(configured).toHaveBeenCalledOnce();
  });

  it("shows a server error and keeps both keys for a successful retry", async () => {
    fetcher.mockResolvedValueOnce(new Response(JSON.stringify({
      error: { code: "embedding_unavailable", message: "302 embedding service is unavailable" },
    }), { status: 502, headers: { "Content-Type": "application/json" } }));
    await mount();
    await choose("deepseek");
    await edit("sk-deepseek");
    await edit("sk-302", "quick-setup-embedding-api-key");
    await submit();
    expect(document.querySelector('[role="alert"]')?.textContent).toContain("302 embedding service is unavailable");
    expect(configured).not.toHaveBeenCalled();
    expect(input().value).toBe("sk-deepseek");
    expect(input("quick-setup-embedding-api-key").value).toBe("sk-302");
    expect(submitButton().disabled).toBe(false);
    await submit();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(configured).toHaveBeenCalledWith(capabilities);
  });
});

describe("DeepSeek setup API contract", () => {
  it("uses the dedicated endpoint and returns the setup response", async () => {
    const result = await api.quickSetupDeepSeek("sk-deepseek", "sk-302");
    expect(postedRequest()).toEqual({
      url: expect.stringContaining("/api/v1/system/model-setup/deepseek"),
      method: "POST", body: { api_key: "sk-deepseek", embedding_api_key: "sk-302" },
    });
    expect(result).toEqual({ config, capabilities });
  });
});
