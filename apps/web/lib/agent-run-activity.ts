import type { AgentEvent } from "./sse";
import type { Citation, CitationEventRef, MessageStep } from "./types";
import { clientErrorMessage, serverErrorMessage } from "../i18n/client-errors";

export interface LiveStep extends MessageStep {
  id: string;
  status: "active" | "done" | "error";
  startedAt: number;
  progress?: string;
}

export interface ToolScope {
  kind: "knowledge" | "internet" | null;
  sources: NonNullable<NonNullable<MessageStep["details"]>["sources"]>;
}

export function toolScope(step: MessageStep): ToolScope {
  if (step.name === "web_search" || step.details?.scope === "internet") {
    return { kind: "internet", sources: [] };
  }
  const isKnowledgeTool = step.name === "search_context" || step.name === "get_entity";
  if (step.details?.scope === "knowledge" || isKnowledgeTool) {
    const sources = step.details?.sources?.filter((source) => Boolean(source.name)) ?? [];
    return { kind: "knowledge", sources };
  }
  return { kind: null, sources: [] };
}

export function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function toolArguments(value: unknown): Record<string, unknown> {
  return objectValue(value);
}

export function toolArgumentsPreview(value: unknown, limit = 60): string {
  const text = Object.entries(toolArguments(value))
    .filter(([, item]) => item !== null && item !== undefined)
    .map(([key, item]) => `${key}=${typeof item === "string" ? item : JSON.stringify(item)}`)
    .join("; ");
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

function optionalText(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function safeExternalUrl(value: unknown): string | null {
  if (typeof value !== "string" || !value || value !== value.trim() || /\s/.test(value)) {
    return null;
  }
  try {
    const parsed = new URL(value);
    if (!(["http:", "https:"] as const).includes(parsed.protocol as "http:" | "https:")) {
      return null;
    }
    if (!parsed.hostname || parsed.username || parsed.password) return null;
    return value;
  } catch {
    return null;
  }
}

function citationMapping(value: Record<string, unknown>): Partial<Citation> {
  const mapping: Partial<Citation> = {};
  if (typeof value.mapped === "boolean") mapping.mapped = value.mapped;
  if (value.claim_level === "claim" || value.claim_level === "run") {
    mapping.claim_level = value.claim_level;
  }
  return mapping;
}

function normalizeEventRefs(value: unknown): CitationEventRef[] {
  if (!Array.isArray(value)) return [];
  const refs: CitationEventRef[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    const raw = item as Record<string, unknown>;
    const title = optionalText(raw.title)?.trim();
    if (!title) continue;
    const event: CitationEventRef = { title };
    const id = optionalText(raw.id)?.trim();
    const content = optionalText(raw.content)?.trim();
    const summary = optionalText(raw.summary)?.trim();
    const category = optionalText(raw.category)?.trim();
    if (id) event.id = id;
    if (content) event.content = content;
    if (summary) event.summary = summary;
    if (category) event.category = category;
    refs.push(event);
    if (refs.length >= 3) break;
  }
  return refs;
}

function normalizeCitation(value: unknown): Citation | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const citation = value as Record<string, unknown>;
  const n = citation.n;
  if (
    typeof n !== "number"
    || !Number.isFinite(n)
    || !Number.isInteger(n)
    || n < 1
  ) {
    return null;
  }
  if (citation.kind !== undefined && citation.kind !== "internal" && citation.kind !== "external") {
    return null;
  }

  if (citation.kind === "external") {
    const url = safeExternalUrl(citation.url);
    if (!url) return null;
    const parsed = new URL(url);
    const title = optionalText(citation.title)?.trim() || parsed.hostname;
    const source = optionalText(citation.source)?.trim() || parsed.hostname;
    const summary = optionalText(citation.summary)?.trim() || undefined;
    const snippet = optionalText(citation.snippet)?.trim() || "";
    return {
      n,
      kind: "external",
      chunk_id: null,
      heading: title,
      snippet,
      score: 0,
      source_id: null,
      source_name: source,
      url,
      title,
      source,
      ...(summary ? { summary } : {}),
      ...citationMapping(citation),
    };
  }

  const chunkId = citation.chunk_id;
  const sourceId = citation.source_id;
  const score = citation.score;
  if (
    !(chunkId === null || typeof chunkId === "string")
    || !(sourceId === null || typeof sourceId === "string")
    || typeof citation.heading !== "string"
    || typeof citation.snippet !== "string"
    || typeof score !== "number"
    || !Number.isFinite(score)
  ) {
    return null;
  }
  const normalized: Citation = {
    n,
    kind: "internal",
    chunk_id: chunkId,
    heading: citation.heading,
    snippet: citation.snippet,
    score,
    source_id: sourceId,
    ...citationMapping(citation),
  };
  const sourceName = optionalText(citation.source_name);
  if (sourceName !== null) normalized.source_name = sourceName;
  const eventRefs = normalizeEventRefs(citation.event_refs);
  if (eventRefs.length) normalized.event_refs = eventRefs;
  return normalized;
}

export function citationsFromArtifacts(value: unknown): Citation[] {
  const citations = objectValue(value).citations;
  return Array.isArray(citations)
    ? citations.map(normalizeCitation).filter((citation): citation is Citation => citation !== null)
    : [];
}

export function mergeCitations(current: Citation[], incoming: Citation[]): Citation[] {
  if (!incoming.length) return current;
  const key = (citation: Citation) => citation.kind === "external"
    ? `external:${citation.url ?? citation.n}`
    : `internal:${citation.n}`;
  const byReference = new Map(current.map((citation) => [key(citation), citation]));
  incoming.forEach((citation) => byReference.set(key(citation), citation));
  return [...byReference.values()].sort((left, right) => {
    if (left.kind === "external" && right.kind !== "external") return 1;
    if (left.kind !== "external" && right.kind === "external") return -1;
    return left.n - right.n;
  });
}

function elapsed(step: LiveStep, now: number): number {
  return step.ms ?? Math.max(0, now - step.startedAt);
}

export function settleActiveSteps(
  steps: LiveStep[],
  now: number,
  status: "done" | "error" = "done",
  error?: string,
  exceptId?: string,
): LiveStep[] {
  let changed = false;
  const next = steps.map((step) => {
    if (step.status !== "active" || step.id === exceptId) return step;
    changed = true;
    return {
      ...step,
      status,
      ms: elapsed(step, now),
      error: error ?? step.error,
    };
  });
  return changed ? next : steps;
}

function settleActiveModelSteps(steps: LiveStep[], now: number): LiveStep[] {
  let changed = false;
  const next = steps.map((step) => {
    if (step.status !== "active" || step.kind === "tool") return step;
    changed = true;
    return { ...step, status: "done" as const, ms: elapsed(step, now) };
  });
  return changed ? next : steps;
}

function toolDetails(value: unknown): NonNullable<MessageStep["details"]> {
  return objectValue(value) as NonNullable<MessageStep["details"]>;
}

function numericDuration(value: unknown): number {
  const duration = Number(value ?? 0);
  return Number.isFinite(duration) && duration >= 0 ? duration : 0;
}

function upsertToolStep(steps: LiveStep[], event: AgentEvent, now: number): LiveStep[] {
  const payload = event.payload;
  const id = String(payload.tool_call_id ?? `tool-${event.turn}`);
  const argumentsValue = toolArguments(payload.arguments);
  const existingIndex = steps.findIndex((step) => step.id === id);
  if (existingIndex >= 0) {
    const existing = steps[existingIndex];
    const replacement: LiveStep = {
      ...existing,
      kind: "tool",
      name: String(payload.name ?? existing.name ?? ""),
      label: String(payload.label ?? existing.label ?? payload.name ?? clientErrorMessage("tool")),
      args: toolArgumentsPreview(argumentsValue) || existing.args,
      arguments: Object.keys(argumentsValue).length ? argumentsValue : existing.arguments,
      status: "active",
      error: undefined,
    };
    const next = [...steps];
    next[existingIndex] = replacement;
    return next;
  }
  return [
    ...steps,
    {
      id,
      kind: "tool",
      name: String(payload.name ?? ""),
      label: String(payload.label ?? payload.name ?? clientErrorMessage("tool")),
      args: toolArgumentsPreview(argumentsValue),
      arguments: argumentsValue,
      step: event.turn,
      status: "active",
      startedAt: now,
    },
  ];
}

function finishToolStep(
  steps: LiveStep[],
  event: AgentEvent,
  now: number,
  status: "done" | "error",
): LiveStep[] {
  const payload = event.payload;
  const id = String(payload.tool_call_id ?? `tool-${event.turn}`);
  const duration = numericDuration(payload.duration_ms);
  const failure = objectValue(payload.error);
  const details = toolDetails(payload.details);
  const existingIndex = steps.findIndex((step) => step.id === id);
  const replacement: LiveStep = {
    ...(existingIndex >= 0
      ? steps[existingIndex]
      : {
          id,
          kind: "tool" as const,
          step: event.turn,
          startedAt: Math.max(0, now - duration),
        }),
    name: String(payload.name ?? (existingIndex >= 0 ? steps[existingIndex].name ?? "" : "")),
    label: String(
      payload.label ??
        (existingIndex >= 0 ? steps[existingIndex].label : undefined) ??
        payload.name ??
        clientErrorMessage("tool"),
    ),
    status,
    ms: duration,
    count: Number(details.count ?? 0),
    details,
    error: status === "error"
      ? serverErrorMessage(
          typeof failure.code === "string" ? failure.code : "tool_failed",
          String(failure.message ?? clientErrorMessage("toolFailed")),
        )
      : undefined,
  };
  if (existingIndex < 0) return [...steps, replacement];
  const next = [...steps];
  next[existingIndex] = replacement;
  return next;
}

function startAnswerStep(steps: LiveStep[], event: AgentEvent, now: number): LiveStep[] {
  const activeAnswer = steps.find(
    (step) => step.kind === "answer" && step.step === event.turn && step.status === "active",
  );
  if (activeAnswer) return steps;

  const thinkingIndex = steps.findIndex(
    (step) => step.kind === "thinking" && step.step === event.turn && step.status === "active",
  );
  if (thinkingIndex >= 0) {
    const next = [...steps];
    next[thinkingIndex] = {
      ...steps[thinkingIndex],
      id: `answer-${event.turn}`,
      kind: "answer",
    };
    return next;
  }

  return [
    ...settleActiveSteps(steps, now),
    {
      id: `answer-${event.turn}`,
      kind: "answer",
      step: event.turn,
      status: "active",
      startedAt: now,
    },
  ];
}

function finishModelStep(steps: LiveStep[], event: AgentEvent, now: number): LiveStep[] {
  const payload = event.payload;
  const message = objectValue(payload.message);
  if (message.role !== "assistant") return steps;
  const kind: LiveStep["kind"] = payload.has_tool_calls ? "thinking" : "answer";
  const id = `${kind}-${event.turn}`;
  const duration = numericDuration(payload.duration_ms);
  const existingIndex = steps.findIndex(
    (step) => step.kind === kind && step.step === event.turn,
  );
  const replacement: LiveStep = {
    ...(existingIndex >= 0
      ? steps[existingIndex]
      : {
          id,
          kind,
          step: event.turn,
          startedAt: Math.max(0, now - duration),
        }),
    status: "done",
    ms: duration,
  };
  if (existingIndex < 0) return [...steps, replacement];
  const next = [...steps];
  next[existingIndex] = replacement;
  return next;
}

/**
 * Reduces observable Agent events into UI activity. It deliberately records
 * phases, tool inputs/results, and duration only; it never invents or exposes
 * a hidden chain of thought.
 */
export function reduceAgentRunSteps(
  steps: LiveStep[],
  event: AgentEvent,
  now = Date.now(),
): LiveStep[] {
  switch (event.type) {
    case "turn.started": {
      const settled = settleActiveSteps(steps, now);
      const id = `thinking-${event.turn}`;
      if (settled.some((step) => step.id === id)) return settled;
      return [
        ...settled,
        {
          id,
          kind: "thinking",
          step: event.turn,
          status: "active",
          startedAt: now,
        },
      ];
    }
    case "tool.approval_required":
    case "tool.started": {
      return upsertToolStep(settleActiveModelSteps(steps, now), event, now);
    }
    case "tool.progress": {
      const id = String(event.payload.tool_call_id ?? `tool-${event.turn}`);
      const index = steps.findIndex((step) => step.id === id);
      if (index < 0) return steps;
      const details = toolDetails(event.payload.details);
      const next = [...steps];
      next[index] = {
        ...steps[index],
        progress: String(event.payload.message ?? ""),
        details: { ...steps[index].details, ...details },
      };
      return next;
    }
    case "tool.completed":
      return finishToolStep(steps, event, now, "done");
    case "tool.failed":
      return finishToolStep(steps, event, now, "error");
    case "message.delta":
      return event.payload.role === "tool" ? steps : startAnswerStep(steps, event, now);
    case "message.completed":
      return finishModelStep(steps, event, now);
    case "run.completed":
      return settleActiveSteps(steps, now);
    case "run.cancelled":
      return settleActiveSteps(steps, now, "error", clientErrorMessage("stopped"));
    case "run.failed": {
      const error = objectValue(event.payload.error);
      return settleActiveSteps(
        steps,
        now,
        "error",
        serverErrorMessage(
          typeof error.code === "string" ? error.code : "generation_failed",
          String(error.message ?? clientErrorMessage("generationFailed")),
        ),
      );
    }
    default:
      return steps;
  }
}
