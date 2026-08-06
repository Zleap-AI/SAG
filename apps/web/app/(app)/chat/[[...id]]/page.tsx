"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { useParams, usePathname, useRouter } from "next/navigation";

import { DEFAULT_AGENT_AVATAR } from "@/lib/branding";
import { appPath } from "@/lib/deployment";
import { useApp } from "@/components/features/app-shell";
import {
  useConversationRuntime,
  useConversationSession,
} from "@/components/features/chat/conversation-provider";
import { ConversationPanel } from "@/components/features/chat/conversation-panel";
import { PetHeadAvatar } from "@/components/features/pet-head-avatar";

/** 对话主入口；会话数据与迷你问答共享，仅保留完整工作台外壳。 */
export default function ChatPage() {
  const t = useTranslations("ChatPage");
  const { id } = useParams<{ id?: string | string[] }>();
  const pathname = usePathname();
  const router = useRouter();
  const { agent, appMode } = useApp();
  const runtime = useConversationRuntime();
  const routeThreadId =
    (Array.isArray(id) ? id[0] : id) ?? pathname.match(/^\/chat\/([^/]+)/)?.[1] ?? null;
  const [sessionId, setSessionId] = React.useState<string | null>(null);
  const preferredDraftRef = React.useRef<string | null>(null);
  const session = useConversationSession(sessionId);

  React.useEffect(() => {
    if (routeThreadId) {
      preferredDraftRef.current = null;
      setSessionId(runtime.forThread(routeThreadId, { activate: true }));
      return;
    }
    const index = runtime.getIndexSnapshot();
    const next =
      preferredDraftRef.current ??
      index.activeRunSessionId ??
      index.activeSessionId ??
      runtime.createDraft({ activate: true });
    preferredDraftRef.current = null;
    runtime.activate(next);
    setSessionId(next);
  }, [routeThreadId, runtime]);

  React.useEffect(() => {
    const onNewChat = () => {
      const next = runtime.createDraft({ activate: true });
      preferredDraftRef.current = next;
      setSessionId(next);
      if (window.location.pathname !== appPath("/chat")) router.push("/chat");
    };
    window.addEventListener("sag:new-chat", onNewChat);
    return () => window.removeEventListener("sag:new-chat", onNewChat);
  }, [router, runtime]);

  React.useEffect(() => {
    const threadId = session?.threadId;
    if (!threadId || routeThreadId) return;
    const nextPath = appPath(`/chat/${threadId}`);
    if (window.location.pathname === nextPath) return;
    // 不触发路由卸载，确保创建线程后的流式回答持续由同一 runtime 托管。
    window.history.replaceState(window.history.state, "", nextPath);
    window.dispatchEvent(new Event("sag:pathchange"));
  }, [routeThreadId, session?.threadId]);

  const glyph = agent?.avatar || DEFAULT_AGENT_AVATAR;
  const avatarNode = React.useMemo(
    () => <PetHeadAvatar face={glyph} size="sm" className="mt-0.5" />,
    [glyph],
  );
  const heroNode = React.useMemo(
    () => <PetHeadAvatar face={glyph} size="lg" />,
    [glyph],
  );

  if (!agent || !sessionId || !session) return null;

  return (
    <div className="h-full min-h-0">
      <ConversationPanel
        key={sessionId}
        sessionId={sessionId}
        active={appMode === "normal"}
        avatarNode={avatarNode}
        heroNode={heroNode}
        emptyTitle={agent.name}
        suggestions={[
          t("suggestionSummary"),
          t("suggestionConclusions"),
          t("suggestionTimeline"),
        ]}
        emptyHint={agent.persona?.greeting || t("emptyHint")}
        placeholder={t("placeholder", { name: agent.name })}
      />
    </div>
  );
}
