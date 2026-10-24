/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";
import type { SearchStrategy, SearchStrategyDisabledMap } from "@/lib/retrieval-config";
import type { Source } from "@/lib/types";
import { SearchProvider, useSearchWorkspace, type SearchScope } from "./search-provider";

afterEach(() => { vi.unstubAllGlobals(); });

function source(id: string, defaultStrategy: SearchStrategy | null): Source {
  return {
    id, name: id, description: "", source_type: "document", connector_kind: "file_upload", status: "active",
    default_search_strategy: defaultStrategy, document_count: 0, chunk_count: 0, event_count: 0,
    created_at: "2026-10-24T00:00:00Z", updated_at: "2026-10-24T00:00:00Z",
  };
}
const sources = [source("a", "sag2"), source("b", "sag2"), source("c", "multi"), source("d", null)];
const scope = (...ids: string[]): SearchScope[] => ids.map((id) => ({ id, name: id }));

function Controls({ onReady }: { onReady: (workspace: ReturnType<typeof useSearchWorkspace>) => void }) {
  const workspace = useSearchWorkspace();
  onReady(workspace);
  return <>
    <div id="strategy">{workspace.strategy}</div>
    <div id="origin">{workspace.strategyOrigin}</div>
    <div id="scope-default">{workspace.scopeDefaultStrategy ?? "none"}</div>
  </>;
}

async function harness(disabledMap?: SearchStrategyDisabledMap) {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const requests: Array<Record<string, unknown>> = [];
  vi.stubGlobal("fetch", async (url: RequestInfo | URL, options?: RequestInit) => {
    if (String(url).endsWith("/sources")) return Response.json(sources);
    requests.push(JSON.parse(String(options?.body)));
    return Response.json({ error: { code: "boom", message: "stopped" } }, { status: 500 });
  });
  let workspace!: ReturnType<typeof useSearchWorkspace>;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<NextIntlClientProvider locale="zh-CN" messages={messages}>
    <SearchProvider defaultStrategy="vector" disabledMap={disabledMap}>
      <Controls onReady={(value) => { workspace = value; }} />
    </SearchProvider>
  </NextIntlClientProvider>));
  const read = (id: string) => container.querySelector(`#${id}`)?.textContent;
  return {
    requests,
    read,
    get workspace() { return workspace; },
    act: (callback: () => unknown) => act(async () => { await callback(); }),
    cleanup: async () => { await act(async () => root.unmount()); container.remove(); },
  };
}

describe("search scope default strategy", () => {
  it("follows the shared default of the scoped sources once they are loaded", async () => {
    const h = await harness();
    try {
      expect(h.read("strategy")).toBe("vector");
      expect(h.read("origin")).toBe("global");
      await h.act(() => h.workspace.setScope(scope("a", "b")));
      // Scopes restored by id alone resolve only after the source list arrives.
      expect(h.read("origin")).toBe("global");
      await h.act(() => h.workspace.ensureSources());
      expect(h.read("strategy")).toBe("sag2");
      expect(h.read("origin")).toBe("scope");
      expect(h.read("scope-default")).toBe("sag2");
      await h.act(() => h.workspace.setScope(scope("a", "c")));
      expect(h.read("strategy")).toBe("vector");
      expect(h.read("origin")).toBe("global");
      await h.act(() => h.workspace.setScope(scope("a", "d")));
      expect(h.read("origin")).toBe("global");
      await h.act(() => h.workspace.setScope([]));
      expect(h.read("origin")).toBe("global");
    } finally { await h.cleanup(); }
  });
  it("uses the default carried by a toggled source before the list loads", async () => {
    const h = await harness();
    try {
      await h.act(() => h.workspace.toggleSource(sources[2]));
      expect(h.read("strategy")).toBe("multi");
      expect(h.read("origin")).toBe("scope");
    } finally { await h.cleanup(); }
  });
  it("ignores a scope default the deployment can no longer serve", async () => {
    const h = await harness({ sag2: { reason: "jev_api_key_missing", message: "Add a key" } });
    try {
      await h.act(() => h.workspace.toggleSource(sources[0]));
      expect(h.read("strategy")).toBe("vector");
      expect(h.read("origin")).toBe("global");
    } finally { await h.cleanup(); }
  });
  it("keeps an explicit pick across scope changes and sends it with the request", async () => {
    const h = await harness();
    try {
      await h.act(() => h.workspace.ensureSources());
      await h.act(() => h.workspace.setScope(scope("a", "b")));
      expect(h.read("strategy")).toBe("sag2");
      await h.act(() => h.workspace.setStrategy("multi_es_fast"));
      expect(h.read("strategy")).toBe("multi_es_fast");
      expect(h.read("origin")).toBe("pinned");
      await h.act(() => h.workspace.setScope(scope("a")));
      expect(h.read("strategy")).toBe("multi_es_fast");
      expect(h.read("origin")).toBe("pinned");
      await h.act(() => h.workspace.run({ query: "question" }).catch(() => {}));
      expect(h.requests).toEqual([expect.objectContaining({ strategy: "multi_es_fast", source_ids: ["a"] })]);
    } finally { await h.cleanup(); }
  });
  it("runs with the scope default when nothing is pinned", async () => {
    const h = await harness();
    try {
      await h.act(() => h.workspace.ensureSources());
      await h.act(() => h.workspace.setScope(scope("b")));
      await h.act(() => h.workspace.run({ query: "question" }).catch(() => {}));
      expect(h.requests).toEqual([expect.objectContaining({ strategy: "sag2", source_ids: ["b"] })]);
      expect(h.read("origin")).toBe("scope");
    } finally { await h.cleanup(); }
  });
});
