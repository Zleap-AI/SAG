"use client";

import * as React from "react";
import { ArrowUp, Check, Copy, FileUp, Globe2, ImagePlus, Library, Plus, RotateCcw, Square, Trash2, X } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { toast } from "sonner";

import { toolArgumentsPreview } from "@/lib/agent-run-activity";
import { api } from "@/lib/api";
import type { ConversationMessage } from "@/lib/conversation-runtime";
import { parsePetDraft, PET_DRAFT_EVENT, PET_DRAFT_KEY } from "@/lib/pet-events";
import { formatTokenCount, relativeTime } from "@/lib/format";
import type { Citation, Source } from "@/lib/types";
import { cn } from "@/lib/utils";
import { copyText } from "@/lib/clipboard";
import { useApp } from "@/components/features/app-shell";
import {
  useConversationComposer,
  useConversationRuntime,
  useConversationSession,
} from "@/components/features/chat/conversation-provider";
import { useDetailPanel } from "@/components/features/detail-panel";
import type {
  AgentActivityMatch,
  AgentActivityStep,
} from "@/components/features/chat/agent-activity-timeline";
import { CitationBlock } from "@/components/features/chat/citation-block";
import {
  ConversationTranscript,
  type ConversationMessageRenderContext,
} from "@/components/features/chat/conversation-transcript";
import { PromptPreview } from "@/components/features/chat/prompt-preview";
import { AuthImage } from "@/components/features/auth-image";
import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Spinner } from "@/components/ui/spinner";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

