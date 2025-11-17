"""实体相关 Schema"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from sag.api.schemas.base import TimestampMixin


class EntityTypeCreateRequest(BaseModel):
    """创建实体类型请求"""

    type: str = Field(..., min_length=1, max_length=50, description="类型标识符")
    name: str = Field(..., min_length=1, max_length=100, description="类型名称")
    description: str = Field(..., description="类型描述，用于指导LLM提取")
    weight: float = Field(default=1.0, ge=0.0, le=9.99, description="默认权重")
    similarity_threshold: float = Field(
        default=0.8, ge=0.0, le=1.0, description="相似度匹配阈值"
    )
    # 🆕 应用范围配置字段
    scope: str = Field(
        default='global', description="应用范围：global（通用）、source（信息源）、article（文档）")
    article_id: Optional[str] = Field(
        default=None, description="文档ID（scope=article时必填）")
    extraction_prompt: Optional[str] = Field(
        default=None, description="自定义提取提示词模板"
    )
    extraction_examples: Optional[List[Dict[str, str]]] = Field(
        default=None, description="Few-shot示例"
    )
    validation_rule: Optional[Dict[str, Any]] = Field(
        default=None, description="验证规则"
    )
    metadata_schema: Optional[Dict[str, Any]] = Field(
        default=None, description="元数据Schema"
    )
    # 🆕 值类型化配置字段
    value_format: Optional[str] = Field(
        default=None, max_length=100, description="值格式模板（如 {number}{unit}）"
    )
    value_constraints: Optional[Dict[str, Any]] = Field(
        default=None, description="值约束配置（type, enum_values, min, max, unit等）"
    )

    @field_validator("weight")
    @classmethod
    def validate_weight(cls, v: float) -> float:
        """验证权重范围"""
        return round(v, 2)

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, v: str) -> str:
        """验证应用范围"""
        valid_scopes = ["global", "source", "article"]
        if v not in valid_scopes:
            raise ValueError(f"scope 必须是以下之一: {', '.join(valid_scopes)}")
        return v

    @field_validator("article_id")
    @classmethod
    def validate_article_id(cls, v: Optional[str], info) -> Optional[str]:
        """验证文档ID - 当 scope=article 时必填"""
        # 注意：此时无法访问 scope 字段，需要在 model_validator 中验证
        return v

    @field_validator("similarity_threshold")
    @classmethod
    def validate_similarity_threshold(cls, v: float) -> float:
        """验证相似度阈值范围并保留3位小数"""
        return round(v, 3)

    @field_validator("value_constraints")
    @classmethod
    def validate_value_constraints(cls, v: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """验证值约束配置"""
        if v is None:
            return None

        # 验证必须包含 type 字段
        if "type" not in v:
            raise ValueError("value_constraints 必须包含 type 字段")

        value_type = v.get("type")
        valid_types = ["int", "float", "datetime", "bool", "enum", "text"]
        if value_type not in valid_types:
            raise ValueError(
                f"value_constraints.type 必须是以下之一: {', '.join(valid_types)}")

        # 枚举类型必须提供 enum_values 且不能为空
        if value_type == "enum":
            enum_values = v.get("enum_values")
            if not enum_values or not isinstance(enum_values, list) or len(enum_values) == 0:
                raise ValueError("枚举类型必须提供 enum_values 列表且至少包含1个值")

        # 数值类型验证 min/max 范围
        if value_type in ["int", "float"]:
            min_val = v.get("min")
            max_val = v.get("max")
            if min_val is not None and max_val is not None:
                if min_val > max_val:
                    raise ValueError("最小值不能大于最大值")

        return v

    @model_validator(mode='after')
    def validate_article_scope(self):
        """验证当 scope=article 时，article_id 必填"""
        if self.scope == 'article' and not self.article_id:
            raise ValueError("当 scope 为 article 时，必须提供 article_id")
        return self


class EntityTypeUpdateRequest(BaseModel):
    """更新实体类型请求"""

    name: Optional[str] = Field(default=None, description="类型名称")
    description: Optional[str] = Field(default=None, description="类型描述")
    weight: Optional[float] = Field(
        default=None, ge=0.0, le=9.99, description="默认权重")
    similarity_threshold: Optional[float] = Field(
        default=None, ge=0.0, le=1.0, description="相似度阈值"
    )
    is_active: Optional[bool] = Field(default=None, description="是否启用")
    extraction_prompt: Optional[str] = Field(default=None, description="提取提示词")
    extraction_examples: Optional[List[Dict[str, str]]] = Field(
        default=None, description="Few-shot示例"
    )
    # 🆕 值类型化配置字段
    value_format: Optional[str] = Field(
        default=None, max_length=100, description="值格式模板")
    value_constraints: Optional[Dict[str, Any]] = Field(
        default=None, description="值约束配置")

    @field_validator("value_constraints")
    @classmethod
    def validate_value_constraints(cls, v: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """验证值约束配置（复用 CreateRequest 的验证逻辑）"""
        if v is None:
            return None

        if "type" not in v:
            raise ValueError("value_constraints 必须包含 type 字段")

        value_type = v.get("type")
        valid_types = ["int", "float", "datetime", "bool", "enum", "text"]
        if value_type not in valid_types:
            raise ValueError(
                f"value_constraints.type 必须是以下之一: {', '.join(valid_types)}")

        if value_type == "enum":
            enum_values = v.get("enum_values")
            if not enum_values or not isinstance(enum_values, list) or len(enum_values) == 0:
                raise ValueError("枚举类型必须提供 enum_values 列表且至少包含1个值")

        if value_type in ["int", "float"]:
            min_val = v.get("min")
            max_val = v.get("max")
            if min_val is not None and max_val is not None:
                if min_val > max_val:
                    raise ValueError("最小值不能大于最大值")

        return v


class EntityTypeResponse(TimestampMixin):
    """实体类型响应"""

    id: str
    scope: str = 'global'  # 🆕 应用范围
    source_config_id: Optional[str] = None
    article_id: Optional[str] = None  # 🆕 文档ID
    type: str
    name: str
    description: Optional[str] = None
    weight: float
    similarity_threshold: float
    is_active: bool
    is_default: bool
    extra_data: Optional[Dict[str, Any]] = None
    # 🆕 值类型化配置字段
    value_format: Optional[str] = None
    value_constraints: Optional[Dict[str, Any]] = None
    created_time: Optional[datetime] = None
    updated_time: Optional[datetime] = None

    class Config:
        from_attributes = True
