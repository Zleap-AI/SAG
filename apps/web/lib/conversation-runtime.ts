import {
  citationsFromArtifacts,
  mergeCitations,
  objectValue,
  reduceAgentRunSteps,
  settleActiveSteps,
  type LiveStep,
} from "./agent-run-activity";
import type { AgentEvent, AgentRunOutcome } from "./sse";
import { clientErrorMessage } from "../i18n/client-errors";
import type {
  Citation,
  Message,
  MessageAttachment,
  MessagePage,
  MessageStep,
  UniverseActivation,
} from "./types";

const HISTORY_PAGE_FLOOR = 40;
const DEFAULT_MAX_MESSAGES_PER_SESSION = 200;

export interface ConversationTransport {
  createThread(input: {
    agentId: string;
    title: string;
    signal: AbortSignal;
  }): Promise<{ id: string }>;
  listMessages(input: {
    agentId: string;
    threadId: string;
    cursor?: string | null;
    signal: AbortSignal;
  }): Promise<MessagePage>;
  stream(input: {
    agentId: string;
    threadId: string;
    query: string;
    attachmentIds?: string[];
    sourceIds?: string[];
    knowledgeOnly?: boolean;
    webEnabled: boolean;
    onEvent: (event: AgentEvent) => void;
    signal: AbortSignal;
  }): Promise<AgentRunOutcome>;
  cancelRun(input: { agentId: string; threadId: string; runId: string }): Promise<unknown>;
  approveTool(input: {
    agentId: string;
    threadId: string;
    runId: string;
    toolCallId: string;
  }): Promise<unknown>;
  rejectTool(input: {
    agentId: string;
    threadId: string;
    runId: string;
    toolCallId: string;
    reason: string;
  }): Promise<unknown>;
  deleteMessage?(input: {
    agentId: string;
    threadId: string;
    messageId: string;
  }): Promise<unknown>;
}

export type ConversationDelivery =
  | "persisted"
  | "pending"
  | "streaming"
  | "complete"
  | "failed"
  | "cancelled";

export interface ConversationMessage {
  id: string;
  threadId: string | null;
  role: "user" | "assistant" | "system";
  content: string;
  citations: Citation[];
  attachments: MessageAttachment[];
  steps: MessageStep[];
  createdAt: string;
  delivery: ConversationDelivery;
  promptPreview?: string;
  universeActivation?: UniverseActivation;
  /** 失败/取消时的可读错误文本。UI 用它在气泡内展示错误说明。 */
  errorMessage?: string;
}

export type ConversationHistoryStatus = "idle" | "loading" | "ready" | "error";

export interface ConversationHistoryState {
  status: ConversationHistoryStatus;
  requestId: number;
  hasMore: boolean;
  nextCursor: string | null;
  error: string | null;
}

export interface ConversationToolApproval {
  toolCallId: string;
  label: string;
  risk: string;
  arguments: Record<string, unknown>;
  resolving: boolean;
}

export interface ConversationRunState {
  requestId: string;
  runId: string | null;
  lifecycle: "preparing" | "running" | "stopping";
  startedAt: number;
  assistantMessageId: string;
  steps: LiveStep[];
  pendingApproval: ConversationToolApproval | null;
}

export interface ConversationSessionSnapshot {
  sessionId: string;
  agentId: string;
  threadId: string | null;
  messages: ConversationMessage[];
  history: ConversationHistoryState;
  run: ConversationRunState | null;
  error: string | null;
}

export interface ConversationIndexEntry {
  sessionId: string;
  threadId: string | null;
  running: boolean;
  startedAt: number | null;
}

export interface ConversationIndexSnapshot {
  activeSessionId: string | null;
  activeRunSessionId: string | null;
  sessions: ConversationIndexEntry[];
}

export interface ConversationSendInput {
  query: string;
  attachmentIds?: string[];
  sourceIds?: string[];
  knowledgeOnly?: boolean;
  webEnabled?: boolean;
  title?: string;
}

export interface ConversationSendResult {
  threadId: string;
  outcome: AgentRunOutcome;
}

export interface ConversationRuntimeCallbacks {
  onActivity?: () => void | Promise<void>;
  onUniverseActivation?: (activation: UniverseActivation) => void;
}

export interface ConversationRuntimeOptions extends ConversationRuntimeCallbacks {
  agentId: string;
  transport: ConversationTransport;
  flushIntervalMs?: number;
  stopGraceMs?: number;
  maxSessions?: number;
  maxMessagesPerSession?: number;
  now?: () => number;
  createId?: (prefix: string) => string;
}

interface SessionRecord {
  snapshot: ConversationSessionSnapshot;
  listeners: Set<() => void>;
  lastAccess: number;
}

interface HistoryOperation {
  requestId: number;
  controller: AbortController;
}

interface ActiveOperation {
  sessionId: string;
  requestId: string;
  userMessageId: string;
  assistantMessageId: string;
  controller: AbortController;
  runId: string | null;
  lastSequence: number;
  seenTypesAtLastSequence: Set<string>;
  pendingTokens: string;
  flushTimer: ReturnType<typeof setTimeout> | null;
  stopTimer: ReturnType<typeof setTimeout> | null;
  stopRequested: boolean;
}

