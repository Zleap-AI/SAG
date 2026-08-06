"""Agent 领域逻辑：CRUD、绑定（信源/MCP）、上下文解析、多源 fan-out 对话。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sag_api.branding import DEFAULT_AGENT_AVATAR, DEFAULT_AGENT_NAME
from sag_api.core.config import settings
from sag_api.core.error_taxonomy import ErrorCode
from sag_api.core.errors import ConflictError, NotFoundError, ValidationError
from sag_api.db.models import Agent, AgentBinding, Message, Source, Thread
from sag_api.enums import BindingTargetType, MessageRole
from sag_api.generation import build_agent_messages, build_prompt_preview
from sag_api.generation.prompt import estimate_tokens
from sag_api.services.source_service import search_source_candidates

_DEFAULT_TITLES = {"新会话", "New chat"}
THREAD_PAGE_DEFAULT = 6
THREAD_PAGE_MAX = 100
MESSAGE_PAGE_DEFAULT = 40
MESSAGE_PAGE_MAX = 100
MESSAGE_CURSOR_MAX_LENGTH = 2048
_MESSAGE_CURSOR_ID = re.compile(r"[0-9a-f]{32}\Z")


@dataclass(frozen=True, slots=True)
class MessagePage:
    items: list[Message]
    next_cursor: str | None
    has_more: bool


def _message_cursor_scope(thread_id: str) -> str:
    return hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:24]


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    if not value:
        raise ValueError("empty base64 value")
    padded = f"{value}{'=' * (-len(value) % 4)}".encode("ascii")
    decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    if _urlsafe_encode(decoded) != value:
        raise ValueError("non-canonical base64 value")
    return decoded


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant: {value}")


def _encode_message_cursor(thread_id: str, message: Message) -> str:
    payload = {
        "v": 1,
        "kind": "messages",
        "scope": _message_cursor_scope(thread_id),
        "created_at": message.created_at.astimezone(UTC).isoformat(timespec="microseconds"),
        "id": message.id,
    }
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    signature = hmac.new(settings.secret_key.encode("utf-8"), raw, hashlib.sha256).digest()
    return f"{_urlsafe_encode(raw)}.{_urlsafe_encode(signature)}"


def _decode_message_cursor(thread_id: str, value: str) -> tuple[datetime, str]:
    def invalid() -> ValidationError:
        return ValidationError("消息游标无效", code=ErrorCode.INVALID_CURSOR)

    if not value or len(value) > MESSAGE_CURSOR_MAX_LENGTH or value.count(".") != 1:
        raise invalid()
    try:
        encoded_payload, encoded_signature = value.split(".", 1)
        raw = _urlsafe_decode(encoded_payload)
        signature = _urlsafe_decode(encoded_signature)
        if len(raw) > 512 or len(signature) != hashlib.sha256().digest_size:
            raise ValueError("invalid cursor size")
        expected = hmac.new(settings.secret_key.encode("utf-8"), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid cursor signature")
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        expected_keys = {"v", "kind", "scope", "created_at", "id"}
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            raise ValueError("invalid cursor payload")
        if (
            not isinstance(payload["v"], int)
            or isinstance(payload["v"], bool)
            or payload["v"] != 1
            or payload["kind"] != "messages"
            or payload["scope"] != _message_cursor_scope(thread_id)
            or not isinstance(payload["created_at"], str)
            or not isinstance(payload["id"], str)
            or _MESSAGE_CURSOR_ID.fullmatch(payload["id"]) is None
        ):
            raise ValueError("invalid cursor scope or values")
        created_at = datetime.fromisoformat(payload["created_at"])
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("cursor timestamp must include a timezone")
        return created_at.astimezone(UTC), payload["id"]
    except (
        UnicodeEncodeError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise invalid() from error


# ── CRUD ────────────────────────────────────────────────────────────
async def list_agents(session: AsyncSession) -> list[Agent]:
    rows = await session.execute(select(Agent).order_by(Agent.created_at.desc()))
    return list(rows.scalars().all())


async def get_agent(session: AsyncSession, agent_id: str) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise NotFoundError("Agent 不存在")
    return agent


async def create_agent(session: AsyncSession, *, name: str, avatar: str = "", persona: dict | None = None) -> Agent:
    name = name.strip()
    if not name:
        raise ValidationError("Agent 名称不能为空")
    agent = Agent(name=name, avatar=avatar or name[:1], persona=persona or {})
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def update_agent(
    session: AsyncSession,
    agent_id: str,
    *,
    name: str | None = None,
    avatar: str | None = None,
    persona: dict | None = None,
) -> Agent:
    agent = await get_agent(session, agent_id)
    if name is not None:
        agent.name = name
    if avatar is not None:
        agent.avatar = avatar
    if persona is not None:
        agent.persona = persona
    await session.commit()
    await session.refresh(agent)
    return agent


_DEFAULT_GREETING = "我在。上传资料到知识库，或直接问我任何问题。"
_DEFAULT_PERSONA = {"greeting": _DEFAULT_GREETING, "system_prompt": ""}


def _is_legacy_default_agent(agent: Agent) -> bool:
    """仅识别旧版本完全未自定义的默认助手，避免覆盖用户修改。"""
    return agent.name == "sag" and agent.avatar in {"s", "S"} and (agent.persona or {}) == _DEFAULT_PERSONA


async def get_default_agent(session: AsyncSession) -> Agent:
    """默认 agent（开箱即用的主对话入口）：get-or-create，幂等。"""
    agent = await session.scalar(select(Agent).where(Agent.is_default.is_(True)))
    if agent is not None:
        if _is_legacy_default_agent(agent):
            agent.name = DEFAULT_AGENT_NAME
            agent.avatar = DEFAULT_AGENT_AVATAR
            await session.commit()
            await session.refresh(agent)
        return agent
    agent = Agent(
        name=DEFAULT_AGENT_NAME,
        avatar=DEFAULT_AGENT_AVATAR,
        is_default=True,
        persona=dict(_DEFAULT_PERSONA),
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def delete_agent(session: AsyncSession, agent_id: str) -> None:
    agent = await get_agent(session, agent_id)
    await session.delete(agent)
    await session.commit()


# ── 绑定（信源 / MCP server）────────────────────────────────────────
async def list_bindings(session: AsyncSession, agent: Agent) -> list[AgentBinding]:
    rows = await session.execute(select(AgentBinding).where(AgentBinding.agent_id == agent.id))
    return list(rows.scalars().all())


async def add_binding(
    session: AsyncSession,
    agent: Agent,
    *,
    target_type: BindingTargetType,
    target_id: str,
    config: dict | None = None,
) -> AgentBinding:
    config = config or {}
    if target_type == BindingTargetType.SOURCE:
        if await session.get(Source, target_id) is None:
            raise NotFoundError("信源不存在")
    elif target_type == BindingTargetType.MCP_SERVER:
        if not (config.get("url") or config.get("command")):
            raise ValidationError("MCP server 需提供 url 或 command")
        target_id = target_id or (config.get("name") or config.get("url") or "mcp")
    exists = await session.scalar(
        select(AgentBinding).where(
            AgentBinding.agent_id == agent.id,
            AgentBinding.target_type == target_type,
            AgentBinding.target_id == target_id,
        )
    )
    if exists is not None:
        raise ConflictError("已绑定该目标")
    binding = AgentBinding(agent_id=agent.id, target_type=target_type, target_id=target_id, config=config)
    session.add(binding)
    await session.commit()
    await session.refresh(binding)
    return binding


async def remove_binding(session: AsyncSession, agent: Agent, binding_id: str) -> None:
    binding = await session.get(AgentBinding, binding_id)
    if binding is None or binding.agent_id != agent.id:
        raise NotFoundError("绑定不存在")
    await session.delete(binding)
    await session.commit()


async def resolve_sources(
    session: AsyncSession,
    agent: Agent,
    source_ids: list[str] | None = None,
) -> list[Source]:
    """解析本轮可见信源。

    显式 `source_ids` 来自输入框的 @ 范围，优先于持久绑定；所有入口共用同一
    候选上限，避免默认 Agent 或大量绑定造成无界 fan-out。
    """
    if source_ids:
        return await search_source_candidates(session, source_ids)
    if agent.is_default:
        return await search_source_candidates(session)
    bindings = await list_bindings(session, agent)
    src_ids = [b.target_id for b in bindings if b.target_type == BindingTargetType.SOURCE]
    if not src_ids:
        return []
    return await search_source_candidates(session, src_ids)


async def resolve_mcp_specs(session: AsyncSession, agent: Agent) -> list[tuple[str, dict]]:
    """展开外部 MCP server 绑定 → `[(label, config), …]`，供 agent 作 MCP 客户端挂载。"""
    bindings = await list_bindings(session, agent)
    specs: list[tuple[str, dict]] = []
    for b in bindings:
        if b.target_type != BindingTargetType.MCP_SERVER:
            continue
        cfg = b.config or {}
        specs.append((cfg.get("name") or b.target_id or "mcp", cfg))
    return specs


# ── 会话 ────────────────────────────────────────────────────────────
async def list_threads(
    session: AsyncSession,
    agent_id: str,
    *,
    archived: bool = False,
    limit: int = THREAD_PAGE_DEFAULT,
    offset: int = 0,
) -> list[Thread]:
    statement = (
        select(Thread)
        .where(Thread.agent_id == agent_id, Thread.archived.is_(archived))
        .order_by(Thread.updated_at.desc(), Thread.id.desc())
    )
    if offset:
        statement = statement.offset(offset)
    statement = statement.limit(max(1, min(int(limit), THREAD_PAGE_MAX)))
    rows = await session.execute(statement)
    return list(rows.scalars().all())


async def update_thread(
    session: AsyncSession,
    agent_id: str,
    thread_id: str,
    *,
    title: str | None = None,
    archived: bool | None = None,
) -> Thread:
    thread = await get_thread(session, agent_id, thread_id)
    if title is not None and title.strip():
        thread.title = title.strip()[:200]
    if archived is not None:
        thread.archived = archived
    await session.commit()
    await session.refresh(thread)
    return thread


async def create_thread(session: AsyncSession, agent: Agent, title: str = "新会话") -> Thread:
    thread = Thread(agent_id=agent.id, title=title or "新会话")
    session.add(thread)
    await session.commit()
    await session.refresh(thread)
    return thread


async def get_thread(session: AsyncSession, agent_id: str, thread_id: str) -> Thread:
    thread = await session.get(Thread, thread_id)
    if thread is None or thread.agent_id != agent_id:
        raise NotFoundError("会话不存在")
    return thread


async def delete_thread(session: AsyncSession, agent_id: str, thread_id: str) -> None:
    thread = await get_thread(session, agent_id, thread_id)
    await session.delete(thread)
    await session.commit()


async def list_messages_page(
    session: AsyncSession,
    thread_id: str,
    *,
    limit: int = MESSAGE_PAGE_DEFAULT,
    cursor: str | None = None,
) -> MessagePage:
    """返回最近一页消息，页内保持正向时间顺序。

    数据库以 `(created_at, id)` 倒序做 keyset 读取，只取 `limit + 1`
    判断是否还有更旧消息；不做 COUNT 扫描。
    """
    if limit < 1 or limit > MESSAGE_PAGE_MAX:
        raise ValidationError(
            f"消息页大小必须在 1 到 {MESSAGE_PAGE_MAX} 之间",
            code=ErrorCode.INVALID_PAGE_LIMIT,
        )

    statement = select(Message).where(Message.thread_id == thread_id)
    if cursor:
        created_at, message_id = _decode_message_cursor(thread_id, cursor)
        statement = statement.where(
            or_(
                Message.created_at < created_at,
                and_(Message.created_at == created_at, Message.id < message_id),
            )
        )
    rows = await session.execute(statement.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit + 1))
    candidates = list(rows.scalars().all())
    has_more = len(candidates) > limit
    page_desc = candidates[:limit]
    next_cursor = _encode_message_cursor(thread_id, page_desc[-1]) if has_more and page_desc else None
    return MessagePage(
        items=list(reversed(page_desc)),
        next_cursor=next_cursor,
        has_more=has_more,
    )


async def _history(session: AsyncSession, thread_id: str, exclude_id: str) -> list[dict[str, str]]:
    rows = await session.execute(
        select(Message)
        .where(
            Message.thread_id == thread_id,
            Message.id != exclude_id,
            Message.role.in_((MessageRole.USER, MessageRole.ASSISTANT)),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(settings.history_load_limit)
    )
    messages = list(reversed(rows.scalars().all()))
    return [{"role": m.role.value, "content": m.content} for m in messages]


def _history_tokens(history: list[dict[str, str]]) -> int:
    return sum(estimate_tokens(m["content"]) for m in history)


async def compress_history(history: list[dict[str, str]], *, llm=None, budget_tokens: int) -> list[dict[str, str]]:
    """上下文阈值压缩：超预算时把较早消息压成一段摘要，仅保留最近 N 条原文。

    有 LLM → 摘要旧段（保留事实/结论/称呼/待办）；无 LLM/失败 → 按预算从尾部裁剪。
    """
    if _history_tokens(history) <= budget_tokens:
        return history

    keep = max(2, settings.history_keep_recent)
    recent = history[-keep:]
    older = history[:-keep]
    if not older:
        return recent

    if llm is not None and getattr(llm, "configured", False):
        transcript = "\n".join(f"{'用户' if m['role'] == 'user' else '助手'}：{m['content']}" for m in older)[:12000]
        try:
            summary = await llm.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "把以下对话压缩为要点摘要（≤400字）：保留事实、结论、数字、人物称呼与未决事项；不要评论。"
                        ),
                    },
                    {"role": "user", "content": transcript},
                ]
            )
            return [
                {"role": "user", "content": f"（此前对话摘要，供参考）\n{summary.strip()}"},
                *recent,
            ]
        except Exception:  # noqa: BLE001
            pass

    # 兜底：从最近往前装，装满预算为止
    trimmed: list[dict[str, str]] = []
    used = 0
    for m in reversed(history):
        t = estimate_tokens(m["content"])
        if used + t > budget_tokens and trimmed:
            break
        trimmed.append(m)
        used += t
    return list(reversed(trimmed))


# ── 问答计划 ─────────────────────────────────────────────────────────
@dataclass
class AskPlan:
    """一次问答的提示词计划（agent-first：检索由循环内工具按需完成，不预置资料区）。"""

    query: str = ""
    messages: list[dict[str, str]] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    prompt_preview: str = ""
    source_ids: list[str] | None = None  # @范围：限定循环内检索工具可见的信源
    user_message_id: str | None = None


def build_ask_context(
    *,
    agent: Agent,
    query: str,
    history: list[dict[str, str]] | None = None,
    attachments: list[dict] | None = None,
    source_ids: list[str] | None = None,
) -> AskPlan:
    """组装带系统提示的消息（不落库，对话与 OpenAI 端点复用）。是否检索由模型经工具决定。"""
    messages = build_agent_messages(
        agent.name,
        agent.persona or {},
        query,
        history=history,
        language=settings.sag_language,
        timezone=settings.timezone,
        attachments=attachments,
    )
    return AskPlan(
        query=query,
        messages=messages,
        prompt_preview=build_prompt_preview(messages),
        source_ids=source_ids or None,
    )


async def prepare_ask(
    session: AsyncSession,
    *,
    agent: Agent,
    thread: Thread,
    query: str,
    attachments: list[str] | None = None,
    source_ids: list[str] | None = None,
    llm=None,
) -> AskPlan:
    """落库用户消息（含图片附件 meta）、解析历史（超上下文阈值时主动压缩），组装计划。"""
    from sag_api.api.v1.attachments import attachment_path

    resolved: list[dict] = []
    for aid in attachments or []:
        path = attachment_path(aid)
        if path is None:
            raise ValidationError(f"附件不存在或已过期：{aid}")
        ext = aid.rsplit(".", 1)[-1].lower()
        media_type = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "webp": "image/webp",
            "gif": "image/gif",
        }.get(ext, "image/png")
        resolved.append({"id": aid, "media_type": media_type, "path": path})

    user_msg = Message(
        thread_id=thread.id,
        role=MessageRole.USER,
        content=query,
        citations=[],
        attachments=[{k: a[k] for k in ("id", "media_type")} for a in resolved],
    )
    session.add(user_msg)
    if thread.title in _DEFAULT_TITLES:
        thread.title = query[:40] or "图片对话"
    await session.commit()
    await session.refresh(user_msg)

    history = await _history(session, thread.id, exclude_id=user_msg.id)
    # 历史预算 = 上下文窗口的 40%（其余留给工具轮/回答）
    history = await compress_history(history, llm=llm, budget_tokens=int(settings.llm_context_window * 0.4))
    plan = build_ask_context(
        agent=agent,
        query=query,
        history=history,
        attachments=resolved or None,
        source_ids=source_ids,
    )
    plan.user_message_id = user_msg.id
    return plan


async def delete_message(session: AsyncSession, agent_id: str, thread_id: str, message_id: str) -> None:
    thread = await get_thread(session, agent_id, thread_id)
    message = await session.get(Message, message_id)
    if message is None or message.thread_id != thread.id:
        raise NotFoundError("消息不存在")
    await session.delete(message)
    await session.commit()


async def persist_answer(
    session_factory: async_sessionmaker,
    thread_id: str,
    answer: str,
    citations: list[dict],
    steps: list[dict] | None = None,
    prompt_preview: str = "",
) -> str:
    async with session_factory() as session:
        message = Message(
            thread_id=thread_id,
            role=MessageRole.ASSISTANT,
            content=answer,
            citations=citations,
            steps=steps or [],
            prompt_preview=prompt_preview,
        )
        session.add(message)
        await session.commit()
        await session.refresh(message)
        return message.id
