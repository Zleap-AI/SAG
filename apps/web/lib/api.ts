import { clearToken, getToken } from "./auth";
import { readClientLocale } from "../i18n/client";
import { clientErrorMessage, serverErrorMessage } from "../i18n/client-errors";
import type { SearchStrategy } from "./retrieval-config";
import type {
  ActivityItem,
  Entity,
  Agent,
  Binding,
  BindingTargetType,
  Capabilities,
  Doc,
  MessagePage,
  ModelConfig,
  ModelConfigPatch,
  ModelProviderSpec,
  ModelSetupStatus,
  KnowledgeMcpDescriptor,
  Persona,
  SearchResponse,
  Source,
  SourceGraphResponse,
  SourceMcpDescriptor,
  SystemPreferences,
  Thread,
  TokenResponse,
  User,
  UniverseManifest,
  UniverseGraphPatch,
  UniverseTimelineSlice,
  UniverseNodeDetail,
  BackgroundJob,
  ExplorationDetail,
  ExplorationSession,
} from "./types";

/** 浏览器通过局域网 IP 打开前端时，自动将 API 指向同主机 8000 端口。 */
function resolveApiBase(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE;
  if (typeof window !== "undefined") {
    const { protocol, hostname } = window.location;
    const isLocalHost = hostname === "localhost" || hostname === "127.0.0.1";
    if (!isLocalHost) {
      const pointsToLocal =
        !configured ||
        configured.includes("localhost") ||
        configured.includes("127.0.0.1");
      if (pointsToLocal) {
        return `${protocol}//${hostname}:8000`;
      }
    }
  }
  return configured || "http://localhost:8000";
}

export const API_BASE = resolveApiBase();

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export interface GlobalSearchBody {
  query: string;
  source_ids?: string[];
  top_k?: number;
  strategy?: SearchStrategy;
  save_exploration?: boolean;
}

export interface GlobalSearchStreamHandlers {
  onResult: (result: SearchResponse) => void;
  onSummaryDelta: (delta: string) => void;
  onCompleted: (result: SearchResponse) => void;
}

interface SearchStreamFrame {
  event: string;
  data: unknown;
}

type SearchStreamTimeout = "first-result" | "idle";

const SEARCH_FIRST_RESULT_TIMEOUT_MS = 30_000;
const SEARCH_STREAM_IDLE_TIMEOUT_MS = 45_000;

function isSearchResponse(value: unknown): value is SearchResponse {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<SearchResponse>;
  return (
    typeof candidate.query === "string" &&
    Array.isArray(candidate.sections) &&
    Array.isArray(candidate.events) &&
    Array.isArray(candidate.entities) &&
    Array.isArray(candidate.relations) &&
    Array.isArray(candidate.source_hits) &&
    typeof candidate.summary === "string" &&
    Boolean(candidate.stats) &&
    typeof candidate.stats === "object"
  );
}

function parseSearchStreamFrame(frame: string): SearchStreamFrame | null {
  let event = "";
  const data: string[] = [];
  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return null;
  try {
    return { event, data: JSON.parse(data.join("\n")) };
  } catch {
    throw new ApiError(
      0,
      "invalid_search_stream",
      clientErrorMessage("invalidSearchStream"),
    );
  }
}