export class ConversationBusyError extends Error {
  constructor() {
    super(clientErrorMessage("conversationBusy"));
    this.name = "ConversationBusyError";
  }
}

export class ConversationDisposedError extends Error {
  constructor() {
    super("Conversation runtime has been disposed");
    this.name = "ConversationDisposedError";
  }
}

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

function isAbortError(reason: unknown): boolean {
  return reason instanceof DOMException
    ? reason.name === "AbortError"
    : reason instanceof Error && reason.name === "AbortError";
}

function normalizeMessage(message: Message): ConversationMessage {
  const status = message.status ?? "ok";
  const delivery: ConversationDelivery =
    status === "failed" ? "failed" : status === "cancelled" ? "cancelled" : "persisted";
  const errorMessage =
    status !== "ok" && message.error && typeof message.error.message === "string"
      ? message.error.message
      : undefined;
  return {
    id: message.id,
    threadId: message.thread_id,
    role: message.role,
    content: message.content,
    citations: citationsFromArtifacts({ citations: message.citations }),
    attachments: Array.isArray(message.attachments) ? message.attachments : [],
    steps: Array.isArray(message.steps) ? message.steps : [],
    promptPreview:
      typeof message.prompt_preview === "string" && message.prompt_preview
        ? message.prompt_preview
        : undefined,
    createdAt: message.created_at,
    delivery,
    errorMessage,
  };
}

function persistentSteps(steps: LiveStep[]): MessageStep[] {
  return steps.map((step) => {
    const persisted: Partial<LiveStep> = { ...step };
    delete persisted.id;
    delete persisted.status;
    delete persisted.startedAt;
    delete persisted.progress;
    return persisted as MessageStep;
  });
}

function updateMessage(
  messages: ConversationMessage[],
  id: string,
  update: (message: ConversationMessage) => ConversationMessage,
): ConversationMessage[] {
  const index = messages.findIndex((message) => message.id === id);
  if (index < 0) return messages;
  const next = [...messages];
  next[index] = update(messages[index]);
  return next;
}

function mergeHistory(
  persisted: ConversationMessage[],
  current: ConversationMessage[],
): ConversationMessage[] {
  const uniquePersisted: ConversationMessage[] = [];
  const ids = new Set<string>();
  const currentById = new Map(current.map((message) => [message.id, message]));
  persisted.forEach((message) => {
    if (ids.has(message.id)) return;
    ids.add(message.id);
    const local = currentById.get(message.id);
    uniquePersisted.push(
      local
        ? {
            ...message,
            promptPreview: local.promptPreview,
            universeActivation: local.universeActivation,
          }
        : message,
    );
  });
  const local = current.filter(
    (message) =>
      // 只有真正“进行中”的本地气泡才保留：complete/failed/cancelled 一旦被后端确认落库
      // （outcome.messageId 存在），会经 finishOperation 改写 id 后走 uniquePersisted 分支合并；
      // 未落库的终态气泡由 finishOperation 直接跳过 ensureHistory({force:true}) 兜底，
      // 因此这里不需要留在 local。
      (message.delivery === "pending" || message.delivery === "streaming") &&
      !ids.has(message.id),
  );
  return [...uniquePersisted, ...local];
}

function prependHistory(
  older: ConversationMessage[],
  current: ConversationMessage[],
): ConversationMessage[] {
  const ids = new Set(current.map((message) => message.id));
  const uniqueOlder: ConversationMessage[] = [];
  older.forEach((message) => {
    if (ids.has(message.id)) return;
    ids.add(message.id);
    uniqueOlder.push(message);
  });
  return [...uniqueOlder, ...current];
}

export class ConversationRuntime {
  readonly agentId: string;

  private readonly transport: ConversationTransport;
  private readonly flushIntervalMs: number;
  private readonly stopGraceMs: number;
  private readonly maxSessions: number;
  private readonly maxMessagesPerSession: number;
  private readonly now: () => number;
  private readonly createId: (prefix: string) => string;
  private callbacks: ConversationRuntimeCallbacks;
  private sessions = new Map<string, SessionRecord>();
  private sessionsByThread = new Map<string, string>();
  private historyOperations = new Map<string, HistoryOperation>();
  private indexListeners = new Set<() => void>();
  private indexSnapshot: ConversationIndexSnapshot = {
    activeSessionId: null,
    activeRunSessionId: null,
    sessions: [],
  };
  private activeOperation: ActiveOperation | null = null;
  private activeSessionId: string | null = null;
  private serial = 0;
  private accessSerial = 0;
  private disposed = false;