function MessageActions({
  content,
  citations,
  createdAt,
  onRetry,
  onDelete,
  onCitationClick,
}: {
  content: string;
  citations: Citation[];
  createdAt?: string;
  onRetry?: () => void;
  onDelete?: () => void;
  onCitationClick?: (citation: Citation) => void;
}) {
  const t = useTranslations("Conversation");
  const locale = useLocale();
  const [done, setDone] = React.useState(false);
  const { timezone } = useApp();
  const btn =
    "grid size-7 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground";
  return (
    <div className="mt-1 flex min-h-7 flex-wrap items-center gap-x-1 gap-y-0">
      <CitationBlock citations={citations} onCitationClick={onCitationClick} />
      <div className="flex items-center gap-1 opacity-0 transition-opacity group-hover/msg:opacity-100 focus-within:opacity-100">
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={async () => {
                try {
                  await copyText(content);
                  setDone(true);
                  setTimeout(() => setDone(false), 1500);
                } catch {
                  toast.error(t("copyFailed"));
                }
              }}
              className={btn}
              aria-label={done ? t("copied") : t("copyAnswer")}
            >
              {done ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
            </button>
          </TooltipTrigger>
          <TooltipContent>{done ? t("copied") : t("copy")}</TooltipContent>
        </Tooltip>
        {onRetry && (
          <Tooltip>
            <TooltipTrigger asChild>
              <button type="button" onClick={onRetry} className={btn} aria-label={t("answerAgain")}>
                <RotateCcw className="size-3.5" />
              </button>
            </TooltipTrigger>
            <TooltipContent>{t("retry")}</TooltipContent>
          </Tooltip>
        )}
        {onDelete && (
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                onClick={onDelete}
                className={btn + " hover:text-destructive"}
                aria-label={t("deleteMessage")}
              >
                <Trash2 className="size-3.5" />
              </button>
            </TooltipTrigger>
            <TooltipContent>{t("delete")}</TooltipContent>
          </Tooltip>
        )}
        {createdAt && (
          <span className="px-1.5 text-[11px] tabular-nums text-muted-foreground/70">
            {relativeTime(createdAt, timezone, locale)}
          </span>
        )}
      </div>
    </div>
  );
}

export interface ConversationPanelProps {
  sessionId: string;
  avatarNode: React.ReactNode;
  heroNode: React.ReactNode;
  emptyTitle: string;
  emptyHint: string;
  /** 空态建议提问：点击即发送 */
  suggestions?: string[];
  placeholder?: string;
  /** 面板可交互时激活会话与焦点；隐藏但保留挂载时设为 false。 */
  active?: boolean;
  /** 紧凑入口可隐藏提示词预览，避免在小面板上叠加弹框。 */
  showPromptPreview?: boolean;
  /** 消息区顶部的入口级内容，例如迷你面板的历史会话选择器。 */
  beforeMessages?: React.ReactNode;
  /** 来自图谱等外部入口的显式草稿；相同 id 只应用一次。 */
  draftPrompt?: { id: number; text: string } | null;
  onCitationClick?: (citation: Citation, message: ConversationMessage) => void;
  onToolMatchClick?: (
    match: AgentActivityMatch,
    step: AgentActivityStep,
    message: ConversationMessage,
  ) => void;
}

/**
 * 完整、自适应的对话面板。主工作台和迷你入口只提供外层容器，
 * 历史、流式过程、工具、引用、附件、@ 范围和输入器统一由这里管理。
 */
export function ConversationPanel({
  sessionId,
  avatarNode,
  heroNode,
  emptyTitle,
  emptyHint,
  suggestions,
  placeholder,
  active = true,
  showPromptPreview = true,
  beforeMessages,
  draftPrompt,
  onCitationClick,
  onToolMatchClick,
}: ConversationPanelProps) {
  const t = useTranslations("Conversation");
  const locale = useLocale();
  const resolvedPlaceholder = placeholder ?? t("defaultPlaceholder");
  const { capabilities } = useApp();
  const runtime = useConversationRuntime();
  const session = useConversationSession(sessionId);
  const detailPanel = useDetailPanel();
  const {
    input,
    setInput,
    images,
    setImages,
    scoped,
    setScoped,
    webEnabled,
    setWebEnabled,
  } =
    useConversationComposer(sessionId);
  const imagesRef = React.useRef(images);
  const [sources, setSources] = React.useState<Source[]>([]);
  const [mentionOpen, setMentionOpen] = React.useState(false);
  const [mentionIdx, setMentionIdx] = React.useState(0);
  const [uploading, setUploading] = React.useState(false);
  const uploadingRef = React.useRef(false);
  const [stepsCollapsed, setStepsCollapsed] = React.useState(true);
  const mentionListRef = React.useRef<HTMLDivElement>(null);
  const docRef = React.useRef<HTMLInputElement>(null);
  const fileRef = React.useRef<HTMLInputElement>(null);
  const textareaRef = React.useRef<HTMLTextAreaElement>(null);
  const scrollRef = React.useRef<HTMLDivElement>(null);
  const bottomRef = React.useRef<HTMLDivElement>(null);
  const lastScrollAt = React.useRef(0);
  const followOutputRef = React.useRef(true);
  const sendRef = React.useRef<(text: string) => void>(() => {});
  const lastDraftPromptRef = React.useRef<number | null>(null);

  if (!session) throw new Error(t("sessionMissing", { id: sessionId }));
  const messages = session.messages;
  const streaming = session.run !== null;
  const stopping = session.run?.lifecycle === "stopping";
  const submitting = uploading || session.run?.lifecycle === "preparing";
  const pendingApproval = session.run?.pendingApproval ?? null;
  const approvalBusy = pendingApproval?.resolving ?? false;

  React.useEffect(() => {
    if (active) runtime.activate(sessionId);
    void runtime.ensureHistory(sessionId);
  }, [active, runtime, sessionId]);

  React.useEffect(() => {
    if (
      !active
      || !draftPrompt
      || lastDraftPromptRef.current === draftPrompt.id
    ) return;
    lastDraftPromptRef.current = draftPrompt.id;
    setInput(draftPrompt.text);
    const frame = window.requestAnimationFrame(() => {
      textareaRef.current?.focus();
      const length = textareaRef.current?.value.length ?? 0;
      textareaRef.current?.setSelectionRange(length, length);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [active, draftPrompt, setInput]);

  React.useEffect(() => {
    imagesRef.current = images;
  }, [images]);

  const mentionQuery = React.useMemo(() => {
    if (!mentionOpen) return "";
    const at = input.lastIndexOf("@");
    return at >= 0 ? input.slice(at + 1) : "";
  }, [mentionOpen, input]);
  const mentionMatches = React.useMemo(() => {
    const q = mentionQuery.trim().toLowerCase();
    return q ? sources.filter((s) => s.name.toLowerCase().includes(q)) : sources;
  }, [sources, mentionQuery]);
  React.useEffect(() => setMentionIdx(0), [mentionQuery]);
  React.useEffect(() => {
    mentionListRef.current
      ?.querySelector(`[data-idx="${mentionIdx}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [mentionIdx]);

  const selectMention = React.useCallback(
    (src: { id: string; name: string }) => {
      setScoped((p) =>
        p.some((x) => x.id === src.id) ? p : [...p, { id: src.id, name: src.name }],
      );
      setInput((v) => {
        const at = v.lastIndexOf("@");
        return at >= 0 ? v.slice(0, at) : v;
      });
      setMentionOpen(false);
      textareaRef.current?.focus();
    },
    [setInput, setScoped],
  );
  const toggleTranscriptSteps = React.useCallback(
    () => setStepsCollapsed((value) => !value),
    [],
  );
  sendRef.current = (text) => {
    void send(text);
  };

  React.useEffect(() => {
    if (!active) return;
    const applyDraft = (value: unknown) => {
      const payload = parsePetDraft(value);
      if (!payload) return;
      if (payload.submit) {
        requestAnimationFrame(() => sendRef.current(payload.text));
        return;
      }
      setInput(payload.text);
      requestAnimationFrame(() => textareaRef.current?.focus());
    };
    const onPetDraft = (event: Event) => {
      window.sessionStorage.removeItem(PET_DRAFT_KEY);
      applyDraft((event as CustomEvent<unknown>).detail);
    };

    window.addEventListener(PET_DRAFT_EVENT, onPetDraft);
    try {
      const pending = window.sessionStorage.getItem(PET_DRAFT_KEY);
      if (pending) {
        window.sessionStorage.removeItem(PET_DRAFT_KEY);
        applyDraft(pending);
      }
    } catch {
      /* ignore */
    }
    return () => window.removeEventListener(PET_DRAFT_EVENT, onPetDraft);
  }, [active, sessionId, setInput]);

  const handleTranscriptCitation = React.useCallback(
    (citation: Citation, message: ConversationMessage) => {
      if (onCitationClick) {
        onCitationClick(citation, message);
        return;
      }
      if (!citation.chunk_id || !citation.source_id) return;
      detailPanel.open({
        kind: "chunk",
        sourceId: citation.source_id,
        chunkId: citation.chunk_id,
        heading: citation.heading ?? undefined,
        sourceName: citation.source_name ?? undefined,
        eventRefs: citation.event_refs,
      });
    },
    [detailPanel, onCitationClick],
  );

  const handleTranscriptMatch = React.useCallback(
    (
      match: AgentActivityMatch,
      step: AgentActivityStep,
      message: ConversationMessage,
    ) => {
      if (onToolMatchClick) {
        onToolMatchClick(match, step, message);
        return;
      }
      if (!match.chunk_id || !match.source_id) return;
      detailPanel.open({
        kind: "chunk",
        sourceId: match.source_id,
        chunkId: match.chunk_id,
        heading: match.heading,
        sourceName: match.source_name,
      });
    },
    [detailPanel, onToolMatchClick],
  );

  const renderTranscriptAttachments = React.useCallback(
    (message: ConversationMessage) =>
      message.attachments.length ? (
        <div className="flex max-w-[85%] flex-wrap justify-end gap-1.5">
          {message.attachments.map((attachment, index) => (
            <AuthImage
              key={attachment.id ?? index}
              id={attachment.id}
              className="max-h-40 max-w-56 rounded-lg border object-cover shadow-soft"
            />
          ))}
        </div>
      ) : null,
    [],
  );

  const deleteTranscriptMessage = React.useCallback(
    async (messageId: string) => {
      try {
        await runtime.deleteMessage(sessionId, messageId);
      } catch (error) {
        toast.error(error instanceof Error ? error.message : t("deleteFailed"));
      }
    },
    [runtime, sessionId, t],
  );

  const renderTranscriptFooter = React.useCallback(
    ({
      message,
      previousUser,
    }: ConversationMessageRenderContext<ConversationMessage>) => {
      const canMutate = message.role === "assistant" && !streaming;
      return (
        <>
          <MessageActions
            content={message.content}
            citations={message.citations}
            createdAt={message.createdAt}
            onRetry={
              canMutate && previousUser?.content
                ? () => sendRef.current(previousUser.content)
                : undefined
            }
            onDelete={
              canMutate && message.delivery === "persisted"
                ? () => void deleteTranscriptMessage(message.id)
                : undefined
            }
            onCitationClick={(citation) => handleTranscriptCitation(citation, message)}
          />
          {showPromptPreview && message.promptPreview && (
            <PromptPreview preview={message.promptPreview} />
          )}
        </>
      );
    },
    [deleteTranscriptMessage, handleTranscriptCitation, showPromptPreview, streaming],
  );

  const transcriptLive = session.run
    ? {
        messageId: session.run.assistantMessageId,
        streaming: true,
        steps: session.run.steps,
        collapsed: stepsCollapsed,
        onToggle: toggleTranscriptSteps,
      }
    : undefined;

  React.useEffect(() => {
    const el = bottomRef.current;
    if (!el) return;
    if (streaming && !followOutputRef.current) return;
    const now = Date.now();
    if (streaming && now - lastScrollAt.current < 120) return;
    lastScrollAt.current = now;
    el.scrollIntoView({ behavior: "auto", block: "end" });
  }, [messages, streaming]);

  React.useEffect(() => {
    api.listSources().then(setSources).catch(() => {});
  }, []);

  // 上下文用量估算：CJK ≈1 token/字，其余 ≈1 token/4 字符（无 tokenizer 的专业近似）
  const ctxTokens = React.useMemo(() => {
    const est = (t: string) => {
      let cjk = 0;
      for (const ch of t) if (/[\u3000-\u9fff\uf900-\ufaff]/.test(ch)) cjk++;
      return cjk + Math.ceil((t.length - cjk) / 4);
    };
    return messages.reduce((a, m) => a + est(m.content ?? ""), 0) + est(input);
  }, [messages, input]);
  const ctxWindow = capabilities?.context_window ?? 128000;
  const ctxPctRaw = Math.min(100, (ctxTokens / ctxWindow) * 100);
  const ctxPctLabel = `${ctxPctRaw > 0 && ctxPctRaw < 10 ? ctxPctRaw.toFixed(1) : Math.round(ctxPctRaw)}%`;

  React.useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
  }, [input]);

  async function addDocToKnowledge(files: FileList | null) {
    const f = files?.[0];
    if (!f) return;
    try {
      const knownNames = new Set(["对话上传", "Chat uploads", t("chatUploads")]);
      let src = sources.find((source) => knownNames.has(source.name));
      if (!src) {
        src = await api.createSource({
          name: t("chatUploads"),
          description: t("chatUploadsDescription"),
        });
        setSources((p) => [...p, src!]);
      }
      await api.uploadDocument(src.id, f);
      setScoped((p) => (p.some((x) => x.id === src!.id) ? p : [...p, { id: src!.id, name: src!.name }]));
      toast.success(t("documentAdded"));
    } catch {
      toast.error(t("documentUploadFailed"));
    }
  }

  const MAX_IMAGES = 4;
  function addImages(files: FileList | File[] | null) {
    if (!files) return;
    const incoming = Array.from(files).filter((f) => f.type.startsWith("image/"));
    if (!incoming.length) return;
    setImages((prev) => {
      const room = MAX_IMAGES - prev.length;
      const accepted = incoming.slice(0, Math.max(0, room));
      if (incoming.length > room) toast.error(t("maxImages", { count: MAX_IMAGES }));
      const oversize = accepted.filter((f) => f.size > 10 * 1024 * 1024);
      if (oversize.length) toast.error(t("imageTooLarge"));
      return [
        ...prev,
        ...accepted
          .filter((f) => f.size <= 10 * 1024 * 1024)
          .map((f) => ({ file: f, url: URL.createObjectURL(f) })),
      ];
    });
  }
  function removeImage(url: string) {
    setImages((prev) => {
      URL.revokeObjectURL(url);
      return prev.filter((i) => i.url !== url);
    });
  }

  async function send(text?: string) {
    const query = (text ?? input).trim();
    const pendingImages = images;
    if ((!query && pendingImages.length === 0) || uploadingRef.current) return;
    if (!capabilities?.llm_configured) {
      toast.error(t("modelNotConfigured"));
      return;
    }
    if (runtime.getIndexSnapshot().activeRunSessionId) {
      toast.error(t("alreadyGenerating"));
      return;
    }

    uploadingRef.current = true;
    setUploading(true);
    try {
      const uploaded = pendingImages.length
        ? await Promise.all(pendingImages.map((item) => api.uploadAttachment(item.file)))
        : [];
      runtime.activate(sessionId);
      const request = runtime.send(sessionId, {
        query,
        attachmentIds: uploaded.map((item) => item.id),
        sourceIds: scoped.map((source) => source.id),
        webEnabled,
      });

      // send 在首次 await 前就登记 run；未登记表示被全局并发边界拒绝，保留草稿。
      if (!runtime.getSessionSnapshot(sessionId).run) {
        await request;
        return;
      }

      setInput("");
      imagesRef.current = [];
      setImages([]);
      pendingImages.forEach((image) => URL.revokeObjectURL(image.url));
      setStepsCollapsed(false);
      followOutputRef.current = true;

      void request.catch((error) => {
        toast.error(error instanceof Error ? error.message : t("connectionInterrupted"));
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("imageUploadFailed"));
    } finally {
      uploadingRef.current = false;
      setUploading(false);
    }
  }

  async function stop() {
    if (stopping || !streaming) return;
    try {
      await runtime.stop(sessionId);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("stopFailed"));
    }
  }

  async function resolveApproval(approved: boolean) {
    if (!pendingApproval || approvalBusy) return;
    try {
      if (approved) await runtime.approve(sessionId, pendingApproval.toolCallId);
      else await runtime.reject(sessionId, pendingApproval.toolCallId, t("userRejected"));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("approvalFailed"));
    }
  }
  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  return (
    <TooltipProvider delayDuration={300}>
      <div className="flex h-full min-h-0 flex-col">
      <div
        ref={scrollRef}
        onScroll={() => {
          const element = scrollRef.current;
          if (!element) return;
          followOutputRef.current =
            element.scrollHeight - element.scrollTop - element.clientHeight < 80;
        }}
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain"
      >
        <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6">
          {beforeMessages}
          {session.history.hasMore && (
            <div className="flex justify-center">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={session.history.status === "loading"}
                onClick={() => void runtime.loadOlder(sessionId)}
                className="h-7 text-xs text-muted-foreground"
              >
                {session.history.status === "loading" && <Spinner className="mr-1 size-3" />}
                {t("loadEarlier")}
              </Button>
            </div>
          )}
          <ConversationTranscript
            messages={messages}
            live={transcriptLive}
            assistantAvatar={avatarNode}
            onCitationClick={handleTranscriptCitation}
            onToolMatchClick={handleTranscriptMatch}
            renderUserAttachments={renderTranscriptAttachments}
            renderAssistantFooter={renderTranscriptFooter}
            empty={
              <div className="flex flex-col items-center gap-2 py-16 text-center">
                {heroNode}
                <div className="font-display text-xl text-foreground">{emptyTitle}</div>
                <p className="max-w-sm text-sm text-muted-foreground">{emptyHint}</p>
                {suggestions && suggestions.length > 0 && (
                  <div className="mt-3 flex max-w-md flex-wrap justify-center gap-2">
                    {suggestions.map((question) => (
                      <button
                        key={question}
                        type="button"
                        onClick={() => send(question)}
                        className="rounded-full border bg-card px-3 py-1.5 text-xs text-muted-foreground shadow-soft outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        {question}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            }
          />
          <div ref={bottomRef} aria-hidden className="h-8 shrink-0 sm:h-10" />
        </div>
      </div>

      <div className="shrink-0 border-t bg-background/95 px-3 py-3 sm:px-4">
        <div className="mx-auto max-w-3xl">
          <div className="relative flex flex-col gap-1.5 rounded-2xl border bg-card px-3 py-2 shadow-soft transition-[border-color,box-shadow] focus-within:border-foreground/20 focus-within:shadow-lift">
          {images.length > 0 && (
            <div className="flex flex-wrap gap-2 px-1">
              {images.map((img) => (
                <div key={img.url} className="group/thumb relative">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={img.url}
                    alt={img.file.name}
                    className="size-14 rounded-md border object-cover"
                  />
                  <button
                    type="button"
                    onClick={() => removeImage(img.url)}
                    aria-label={t("removeImage")}
                    className="absolute -right-1.5 -top-1.5 grid size-5 place-items-center rounded-full border bg-background text-muted-foreground opacity-0 shadow-soft transition-opacity hover:text-destructive group-hover/thumb:opacity-100"
                  >
                    <X className="size-3" />
                  </button>
                </div>
              ))}
            </div>
          )}
          {mentionOpen && (
            <div
              ref={mentionListRef}
              className="absolute bottom-full left-3 z-20 mb-2 max-h-56 w-72 overflow-y-auto rounded-lg border bg-card p-1 shadow-lift"
            >
              <p className="px-2 py-1 text-[11px] text-muted-foreground">
                {t("knowledgeScope", {
                  query: mentionQuery ? t("querySuffix", { query: mentionQuery }) : "",
                })}
              </p>
              {mentionMatches.length === 0 && (
                <p className="px-2 py-1.5 text-xs text-muted-foreground">{t("noMatchingSources")}</p>
              )}
              {mentionMatches.map((src, i) => {
                const on = scoped.some((x) => x.id === src.id);
                return (
                  <button
                    key={src.id}
                    type="button"
                    data-idx={i}
                    onMouseEnter={() => setMentionIdx(i)}
                    onClick={() => selectMention(src)}
                    className={cn(
                      "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm outline-none transition-colors",
                      i === mentionIdx ? "bg-muted" : "hover:bg-muted/60",
                    )}
                  >
                    <span className="min-w-0 flex-1 truncate">{src.name}</span>
                    {on && <Check className="size-3.5 shrink-0 text-muted-foreground" />}
                  </button>
                );
              })}
            </div>
          )}
          <input
            ref={fileRef}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif"
            multiple
            className="hidden"
            onChange={(e) => {
              addImages(e.target.files);
              e.target.value = "";
            }}
          />
          <input
            ref={docRef}
            type="file"
            accept=".pdf,.md,.markdown,.txt,.docx,.html,.epub"
            className="hidden"
            onChange={(e) => {
              addDocToKnowledge(e.target.files);
              e.target.value = "";
            }}
          />
          <div className="flex flex-wrap items-center gap-1 px-0.5">
            {scoped.map((sc) => (
              <span
                key={sc.id}
                className="inline-flex h-6 max-w-[min(100%,12rem)] items-center gap-1 rounded-md bg-primary/10 px-1.5 text-xs font-medium leading-6 text-primary"
              >
                <Library className="size-3 shrink-0" />
                <span className="truncate">{sc.name}</span>
                <button
                  type="button"
                  aria-label={t("removeSource", { name: sc.name })}
                  onClick={() => setScoped((p) => p.filter((x) => x.id !== sc.id))}
                  className="rounded-sm text-primary/70 hover:bg-primary/15 hover:text-primary"
                >
                  <X className="size-2.5" />
                </button>
              </span>
            ))}
            <textarea
              ref={textareaRef}
              autoFocus={active}
              value={input}
              onChange={(e) => {
                const v = e.target.value;
                setInput(v);
                if (v.endsWith("@")) {
                  setMentionOpen(true);
                } else if (mentionOpen) {
                  const at = v.lastIndexOf("@");
                  if (at < 0 || /\s/.test(v.slice(at + 1))) setMentionOpen(false);
                }
              }}
              onKeyDown={(e) => {
                if (mentionOpen) {
                  if (e.key === "ArrowDown") {
                    e.preventDefault();
                    setMentionIdx((i) => Math.min(i + 1, Math.max(mentionMatches.length - 1, 0)));
                    return;
                  }
                  if (e.key === "ArrowUp") {
                    e.preventDefault();
                    setMentionIdx((i) => Math.max(i - 1, 0));
                    return;
                  }
                  if (e.key === "Enter") {
                    e.preventDefault();
                    const pick = mentionMatches[mentionIdx];
                    if (pick) selectMention(pick);
                    return;
                  }
                  if (e.key === "Escape") {
                    setMentionOpen(false);
                    return;
                  }
                }
                onKeyDown(e);
              }}
              onPaste={(e) => {
                if (e.clipboardData?.files?.length) {
                  e.preventDefault();
                  addImages(e.clipboardData.files);
                }
              }}
              rows={1}
              maxLength={4000}
              placeholder={scoped.length ? t("continueTyping") : resolvedPlaceholder}
              className="max-h-28 min-h-6 min-w-[12ch] flex-1 resize-none overflow-y-auto bg-transparent py-0 text-sm leading-6 text-foreground outline-none placeholder:text-muted-foreground"
            />
          </div>

          <div className="flex min-w-0 items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-1.5">
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    disabled={streaming || submitting}
                    aria-label={t("add")}
                    title={t("add")}
                    className="size-8 rounded-full text-muted-foreground hover:text-foreground"
                  >
                    <Plus className="size-4" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent side="top" align="start">
                  <DropdownMenuItem
                    disabled={images.length >= MAX_IMAGES}
                    onClick={() => fileRef.current?.click()}
                  >
                    <ImagePlus className="size-4" />
                    {t("imagesPaste")}
                  </DropdownMenuItem>
                  <DropdownMenuItem onClick={() => docRef.current?.click()}>
                    <FileUp className="size-4" />
                    {t("documentToKnowledge")}
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="inline-flex">
                    <button
                      type="button"
                      aria-label={t("webSearchAria", {
                        state: webEnabled ? t("enabled") : t("disabled"),
                      })}
                      aria-pressed={webEnabled}
                      disabled={streaming || submitting}
                      onClick={() => setWebEnabled((enabled) => !enabled)}
                      className={cn(
                        "inline-flex h-7 shrink-0 items-center gap-1 rounded-md px-1.5 text-[11px] font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60",
                        webEnabled
                          ? "bg-sky-50 text-sky-600 hover:bg-sky-100 dark:bg-sky-950/40 dark:text-sky-300 dark:hover:bg-sky-950/60"
                          : "bg-transparent text-muted-foreground/75 hover:bg-muted/70 hover:text-foreground",
                      )}
                    >
                      <Globe2 className="size-3.5" aria-hidden />
                      <span>{t("web")}</span>
                    </button>
                  </span>
                </TooltipTrigger>
                <TooltipContent side="top" className="max-w-64">
                  {webEnabled
                    ? t("webEnabledDescription")
                    : t("webDisabledDescription")}
                </TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="min-w-0 max-w-[42vw] truncate px-1 text-xs text-muted-foreground sm:max-w-56">
                    {capabilities?.llm_model ?? t("unconfiguredModel")}
                  </span>
                </TooltipTrigger>
                <TooltipContent side="top" className="max-w-72">
                  {t("currentModel", {
                    model: capabilities?.llm_model ?? t("unconfiguredModel"),
                  })}
                </TooltipContent>
              </Tooltip>

              <Tooltip>
                <TooltipTrigger asChild>
                  <span
                    aria-label={t("contextUsage")}
                    className="size-4 shrink-0 cursor-default rounded-full"
                    style={{
                      background: `conic-gradient(hsl(var(--primary)) ${ctxPctRaw * 3.6}deg, hsl(var(--muted)) 0deg)`,
                      WebkitMask: "radial-gradient(farthest-side, transparent 56%, black 58%)",
                      mask: "radial-gradient(farthest-side, transparent 56%, black 58%)",
                    }}
                  />
                </TooltipTrigger>
                <TooltipContent
                  side="top"
                  className="w-64 rounded-lg border bg-card p-3 text-card-foreground shadow-lift"
                >
                  <div className="space-y-2">
                    <div>
                      <p className="text-sm font-medium">{t("windowUsage")}</p>
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        {t("windowUsageDescription")}
                      </p>
                    </div>
                    <div className="flex items-center justify-between text-xs tabular-nums">
                      <span>{ctxPctLabel}</span>
                      <span className="text-muted-foreground">
                        {formatTokenCount(ctxTokens, locale)} / {formatTokenCount(ctxWindow, locale)}
                      </span>
                    </div>
                    <div className="h-2 overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full rounded-full bg-primary"
                        style={{ width: `${Math.max(ctxPctRaw, ctxTokens > 0 ? 2 : 0)}%` }}
                      />
                    </div>
                  </div>
                </TooltipContent>
              </Tooltip>
            </div>

            {streaming ? (
              <Button
                variant="outline"
                size="icon"
                onClick={stop}
                disabled={stopping}
                title={t("stop")}
                className="size-8 rounded-full"
              >
                {stopping ? <Spinner className="size-4" /> : <Square className="size-4" />}
              </Button>
            ) : (
              <Button
                size="icon"
                onClick={() => send()}
                disabled={submitting || (!input.trim() && images.length === 0)}
                title={t("send")}
                className="size-8 rounded-full shadow-none disabled:bg-muted disabled:text-muted-foreground disabled:opacity-100"
              >
                {submitting ? <Spinner className="size-4" /> : <ArrowUp className="size-4" />}
              </Button>
            )}
          </div>
        </div>
      </div>
      </div>
      <AlertDialog open={active && pendingApproval !== null}>
        <AlertDialogContent className="max-w-sm">
          <AlertDialogHeader>
            <AlertDialogTitle>{t("allowToolTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("toolRequestsExecution", { tool: pendingApproval?.label ?? t("tool") })}
              {pendingApproval?.risk === "destructive"
                ? t("destructiveRisk")
                : pendingApproval?.risk === "write"
                  ? t("writeRisk")
                  : ""}
              {t("sentenceEnd")}{pendingApproval ? toolArgumentsPreview(pendingApproval.arguments, 120) : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <Button variant="outline" disabled={approvalBusy} onClick={() => resolveApproval(false)}>
              {t("reject")}
            </Button>
            <Button disabled={approvalBusy} onClick={() => resolveApproval(true)}>
              {approvalBusy && <Spinner />}
              {t("allowOnce")}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      </div>
    </TooltipProvider>
  );
}
