"""
数据模型基类

所有Pydantic模型的基类
"""

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class SAGBaseModel(BaseModel):
    """SAG基础模型"""

    model_config = ConfigDict(
        # 允许ORM模式（从SQLAlchemy对象创建）
        from_attributes=True,
        # 使用枚举值而非枚举对象
        use_enum_values=True,
        # 验证赋值
        validate_assignment=True,
        # 填充None的默认值
        populate_by_name=True,
    )


class TimestampMixin(BaseModel):
    """时间戳混入类"""

    created_time: datetime = Field(default_factory=datetime.now, description="创建时间")
    updated_time: Optional[datetime] = Field(default=None, description="更新时间")

    model_config = ConfigDict(from_attributes=True)


class MetadataMixin(BaseModel):
    """扩展数据混入类"""

    extra_data: Optional[Dict[str, Any]] = Field(default=None, description="扩展数据(JSON)")

    model_config = ConfigDict(from_attributes=True)
