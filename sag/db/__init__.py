"""
数据库模块

提供SQLAlchemy ORM模型和数据库操作
"""

from sag.db.base import Base, get_engine, get_session_factory, init_database
from sag.db.models import (
    Article,
    ArticleSection,
    ChatConversation,
    ChatMessage,
    Entity,
    EntityType,
    EventEntity,
    ModelConfig,
    SourceChunk,
    SourceConfig,
    SourceEvent,
    Task,
)

__all__ = [
    # Base
    "Base",
    "get_engine",
    "get_session_factory",
    "init_database",
    # Models
    "SourceConfig",
    "Article",
    "ArticleSection",
    "EntityType",
    "Entity",
    "EventEntity",
    "SourceEvent",
    "Task",
    "ChatConversation",
    "ChatMessage",
    "ModelConfig",
    "SourceChunk",
]
