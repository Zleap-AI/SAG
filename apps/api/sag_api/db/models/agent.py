from __future__ import annotations

from sqlalchemy import JSON, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from sag_api.db.base import Base, IDMixin, TimestampMixin
from sag_api.enums import BindingTargetType, MessageRole, MessageStatus


class Agent(IDMixin, TimestampMixin, Base):
    """Agent —— 名字 + 系统提示 + 挂载的信源/工具（经 MCP）。"""

    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(String(120))
    avatar: Mapped[str] = mapped_column(String(64), default="")  # emoji / 首字母
    # 默认 agent：开箱即用的主对话入口，知识库=全部信源（resolve_sources 特判）
    is_default: Mapped[bool] = mapped_column(default=False, index=True)
    # 配置：{ system_prompt, greeting, tools[] }（tools 为额外启用的工具/MCP 名）
    persona: Mapped[dict] = mapped_column("persona_json", JSON, default=dict)


class AgentBinding(IDMixin, TimestampMixin, Base):
    """Agent 挂载的东西：一个信源，或一个 MCP server（工具来源）。"""

    __tablename__ = "agent_bindings"
    __table_args__ = (UniqueConstraint("agent_id", "target_type", "target_id", name="uq_agent_binding"),)

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    target_type: Mapped[BindingTargetType] = mapped_column(SAEnum(BindingTargetType, native_enum=False, length=16))
    target_id: Mapped[str] = mapped_column(String(64), index=True)
    # MCP server 连接配置（url 或 command/args/env）；信源绑定为空
    config: Mapped[dict] = mapped_column("config_json", JSON, default=dict)


class Thread(IDMixin, TimestampMixin, Base):
    __tablename__ = "threads"

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300), default="新会话")
    archived: Mapped[bool] = mapped_column(default=False, index=True)


class Message(IDMixin, TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_thread_created_id", "thread_id", "created_at", "id"),)

    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"))
    # 图片附件 meta：[{id, name, media_type}]（文件在 upload_dir/attachments/）
    attachments: Mapped[list] = mapped_column("attachments_json", JSON, default=list)
    # Agentic 执行轨迹：[{kind:thinking|tool, step, name?, args?, ms, count?}]（助手消息）
    steps: Mapped[list] = mapped_column("steps_json", JSON, default=list)
    role: Mapped[MessageRole] = mapped_column(SAEnum(MessageRole, native_enum=False, length=16))
    content: Mapped[str] = mapped_column(Text, default="")
    citations: Mapped[list] = mapped_column("citations_json", JSON, default=list)
    # Frozen initial model input for this assistant turn. It deliberately
    # excludes tool results and the generated answer so historical playback
    # can audit the same role-separated input that was shown live.
    prompt_preview: Mapped[str] = mapped_column(Text, default="")
    # 助手回复的终态；对话历史需要保留失败/取消气泡才能与实时会话一致。
    # values_callable 强制以枚举 value（小写 ok/failed/cancelled）而不是 name（大写）存储，
    # 与 server_default 保持一致，避免 SAEnum 在读回时按名字查表报 LookupError。
    status: Mapped[MessageStatus] = mapped_column(
        SAEnum(
            MessageStatus,
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=MessageStatus.OK,
        server_default=MessageStatus.OK.value,
    )
    # 失败/取消时的错误结构 {code, message, retryable, details}；成功时为 None。
    error: Mapped[dict | None] = mapped_column("error_json", JSON, default=None, nullable=True)
