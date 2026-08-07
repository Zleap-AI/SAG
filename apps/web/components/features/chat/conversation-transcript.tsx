"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

import type { Citation } from "@/lib/types";
import { cn } from "@/lib/utils";
import { MarkdownContent } from "@/components/features/markdown-content";
import {
  AgentActivityTimeline,
  type AgentActivityMatch,
  type AgentActivityStep,
} from "@/components/features/chat/agent-activity-timeline";

export interface ConversationTranscriptMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  citations?: Citation[];
  steps?: AgentActivityStep[];
  /** 失败/取消气泡的错误说明；有值时以警示样式显示在正文下方。 */
  errorMessage?: string;
}

export interface ConversationLiveActivity {
  messageId: string;
  streaming: boolean;
  steps: readonly AgentActivityStep[];
  collapsed?: boolean;
  onToggle?: () => void;
}

export interface ConversationMessageRenderContext<
  Message extends ConversationTranscriptMessage = ConversationTranscriptMessage,
> {
  message: Message;
  index: number;
  previousUser?: Message;
}

export interface ConversationMessageItemProps {
  message: ConversationTranscriptMessage;
  index: number;
  previousUser?: ConversationTranscriptMessage;
  streaming?: boolean;
  activity?: Omit<ConversationLiveActivity, "messageId" | "streaming">;
  assistantAvatar?: React.ReactNode;
  onCitationClick?: (
    citation: Citation,
    message: ConversationTranscriptMessage,
  ) => void;
  onToolMatchClick?: (
    match: AgentActivityMatch,
    step: AgentActivityStep,
    message: ConversationTranscriptMessage,
  ) => void;
  renderUserAttachments?: (message: ConversationTranscriptMessage) => React.ReactNode;
  renderAssistantFooter?: (
    context: ConversationMessageRenderContext,
  ) => React.ReactNode;
}

export interface ConversationTranscriptProps<
  Message extends ConversationTranscriptMessage = ConversationTranscriptMessage,
> {
  messages: readonly Message[];
  live?: ConversationLiveActivity;
  assistantAvatar?: React.ReactNode;
  empty?: React.ReactNode;
  onCitationClick?: (citation: Citation, message: Message) => void;
  onToolMatchClick?: (
    match: AgentActivityMatch,
    step: AgentActivityStep,
    message: Message,
  ) => void;
  renderUserAttachments?: (message: Message) => React.ReactNode;
  renderAssistantFooter?: (
    context: ConversationMessageRenderContext<Message>,
  ) => React.ReactNode;
  className?: string;
}

export const ConversationMessageItem = React.memo(function ConversationMessageItem({
  message,
  index,
  previousUser,
  streaming = false,
  activity,
  assistantAvatar,
  onCitationClick,
  onToolMatchClick,
  renderUserAttachments,
  renderAssistantFooter,
}: ConversationMessageItemProps) {
  const t = useTranslations("Conversation");
  const handleCitationClick = React.useCallback(
    (citation: Citation) => onCitationClick?.(citation, message),
    [message, onCitationClick],
  );
  const handleToolMatchClick = React.useCallback(
    (match: AgentActivityMatch, step: AgentActivityStep) =>
      onToolMatchClick?.(match, step, message),
    [message, onToolMatchClick],
  );

  if (message.role === "user") {
    return (
      <div className="flex flex-col items-end gap-1.5">
        {renderUserAttachments?.(message)}
        {message.content && (
          <div className="max-w-[85%] whitespace-pre-wrap rounded-lg rounded-tr-sm bg-primary px-4 py-2.5 text-sm text-primary-foreground">
            {message.content}
          </div>
        )}
      </div>
    );
  }

  const steps = activity?.steps ?? message.steps ?? [];
  const waiting = streaming && !message.content;
  const showFooter = !streaming && (message.content || message.errorMessage);
  const footer = showFooter
    ? renderAssistantFooter?.({ message, index, previousUser })
    : null;

  return (
    <div className="group/msg flex gap-3">
      {assistantAvatar}
      <div className="min-w-0 flex-1">
        {steps.length > 0 && (
          <AgentActivityTimeline
            steps={steps}
            collapsed={activity?.collapsed}
            onToggle={activity?.onToggle}
            onMatchClick={handleToolMatchClick}
          />
        )}

        {waiting && steps.length === 0 ? (
          <div className="flex items-center gap-1.5 py-1 text-sm">
            <span className="size-1.5 animate-blink rounded-full bg-primary" />
            <span className="text-shimmer">{t("thinking")}</span>
          </div>
        ) : message.content ? (
          <MarkdownContent
            content={message.content}
            citations={message.citations}
            onCitationClick={handleCitationClick}
            streaming={streaming}
          />
        ) : null}

        {!streaming && message.errorMessage ? (
          <div
            role="alert"
            className={cn(
              "mt-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive",
              message.content ? "" : "mt-0",
            )}
          >
            {message.errorMessage}
          </div>
        ) : null}

        {footer}
      </div>
    </div>
  );
});

export function ConversationTranscript<
  Message extends ConversationTranscriptMessage,
>({
  messages,
  live,
  assistantAvatar,
  empty,
  onCitationClick,
  onToolMatchClick,
  renderUserAttachments,
  renderAssistantFooter,
  className,
}: ConversationTranscriptProps<Message>) {
  const citationHandler = onCitationClick as ConversationMessageItemProps["onCitationClick"];
  const toolMatchHandler = onToolMatchClick as ConversationMessageItemProps["onToolMatchClick"];
  const attachmentsRenderer = renderUserAttachments as ConversationMessageItemProps["renderUserAttachments"];
  const footerRenderer = renderAssistantFooter as ConversationMessageItemProps["renderAssistantFooter"];

  if (messages.length === 0) {
    return <div className={className}>{empty ?? null}</div>;
  }

  let previousUser: Message | undefined;
  return (
    <div className={cn("flex flex-col gap-6", className)}>
      {messages.map((message, index) => {
        const precedingUser = previousUser;
        if (message.role === "user") previousUser = message;
        const currentActivity = live?.messageId === message.id
          ? {
              steps: live.steps,
              collapsed: live.collapsed,
              onToggle: live.onToggle,
            }
          : undefined;

        return (
          <ConversationMessageItem
            key={message.id}
            message={message}
            index={index}
            previousUser={precedingUser}
            streaming={live?.messageId === message.id && live.streaming}
            activity={currentActivity}
            assistantAvatar={assistantAvatar}
            onCitationClick={citationHandler}
            onToolMatchClick={toolMatchHandler}
            renderUserAttachments={attachmentsRenderer}
            renderAssistantFooter={footerRenderer}
          />
        );
      })}
    </div>
  );
}
