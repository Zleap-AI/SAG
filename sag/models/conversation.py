"""
聊天会话和消息数据模型
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import Field

from sag.models.base import SAGBaseModel, MetadataMixin, TimestampMixin


class MessageType(str, Enum):
    """消息类型"""

    TEXT = "TEXT"  # 文本消息
    IMAGE = "IMAGE"  # 图片消息
    FILE = "FILE"  # 文件消息
    SYSTEM = "SYSTEM"  # 系统消息


class SenderRole(str, Enum):
    """发送者角色"""

    USER = "USER"  # 用户
    ASSISTANT = "ASSISTANT"  # 助手/机器人
    SYSTEM = "SYSTEM"  # 系统


class ChatConversation(SAGBaseModel, MetadataMixin, TimestampMixin):
    """聊天会话模型"""

    id: Optional[str] = Field(default=None, description="会话ID (UUID)")
    source_config_id: str = Field(..., description="信息源ID")
    title: Optional[str] = Field(
        default=None, max_length=255, description="会话标题")
    last_message_time: Optional[datetime] = Field(
        default=None, description="最后消息时间")
    messages_count: int = Field(default=0, description="消息数量")


class ChatMessage(SAGBaseModel, MetadataMixin, TimestampMixin):
    """聊天消息模型"""

    id: Optional[str] = Field(default=None, description="消息ID (UUID)")
    conversation_id: str = Field(..., description="会话ID")
    timestamp: datetime = Field(..., description="消息时间戳")
    content: Optional[str] = Field(default=None, description="消息内容")
    sender_id: Optional[str] = Field(
        default=None, max_length=100, description="发送者ID")
    sender_name: Optional[str] = Field(
        default=None, max_length=100, description="发送者名称")
    sender_avatar: Optional[str] = Field(
        default=None, max_length=255, description="发送者头像URL")
    sender_title: Optional[str] = Field(
        default=None, max_length=50, description="发送者头衔")
    type: MessageType = Field(default=MessageType.TEXT, description="消息类型")
    sender_role: SenderRole = Field(
        default=SenderRole.USER, description="发送者角色")
