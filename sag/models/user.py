"""
用户数据模型
"""

from typing import Any, Dict, List, Optional

from pydantic import EmailStr, Field

from sag.models.base import SAGBaseModel, MetadataMixin, TimestampMixin


class UserPreferences(SAGBaseModel):
    """用户偏好设置"""

    focus: List[str] = Field(default_factory=list, description="关注领域")
    language: str = Field(default="zh", description="语言偏好")
    # entity_weights: Optional[Dict[str, float]] = Field(
    #     default=None, description="自定义实体权重"
    # )


class User(SAGBaseModel, MetadataMixin, TimestampMixin):
    """用户模型"""

    id: str = Field(..., description="用户ID (UUID)")
    username: str = Field(..., min_length=1, max_length=100, description="用户名")
    email: Optional[EmailStr] = Field(default=None, description="邮箱")
    preferences: Optional[Dict[str, Any]] = Field(default=None, description="用户偏好")

    def get_focus(self) -> List[str]:
        """获取关注领域"""
        if self.preferences and "focus" in self.preferences:
            return self.preferences["focus"]
        return []

    def get_language(self) -> str:
        """获取语言偏好"""
        if self.preferences and "language" in self.preferences:
            return self.preferences["language"]
        return "zh"


class UserCreate(SAGBaseModel):
    """创建用户请求"""

    username: str = Field(..., min_length=1, max_length=100)
    email: Optional[EmailStr] = None
    preferences: Optional[Dict[str, Any]] = None


class UserUpdate(SAGBaseModel):
    """更新用户请求"""

    username: Optional[str] = Field(default=None, min_length=1, max_length=100)
    email: Optional[EmailStr] = None
    preferences: Optional[Dict[str, Any]] = None
