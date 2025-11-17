"""
实体数据模型
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import Field, field_validator

from sag.models.base import SAGBaseModel, MetadataMixin, TimestampMixin


class EntityType(SAGBaseModel, MetadataMixin, TimestampMixin):
    """实体类型定义模型"""

    id: str = Field(..., description="实体类型ID (UUID)")
    scope: str = Field(
        default="global", description="应用范围：global/source/article")
    source_config_id: Optional[str] = Field(
        default=None, description="信息源ID (NULL表示系统默认类型)")
    article_id: Optional[str] = Field(
        default=None, description="文档ID（仅 scope=article 时有值）")
    type: str = Field(..., min_length=1, max_length=50, description="类型标识符")
    name: str = Field(..., min_length=1, max_length=100, description="类型名称")
    is_default: bool = Field(default=False, description="是否为系统默认类型")
    description: Optional[str] = Field(default=None, description="类型描述")
    weight: float = Field(default=1.0, ge=0.0, le=9.99, description="默认权重")
    similarity_threshold: float = Field(
        default=0.80, ge=0.0, le=1.0, description="实体相似度匹配阈值（0.000-1.000）"
    )
    is_active: bool = Field(default=True, description="是否启用")
    value_format: Optional[str] = Field(
        default=None, description="值格式模板（如 {number}{unit}）")
    value_constraints: Optional[Dict[str, Any]] = Field(
        default=None, description="值约束（如枚举列表、数值范围）")

    @field_validator("weight")
    @classmethod
    def validate_weight(cls, v: float) -> float:
        """验证权重范围"""
        return round(v, 2)

    @field_validator("similarity_threshold")
    @classmethod
    def validate_similarity_threshold(cls, v: float) -> float:
        """验证相似度阈值范围并保留3位小数"""
        return round(v, 3)


class Entity(SAGBaseModel, MetadataMixin, TimestampMixin):
    """实体模型（多对多关系：通过 event_entity 关联表与事项关联）"""

    id: str = Field(..., description="实体ID (UUID)")
    source_config_id: str = Field(..., description="信息源ID")
    entity_type_id: str = Field(..., description="实体类型ID（引用entity_type.id）")
    type: str = Field(
        ..., min_length=1, max_length=50, description="实体类型标识符（冗余字段，便于查询）"
    )
    name: str = Field(..., min_length=1, max_length=500, description="实体名称")
    normalized_name: str = Field(..., min_length=1,
                                 max_length=500, description="标准化名称")
    description: Optional[str] = Field(default=None, description="实体描述")

    # ========== 类型化值字段（用于统计分析） ==========
    value_type: Optional[str] = Field(
        default=None, description="值类型（int/float/datetime/bool/enum/text）")
    value_raw: Optional[str] = Field(
        default=None, description="原始提取文本（如 '199元'）")
    int_value: Optional[int] = Field(default=None, description="整数值")
    float_value: Optional[Decimal] = Field(default=None, description="浮点数值")
    datetime_value: Optional[datetime] = Field(
        default=None, description="日期时间值")
    bool_value: Optional[bool] = Field(default=None, description="布尔值")
    enum_value: Optional[str] = Field(default=None, description="枚举值")
    value_unit: Optional[str] = Field(
        default=None, description="单位（如 '元', '美元'）")
    value_confidence: Optional[Decimal] = Field(
        default=None, ge=0.0, le=1.0, description="解析置信度")

    def get_typed_value(self) -> Any:
        """根据 value_type 获取对应的类型化值"""
        if self.value_type == "int":
            return self.int_value
        elif self.value_type == "float":
            return self.float_value
        elif self.value_type == "datetime":
            return self.datetime_value
        elif self.value_type == "bool":
            return self.bool_value
        elif self.value_type == "enum":
            return self.enum_value
        return None

    def get_synonyms(self) -> List[str]:
        """获取同义词"""
        if self.extra_data and "synonyms" in self.extra_data:
            return self.extra_data["synonyms"]
        return []

    def get_weight(self) -> float:
        """获取权重"""
        if self.extra_data and "weight" in self.extra_data:
            return self.extra_data["weight"]
        return 1.0

    def get_confidence(self) -> float:
        """获取置信度"""
        if self.extra_data and "confidence" in self.extra_data:
            return self.extra_data["confidence"]
        return 1.0


class EventEntity(SAGBaseModel, MetadataMixin, TimestampMixin):
    """事项-实体关联模型（多对多关系）"""

    id: str = Field(..., description="关联ID (UUID)")
    event_id: str = Field(..., description="事项ID")
    entity_id: str = Field(..., description="实体ID")
    weight: float = Field(default=1.0, ge=0.0, le=9.99,
                          description="该实体在此事项中的权重")

    @field_validator("weight")
    @classmethod
    def validate_weight(cls, v: float) -> float:
        """验证权重范围"""
        return round(v, 2)

    def get_confidence(self) -> float:
        """获取置信度"""
        if self.extra_data and "confidence" in self.extra_data:
            return self.extra_data["confidence"]
        return 1.0

    def get_context(self) -> Optional[str]:
        """获取上下文"""
        if self.extra_data and "context" in self.extra_data:
            return self.extra_data["context"]
        return None


class EntityWithWeight(SAGBaseModel):
    """带权重的实体（用于事项查询结果）"""

    entity: Entity
    weight: float = Field(default=1.0, ge=0.0, le=9.99,
                          description="该实体在事项中的权重")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="提取置信度")


class CustomEntityType(SAGBaseModel):
    """自定义实体类型定义"""

    type: str = Field(..., description="类型标识符")
    name: str = Field(..., description="类型名称")
    description: str = Field(..., description="类型描述，用于指导LLM提取")
    weight: float = Field(default=1.0, ge=0.0, le=9.99, description="默认权重")
    extraction_prompt: Optional[str] = Field(
        default=None, description="自定义提取提示词模板")
    extraction_examples: Optional[List[Dict[str, str]]] = Field(
        default=None, description="Few-shot示例"
    )
    validation_rule: Optional[Dict[str, Any]] = Field(
        default=None, description="验证规则")
    metadata_schema: Optional[Dict[str, Any]] = Field(
        default=None, description="元数据Schema")


# 默认实体类型（按固定顺序：时间、地点、人物、行为、话题、标签）
# 权重层级说明：
# - 基本维度（权重=1.0）：time, location, person - 事件的基础要素
# - 重要维度（权重>1.0）：action(1.5), topic(1.8) - 事件的核心要素
# - 兜底维度（权重<1.0）：tags(0.5) - 所有未定义的实体都放这里
# 去重机制：用户自定义维度默认权重=1.0，会覆盖tags(0.5)
# 注意：使用列表而非字典，确保初始化顺序固定，前端展示也会按此顺序
DEFAULT_ENTITY_TYPES = [
    # ========== 基本维度（权重=1.0，事件的基础要素） ==========
    EntityType(
        id="10000000-0000-0000-0000-000000000001",  # default_time
        scope="global",
        source_config_id=None,
        article_id=None,
        type="time",
        name="时间",
        is_default=True,
        description="<When> 时间节点或时间范围，事件发生的时间",
        weight=1.0,  # 基本维度权重=1
        similarity_threshold=0.900,  # 时间需要精确匹配
    ),
    EntityType(
        id="10000000-0000-0000-0000-000000000002",  # default_location
        scope="global",
        source_config_id=None,
        article_id=None,
        type="location",
        name="地点",
        is_default=True,
        description="<Where> 地点位置，事件发生的地点",
        weight=1.0,  # 基本维度权重=1
        similarity_threshold=0.750,  # 地点表达方式多样，相对宽松
    ),
    EntityType(
        id="10000000-0000-0000-0000-000000000003",  # default_person
        scope="global",
        source_config_id=None,
        article_id=None,
        type="person",
        name="人员",
        is_default=True,
        description="<Who> 人物角色，事件涉及的人员",
        weight=1.0,  # 基本维度权重=1
        similarity_threshold=0.950,  # 人名需要较高准确度
    ),
    
    # ========== 重要维度（权重>1.0，事件的核心要素） ==========
    EntityType(
        id="10000000-0000-0000-0000-000000000004",  # default_action
        scope="global",
        source_config_id=None,
        article_id=None,
        type="action",
        name="行为",
        is_default=True,
        description="<How> 行为动作，事件的核心行为或操作",
        weight=1.5,  # 重要维度，权重>1
        similarity_threshold=0.800,  # 行为需要较高准确度
    ),
    EntityType(
        id="10000000-0000-0000-0000-000000000005",  # default_topic
        scope="global",
        source_config_id=None,
        article_id=None,
        type="topic",
        name="话题",
        is_default=True,
        description="<What> 核心话题，事件讨论的核心主题",
        weight=1.8,  # 重要维度，权重最高（话题是事件的核心）
        similarity_threshold=0.600,  # 话题相对宽松，允许相关话题建立关联
    ),
    
    # ========== 兜底维度（权重<1.0，所有未定义的实体都放这里） ==========
    EntityType(
        id="10000000-0000-0000-0000-000000000006",  # default_tags
        scope="global",
        source_config_id=None,
        article_id=None,
        type="tags",
        name="标签",
        is_default=True,
        description="<Tag> 分类标签，所有未在具体维度中定义的实体都归类到这里（如物品、概念等）",
        weight=0.5,  # 兜底维度，权重最低，会被用户自定义维度（权重≥1）覆盖
        similarity_threshold=0.700,  # 标签最宽松，允许相关标签建立关联
    ),
]
