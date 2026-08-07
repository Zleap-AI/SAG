from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sag_api.enums import BindingTargetType, MessageRole, MessageStatus


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    avatar: str = ""
    persona: dict[str, Any] = Field(default_factory=dict)  # { system_prompt, greeting, tools[] }


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    avatar: str | None = None
    persona: dict[str, Any] | None = None


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    avatar: str
    persona: dict[str, Any]
    is_default: bool = False
    created_at: datetime
    updated_at: datetime


class BindingCreate(BaseModel):
    target_type: BindingTargetType = BindingTargetType.SOURCE
    target_id: str = ""
    config: dict[str, Any] = Field(default_factory=dict)  # MCP: url 或 command/args/env


class BindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    target_type: BindingTargetType
    target_id: str
    config: dict[str, Any]


class ThreadCreate(BaseModel):
    title: str = "新会话"


class ThreadUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    archived: bool | None = None


class ThreadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    title: str
    archived: bool = False
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    thread_id: str
    role: MessageRole
    content: str
    citations: list[dict[str, Any]]
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    prompt_preview: str = ""
    # 助手气泡的终态：ok / failed / cancelled。用户消息始终 ok。
    status: MessageStatus = MessageStatus.OK
    # 失败/取消时的结构化错误 {code, message, retryable, details}；ok 时为 None。
    error: dict[str, Any] | None = None
    created_at: datetime


class MessagePageOut(BaseModel):
    items: list[MessageOut] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class AskRequest(BaseModel):
    query: str = Field(default="", max_length=4000)
    # 图片附件 id 列表（≤4，经 POST /attachments 上传）
    attachments: list[str] = Field(default_factory=list, max_length=4)
    # @知识库 范围限定：仅在这些信源内检索（空=默认全部）
    source_ids: list[str] = Field(default_factory=list, max_length=8)
    # 联网能力由用户逐轮授权；默认关闭，开启后才暴露 Agent 配置的外部/MCP 工具。
    web_enabled: bool = False
    # 旧客户端兼容字段：knowledge_only=false 等价于 web_enabled=true。
    # 新旧字段同时出现时，以语义更明确的 web_enabled 为准。
    knowledge_only: bool | None = None

    @model_validator(mode="after")
    def require_text_or_attachment(self):
        if not self.query.strip() and not self.attachments:
            raise ValueError("问题或图片至少提供一项")
        return self

    @property
    def effective_web_enabled(self) -> bool:
        """Resolve the new opt-in switch while preserving explicit legacy requests."""

        if "web_enabled" in self.model_fields_set:
            return self.web_enabled
        if self.knowledge_only is not None:
            return not self.knowledge_only
        return False


class ToolRejection(BaseModel):
    reason: str = Field(default="用户拒绝执行", max_length=500)