  constructor(options: ConversationRuntimeOptions) {
    this.agentId = options.agentId;
    this.transport = options.transport;
    this.flushIntervalMs = Math.max(0, options.flushIntervalMs ?? 50);
    this.stopGraceMs = Math.max(0, options.stopGraceMs ?? 5_000);
    this.maxSessions = Math.max(1, Math.floor(options.maxSessions ?? 12));
    this.maxMessagesPerSession = Math.max(
      HISTORY_PAGE_FLOOR,
      Math.floor(options.maxMessagesPerSession ?? DEFAULT_MAX_MESSAGES_PER_SESSION),
    );
    this.now = options.now ?? Date.now;
    this.createId = options.createId ?? ((prefix) => `${prefix}-${this.now()}-${++this.serial}`);
    this.callbacks = {
      onActivity: options.onActivity,
      onUniverseActivation: options.onUniverseActivation,
    };
  }

  setCallbacks(callbacks: ConversationRuntimeCallbacks): void {
    this.callbacks = callbacks;
  }

  createDraft(options: { activate?: boolean } = {}): string {
    this.assertAvailable();
    const sessionId = this.createId("conversation");
    this.sessions.set(sessionId, {
      snapshot: this.initialSnapshot(sessionId, null),
      listeners: new Set(),
      lastAccess: ++this.accessSerial,
    });
    if (options.activate !== false || this.activeSessionId === null) {
      this.activeSessionId = sessionId;
    }
    this.pruneSessions();
    this.refreshIndex();
    return sessionId;
  }

  forThread(threadId: string, options: { activate?: boolean } = {}): string {
    this.assertAvailable();
    const existing = this.sessionsByThread.get(threadId);
    if (existing) {
      this.touch(existing);
      if (options.activate) this.activate(existing);
      return existing;
    }
    const sessionId = this.createId("conversation");
    this.sessions.set(sessionId, {
      snapshot: this.initialSnapshot(sessionId, threadId),
      listeners: new Set(),
      lastAccess: ++this.accessSerial,
    });
    this.sessionsByThread.set(threadId, sessionId);
    if (options.activate || this.activeSessionId === null) this.activeSessionId = sessionId;
    this.pruneSessions();
    this.refreshIndex();
    return sessionId;
  }

  activate(sessionId: string): void {
    this.record(sessionId);
    this.touch(sessionId);
    if (this.activeSessionId === sessionId) return;
    this.activeSessionId = sessionId;
    this.pruneSessions();
    this.refreshIndex();
  }

  getSessionSnapshot(sessionId: string): ConversationSessionSnapshot {
    return this.record(sessionId).snapshot;
  }

  subscribeSession(sessionId: string, listener: () => void): () => void {
    const record = this.record(sessionId);
    this.touch(sessionId);
    record.listeners.add(listener);
    return () => {
      record.listeners.delete(listener);
      this.pruneSessions();
      this.refreshIndex();
    };
  }

  getIndexSnapshot = (): ConversationIndexSnapshot => this.indexSnapshot;

  subscribeIndex = (listener: () => void): (() => void) => {
    this.indexListeners.add(listener);
    return () => this.indexListeners.delete(listener);
  };

  async ensureHistory(sessionId: string, options: { force?: boolean } = {}): Promise<void> {
    this.assertAvailable();
    const current = this.record(sessionId).snapshot;
    if (!current.threadId) {
      this.updateSession(sessionId, (snapshot) => ({
        ...snapshot,
        history: { ...snapshot.history, status: "ready", error: null },
      }));
      return;
    }
    if (!options.force && (current.history.status === "loading" || current.history.status === "ready")) {
      return;
    }
    await this.loadHistoryPage(sessionId, null, "replace");
  }

  async loadOlder(sessionId: string): Promise<void> {
    this.assertAvailable();
    const current = this.record(sessionId).snapshot;
    if (current.messages.length >= this.maxMessagesPerSession) {
      this.updateSession(sessionId, (snapshot) => ({
        ...snapshot,
        history: {
          ...snapshot.history,
          status: "ready",
          hasMore: false,
          nextCursor: null,
          error: null,
        },
      }));
      return;
    }
    if (
      !current.threadId ||
      current.history.status === "loading" ||
      !current.history.hasMore ||
      !current.history.nextCursor
    ) {
      return;
    }
    await this.loadHistoryPage(sessionId, current.history.nextCursor, "prepend");
  }

