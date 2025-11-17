"""文档相关 Schema"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from sag.api.schemas.base import TimestampMixin


class DocumentUploadResponse(BaseModel):
    """文档上传响应"""

    filename: Optional[str] = None
    file_path: str
    article_id: Optional[str] = None
    task_id: Optional[str] = None
    success: bool = True
    message: Optional[str] = None


class DocumentUpdate(BaseModel):
    """文档更新请求"""

    title: Optional[str] = None
    summary: Optional[str] = None
    tags: Optional[List[str]] = None
    category: Optional[str] = None


class DocumentResponse(TimestampMixin):
    """文档响应"""

    id: str
    source_config_id: str
    title: str
    summary: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    status: str  # PENDING, PROCESSING, COMPLETED, FAILED
    error: Optional[str] = None  # 错误信息
    extra_data: Optional[Dict[str, Any]] = None
    created_time: datetime
    updated_time: Optional[datetime] = None

    # 统计信息
    sections_count: int = 0
    events_count: int = 0

    class Config:
        from_attributes = True


class ArticleSectionResponse(BaseModel):
    """文章片段响应"""

    id: str
    article_id: str
    rank: int
    heading: str
    content: str
    extra_data: Optional[Dict[str, Any]] = None
    created_time: datetime
    updated_time: datetime

    class Config:
        from_attributes = True


class EntityInfo(BaseModel):
    """实体信息（包含在事项中的描述）"""

    id: str
    name: str
    type: str
    weight: float = 1.0
    description: Optional[str] = None  # 该实体在此事项中的具体描述/角色


class SourceEventResponse(BaseModel):
    """事项响应"""

    id: str
    source_config_id: str
    source_type: str
    source_id: str
    article_id: Optional[str] = None  # 兼容前端（从 source_id 计算）
    conversation_id: Optional[str] = None  # 兼容前端
    title: str
    summary: str
    content: str
    category: Optional[str] = None  # 事项分类（技术、产品、市场、研究、管理等）
    rank: int
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    references: Optional[List[ArticleSectionResponse]] = []
    entities: Optional[List[EntityInfo]] = []
    extra_data: Optional[Dict[str, Any]] = None
    created_time: datetime
    updated_time: datetime

    # 新增字段：来源和文档名称
    source_name: Optional[str] = ""  # 信息源名称（from SourceConfig.name）
    document_name: Optional[str] = ""  # 文档名称（from Article.title）

    class Config:
        from_attributes = True

    @classmethod
    def from_orm_with_entities(cls, event, sections_dict: dict = None):
        """从ORM对象转换，包含实体信息和完整片段信息"""
        entities = [
            EntityInfo(
                id=assoc.entity.id,
                name=assoc.entity.name,
                type=assoc.entity.type,
                weight=float(assoc.weight),
                description=assoc.description  # 从 event_entity 关联表获取描述
            )
            for assoc in event.event_associations
        ]

        # 根据 references ID 查询完整片段信息
        reference_sections = []
        if sections_dict and event.references:
            for ref_id in event.references:
                if ref_id in sections_dict:
                    section = sections_dict[ref_id]
                    reference_sections.append(ArticleSectionResponse(
                        id=section.id,
                        article_id=section.article_id,
                        rank=section.rank,
                        heading=section.heading,
                        content=section.content,
                        extra_data=section.extra_data,
                        created_time=section.created_time,
                        updated_time=section.updated_time
                    ))

        return cls(
            id=event.id,
            source_config_id=event.source_config_id,
            source_type=event.source_type,
            source_id=event.source_id,
            article_id=event.article_id,
            conversation_id=event.conversation_id,
            title=event.title,
            summary=event.summary,
            content=event.content,
            category=event.category,
            rank=event.rank,
            start_time=event.start_time,
            end_time=event.end_time,
            references=reference_sections,
            entities=entities,
            extra_data=event.extra_data,
            created_time=event.created_time,
            updated_time=event.updated_time,
            # 从 event 对象读取动态添加的属性
            source_name=getattr(event, 'source_name', ''),
            document_name=getattr(event, 'document_name', '')
        )
