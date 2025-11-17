"""信息源配置 Schema"""

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from sag.api.schemas.base import TimestampMixin


class SourceConfigCreateRequest(BaseModel):
    """创建信息源配置请求"""

    name: str = Field(..., min_length=1, max_length=100, description="信息源配置名称")
    description: Optional[str] = Field(
        default=None, max_length=255, description="信息源配置描述"
    )
    config: Optional[Dict[str, Any]] = Field(
        default=None,
        description='偏好设置，格式：{"focus": ["AI"], "language": "zh"}',
    )


class SourceConfigUpdateRequest(BaseModel):
    """更新信息源配置请求"""

    name: Optional[str] = Field(
        default=None, min_length=1, max_length=100, description="信息源配置名称"
    )
    description: Optional[str] = Field(
        default=None, max_length=255, description="信息源配置描述"
    )
    config: Optional[Dict[str, Any]] = Field(default=None, description="偏好设置")


class SourceConfigResponse(TimestampMixin):
    """信息源配置响应"""

    id: str
    name: str
    description: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    created_time: datetime
    updated_time: Optional[datetime] = None
    document_count: int = Field(default=0, description="文档数量")
    entity_types_count: int = Field(default=0, description="专属实体类型数量")

    class Config:
        from_attributes = True