async function streamGlobalSearch(
  body: GlobalSearchBody,
  handlers: GlobalSearchStreamHandlers,
  signal?: AbortSignal,
): Promise<SearchResponse> {
  const token = getToken();
  const timeoutController = new AbortController();
  const combinedSignal = signal
    ? AbortSignal.any([signal, timeoutController.signal])
    : timeoutController.signal;
  let timeoutHandle: ReturnType<typeof setTimeout> | null = null;
  let timeoutReason: SearchStreamTimeout | null = null;
  const clearStreamTimeout = () => {
    if (timeoutHandle !== null) clearTimeout(timeoutHandle);
    timeoutHandle = null;
  };
  const armStreamTimeout = (reason: SearchStreamTimeout, delay: number) => {
    clearStreamTimeout();
    timeoutHandle = setTimeout(() => {
      timeoutReason = reason;
      timeoutController.abort();
    }, delay);
  };
  const timeoutError = () =>
    new ApiError(
      0,
      "timeout",
      timeoutReason === "first-result"
        ? clientErrorMessage("searchPrepareTimeout")
        : clientErrorMessage("searchIdleTimeout"),
    );

  armStreamTimeout("first-result", SEARCH_FIRST_RESULT_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/v1/search/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept-Language": readClientLocale(),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(body),
      signal: combinedSignal,
    });
  } catch (error) {
    clearStreamTimeout();
    if (timeoutReason) throw timeoutError();
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError(0, "aborted", clientErrorMessage("cancelled"));
    }
    if (signal?.aborted)
      throw new ApiError(0, "aborted", clientErrorMessage("cancelled"));
    throw new ApiError(0, "network", clientErrorMessage("network"));
  }

  if (response.status === 401 && typeof window !== "undefined") {
    clearToken();
    window.location.href = "/login";
  }
  if (!response.ok || !response.body) {
    clearStreamTimeout();
    let code = "search_stream_error";
    let message = response.statusText || clientErrorMessage("searchFailed");
    try {
      const value = await response.json();
      code = value?.error?.code ?? code;
      const detail = value?.error?.message ?? value?.detail;
      if (detail)
        message = typeof detail === "string" ? detail : JSON.stringify(detail);
    } catch {
      /* Keep the stable fallback when a proxy returns HTML or an empty body. */
    }
    throw new ApiError(
      response.status,
      code,
      serverErrorMessage(code, message, response.status),
    );
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let completed: SearchResponse | null = null;
  const protocolState: { stage: "awaiting-result" | "streaming" } = {
    stage: "awaiting-result",
  };

  const dispatch = (rawFrame: string) => {
    const frame = parseSearchStreamFrame(rawFrame);
    if (!frame) return;
    if (frame.event === "result") {
      if (
        protocolState.stage !== "awaiting-result" ||
        !isSearchResponse(frame.data)
      ) {
        throw new ApiError(
          0,
          "invalid_search_stream",
          clientErrorMessage("invalidResult"),
        );
      }
      protocolState.stage = "streaming";
      armStreamTimeout("idle", SEARCH_STREAM_IDLE_TIMEOUT_MS);
      handlers.onResult(frame.data);
      return;
    }
    if (frame.event === "summary.delta") {
      if (protocolState.stage !== "streaming") {
        throw new ApiError(
          0,
          "invalid_search_stream",
          clientErrorMessage("summaryBeforeResult"),
        );
      }
      const delta = (frame.data as { delta?: unknown })?.delta;
      if (typeof delta !== "string") {
        throw new ApiError(
          0,
          "invalid_search_stream",
          clientErrorMessage("invalidSummaryDelta"),
        );
      }
      if (delta) handlers.onSummaryDelta(delta);
      return;
    }
    if (frame.event === "completed") {
      if (
        protocolState.stage !== "streaming" ||
        !isSearchResponse(frame.data)
      ) {
        throw new ApiError(
          0,
          "invalid_search_stream",
          clientErrorMessage("invalidCompleted"),
        );
      }
      completed = frame.data;
      clearStreamTimeout();
      handlers.onCompleted(completed);
      return;
    }
    if (frame.event === "error") {
      const payload = frame.data as { code?: unknown; message?: unknown };
      const code =
        typeof payload?.code === "string"
          ? payload.code
          : "search_stream_error";
      const message =
        typeof payload?.message === "string"
          ? payload.message
          : clientErrorMessage("searchFailed");
      throw new ApiError(0, code, serverErrorMessage(code, message));
    }
    throw new ApiError(
      0,
      "invalid_search_stream",
      clientErrorMessage("unknownSearchEvent", { event: frame.event }),
    );
  };

  const cancelReader = async () => {
    try {
      await reader.cancel();
    } catch {
      /* The original protocol/network error is more useful than cancel noise. */
    }
  };

  try {
    readLoop: while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (protocolState.stage === "streaming") {
        // Includes EventSourceResponse ping comments, so a healthy long model
        // generation is kept alive without weakening the first-result limit.
        armStreamTimeout("idle", SEARCH_STREAM_IDLE_TIMEOUT_MS);
      }
      buffer += decoder.decode(value, { stream: true });
      let separator = buffer.match(/\r?\n\r?\n/);
      while (separator?.index !== undefined) {
        const frame = buffer.slice(0, separator.index);
        buffer = buffer.slice(separator.index + separator[0].length);
        if (frame.trim()) dispatch(frame);
        if (completed) break readLoop;
        separator = buffer.match(/\r?\n\r?\n/);
      }
    }
    if (!completed) {
      buffer += decoder.decode();
      if (buffer.trim()) dispatch(buffer);
    }
    if (completed) await cancelReader();
  } catch (error) {
    await cancelReader();
    if (timeoutReason) throw timeoutError();
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError(0, "aborted", clientErrorMessage("cancelled"));
    }
    if (signal?.aborted)
      throw new ApiError(0, "aborted", clientErrorMessage("cancelled"));
    throw error;
  } finally {
    clearStreamTimeout();
    reader.releaseLock();
  }

  if (!completed) {
    throw new ApiError(
      0,
      "search_stream_incomplete",
      clientErrorMessage("searchIncomplete"),
    );
  }
  return completed;
}

