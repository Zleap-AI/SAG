import type { AppLocale } from "./config";
import { readClientLocale } from "./client";

interface ClientErrorValues {
  event?: string;
  status?: number;
  heading?: string;
  index?: number;
  label?: string;
}

const messages = {
  "zh-CN": {
    invalidSearchStream: "搜索服务返回了无法解析的流式数据",
    searchPrepareTimeout: "检索准备超时，请重试",
    searchIdleTimeout: "搜索流长时间无响应，请重试",
    cancelled: "请求已取消",
    network: "网络异常，请稍后重试",
    searchFailed: "检索失败",
    invalidResult: "搜索结果事件顺序或格式无效",
    summaryBeforeResult: "搜索总结出现在检索结果之前",
    invalidSummaryDelta: "搜索总结增量格式无效",
    invalidCompleted: "搜索完成事件顺序或格式无效",
    unknownSearchEvent: ({ event }: ClientErrorValues) => `未知的搜索流事件：${event || "（空）"}`,
    searchIncomplete: "搜索连接提前结束，请重试",
    requestTimeout: "请求超时，请检查网络后重试",
    octxExportStartBusy: "导出检查暂时繁忙，请稍后重试",
    requestFailed: "请求失败",
    uploadFailed: "上传失败",
    uploadNetwork: "网络错误，上传中断",
    newConversation: "新会话",
    missingSource: "知识星点缺少信息源上下文",
    conversationBusy: "已有回答正在生成，请等待完成或先停止",
    historyLoadFailed: "加载对话失败",
    queryOrAttachmentRequired: "问题或附件至少提供一项",
    conversationCreateFailed: "创建会话失败",
    connectionInterrupted: "连接中断",
    userRejected: "用户拒绝执行",
    messageMissing: "消息不存在",
    originalQuestionMissing: "找不到原始问题",
    conversationNotCreated: "会话尚未创建",
    deleteMessageUnsupported: "当前连接不支持删除消息",
    threadAlreadyBound: "会话已绑定到其他运行实例",
    tool: "工具",
    toolFailed: "工具执行失败",
    stopped: "已停止",
    generationFailed: "生成失败",
    approvalExpired: "工具审批已失效",
    approvalFailed: "处理工具审批失败",
    sessionMissing: ({ event }: ClientErrorValues) => `对话运行实例不存在：${event || "-"}`,
    unreadableAgentEvent: "服务端返回了无法解析的 Agent 事件",
    agentEventNotObject: "Agent 事件必须是对象",
    agentEventMissingFields: "Agent 事件缺少必需字段",
    agentEventTypeMismatch: "SSE 事件名与 Agent 事件类型不一致",
    eventAfterTerminal: "终态事件之后又收到了额外事件",
    missingTerminalEvent: "连接提前结束，未收到 Agent 终态事件",
    petThinking: "正在思考下一步",
    petSearching: "正在翻找知识背包",
    petWorking: "正在使用工具处理",
    petAnswering: "正在组织回答",
    petComplete: "回答完成，来看看吧",
    petFailed: "这次没有顺利完成",
    citationSection: ({ heading }: ClientErrorValues) => `章节：${heading || "-"}`,
    externalSource: ({ index }: ClientErrorValues) => `外部来源 ${index ?? "-"}`,
    knowledgeSource: ({ index }: ClientErrorValues) => `知识库来源 ${index ?? "-"}`,
    askEntity: ({ label }: ClientErrorValues) => `围绕“${label || "-"}”梳理关键事实、相关事件和时间线，并标出知识库依据。`,
    askEvent: ({ label }: ClientErrorValues) => `解释事件“${label || "-"}”的背景、关键实体和后续关联，并标出知识库依据。`,
    serverNotFound: "请求的资源不存在",
    serverConflict: "当前操作与资源状态冲突",
    serverInvalidRequest: "请求内容无效，请检查后重试",
    serverUnauthorized: "登录状态已失效，请重新登录",
    serverForbidden: "你没有执行此操作的权限",
    serverConfiguration: "服务配置不完整，请先完成相关设置",
    serverUpstream: "上游服务处理失败，请稍后重试",
    serverUnavailable: "服务暂时不可用，请稍后重试",
    serverUnexpected: "服务发生异常，请稍后重试",
    serverRequestFailed: "请求处理失败，请稍后重试",
  },
  "en-US": {
    invalidSearchStream: "The search service returned an unreadable data stream",
    searchPrepareTimeout: "Search preparation timed out. Try again.",
    searchIdleTimeout: "The search stream stopped responding. Try again.",
    cancelled: "Request cancelled",
    network: "Network error. Try again shortly.",
    searchFailed: "Search failed",
    invalidResult: "The search result event order or format is invalid",
    summaryBeforeResult: "A search summary arrived before the search result",
    invalidSummaryDelta: "The search summary update format is invalid",
    invalidCompleted: "The search completion event order or format is invalid",
    unknownSearchEvent: ({ event }: ClientErrorValues) => `Unknown search stream event: ${event || "(empty)"}`,
    searchIncomplete: "The search connection ended early. Try again.",
    requestTimeout: "The request timed out. Check your connection and try again.",
    octxExportStartBusy: "The export check is temporarily busy. Try again shortly.",
    requestFailed: "Request failed",
    uploadFailed: "Upload failed",
    uploadNetwork: "A network error interrupted the upload",
    newConversation: "New conversation",
    missingSource: "The knowledge node is missing its source context",
    conversationBusy: "An answer is already being generated. Wait for it to finish or stop it first.",
    historyLoadFailed: "Failed to load conversation",
    queryOrAttachmentRequired: "Provide a question or at least one attachment",
    conversationCreateFailed: "Failed to create conversation",
    connectionInterrupted: "Connection interrupted",
    userRejected: "User rejected the action",
    messageMissing: "Message not found",
    originalQuestionMissing: "Original question not found",
    conversationNotCreated: "The conversation has not been created yet",
    deleteMessageUnsupported: "The current connection does not support deleting messages",
    threadAlreadyBound: "The conversation is already bound to another runtime session",
    tool: "Tool",
    toolFailed: "Tool execution failed",
    stopped: "Stopped",
    generationFailed: "Generation failed",
    approvalExpired: "This tool approval is no longer valid",
    approvalFailed: "Failed to process tool approval",
    sessionMissing: ({ event }: ClientErrorValues) => `Conversation runtime session not found: ${event || "-"}`,
    unreadableAgentEvent: "The server returned an unreadable Agent event",
    agentEventNotObject: "The Agent event must be an object",
    agentEventMissingFields: "The Agent event is missing required fields",
    agentEventTypeMismatch: "The SSE event name does not match the Agent event type",
    eventAfterTerminal: "An extra event arrived after the terminal event",
    missingTerminalEvent: "The connection ended before an Agent terminal event arrived",
    petThinking: "Thinking through the next step",
    petSearching: "Searching the knowledge pack",
    petWorking: "Using tools",
    petAnswering: "Composing the answer",
    petComplete: "Answer complete. Take a look.",
    petFailed: "That did not finish successfully",
    citationSection: ({ heading }: ClientErrorValues) => `Section: ${heading || "-"}`,
    externalSource: ({ index }: ClientErrorValues) => `External source ${index ?? "-"}`,
    knowledgeSource: ({ index }: ClientErrorValues) => `Knowledge source ${index ?? "-"}`,
    askEntity: ({ label }: ClientErrorValues) => `Organize the key facts, related events, and timeline around “${label || "-"}”, citing the supporting knowledge sources.`,
    askEvent: ({ label }: ClientErrorValues) => `Explain the background, key entities, and later relationships of “${label || "-"}”, citing the supporting knowledge sources.`,
    serverNotFound: "The requested resource was not found",
    serverConflict: "This action conflicts with the current resource state",
    serverInvalidRequest: "The request is invalid. Check it and try again.",
    serverUnauthorized: "Your session has expired. Sign in again.",
    serverForbidden: "You do not have permission to perform this action",
    serverConfiguration: "The service is not fully configured. Complete the required settings first.",
    serverUpstream: "An upstream service failed to process the request. Try again shortly.",
    serverUnavailable: "The service is temporarily unavailable. Try again shortly.",
    serverUnexpected: "The service encountered an unexpected error. Try again shortly.",
    serverRequestFailed: "The request could not be processed. Try again shortly.",
  },
} satisfies Record<AppLocale, Record<string, string | ((values: ClientErrorValues) => string)>>;

