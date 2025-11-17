"""
事项数据模型
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from sag.models.base import SAGBaseModel, MetadataMixin, TimestampMixin


class EventCategory(str, Enum):
    """事项分类"""

    TECHNICAL = "TECHNICAL"  # 技术
    BUSINESS = "BUSINESS"  # 业务
    PERSONAL = "PERSONAL"  # 个人
    OTHER = "OTHER"  # 其他


class EventPriority(str, Enum):
    """优先级"""

    HIGH = "HIGH"  # 高
    MEDIUM = "MEDIUM"  # 中
    LOW = "LOW"  # 低


class EventStatus(str, Enum):
    """事项状态"""

    TODO = "TODO"  # 待办
    IN_PROGRESS = "IN_PROGRESS"  # 进行中
    DONE = "DONE"  # 已完成
    UNKNOWN = "UNKNOWN"  # 未知


class SourceEvent(SAGBaseModel, MetadataMixin, TimestampMixin):
    """源事项模型"""

    id: str = Field(..., description="事项ID (UUID)")
    source_config_id: str = Field(..., description="信息源配置ID")
    article_id: str = Field(..., description="文章ID")
    title: str = Field(..., min_length=1, max_length=255, description="标题")
    summary: str = Field(..., description="摘要")
    content: str = Field(..., description="内容（事项正文）")
    category: Optional[str] = Field(default="", max_length=50, description="事项分类（技术、产品、市场、研究、管理等）")
    rank: int = Field(default=0, ge=0, description="事项序号（同一来源内排序，从0开始）")
    start_time: Optional[datetime] = Field(default=None, description="开始时间")
    end_time: Optional[datetime] = Field(default=None, description="结束时间")
    references: Optional[List[str]] = Field(default=None, description="原始片段引用（从 SourceChunk.references 复制）")
    chunk_id: Optional[str] = Field(default=None, description="来源片段ID（指向 SourceChunk）")

    def get_category(self) -> Optional[str]:
        """获取分类"""
        if self.extra_data and "category" in self.extra_data:
            return self.extra_data["category"]
        return None

    def get_priority(self) -> Optional[str]:
        """获取优先级"""
        if self.extra_data and "priority" in self.extra_data:
            return self.extra_data["priority"]
        return None

    def get_status(self) -> Optional[str]:
        """获取状态"""
        if self.extra_data and "status" in self.extra_data:
            return self.extra_data["status"]
        return None

    def get_keywords(self) -> List[str]:
        """获取关键词"""
        if self.extra_data and "keywords" in self.extra_data:
            return self.extra_data["keywords"]
        return []

    def get_tags(self) -> List[str]:
        """获取标签"""
        if self.extra_data and "tags" in self.extra_data:
            return self.extra_data["tags"]
        return []

    def get_references(self) -> List[str]:
        """获取原始片段引用"""
        if self.references:
            return self.references
        return []


class EventWithEntities(SAGBaseModel):
    """带实体的事项模型（用于检索结果）"""

    event: SourceEvent
    entities: List[Dict[str, Any]] = Field(
        ...,
        description="实体列表：[{type, name, weight, ...}]"
    )
    relevance_score: float = Field(default=0.0, ge=0.0, le=1.0, description="相关度分数")

    def get_entities_by_type(self, entity_type: str) -> List[Dict[str, Any]]:
        """按类型获取实体"""
        return [e for e in self.entities if e.get("type") == entity_type]

    def get_entity_names_by_type(self, entity_type: str) -> List[str]:
        """按类型获取实体名称列表"""
        return [e.get("name", "") for e in self.entities if e.get("type") == entity_type]