  private async loadHistoryPage(
    sessionId: string,
    cursor: string | null,
    mode: "replace" | "prepend",
  ): Promise<void> {
    const current = this.record(sessionId).snapshot;
    if (!current.threadId) return;
    const previous = this.historyOperations.get(sessionId);
    previous?.controller.abort();
    const controller = new AbortController();
    const requestId = current.history.requestId + 1;
    this.historyOperations.set(sessionId, { requestId, controller });
    this.updateSession(sessionId, (snapshot) => ({
      ...snapshot,
      history: {
        ...snapshot.history,
        requestId,
        status: "loading",
        error: null,
      },
    }));

    try {
      const page = await this.transport.listMessages({
        agentId: this.agentId,
        threadId: current.threadId,
        cursor,
        signal: controller.signal,
      });
      if (!this.isCurrentHistory(sessionId, requestId)) return;
      const persisted = page.items.map(normalizeMessage);
      this.updateSession(sessionId, (snapshot) => {
        const currentIds = new Set(snapshot.messages.map((message) => message.id));
        const merged =
          mode === "prepend"
            ? prependHistory(persisted, snapshot.messages)
            : mergeHistory(persisted, snapshot.messages);
        const messages = this.boundMessages(sessionId, merged);
        const reachedWindow = messages.length >= this.maxMessagesPerSession;
        const addedMessage =
          mode === "replace" || persisted.some((message) => !currentIds.has(message.id));
        const canContinue =
          !reachedWindow && addedMessage && page.has_more && Boolean(page.next_cursor);
        return {
          ...snapshot,
          messages,
          history: {
            requestId,
            status: "ready",
            hasMore: canContinue,
            nextCursor: canContinue ? page.next_cursor : null,
            error: null,
          },
        };
      });
    } catch (reason) {
      if (!this.isCurrentHistory(sessionId, requestId) || isAbortError(reason)) return;
      this.updateSession(sessionId, (snapshot) => ({
        ...snapshot,
        history: {
          ...snapshot.history,
          status: "error",
          error: errorMessage(reason, clientErrorMessage("historyLoadFailed")),
        },
      }));
    } finally {
      const operation = this.historyOperations.get(sessionId);
      if (operation?.requestId === requestId) this.historyOperations.delete(sessionId);
      this.pruneSessions();
    }
  }

  async send(sessionId: string, input: ConversationSendInput): Promise<ConversationSendResult> {
    this.assertAvailable();
    this.record(sessionId);
    const query = input.query.trim();
    const attachmentIds = [...(input.attachmentIds ?? [])];
    if (!query && !attachmentIds.length) {
      throw new Error(clientErrorMessage("queryOrAttachmentRequired"));
    }
    if (this.activeOperation) throw new ConversationBusyError();

    const startedAt = this.now();
    const requestId = this.createId("request");
    const userMessageId = this.createId("local-user");
    const assistantMessageId = this.createId("local-assistant");
    const controller = new AbortController();
    const operation: ActiveOperation = {
      sessionId,
      requestId,
      userMessageId,
      assistantMessageId,
      controller,
      runId: null,
      lastSequence: -1,
      seenTypesAtLastSequence: new Set(),
      pendingTokens: "",
      flushTimer: null,
      stopTimer: null,
      stopRequested: false,
    };
    this.activeOperation = operation;
    this.touch(sessionId);

    this.updateSession(
      sessionId,
      (snapshot) => ({
        ...snapshot,
        error: null,
        messages: this.boundMessages(sessionId, [
          ...snapshot.messages,
          {
            id: userMessageId,
            threadId: snapshot.threadId,
            role: "user",
            content: query,
            citations: [],
            attachments: attachmentIds.map((id) => ({ id })),
            steps: [],
            createdAt: new Date(startedAt).toISOString(),
            delivery: "pending",
          },
          {
            id: assistantMessageId,
            threadId: snapshot.threadId,
            role: "assistant",
            content: "",
            citations: [],
            attachments: [],
            steps: [],
            createdAt: new Date(startedAt).toISOString(),
            delivery: "streaming",
          },
        ]),
        run: {
          requestId,
          runId: null,
          lifecycle: "preparing",
          startedAt,
          assistantMessageId,
          steps: [],
          pendingApproval: null,
        },
      }),
      true,
    );

    try {
      let snapshot = this.getSessionSnapshot(sessionId);
      if (!snapshot.threadId) {
        const thread = await this.transport.createThread({
          agentId: this.agentId,
          title: (input.title ?? query).slice(0, 80) || clientErrorMessage("newConversation"),
          signal: controller.signal,
        });
        if (!this.isCurrentOperation(operation)) throw new DOMException("Aborted", "AbortError");
        this.bindThread(sessionId, thread.id);
        snapshot = this.getSessionSnapshot(sessionId);
        this.notifyActivity();
      }
      const threadId = snapshot.threadId;
      if (!threadId) throw new Error(clientErrorMessage("conversationCreateFailed"));

      this.updateSession(sessionId, (current) =>
        current.run?.requestId === requestId
          ? { ...current, run: { ...current.run, lifecycle: "running" } }
          : current,
      );
      const outcome = await this.transport.stream({
        agentId: this.agentId,
        threadId,
        query,
        attachmentIds: attachmentIds.length ? attachmentIds : undefined,
        sourceIds: input.sourceIds?.length ? [...input.sourceIds] : undefined,
        knowledgeOnly: input.knowledgeOnly,
        webEnabled: input.webEnabled === true,
        onEvent: (event) => this.handleEvent(operation, event),
        signal: controller.signal,
      });
      await this.finishOperation(operation, outcome);
      return { threadId, outcome };
    } catch (reason) {
      const cancelled = operation.stopRequested || isAbortError(reason);
      const outcome: AgentRunOutcome = cancelled
        ? { status: "cancelled", runId: operation.runId ?? "" }
        : {
            status: "failed",
            runId: operation.runId ?? "",
            error: {
              code: "stream_error",
              message: errorMessage(reason, clientErrorMessage("connectionInterrupted")),
            },
          };
      await this.finishOperation(operation, outcome);
      if (cancelled) {
        const threadId = this.getSessionSnapshot(sessionId).threadId;
        if (!threadId) throw reason;
        return { threadId, outcome };
      }
      throw reason;
    }
  }

