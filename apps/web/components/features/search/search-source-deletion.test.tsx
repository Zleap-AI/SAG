/** @vitest-environment jsdom */
import * as React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, expect, it, vi } from "vitest";
import messages from "@/messages/zh-CN.json";
import type { Source } from "@/lib/types";
import { SearchProvider, useSearchWorkspace } from "./search-provider";

afterEach(() => { vi.unstubAllGlobals(); });

it("hides deleting choices while retaining an explicitly selected unavailable scope", async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const requests: unknown[] = [];
  const sources = ["active", "paused", "error", "deleting", "delete_failed"].map((status) => ({
    id: status, name: status, description: "", source_type: "document", connector_kind: "file_upload",
    status, document_count: 0, chunk_count: 0, event_count: 0,
    created_at: "2026-10-09T00:00:00Z", updated_at: "2026-10-09T00:00:00Z",
  } as Source));
  vi.stubGlobal("fetch", async (url: RequestInfo | URL, options?: RequestInit) => {
    if (String(url).endsWith("/sources")) return Response.json(sources);
    requests.push(JSON.parse(String(options?.body)));
    return Response.json({ error: { code: "source_unavailable", message: "Selected source is unavailable" } }, { status: 409 });
  });
  function Controls() {
    const state = useSearchWorkspace();
    const { setScope, ensureSources } = state;
    React.useEffect(() => {
      setScope([{ id: "deleting", name: "Saved scope" }]);
      void ensureSources();
    }, [ensureSources, setScope]);
    return <>
      <div id="choices">{state.sources.map((source) => source.name).join(",")}</div>
      <div id="scope">{state.scoped.map((scope) => scope.id).join(",")}</div>
      <div id="error">{state.error}</div>
      <button onClick={() => void state.run({ query: "question" }).catch(() => {})}>Run</button>
    </>;
  }
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<NextIntlClientProvider locale="zh-CN" messages={messages}>
      <SearchProvider defaultStrategy="vector"><Controls /></SearchProvider>
    </NextIntlClientProvider>));
    expect(container.querySelector("#choices")?.textContent).toBe("active,paused,error");
    expect(container.querySelector("#scope")?.textContent).toBe("deleting");
    await act(async () => container.querySelector<HTMLButtonElement>("button")!.click());
    expect(requests).toEqual([expect.objectContaining({ source_ids: ["deleting"] })]);
    expect(container.querySelector("#error")?.textContent).toBe("Selected source is unavailable");
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});
