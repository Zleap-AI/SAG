import { API_BASE } from "./api";
import { getToken } from "./auth";
import { getDiagnosticsStore } from "./diagnostics";
import { readClientLocale } from "../i18n/client";
import { clientErrorMessage, serverErrorMessage } from "../i18n/client-errors";

export type AgentEventType =
  | "run.started"
  | "run.completed"
  | "run.failed"
  | "run.cancelled"
  | "turn.started"
  | "turn.completed"
  | "message.started"
  | "message.delta"
  | "message.completed"
  | "tool.started"
  | "tool.progress"
  | "tool.approval_required"
  | "tool.completed"
  | "tool.failed"
  | "universe.activation";

export interface AgentErrorPayload {
  code: string;
  message: string;
  retryable?: boolean;
  details?: Record<string, unknown>;
}

export interface AgentEvent {
  version: number;
  type: AgentEventType;
  run_id: string;
  sequence: number;
  timestamp: string;
  turn: number;
  payload: Record<string, unknown>;
}

export interface AgentRunOutcome {
  status: "completed" | "failed" | "cancelled";
  runId: string;
  messageId?: string;
  error?: AgentErrorPayload;
}

export class AgentHttpError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "AgentHttpError";
  }
}

export class AgentStreamProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AgentStreamProtocolError";
  }
}

const terminalTypes = new Set<AgentEventType>([
  "run.completed",
  "run.failed",
  "run.cancelled",
]);

function toOutcome(event: AgentEvent): AgentRunOutcome {
  const error = event.payload.error as AgentErrorPayload | undefined;
  if (event.type === "run.completed") {
    return {
      status: "completed",
      runId: event.run_id,
      messageId: event.payload.message_id as string | undefined,
    };
  }
  return {
    status: event.type === "run.cancelled" ? "cancelled" : "failed",
    runId: event.run_id,
    error: error
      ? { ...error, message: serverErrorMessage(error.code, error.message) }
      : undefined,
  };
}

function parseFrame(frame: string): AgentEvent | null {
  const lines = frame.split(/\r?\n/);
  let wireType = "";
  const dataLines: string[] = [];
  for (const line of lines) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) wireType = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!dataLines.length) return null;

  let value: unknown;
  try {
    value = JSON.parse(dataLines.join("\n"));
  } catch {
    throw new AgentStreamProtocolError(clientErrorMessage("unreadableAgentEvent"));
  }
  if (!value || typeof value !== "object") {
    throw new AgentStreamProtocolError(clientErrorMessage("agentEventNotObject"));
  }
  const event = value as AgentEvent;
  if (
    typeof event.version !== "number" ||
    typeof event.type !== "string" ||
    typeof event.run_id !== "string" ||
    typeof event.sequence !== "number" ||
    !event.payload ||
    typeof event.payload !== "object"
  ) {
    throw new AgentStreamProtocolError(clientErrorMessage("agentEventMissingFields"));
  }
  if (wireType && wireType !== event.type) {
    throw new AgentStreamProtocolError(clientErrorMessage("agentEventTypeMismatch"));
  }
  return event;
}

async function streamPost(
  path: string,
  body: Record<string, unknown>,
  onEvent: (event: AgentEvent) => void,
  signal?: AbortSignal,
): Promise<AgentRunOutcome> {
  const token = getToken();
  const startMs = Date.now();
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept-Language": readClientLocale(),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(body),
      signal,
    });
  } catch (e) {
    const durationMs = Date.now() - startMs;
    getDiagnosticsStore().record("error", {
      source: "sse",
      path,
      duration_ms: durationMs,
      error_code: e instanceof DOMException && e.name === "AbortError" ? "aborted" : "network",
      error_message: e instanceof DOMException && e.name === "AbortError"
        ? "SSE connection cancelled"
        : "SSE connection failed — server unreachable",
    });
    throw e;
  }

  if (!response.ok || !response.body) {
    const durationMs = Date.now() - startMs;
    let message = clientErrorMessage("generationFailed");
    let code = "http_error";
    let layer: string | undefined;
    let stage: string | undefined;
    let retryable: boolean | undefined;
    try {
      const value = await response.json();
      code = value?.error?.code || code;
      const detail = value?.error?.message || value?.detail;
      message = typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : message;
      layer = value?.error?.layer ?? undefined;
      stage = value?.error?.stage ?? undefined;
      retryable = typeof value?.error?.retryable === "boolean" ? value.error.retryable : undefined;
    } catch {
      // Keep the stable fallback when a proxy returns HTML or an empty body.
    }
    getDiagnosticsStore().record("error", {
      source: "sse",
      path,
      duration_ms: durationMs,
      status: response.status,
      error_code: code,
      error_message: message,
      error_layer: layer,
      error_stage: stage,
      retryable,
      request_id: response.headers.get("X-Request-Id") ?? undefined,
    });
    throw new AgentHttpError(
      serverErrorMessage(code, message, response.status),
      code,
      response.status,
    );
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminal: AgentRunOutcome | null = null;

  const dispatch = (frame: string) => {
    const event = parseFrame(frame);
    if (!event) return;
    if (terminal) {
      throw new AgentStreamProtocolError(clientErrorMessage("eventAfterTerminal"));
    }
    onEvent(event);
    if (terminalTypes.has(event.type)) terminal = toOutcome(event);
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let separator = buffer.match(/\r?\n\r?\n/);
    while (separator?.index !== undefined) {
      const frame = buffer.slice(0, separator.index);
      buffer = buffer.slice(separator.index + separator[0].length);
      if (frame.trim()) dispatch(frame);
      separator = buffer.match(/\r?\n\r?\n/);
    }
  }

  buffer += decoder.decode();
  if (buffer.trim()) dispatch(buffer);
  if (!terminal) {
    throw new AgentStreamProtocolError(clientErrorMessage("missingTerminalEvent"));
  }
  return terminal;
}

export function streamAgentAsk(
  agentId: string,
  threadId: string,
  body: {
    query: string;
    attachments?: string[];
    source_ids?: string[];
    knowledge_only?: boolean;
    web_enabled: boolean;
  },
  onEvent: (event: AgentEvent) => void,
  signal?: AbortSignal,
): Promise<AgentRunOutcome> {
  return streamPost(
    `/api/v1/agents/${agentId}/threads/${threadId}/ask`,
    body,
    onEvent,
    signal,
  );
}