  async stop(sessionId: string): Promise<void> {
    this.assertAvailable();
    const operation = this.activeOperation;
    if (!operation || operation.sessionId !== sessionId) return;
    operation.stopRequested = true;
    this.updateSession(sessionId, (snapshot) =>
      snapshot.run?.requestId === operation.requestId
        ? { ...snapshot, run: { ...snapshot.run, lifecycle: "stopping" } }
        : snapshot,
    );
    const snapshot = this.getSessionSnapshot(sessionId);
    if (!snapshot.threadId || !operation.runId) {
      operation.controller.abort();
      return;
    }
    try {
      await this.transport.cancelRun({
        agentId: this.agentId,
        threadId: snapshot.threadId,
        runId: operation.runId,
      });
      if (!this.isCurrentOperation(operation)) return;
      operation.stopTimer = setTimeout(() => operation.controller.abort(), this.stopGraceMs);
    } catch (reason) {
      operation.controller.abort();
      throw reason;
    }
  }

  async approve(sessionId: string, toolCallId: string): Promise<void> {
    await this.resolveApproval(sessionId, toolCallId, true);
  }

  async reject(
    sessionId: string,
    toolCallId: string,
    reason = clientErrorMessage("userRejected"),
  ): Promise<void> {
    await this.resolveApproval(sessionId, toolCallId, false, reason);
  }

  async retry(sessionId: string, assistantMessageId: string): Promise<ConversationSendResult> {
    const snapshot = this.getSessionSnapshot(sessionId);
    const index = snapshot.messages.findIndex((message) => message.id === assistantMessageId);
    if (index < 0) throw new Error(clientErrorMessage("messageMissing"));
    const user = [...snapshot.messages.slice(0, index)]
      .reverse()
      .find((message) => message.role === "user");
    if (!user) throw new Error(clientErrorMessage("originalQuestionMissing"));
    return this.send(sessionId, { query: user.content });
  }

  async deleteMessage(sessionId: string, messageId: string): Promise<void> {
    this.assertAvailable();
    const snapshot = this.getSessionSnapshot(sessionId);
    if (!snapshot.threadId) throw new Error(clientErrorMessage("conversationNotCreated"));
    if (!this.transport.deleteMessage) {
      throw new Error(clientErrorMessage("deleteMessageUnsupported"));
    }
    await this.transport.deleteMessage({
      agentId: this.agentId,
      threadId: snapshot.threadId,
      messageId,
    });
    this.updateSession(sessionId, (current) => ({
      ...current,
      messages: current.messages.filter((message) => message.id !== messageId),
    }));
    this.notifyActivity();
    this.pruneSessions();
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.historyOperations.forEach((operation) => operation.controller.abort());
    this.historyOperations.clear();
    const operation = this.activeOperation;
    this.activeOperation = null;
    if (operation) {
      operation.controller.abort();
      if (operation.flushTimer) clearTimeout(operation.flushTimer);
      if (operation.stopTimer) clearTimeout(operation.stopTimer);
    }
    this.sessions.forEach((record) => record.listeners.clear());
    this.indexListeners.clear();
  }

  private initialSnapshot(sessionId: string, threadId: string | null): ConversationSessionSnapshot {
    return {
      sessionId,
      agentId: this.agentId,
      threadId,
      messages: [],
      history: {
        status: "idle",
        requestId: 0,
        hasMore: false,
        nextCursor: null,
        error: null,
      },
      run: null,
      error: null,
    };
  }

  private bindThread(sessionId: string, threadId: string): void {
    const mapped = this.sessionsByThread.get(threadId);
    if (mapped && mapped !== sessionId) {
      throw new Error(clientErrorMessage("threadAlreadyBound"));
    }
    const snapshot = this.getSessionSnapshot(sessionId);
    if (snapshot.threadId && snapshot.threadId !== threadId) {
      this.sessionsByThread.delete(snapshot.threadId);
    }
    this.sessionsByThread.set(threadId, sessionId);
    this.updateSession(
      sessionId,
      (current) => ({
        ...current,
        threadId,
        messages: current.messages.map((message) => ({ ...message, threadId })),
      }),
      true,
    );
  }

