"""
Extract模块配置类

定义事项提取的配置选项
"""

from typing import List, Optional

from pydantic import Field

from sag.models.base import SAGBaseModel
from sag.models.entity import CustomEntityType


class ExtractBaseConfig(SAGBaseModel):
    """
    提取配置基类 - 基础配置
    
    包含提取行为的基础参数，可在Engine中预设
    """

    # === 并发控制 ===
    max_concurrency: int = Field(
        default=10,
        ge=1,
        le=100,
        description="最大并发数（Agent并发处理chunk数量）"
    )

    # === 实体配置 ===
    custom_entity_types: List[CustomEntityType] = Field(
        default_factory=list,
        description="自定义实体类型列表（运行时优先级最高）"
    )

    # === 存储选项 ===
    auto_vector: bool = Field(default=True, description="是否同步到Elasticsearch")



class ExtractConfig(ExtractBaseConfig):
    """
    事项提取配置 - 完整配置（基础+运行时上下文）
    
    运行时上下文可由以下方式提供：
    1. 直接传入chunk_ids（单独调用）
    2. Engine处理后设置chunk_ids（链式调用）
    3. Engine从上下文自动读取chunk_ids（自动模式）
    """

    # === 运行时上下文 ===
    source_config_id: str = Field(..., description="信息源ID")
    chunk_ids: List[str] = Field(..., min_length=1, description="Chunk ID列表")
    # === 提示词增强 ===
    background: Optional[str] = Field(
        default=None,
        description="背景信息（补充Agent上下文）"
    )
