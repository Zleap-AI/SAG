"""
信息源配置数据模型
"""

from typing import Any, Dict, List, Optional

from pydantic import Field

from sag.models.base import SAGBaseModel, MetadataMixin, TimestampMixin


class SourceConfig(SAGBaseModel, MetadataMixin, TimestampMixin):
    """信息源配置模型"""

    id: str = Field(..., description="信息源配置ID (UUID)")
    name: str = Field(..., min_length=1, max_length=100, description="信息源配置名称")
    description: Optional[str] = Field(
        default=None, max_length=255, description="信息源配置描述"
    )
    config: Optional[Dict[str, Any]] = Field(
        default=None, description='偏好设置，格式：{"focus": ["AI"], "language": "zh"}'
    )

    def get_focus(self) -> List[str]:
        """获取关注领域"""
        if self.config and "focus" in self.config:
            return self.config["focus"]
        return []

    def get_language(self) -> str:
        """获取语言偏好"""
        if self.config and "language" in self.config:
            return self.config["language"]
        return "zh"


class SourceConfigCreate(SAGBaseModel):
    """创建信息源配置请求"""

    name: str = Field(..., min_length=1, max_length=100, description="信息源配置名称")
    description: Optional[str] = Field(default=None, max_length=255, description="信息源配置描述")
    config: Optional[Dict[str, Any]] = Field(default=None, description="偏好设置")


class SourceConfigUpdate(SAGBaseModel):
    """更新信息源配置请求"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=100, description="信息源配置名称")
    description: Optional[str] = Field(default=None, max_length=255, description="信息源配置描述")
    config: Optional[Dict[str, Any]] = Field(default=None, description="偏好设置")