  private handleEvent(operation: ActiveOperation, event: AgentEvent): void {
    if (!this.isCurrentOperation(operation)) return;
    if (event.sequence < operation.lastSequence) return;
    if (
      event.sequence === operation.lastSequence &&
      operation.seenTypesAtLastSequence.has(event.type)
    ) {
      return;
    }
    if (operation.runId && event.run_id !== operation.runId) return;
    if (event.sequence > operation.lastSequence) {
      operation.lastSequence = event.sequence;
      operation.seenTypesAtLastSequence.clear();
    }
    operation.seenTypesAtLastSequence.add(event.type);
    if (!operation.runId && event.run_id) operation.runId = event.run_id;

    const snapshot = this.getSessionSnapshot(operation.sessionId);
    if (!snapshot.run || snapshot.run.requestId !== operation.requestId) return;
    const nextSteps = reduceAgentRunSteps(snapshot.run.steps, event, this.now());
    let messages = snapshot.messages;
    let pendingApproval = snapshot.run.pendingApproval;
    let error = snapshot.error;

    if (event.type === "run.started") {
      const persistedUserId = event.payload.user_message_id;
      if (typeof persistedUserId === "string" && persistedUserId) {
        messages = updateMessage(messages, operation.userMessageId, (message) => ({
          ...message,
          id: persistedUserId,
        }));
        operation.userMessageId = persistedUserId;
      }
      const citations = citationsFromArtifacts({ citations: event.payload.citations });
      if (citations.length) {
        messages = updateMessage(messages, operation.assistantMessageId, (message) => ({
          ...message,
          citations: mergeCitations(message.citations, citations),
        }));
      }
    } else if (event.type === "run.completed") {
      const canonicalOutput = typeof event.payload.output === "string"
        ? event.payload.output
        : null;
      if (canonicalOutput !== null) {
        if (operation.flushTimer) clearTimeout(operation.flushTimer);
        operation.flushTimer = null;
        operation.pendingTokens = "";
      }
      const citations = citationsFromArtifacts({ citations: event.payload.citations });
      const hasCanonicalCitations = Array.isArray(event.payload.citations);
      if (
        canonicalOutput !== null
        || hasCanonicalCitations
        || typeof event.payload.prompt_preview === "string"
      ) {
        messages = updateMessage(messages, operation.assistantMessageId, (message) => ({
          ...message,
          content: canonicalOutput ?? message.content,
          citations: hasCanonicalCitations ? citations : message.citations,
          promptPreview:
            typeof event.payload.prompt_preview === "string"
              ? event.payload.prompt_preview
              : message.promptPreview,
        }));
      }
    } else if (event.type === "tool.completed") {
      const citations = citationsFromArtifacts(event.payload.artifacts);
      if (citations.length) {
        messages = updateMessage(messages, operation.assistantMessageId, (message) => ({
          ...message,
          citations: mergeCitations(message.citations, citations),
        }));
      }
      const id = String(event.payload.tool_call_id ?? "");
      if (pendingApproval?.toolCallId === id) pendingApproval = null;
    } else if (event.type === "tool.failed") {
      const id = String(event.payload.tool_call_id ?? "");
      if (pendingApproval?.toolCallId === id) pendingApproval = null;
    } else if (event.type === "tool.approval_required") {
      pendingApproval = {
        toolCallId: String(event.payload.tool_call_id ?? ""),
        label: String(event.payload.label ?? event.payload.name ?? clientErrorMessage("tool")),
        risk: String(event.payload.risk ?? "write"),
        arguments: objectValue(event.payload.arguments),
        resolving: false,
      };
    } else if (event.type === "tool.started") {
      const id = String(event.payload.tool_call_id ?? "");
      if (pendingApproval?.toolCallId === id) pendingApproval = null;
    } else if (event.type === "message.delta") {
      operation.pendingTokens += String(event.payload.delta ?? "");
      this.scheduleTokenFlush(operation);
    } else if (event.type === "universe.activation") {
      const activation = event.payload as unknown as UniverseActivation;
      messages = updateMessage(messages, operation.assistantMessageId, (message) => ({
        ...message,
        universeActivation: activation,
      }));
      try {
        this.callbacks.onUniverseActivation?.(activation);
      } catch {
        // Universe projection is a secondary consumer of the conversation run.
      }
    } else if (event.type === "run.failed" || event.type === "run.cancelled") {
      const failure = objectValue(event.payload.error);
      error =
        event.type === "run.cancelled"
          ? clientErrorMessage("stopped")
          : String(failure.message ?? clientErrorMessage("generationFailed"));
    }

    const runId = operation.runId ?? snapshot.run.runId;
    const changed =
      nextSteps !== snapshot.run.steps ||
      messages !== snapshot.messages ||
      pendingApproval !== snapshot.run.pendingApproval ||
      error !== snapshot.error ||
      runId !== snapshot.run.runId;
    if (!changed) return;
    this.updateSession(operation.sessionId, (current) =>
      current.run?.requestId === operation.requestId
        ? {
            ...current,
            messages,
            error,
            run: {
              ...current.run,
              runId,
              steps: nextSteps,
              pendingApproval,
            },
          }
        : current,
    );
  }

  private scheduleTokenFlush(operation: ActiveOperation): void {
    if (operation.flushTimer || !operation.pendingTokens) return;
    operation.flushTimer = setTimeout(() => {
      operation.flushTimer = null;
      this.flushTokens(operation);
    }, this.flushIntervalMs);
  }