async function request<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    ...(opts.headers as Record<string, string>),
  };
  if (opts.body && !(opts.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (!headers["Accept-Language"])
    headers["Accept-Language"] = readClientLocale();

  // 30s 超时护栏（SSE 流式接口不走此函数，不受影响）
  const timeoutSignal = AbortSignal.timeout(30_000);
  const signal = opts.signal
    ? AbortSignal.any([opts.signal, timeoutSignal])
    : timeoutSignal;
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, { ...opts, headers, signal });
  } catch (e) {
    if (e instanceof DOMException && e.name === "TimeoutError") {
      throw new ApiError(0, "timeout", clientErrorMessage("requestTimeout"));
    }
    if (e instanceof DOMException && e.name === "AbortError") {
      throw new ApiError(0, "aborted", clientErrorMessage("cancelled"));
    }
    throw new ApiError(0, "network", clientErrorMessage("network"));
  }

  if (
    res.status === 401 &&
    typeof window !== "undefined" &&
    !path.includes("/auth/")
  ) {
    clearToken();
    window.location.href = "/login";
  }

  if (!res.ok) {
    let code = "error";
    let message = res.statusText || clientErrorMessage("requestFailed");
    try {
      const j = await res.json();
      if (j?.error) {
        code = j.error.code ?? code;
        message = j.error.message ?? message;
      } else if (j?.detail) {
        message =
          typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      }
    } catch {
      /* ignore */
    }
    throw new ApiError(
      res.status,
      code,
      serverErrorMessage(code, message, res.status),
    );
  }

  if (res.status === 204) return undefined as T;
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json")
    ? res.json()
    : ((await res.text()) as unknown as T);
}

