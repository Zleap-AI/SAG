"""
Load模块配置类和返回结果

定义文档加载的配置选项和返回格式
"""

from pathlib import Path
from typing import Dict, List, Optional, Union

from pydantic import Field, model_validator

from sag.models.base import SAGBaseModel


# ============ 返回结果模型 ============

class LoadResult(SAGBaseModel):
    """
    加载结果（统一返回格式）
    
    用于衔接Load → Extract流程
    """
    
    # === 核心数据 ===
    source_id: str = Field(..., description="源ID（article_id或conversation_id）")
    source_type: str = Field(..., description="源类型（ARTICLE/CHAT）")
    chunk_ids: List[str] = Field(..., description="生成的Chunk ID列表")
    
    # === 元数据 ===
    source_config_id: str = Field(..., description="信息源配置ID")
    title: Optional[str] = Field(default=None, description="标题")
    chunk_count: int = Field(..., description="Chunk数量")
    
    # === 扩展数据 ===
    extra: Dict = Field(default_factory=dict, description="额外信息")


# ============ 配置模型 ============


class LoadBaseConfig(SAGBaseModel):
    """
    加载配置基类 - 基础配置
    
    包含所有数据源通用的配置参数
    """

    # === 通用配置 ===
    max_tokens: int = Field(
        default=8000, 
        ge=100,
        le=100000, 
        description="每个chunk的最大token数"
    )

    # === 存储配置 ===
    auto_vector: bool = Field(
        default=True, 
        description="是否自动索引到Elasticsearch"
    )

    # === 提示词增强 ===
    background: Optional[str] = Field(
        default=None, 
        description="背景信息（补充元数据生成上下文）"
    )

    # === 信息源 ===
    source_config_id: Optional[str] = Field(default=None, description="信息源ID")


class DocumentLoadConfig(LoadBaseConfig):
    """文档加载配置 - 完整配置（基础+运行时上下文）"""


    # === 数据来源（path或article_id二选一）===
    path: Optional[Union[str, Path]] = Field(
        default=None, 
        description="文件路径"
    )
    article_id: Optional[str] = Field(
        default=None, 
        description="文章ID（用于更新已存在的文章）"
    )

    # === 加载行为 ===
    load_from_database: bool = Field(
        default=False, 
        description="是否从数据库加载文章（需要article_id）"
    )

    # === 文档处理配置 ===
    min_content_length: int = Field(
        default=100, 
        ge=10,
        description="最小内容长度（字符数）"
    )
    merge_short_sections: bool = Field(
        default=True, 
        description="是否启用短片段合并"
    )

    @model_validator(mode='after')
    def check_path_or_article_id(self) -> 'DocumentLoadConfig':
        """验证：path 和 article_id 至少有一个有值"""
        if not self.path and not self.article_id:
            raise ValueError("必须提供 path 或 article_id 参数之一")
        return self


class ConversationLoadConfig(LoadBaseConfig):
    """会话加载配置 - 完整配置（基础+运行时上下文）"""

    # === 运行时上下文 ===
    conversation_id: str = Field(..., description="会话ID")

    # === 时间范围参数（必需）===
    start_time: str = Field(
        ..., 
        description="开始时间（ISO格式：YYYY-MM-DD HH:MM:SS）"
    )
    end_time: str = Field(
        ..., 
        description="结束时间（ISO格式：YYYY-MM-DD HH:MM:SS）"
    )
    interval_minutes: int = Field(
        default=60, 
        ge=1, 
        description="时间窗口间隔（分钟）"
    )

    # === 会话处理参数 ===
    min_content_length: int = Field(
        default=100, 
        ge=10,
        description="最小内容长度（字符数）"
    )