  private flushTokens(operation: ActiveOperation): void {
    if (!this.isCurrentOperation(operation)) {
      operation.pendingTokens = "";
      return;
    }
    const tokens = operation.pendingTokens;
    if (!tokens) return;
    operation.pendingTokens = "";
    this.updateSession(operation.sessionId, (snapshot) => ({
      ...snapshot,
      messages: updateMessage(snapshot.messages, operation.assistantMessageId, (message) => ({
        ...message,
        content: message.content + tokens,
        delivery: "streaming",
      })),
    }));
  }

  private boundMessages(
    sessionId: string,
    messages: ConversationMessage[],
  ): ConversationMessage[] {
    const seen = new Set<string>();
    const uniqueReversed: ConversationMessage[] = [];
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (seen.has(message.id)) continue;
      seen.add(message.id);
      uniqueReversed.push(message);
    }
    const unique = uniqueReversed.reverse();
    if (unique.length <= this.maxMessagesPerSession) return unique;

    const protectedIds = new Set<string>();
    if (this.activeOperation?.sessionId === sessionId) {
      protectedIds.add(this.activeOperation.userMessageId);
      protectedIds.add(this.activeOperation.assistantMessageId);
    }
    const keepIds = new Set<string>();
    unique.forEach((message) => {
      if (protectedIds.has(message.id)) keepIds.add(message.id);
    });
    for (
      let index = unique.length - 1;
      index >= 0 && keepIds.size < this.maxMessagesPerSession;
      index -= 1
    ) {
      keepIds.add(unique[index].id);
    }
    return unique.filter((message) => keepIds.has(message.id));
  }

  private async finishOperation(
    operation: ActiveOperation,
    outcome: AgentRunOutcome,
  ): Promise<void> {
    if (!this.isCurrentOperation(operation)) return;
    if (operation.flushTimer) {
      clearTimeout(operation.flushTimer);
      operation.flushTimer = null;
    }
    if (operation.stopTimer) {
      clearTimeout(operation.stopTimer);
      operation.stopTimer = null;
    }
    this.flushTokens(operation);

    const snapshot = this.getSessionSnapshot(operation.sessionId);
    const failure = outcome.error?.message ?? (
      outcome.status === "cancelled"
        ? clientErrorMessage("stopped")
        : clientErrorMessage("generationFailed")
    );
    const liveSteps = snapshot.run?.requestId === operation.requestId ? snapshot.run.steps : [];
    const steps =
      outcome.status === "completed"
        ? settleActiveSteps(liveSteps, this.now())
        : settleActiveSteps(
            liveSteps,
            this.now(),
            "error",
            outcome.status === "cancelled" ? clientErrorMessage("stopped") : failure,
          );
    const delivery: ConversationDelivery =
      outcome.status === "completed"
        ? "complete"
        : outcome.status === "cancelled"
          ? "cancelled"
          : "failed";
    const assistantMessageId = outcome.messageId || operation.assistantMessageId;
    const errorText =
      outcome.status === "cancelled"
        ? clientErrorMessage("stopped")
        : outcome.status === "failed"
          ? failure
          : undefined;
    this.activeOperation = null;
    this.updateSession(
      operation.sessionId,
      (current) => ({
        ...current,
        error: outcome.status === "completed" ? null : failure,
        messages: this.boundMessages(
          operation.sessionId,
          updateMessage(current.messages, operation.assistantMessageId, (message) => ({
            ...message,
            id: assistantMessageId,
            // 失败/取消时保留已流出的 partial content;为空时也不再塞错误文本，
            // 错误说明改由 errorMessage 承载,与后端持久化的 (content, error) 分离一致。
            content: message.content,
            delivery,
            errorMessage: errorText,
            steps: persistentSteps(steps),
          })).map((message) =>
            message.delivery === "pending"
              ? { ...message, delivery: "complete" as const }
              : message,
          ),
        ),
        run: current.run?.requestId === operation.requestId ? null : current.run,
      }),
      true,
    );
    this.notifyActivity();

    const threadId = this.getSessionSnapshot(operation.sessionId).threadId;
    // 只有服务端确认落库（拿到 outcome.messageId）时才重拉历史，避免把仅存在于前端的
    // 终态气泡（例如网络中断、老版本服务端）在 mergeHistory 中被过滤掉。
    // 修复目标：失败气泡持久化后重跑 mergeHistory，把 (user, failed-assistant) 顺序稳住，
    // 避免第二次成功回答落回底部时把失败气泡顶到最下。
    if (threadId && outcome.messageId) {
      await this.ensureHistory(operation.sessionId, { force: true });
    }
  }

  private async resolveApproval(
    sessionId: string,
    toolCallId: string,
    approved: boolean,
    reason = "",
  ): Promise<void> {
    this.assertAvailable();
    const operation = this.activeOperation;
    const snapshot = this.getSessionSnapshot(sessionId);
    const approval = snapshot.run?.pendingApproval;
    if (approval?.resolving) return;
    if (
      !operation ||
      operation.sessionId !== sessionId ||
      !snapshot.threadId ||
      !operation.runId ||
      !approval ||
      approval.toolCallId !== toolCallId
    ) {
      throw new Error(clientErrorMessage("approvalExpired"));
    }
    this.updateSession(sessionId, (current) =>
      current.run?.pendingApproval?.toolCallId === toolCallId
        ? {
            ...current,
            run: {
              ...current.run,
              pendingApproval: { ...current.run.pendingApproval, resolving: true },
            },
          }
        : current,
    );
    try {
      if (approved) {
        await this.transport.approveTool({
          agentId: this.agentId,
          threadId: snapshot.threadId,
          runId: operation.runId,
          toolCallId,
        });
      } else {
        await this.transport.rejectTool({
          agentId: this.agentId,
          threadId: snapshot.threadId,
          runId: operation.runId,
          toolCallId,
          reason,
        });
      }
      if (!this.isCurrentOperation(operation)) return;
      this.updateSession(sessionId, (current) =>
        current.run?.pendingApproval?.toolCallId === toolCallId
          ? { ...current, run: { ...current.run, pendingApproval: null } }
          : current,
      );
    } catch (failure) {
      if (this.isCurrentOperation(operation)) {
        this.updateSession(sessionId, (current) =>
          current.run?.pendingApproval?.toolCallId === toolCallId
            ? {
                ...current,
                error: errorMessage(failure, clientErrorMessage("approvalFailed")),
                run: {
                  ...current.run,
                  pendingApproval: { ...current.run.pendingApproval, resolving: false },
                },
              }
            : current,
        );
      }
      throw failure;
    }
  }

  private isCurrentOperation(operation: ActiveOperation): boolean {
    return !this.disposed && this.activeOperation === operation;
  }

  private isCurrentHistory(sessionId: string, requestId: number): boolean {
    return !this.disposed && this.historyOperations.get(sessionId)?.requestId === requestId;
  }

  private updateSession(
    sessionId: string,
    update: (snapshot: ConversationSessionSnapshot) => ConversationSessionSnapshot,
    refreshIndex = false,
  ): void {
    const record = this.record(sessionId);
    const next = update(record.snapshot);
    if (next === record.snapshot) return;
    record.snapshot = next;
    record.listeners.forEach((listener) => listener());
    if (refreshIndex) this.refreshIndex();
  }

  private refreshIndex(): void {
    const sessions = [...this.sessions.values()].map(({ snapshot }) => ({
      sessionId: snapshot.sessionId,
      threadId: snapshot.threadId,
      running: snapshot.run !== null,
      startedAt: snapshot.run?.startedAt ?? null,
    }));
    const next: ConversationIndexSnapshot = {
      activeSessionId: this.activeSessionId,
      activeRunSessionId: this.activeOperation?.sessionId ?? null,
      sessions,
    };
    const unchanged =
      next.activeSessionId === this.indexSnapshot.activeSessionId &&
      next.activeRunSessionId === this.indexSnapshot.activeRunSessionId &&
      next.sessions.length === this.indexSnapshot.sessions.length &&
      next.sessions.every((entry, index) => {
        const current = this.indexSnapshot.sessions[index];
        return (
          entry.sessionId === current.sessionId &&
          entry.threadId === current.threadId &&
          entry.running === current.running &&
          entry.startedAt === current.startedAt
        );
      });
    if (unchanged) return;
    this.indexSnapshot = next;
    this.indexListeners.forEach((listener) => listener());
  }

  private record(sessionId: string): SessionRecord {
    this.assertAvailable();
    const record = this.sessions.get(sessionId);
    if (!record) {
      throw new Error(clientErrorMessage("sessionMissing", { event: sessionId }));
    }
    return record;
  }

  private touch(sessionId: string): void {
    const record = this.sessions.get(sessionId);
    if (record) record.lastAccess = ++this.accessSerial;
  }

  private pruneSessions(): void {
    while (this.sessions.size > this.maxSessions) {
      const candidate = [...this.sessions.entries()]
        .filter(([sessionId, record]) => {
          const { snapshot } = record;
          return (
            sessionId !== this.activeSessionId &&
            sessionId !== this.activeOperation?.sessionId &&
            snapshot.run === null &&
            record.listeners.size === 0 &&
            !this.historyOperations.has(sessionId)
          );
        })
        .sort((left, right) => left[1].lastAccess - right[1].lastAccess)[0];
      if (!candidate) return;
      const [sessionId, record] = candidate;
      this.sessions.delete(sessionId);
      if (record.snapshot.threadId) this.sessionsByThread.delete(record.snapshot.threadId);
    }
  }

  private assertAvailable(): void {
    if (this.disposed) throw new ConversationDisposedError();
  }

  private notifyActivity(): void {
    try {
      const result = this.callbacks.onActivity?.();
      if (result && typeof result.then === "function") void result.catch(() => {});
    } catch {
      // Activity refresh is best effort and must not fail the conversation.
    }
  }
}