export const api = {
  // auth / system
  register: (b: { email: string; password: string; name?: string }) =>
    request<TokenResponse>("/api/v1/auth/register", {
      method: "POST",
      body: JSON.stringify(b),
    }),
  login: (b: { name: string; email?: string; password?: string }) =>
    request<TokenResponse>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify(b),
    }),
  me: () => request<User>("/api/v1/auth/me"),
  capabilities: () => request<Capabilities>("/api/v1/system/capabilities"),
  getSystemPreferences: () =>
    request<SystemPreferences>("/api/v1/system/preferences"),
  saveSystemPreferences: (preferences: SystemPreferences) =>
    request<SystemPreferences>("/api/v1/system/preferences", {
      method: "PUT",
      body: JSON.stringify(preferences),
    }),

  // 模型与检索配置
  getModelConfig: () => request<ModelConfig>("/api/v1/system/model-config"),
  getModelProviders: () =>
    request<ModelProviderSpec[]>("/api/v1/system/model-providers"),
  modelSetupStatus: () =>
    request<ModelSetupStatus>("/api/v1/system/model-setup"),
  quickSetup302: (apiKey: string) =>
    request<{ config: ModelConfig; capabilities: Capabilities }>(
      "/api/v1/system/model-setup/302",
      {
        method: "POST",
        body: JSON.stringify({ api_key: apiKey }),
      },
    ),
  saveModelConfig: (b: ModelConfigPatch) =>
    request<{ config: ModelConfig; capabilities: Capabilities }>(
      "/api/v1/system/model-config",
      {
        method: "PUT",
        body: JSON.stringify(b),
      },
    ),
  setup302MinerU: () =>
    request<{ config: ModelConfig; capabilities: Capabilities }>(
      "/api/v1/system/model-config/mineru/302",
      { method: "POST" },
    ),
  testModelConfig: (b?: ModelConfigPatch) =>
    request<{ ok: boolean; message: string }>(
      "/api/v1/system/model-config/test",
      {
        method: "POST",
        body: b ? JSON.stringify(b) : undefined,
      },
    ),

  // 信源
  listSources: () => request<Source[]>("/api/v1/sources"),
  getSource: (id: string) => request<Source>(`/api/v1/sources/${id}`),
  createSource: (b: { name: string; description?: string }) =>
    request<Source>("/api/v1/sources", {
      method: "POST",
      body: JSON.stringify(b),
    }),
  updateSource: (id: string, b: Record<string, unknown>) =>
    request<Source>(`/api/v1/sources/${id}`, {
      method: "PATCH",
      body: JSON.stringify(b),
    }),
  deleteSource: (id: string) =>
    request<{ ok: boolean }>(`/api/v1/sources/${id}`, { method: "DELETE" }),
  syncSource: (id: string) =>
    request<{ id: string; type: string }>(`/api/v1/sources/${id}/sync`, {
      method: "POST",
    }),

  // 文档
  listDocuments: (sid: string) =>
    request<Doc[]>(`/api/v1/sources/${sid}/documents`),
  uploadDocumentWithProgress: (
    sid: string,
    file: File,
    onProgress: (pct: number) => void,
  ) =>
    new Promise<Doc>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${API_BASE}/api/v1/sources/${sid}/documents`);
      xhr.setRequestHeader("Authorization", `Bearer ${getToken() ?? ""}`);
      xhr.setRequestHeader("Accept-Language", readClientLocale());
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable)
          onProgress(Math.round((e.loaded / e.total) * 100));
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300)
          resolve(JSON.parse(xhr.responseText));
        else {
          let msg = clientErrorMessage("uploadFailed");
          try {
            msg = JSON.parse(xhr.responseText)?.error?.message ?? msg;
          } catch {
            /* noop */
          }
          reject(
            new ApiError(
              xhr.status,
              "upload_failed",
              serverErrorMessage("upload_failed", msg, xhr.status),
            ),
          );
        }
      };
      xhr.onerror = () =>
        reject(
          new ApiError(0, "network_error", clientErrorMessage("uploadNetwork")),
        );
      const fd = new FormData();
      fd.append("file", file);
      xhr.send(fd);
    }),

  uploadDocument: (sid: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<Doc>(`/api/v1/sources/${sid}/documents`, {
      method: "POST",
      body: fd,
    });
  },
  reprocessDocument: (sid: string, did: string) =>
    request(`/api/v1/sources/${sid}/documents/${did}/reprocess`, {
      method: "POST",
    }),
  pauseDocument: (sid: string, did: string) =>
    request(`/api/v1/sources/${sid}/documents/${did}/pause`, {
      method: "POST",
    }),
  resumeDocument: (sid: string, did: string) =>
    request(`/api/v1/sources/${sid}/documents/${did}/resume`, {
      method: "POST",
    }),
  deleteDocument: (sid: string, did: string) =>
    request(`/api/v1/sources/${sid}/documents/${did}`, { method: "DELETE" }),

  // Agent
  listAgents: () => request<Agent[]>("/api/v1/agents"),
  getDefaultAgent: () => request<Agent>("/api/v1/agents/default"),
  getAgent: (id: string) => request<Agent>(`/api/v1/agents/${id}`),
  createAgent: (b: { name: string; avatar?: string; persona?: Persona }) =>
    request<Agent>("/api/v1/agents", {
      method: "POST",
      body: JSON.stringify(b),
    }),
  updateAgent: (
    id: string,
    b: { name?: string; avatar?: string; persona?: Persona },
  ) =>
    request<Agent>(`/api/v1/agents/${id}`, {
      method: "PATCH",
      body: JSON.stringify(b),
    }),
  deleteAgent: (id: string) =>
    request<{ ok: boolean }>(`/api/v1/agents/${id}`, { method: "DELETE" }),

  listBindings: (id: string) =>
    request<Binding[]>(`/api/v1/agents/${id}/bindings`),
  addBinding: (
    id: string,
    b: {
      target_type: BindingTargetType;
      target_id?: string;
      config?: Record<string, unknown>;
    },
  ) =>
    request<Binding>(`/api/v1/agents/${id}/bindings`, {
      method: "POST",
      body: JSON.stringify(b),
    }),
  removeBinding: (id: string, bindingId: string) =>
    request<{ ok: boolean }>(`/api/v1/agents/${id}/bindings/${bindingId}`, {
      method: "DELETE",
    }),

  listThreads: (
    id: string,
    opts?: { archived?: boolean; limit?: number; offset?: number },
  ) => {
    const params = new URLSearchParams();
    if (opts?.archived) params.set("archived", "true");
    if (opts?.limit != null) params.set("limit", String(opts.limit));
    if (opts?.offset) params.set("offset", String(opts.offset));
    const query = params.toString();
    return request<Thread[]>(
      `/api/v1/agents/${id}/threads${query ? `?${query}` : ""}`,
    );
  },
  updateThread: (
    id: string,
    tid: string,
    b: { title?: string; archived?: boolean },
  ) =>
    request<Thread>(`/api/v1/agents/${id}/threads/${tid}`, {
      method: "PATCH",
      body: JSON.stringify(b),
    }),
  createThread: (id: string, title?: string, signal?: AbortSignal) =>
    request<Thread>(`/api/v1/agents/${id}/threads`, {
      method: "POST",
      body: JSON.stringify({
        title: title ?? clientErrorMessage("newConversation"),
      }),
      signal,
    }),
  deleteMessage: (id: string, tid: string, mid: string) =>
    request<{ ok: boolean }>(
      `/api/v1/agents/${id}/threads/${tid}/messages/${mid}`,
      {
        method: "DELETE",
      },
    ),
  listMessages: (
    id: string,
    tid: string,
    options?: { limit?: number; cursor?: string | null; signal?: AbortSignal },
  ) => {
    const params = new URLSearchParams();
    if (options?.limit != null) params.set("limit", String(options.limit));
    if (options?.cursor) params.set("cursor", options.cursor);
    const query = params.toString();
    return request<MessagePage>(
      `/api/v1/agents/${id}/threads/${tid}/messages${query ? `?${query}` : ""}`,
      { signal: options?.signal },
    );
  },
  cancelAgentRun: (id: string, tid: string, runId: string) =>
    request<{ ok: boolean }>(
      `/api/v1/agents/${id}/threads/${tid}/runs/${runId}/cancel`,
      {
        method: "POST",
      },
    ),
  approveAgentTool: (
    id: string,
    tid: string,
    runId: string,
    toolCallId: string,
  ) =>
    request<{ ok: boolean }>(
      `/api/v1/agents/${id}/threads/${tid}/runs/${runId}/tool-calls/${toolCallId}/approve`,
      { method: "POST" },
    ),
  rejectAgentTool: (
    id: string,
    tid: string,
    runId: string,
    toolCallId: string,
    reason: string,
  ) =>
    request<{ ok: boolean }>(
      `/api/v1/agents/${id}/threads/${tid}/runs/${runId}/tool-calls/${toolCallId}/reject`,
      { method: "POST", body: JSON.stringify({ reason }) },
    ),
  deleteThread: (id: string, tid: string) =>
    request(`/api/v1/agents/${id}/threads/${tid}`, { method: "DELETE" }),

  // 搜索
  globalSearch: (b: GlobalSearchBody, signal?: AbortSignal) =>
    request<SearchResponse>("/api/v1/search", {
      method: "POST",
      body: JSON.stringify(b),
      signal,
    }),
  streamGlobalSearch,

  // 知识宇宙：统计轮廓 + 原子时间线与显式探索
  universeManifest: () =>
    request<UniverseManifest>("/api/v1/universe/manifest"),
  universeNode: (
    kind: "event" | "entity",
    id: string,
    sourceId?: string | null,
  ) => {
    if (!sourceId) {
      throw new ApiError(
        0,
        "missing_source",
        clientErrorMessage("missingSource"),
      );
    }
    const query = `?source_id=${encodeURIComponent(sourceId)}`;
    return request<UniverseNodeDetail>(
      `/api/v1/universe/nodes/${kind}/${id}${query}`,
    );
  },
  universeExpand: (
    body: {
      epoch: number;
      source_id: string;
      node_kind: "event" | "entity";
      node_id: string;
      limit?: number;
      cursor?: string | null;
      snapshot_id?: string | null;
      after?: string | null;
      before?: string | null;
    },
    signal?: AbortSignal,
  ) =>
    request<UniverseGraphPatch>("/api/v1/universe/expand", {
      method: "POST",
      body: JSON.stringify(body),
      signal,
    }),
  universeTimeline: (
    body: {
      epoch: number;
      source_id: string;
      limit?: number;
      direction?: "older" | "newer";
      cursor?: string | null;
      snapshot_id?: string | null;
    },
    signal?: AbortSignal,
  ) =>
    request<UniverseTimelineSlice>("/api/v1/universe/timeline", {
      method: "POST",
      body: JSON.stringify(body),
      signal,
    }),
  rebuildUniverse: (signal?: AbortSignal) =>
    request<BackgroundJob>("/api/v1/universe/rebuild", {
      method: "POST",
      signal,
    }),
  getJob: (id: string, signal?: AbortSignal) =>
    request<BackgroundJob>(`/api/v1/jobs/${id}`, { signal }),
  listExplorations: (limit = 20) =>
    request<ExplorationSession[]>(
      `/api/v1/universe/explorations?limit=${limit}`,
    ),
  getExploration: (id: string) =>
    request<ExplorationDetail>(`/api/v1/universe/explorations/${id}`),

  // 引用溯源：分块原文
  getChunk: (sourceId: string, chunkId: string) =>
    request<{
      chunk_id: string;
      heading: string;
      content: string;
      source_id: string;
      source_name: string;
    }>(`/api/v1/sources/${sourceId}/chunks/${chunkId}`),

  // 检索结果关联实体
  listEntities: (sid: string) =>
    request<Entity[]>(`/api/v1/sources/${sid}/entities`),
  getSourceGraph: (
    sid: string,
    options?: {
      limit?: number;
      documentIds?: readonly string[];
      signal?: AbortSignal;
    },
  ) => {
    const limit = options?.limit ?? 1_000;
    const params = new URLSearchParams({
      document_limit: String(limit),
      event_limit: String(limit),
      entity_limit: String(limit),
    });
    if (options?.documentIds !== undefined) {
      const documentIds = Array.from(
        new Set(options.documentIds.map((id) => id.trim()).filter(Boolean)),
      );
      if (documentIds.length === 0) params.append("document_ids", "");
      else documentIds.forEach((id) => params.append("document_ids", id));
    }
    return request<SourceGraphResponse>(
      `/api/v1/sources/${sid}/graph?${params.toString()}`,
      { signal: options?.signal },
    );
  },

  // 近期动态（搜索页时间线）
  getActivity: (sourceIds?: string[]) => {
    const params = new URLSearchParams();
    [...new Set(sourceIds?.map((sourceId) => sourceId.trim()).filter(Boolean) ?? [])]
      .forEach((sourceId) => params.append("source_ids", sourceId));
    const query = params.size ? `?${params.toString()}` : "";
    return request<ActivityItem[]>(`/api/v1/activity${query}`);
  },

  // 单文档元数据 + 原始文件（预览用 blob 拉取，需带 Bearer）
  getDocument: (sid: string, did: string) =>
    request<Doc>(`/api/v1/sources/${sid}/documents/${did}`),
  documentFileUrl: (sid: string, did: string) =>
    `${API_BASE}/api/v1/sources/${sid}/documents/${did}/file`,
  documentPreviewUrl: (sid: string, did: string) =>
    `${API_BASE}/api/v1/sources/${sid}/documents/${did}/preview`,
  documentParsedUrl: (sid: string, did: string) =>
    `${API_BASE}/api/v1/sources/${sid}/documents/${did}/parsed`,

  // 对话图片附件（≤10MB，png/jpg/webp/gif）
  uploadAttachment: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<{ id: string; name: string; media_type: string }>(
      "/api/v1/attachments",
      {
        method: "POST",
        body: fd,
      },
    );
  },
  attachmentUrl: (id: string) => `${API_BASE}/api/v1/attachments/${id}`,

  // 信源即 MCP：外部宿主（Claude Desktop / Cursor）挂载信息
  sourceMcp: (sourceId: string) =>
    request<SourceMcpDescriptor>(`/api/v1/sources/${sourceId}/mcp`),

  // 整个 SAG 知识库的 MCP 挂载信息
  knowledgeMcp: () => request<KnowledgeMcpDescriptor>("/api/v1/system/mcp"),
};