export type ClientErrorKey = keyof (typeof messages)["en-US"];

export function clientErrorMessage(
  key: ClientErrorKey,
  values: ClientErrorValues = {},
  locale = readClientLocale(),
): string {
  const message = messages[locale][key];
  return typeof message === "function" ? message(values) : message;
}

/**
 * The API predates UI localization and can still return fixed Chinese error
 * messages. Preserve useful messages that already match the active locale,
 * but provide a stable English fallback from the machine-readable code/status
 * instead of leaking untranslated implementation text into the interface.
 */
export function serverErrorMessage(
  code: string | undefined,
  message: string,
  status = 0,
  locale = readClientLocale(),
): string {
  if (locale !== "en-US" || !/\p{Script=Han}/u.test(message)) return message;

  const normalizedCode = (code ?? "").toLowerCase();
  let key: ClientErrorKey = "serverRequestFailed";
  if (normalizedCode.includes("not_found") || status === 404) key = "serverNotFound";
  else if (normalizedCode.includes("conflict") || status === 409) key = "serverConflict";
  else if (normalizedCode.includes("unauthorized") || status === 401) key = "serverUnauthorized";
  else if (normalizedCode.includes("forbidden") || status === 403) key = "serverForbidden";
  else if (normalizedCode.includes("configuration")) key = "serverConfiguration";
  else if (normalizedCode.includes("upstream") || status === 502) key = "serverUpstream";
  else if (
    normalizedCode.includes("unavailable")
    || normalizedCode.includes("timeout")
    || status === 429
    || status === 503
    || status === 504
  ) key = "serverUnavailable";
  else if (
    normalizedCode.includes("validation")
    || normalizedCode.startsWith("invalid_")
    || status === 400
    || status === 422
  ) key = "serverInvalidRequest";
  else if (status >= 500) key = "serverUnexpected";

  return clientErrorMessage(key, {}, locale);
}
