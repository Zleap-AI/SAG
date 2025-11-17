"""
搜索模块配置

简洁清晰的三阶段配置：
1. RecallConfig - 实体召回
2. ExpandConfig - 实体扩展  
3. RerankConfig - 事项重排
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from sag.models.base import SAGBaseModel


class RerankStrategy(str, Enum):
    """
    重排策略

    - PAGERANK: 基于图算法的PageRank排序
    - RRF: 基于倒数排名融合的排序
    """
    PAGERANK = "pagerank"
    RRF = "rrf"

    def __str__(self) -> str:
        return self.value


class ReturnType(str, Enum):
    """
    返回类型

    - EVENT: 事项（默认）
    - PARAGRAPH: 段落
    """
    EVENT = "event"
    PARAGRAPH = "paragraph"

    def __str__(self) -> str:
        return self.value


class RecallConfig(SAGBaseModel):
    """
    实体召回配置

    从query召回相关实体（entities/keys）
    """

    # 模式开关
    use_fast_mode: bool = Field(
        default=True,
        description="快速模式（跳过LLM属性抽取，直接用query向量召回实体）"
    )

    # 向量检索
    vector_top_k: int = Field(
        default=15,
        ge=1, le=100,
        description="向量检索返回数量"
    )

    vector_candidates: int = Field(
        default=20,
        ge=10, le=500,
        description="向量检索候选池大小"
    )

    # 相似度阈值
    entity_similarity_threshold: float = Field(
        default=0.4,
        ge=0.0, le=1.0,
        description="实体相似度阈值"
    )

    event_similarity_threshold: float = Field(
        default=0.4,
        ge=0.0, le=1.0,
        description="事项相似度阈值"
    )

    # 数量控制
    max_entities: int = Field(
        default=25,
        ge=1, le=200,
        description="最大实体数量"
    )

    max_events: int = Field(
        default=60,
        ge=1, le=500,
        description="最大事项数量"
    )

    # 权重过滤
    entity_weight_threshold: float = Field(
        default=0.05,
        ge=0.0, le=1.0,
        description="实体权重阈值"
    )

    final_entity_count: int = Field(
        default=15,
        ge=1, le=100,
        description="最终返回实体数量"
    )


class ExpandConfig(SAGBaseModel):
    """
    实体扩展配置

    通过多跳关系扩展更多相关实体
    """

    # 开关
    enabled: bool = Field(
        default=True,
        description="是否启用扩展"
    )

    # 跳数控制
    max_hops: int = Field(
        default=3,
        ge=1, le=10,
        description="最大跳数"
    )

    entities_per_hop: int = Field(
        default=10,
        ge=1, le=50,
        description="每跳新增实体数"
    )

    # 收敛控制
    weight_change_threshold: float = Field(
        default=0.1,
        ge=0.0, le=1.0,
        description="权重变化阈值（收敛判断）"
    )

    # 事项过滤
    event_similarity_threshold: float = Field(
        default=0.3,
        ge=0.0, le=1.0,
        description="事项相似度阈值"
    )

    min_events_per_hop: int = Field(
        default=5,
        ge=1, le=100,
        description="每跳最少事项数"
    )

    max_events_per_hop: int = Field(
        default=100,
        ge=1, le=1000,
        description="每跳最多事项数"
    )


class RerankConfig(SAGBaseModel):
    """
    事项重排配置

    基于实体列表排序最终事项结果
    """

    # 排序策略
    strategy: RerankStrategy = Field(
        default=RerankStrategy.RRF,
        description="排序策略"
    )

    # 结果控制
    score_threshold: float = Field(
        default=0.5,
        ge=0.0, le=1.0,
        description="分数阈值"
    )

    max_results: int = Field(
        default=10,
        ge=1, le=100,
        description="最大返回数量"
    )

    # 召回数量控制（分阶段）
    max_key_recall_results: int = Field(
        default=30,
        ge=5, le=200,
        description="Step1 Key召回的最大事项/段落数（按相似度排序截断）"
    )

    max_query_recall_results: int = Field(
        default=30,
        ge=5, le=200,
        description="Step2 Query召回的最大事项/段落数（按相似度排序截断）"
    )


    pagerank_damping_factor: float = Field(
        default=0.85,
        ge=0.0, le=1.0,
        description="阻尼系数"
    )

    pagerank_max_iterations: int = Field(
        default=100,
        ge=1, le=1000,
        description="最大迭代次数"
    )

    # RRF参数
    rrf_k: int = Field(
        default=60,
        ge=1, le=100,
        description="RRF融合参数K"
    )



class SearchBaseConfig(SAGBaseModel):
    """
    搜索基础配置

    用于引擎层统一配置，包含基础参数 + 算法配置
    """

    # 基础参数（引擎需要）
    query: str = Field(..., description="搜索查询")
    original_query: str = Field(default="", description="原始查询")

    # 功能开关
    enable_query_rewrite: bool = Field(
        default=True,
        description="启用query重写（将口语化表述整理为更适合查询的问题）"
    )

    # 返回类型控制
    return_type: ReturnType = Field(
        default=ReturnType.EVENT,
        description="返回类型：事项(event) 或 段落(paragraph)，默认是事项"
    )

    # 三阶段配置
    recall: RecallConfig = Field(
        default_factory=RecallConfig, description="召回配置")
    expand: ExpandConfig = Field(
        default_factory=ExpandConfig, description="扩展配置")
    rerank: RerankConfig = Field(
        default_factory=RerankConfig, description="重排配置")


class SearchConfig(SearchBaseConfig):
    """
    搜索完整配置（基础配置 + 运行时上下文）

    继承SearchBaseConfig，添加运行时必需的上下文信息

    示例：
        # 单源搜索（向后兼容）
        config = SearchConfig(
            query="人工智能",
            source_config_id="source_123",
            recall=RecallConfig(max_entities=30),
            expand=ExpandConfig(max_hops=3),
            rerank=RerankConfig(strategy=RerankStrategy.PAGERANK)
        )

        # 多源搜索（新增功能）
        config = SearchConfig(
            query="人工智能",
            source_config_ids=["source_001", "source_002", "source_003"],
            recall=RecallConfig(max_entities=30),
            expand=ExpandConfig(max_hops=3),
            rerank=RerankConfig(strategy=RerankStrategy.PAGERANK)
        )
    """

    # === 运行时上下文 ===
    source_config_id: Optional[str] = Field(None, description="数据源ID（单个，向后兼容）")
    source_config_ids: Optional[List[str]] = Field(
        None, description="数据源ID列表（支持多源搜索）")
    article_id: Optional[str] = Field(None, description="文章ID")
    background: Optional[str] = Field(None, description="背景信息")

    def model_post_init(self, __context):
        """初始化后验证和处理 source_config_id/source_config_ids"""
        # 验证：至少提供一个
        if not self.source_config_id and not self.source_config_ids:
            raise ValueError("必须提供 source_config_id 或 source_config_ids 参数")

        # 统一处理：如果只提供 source_config_id，转换为 source_config_ids
        if self.source_config_id and not self.source_config_ids:
            self.source_config_ids = [self.source_config_id]
        elif self.source_config_ids and not self.source_config_id:
            # 多源场景，source_config_id 设为第一个（向后兼容）
            self.source_config_id = self.source_config_ids[0]

    def get_source_config_ids(self) -> List[str]:
        """
        获取统一的 source_config_ids 列表

        Returns:
            source_config_ids 列表（至少包含一个元素）
        """
        return self.source_config_ids or []

    def is_multi_source(self) -> bool:
        """
        判断是否为多源搜索

        Returns:
            True 表示多源搜索，False 表示单源搜索
        """
        return len(self.get_source_config_ids()) > 1

    # 运行时缓存
    query_embedding: Optional[List[float]] = Field(None, description="查询向量缓存")
    has_query_embedding: bool = Field(False, description="是否已生成查询向量")

    # Query召回的实体缓存
    query_recalled_keys: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Query召回的所有实体（用于构建线索）"
    )

    # === 线索追踪 (统一管理) ===
    all_clues: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="所有线索（统一追踪，支持知识图谱构建）"
    )

    # 旧版线索字段（兼容性保留，逐步废弃）
    recall_clues: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="召回线索（已废弃，使用all_clues）"
    )

    expansion_clues: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="扩展线索（已废弃，使用all_clues）"
    )

    rerank_clues: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="重排线索（已废弃，使用all_clues）"
    )

    # 节点缓存
    entity_node_cache: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="实体节点缓存"
    )


__all__ = [
    # 配置
    "SearchConfig",
    "SearchBaseConfig",
    "RecallConfig",
    "ExpandConfig",
    "RerankConfig",
    "RerankStrategy",
    "ReturnType",
]
