"""
搜索 Rerank 模块

实现6步骤的查找最重要原文块的方法：
1. key找content：根据[key-final]从sql中提取原文块[content-key-related]，从ES获取预存向量并计算和query的余弦相似度（记录event_id）
2. query找content：通过向量相似度（KNN+余弦相似度）在向量数据库找到原文块[content-query-related]（event_ids为空）
3. content合并+去重：合并[content-key-related]和[content-query-related]，如果同一原文块（source_id+chunk_id相同）同时出现在SQL和Embedding结果中，只保留SQL的结果
4. 制作[content-related]权重向量：使用公式 weight = 0.5*相似度 + ln(1 + Σ(key权重 × ln(1+出现次数) / step))
5. PageRank重排序：根据权重对原文块排序（从大到小）
6. 取Top-N并返回：从Top-N原文块中提取关联的事项列表

返回格式：
Dict[str, Any]: 包含以下字段的字典：
    - events (List[SourceEvent]): 事项对象列表（按原文块 PageRank 顺序排列，已去重）
    - clues (Dict): 召回线索信息
        - origin_query (str): 原始查询（重写前）
        - final_query (str): LLM重写后的查询（重写后）
        - query_entities (List[Dict]): 查询召回的实体列表（key_id改为id）
        - recall_entities (List[Dict]): 召回的实体列表（key_id改为id，过滤掉query_entities中的值）

注意：step1 和 step2 都使用 ES 预存的 content_vector（生成时使用"标题 + 内容[:500]"），确保向量一致性

"""

from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
import numpy as np
import math
import re
import time
import asyncio
from collections import Counter, defaultdict


from sqlalchemy import select, and_, or_
from sqlalchemy.orm import selectinload

from sag.core.storage.elasticsearch import get_es_client
from sag.core.storage.repositories.source_chunk_repository import SourceChunkRepository
from sag.core.storage.repositories.event_repository import EventVectorRepository
from sag.db import SourceEvent, Entity, EventEntity, ArticleSection, Article, SourceChunk, get_session_factory
from sag.exceptions import AIError
from sag.modules.load.processor import DocumentProcessor
from sag.modules.search.config import SearchConfig
from sag.modules.search.tracker import Tracker  # 🆕 添加线索追踪器
from sag.utils import get_logger

logger = get_logger("search.rerank.pagerank")


@dataclass
class ContentSearchResult:
    """
    搜索结果的统一返回格式（SourceChunk架构）

    用于表示从SQL数据库或Embedding向量数据库搜索到的内容
    """
    # 必需字段
    search_type: str      # "sql", "embedding" 或带编号的格式如 "SQL-1", "embedding-2"
    source_config_id: str # 数据源配置ID (UUID)
    source_id: str        # 文章ID (Article.id 或 SourceChunk.source_id)
    chunk_id: str         # 原文块ID (SourceChunk.id)
    rank: int             # 原文块在文章中的排序
    heading: str          # 原文块标题
    content: str          # 原文块内容
    score: float = 0.0    # 相关性得分
    weight: float = 0.0   # 权重值（step4计算后赋值）
    event_ids: List[str] = None  # 关联的事件ID列表
    event: str = ""  # 聚合后的事项摘要（多个summary合并）
    clues: List[Dict[str, Any]] = None  # 召回该段落的线索列表（来自 key_final 或 query）

    def __post_init__(self):
        """初始化后验证"""
        # 初始化 event_ids 为空列表
        if self.event_ids is None:
            self.event_ids = []

        # 初始化 clues 为空列表
        if self.clues is None:
            self.clues = []

        # 允许 "sql", "embedding" 或带编号的格式如 "SQL-1", "embedding-2"
        valid_types = ["sql", "embedding"]
        is_valid = (
            self.search_type in valid_types or
            self.search_type.startswith("SQL-") or
            self.search_type.startswith("embedding-")
        )

        if not is_valid:
            raise ValueError(
                f"search_type 必须是 'sql', 'embedding' 或带编号格式(如 'SQL-1', 'embedding-1')，"
                f"当前值: {self.search_type}"
            )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "search_type": self.search_type,
            "source_config_id": self.source_config_id,
            "source_id": self.source_id,
            "chunk_id": self.chunk_id,
            "rank": self.rank,
            "heading": self.heading,
            "content": self.content,
            "score": self.score,
            "weight": self.weight,
            "event_ids": self.event_ids,
            "event": self.event,
            "clues": self.clues,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ContentSearchResult":
        """从字典创建实例"""
        return cls(
            search_type=data.get("search_type", "sql"),
            source_config_id=data["source_config_id"],
            source_id=data["source_id"],
            chunk_id=data["chunk_id"],
            rank=data.get("rank", 0),
            heading=data.get("heading", ""),
            content=data.get("content", ""),
            score=data.get("score", 0.0),
            weight=data.get("weight", 0.0),
            event_ids=data.get("event_ids", []),
            event=data.get("event", ""),
            clues=data.get("clues", []),
        )

    def __repr__(self) -> str:
        """字符串表示"""
        return (
            f"ContentSearchResult(type={self.search_type}, "
            f"chunk_id={self.chunk_id}, "
            f"heading='{self.heading[:30]}...', "
            f"score={self.score:.3f})"
        )


class RerankPageRankSearcher:
    """Rerank段落搜索器 - 实现6步骤的查找最重要段落的方法"""

    def __init__(
        self,
        llm_client=None
    ):
        """
        初始化Rerank段落搜索器

        Args:
            llm_client: LLM客户端（可选）
        """

        self.session_factory = get_session_factory()
        self.logger = get_logger("search.rerank.pagerank")

        # 初始化Elasticsearch仓库
        self.es_client = get_es_client()
        self.content_repo = SourceChunkRepository(self.es_client)
        self.event_repo = EventVectorRepository(self.es_client)  # 添加事项向量仓库

        # 初始化文档处理器用于生成embeding向量
        self.processor = DocumentProcessor(llm_client=llm_client)

        self.logger.info(
            "Rerank段落搜索器初始化完成",
            extra={
                "embedding_model_name": self.processor.embedding_model_name,
            },
        )

    async def search(
        self,
        key_final: List[Dict[str, Any]],
        config: SearchConfig
    ) -> Dict[str, Any]:
        """
        Rerank 搜索主方法

        整合步骤1-6，统一进行query向量化，避免重复计算

        步骤流程：
          1. key找content (SQL) - 记录event_id
          2. query找content (KNN + 余弦相似度) - event_ids为空
          3. 合并结果+去重（优先保留SQL结果）
          4. 计算权重向量
          5. PageRank排序
          6. 取Top-N段落并提取关联的事项列表

        Args:
            key_final: 从Recall返回的关键实体列表
            config: Rerank搜索配置

        Returns:
            Dict[str, Any]: 包含以下字段的字典：
                - events (List[SourceEvent]): 事项对象列表（按段落 PageRank 顺序排列，已去重）
                - clues (Dict): 召回线索信息
                    - origin_query (str): 原始查询（重写前）
                    - final_query (str): LLM重写后的查询（重写后）
                    - query_entities (List[Dict]): 查询召回的实体列表（key_id改为id）
                    - recall_entities (List[Dict]): 召回的实体列表（key_id改为id，过滤掉query_entities中的值）
        """
        try:
            # 记录总体开始时间
            overall_start = time.perf_counter()

            self.logger.info(
                f"Rerank搜索开始: query='{config.query}', source_config_ids={config.get_source_config_ids()}")

            # 统一进行query向量化（避免在step1和step2中重复计算）
            vector_start = time.perf_counter()
            query_vector = await self._generate_query_vector(config.query, config)
            vector_time = time.perf_counter() - vector_start
            if config.has_query_embedding:
                self.logger.info(
                    f"使用缓存的query向量，维度: {len(query_vector)}, 耗时: {vector_time:.3f}秒")
            else:
                self.logger.info(
                    f"查询向量生成成功，维度: {len(query_vector)}, 耗时: {vector_time:.3f}秒")

            # 用于记录各步骤耗时
            step_times = {}

            self.logger.info("=" * 80)
            self.logger.info("【Rerank 搜索】耗时统计")
            self.logger.info("=" * 80)

            # 段落模式：执行完整的step1-step6流程
            # 步骤1和2可以并行执行（互不依赖）
            self.logger.info("步骤1和2并行开始...")
            parallel_start = time.perf_counter()

            # 并行执行 step1 和 step2
            step1_task = self._step1_keys_to_contents(
                key_final=key_final,
                query=config.query,
                source_config_ids=config.get_source_config_ids(),
                query_vector=query_vector,
                config=config  # 传入config用于缓存
            )

            step2_task = self._step2_query_to_contents(
                query=config.query,
                source_config_ids=config.get_source_config_ids(),
                k=config.rerank.max_query_recall_results,  # 🆕 使用 max_query_recall_results
                query_vector=query_vector,
                config=config  # 传入config用于缓存和阈值过滤
            )

            # 等待两个任务都完成
            step1_results, step2_results = await asyncio.gather(step1_task, step2_task)

            parallel_time = time.perf_counter() - parallel_start
            step_times['step1_2并行执行'] = parallel_time

            self.logger.info(
                f"步骤1和2并行完成: "
                f"step1找到 {len(step1_results)} 个段落, "
                f"step2找到 {len(step2_results)} 个段落, "
                f"总耗时: {parallel_time:.3f}秒"
            )

            # 步骤3: 合并结果（不去重，直接合并）
            step3_start = time.perf_counter()
            merged_results = await self._step3_merge_result(step1_results, step2_results, config)
            step3_time = time.perf_counter() - step3_start
            step_times['step3_合并去重'] = step3_time
            self.logger.info(
                f"步骤3完成，合并后总共 {len(merged_results)} 个段落, 耗时: {step3_time:.3f}秒")

            # 步骤4: 计算段落权重
            step4_start = time.perf_counter()
            weighted_results = await self._step4_calculate_weight_of_contents(
                key_final=key_final,
                content_related=merged_results
            )
            step4_time = time.perf_counter() - step4_start
            step_times['step4_权重计算'] = step4_time
            self.logger.info(
                f"步骤4完成，计算了 {len(weighted_results)} 个段落的权重, 耗时: {step4_time:.3f}秒")

            # 步骤5: PageRank重排序（使用PageRank算法）
            step5_start = time.perf_counter()
            sorted_results = await self._step5_pageRank_of_contents(
                content_related=weighted_results,
                key_final=key_final  # 传入key_final用于实体关联
            )
            step5_time = time.perf_counter() - step5_start
            step_times['step5_PageRank排序'] = step5_time
            self.logger.info(
                f"步骤5完成，排序了 {len(sorted_results)} 个段落, 耗时: {step5_time:.3f}秒")

            # 步骤6: 取Top-N段落
            step6_start = time.perf_counter()
            final_sections = await self._step6_get_topn_sections(
                sorted_contents=sorted_results,
                top_k=config.rerank.max_results,
                config=config
            )
            step6_time = time.perf_counter() - step6_start
            step_times['Step6_Top-N筛选'] = step6_time
            self.logger.info(
                f"✓ Step6 完成: 最终返回 {len(final_sections)} 个段落, 耗时: {step6_time:.3f}秒"
            )

            # 计算总耗时
            overall_time = time.perf_counter() - overall_start

            # 输出耗时统计汇总
            self.logger.info("\n" + "=" * 80)
            self.logger.info("【段落级 PageRank】各步骤耗时汇总:")
            self.logger.info("-" * 80)
            self.logger.info(
                f"查询向量生成: {vector_time:.3f}秒 ({vector_time/overall_time*100:.1f}%)")
            for step_name, step_time in step_times.items():
                self.logger.info(
                    f"{step_name}: {step_time:.3f}秒 ({step_time/overall_time*100:.1f}%)")
            self.logger.info("-" * 80)
            self.logger.info(f"✓ 总耗时: {overall_time:.3f}秒")
            self.logger.info("=" * 80)

            # 直接返回段落列表（不再转换为事项）
            return {"sections": final_sections}

        except Exception as e:
            self.logger.error(f"[段落级 PageRank] 搜索失败: {e}", exc_info=True)
            return {"sections": []}  # 失败时返回空字典

    async def _step1_keys_to_contents(
        self,
        key_final: List[Dict[str, Any]],
        query: str,
        source_config_ids: List[str],
        query_vector: Optional[List[float]] = None,  # 可选的查询向量
        config: Optional[SearchConfig] = None  # 添加config参数用于缓存
    ) -> List[Dict[str, Any]]:
        """
        步骤1: key找content
        根据[key-final]从sql中提取原文块[content-key-related]，并计算和query的余弦相似度作为得分

        1. 实体匹配：根据传入的 key_final 中的实体名称和类型，在 Entity 表中查找匹配的实体
        2. 事件关联：通过 EventEntity 表找到与这些实体相关的事件
        3. 段落查找：通过 SourceEvent 和 Article 表的关联，找到对应的文章
        4. 段落过滤：检查事件的 references 字段，只返回真正被事件引用的段落
        5. 向量获取：从 ES 批量获取段落的预存向量（content_vector = 标题 + 内容[:500]）
        6. 相似度计算：计算每个段落向量与 query 的余弦相似度作为最终得分

        Args:
            key_final: 从Recall返回的key_final数据
            query: 查询文本
            source_id: 数据源ID
            k: 返回结果数量
            query_vector: 可选的查询向量，如果为None则自动生成

        Returns:
            相关段落列表（ContentSearchResult.to_dict()格式），按余弦相似度降序排序
        """
        try:
            self.logger.info(
                f"步骤1开始: 处理 {len(key_final)} 个key, query='{query}'")

            if not key_final:
                return []

            # 1. 生成查询向量（如果没有传入）
            if query_vector is None:
                query_vector = await self._generate_query_vector(query, config)
                if config and config.has_query_embedding:
                    self.logger.debug(f"使用缓存的query向量，维度: {len(query_vector)}")
                else:
                    self.logger.debug(f"查询向量生成成功，维度: {len(query_vector)}")
            else:
                self.logger.debug(f"使用传入的查询向量，维度: {len(query_vector)}")

            # 2. 提取实体ID和权重
            # key_id 就是实体的 ID，无需再查询 Entity 表
            entity_ids = [key.get("key_id") or key.get("id")
                          for key in key_final]
            entity_weight_map = {
                key.get("key_id") or key.get("id"): key["weight"]
                for key in key_final
            }

            # 过滤掉可能为 None 的 ID
            entity_ids = [eid for eid in entity_ids if eid]

            if not entity_ids:
                self.logger.warning("key_final 中没有有效的实体ID")
                return []

            self.logger.info(
                f"从 {len(key_final)} 个key中提取到 {len(entity_ids)} 个实体ID")

            async with self.session_factory() as session:
                # 🆕 优化：直接通过实体ID查询 Entity 表（仅用于获取实体详情）
                entity_query = (
                    select(Entity)
                    .where(
                        and_(
                            Entity.source_config_id.in_(source_config_ids),
                            Entity.id.in_(entity_ids)
                        )
                    )
                )

                entity_result = await session.execute(entity_query)
                found_entities = entity_result.scalars().all()

                if not found_entities:
                    self.logger.warning("未找到匹配的实体")
                    return []

                self.logger.info(f"找到 {len(found_entities)} 个匹配实体")

                # 🆕 日志：显示每个 key 召回的实体（按 key_id 一对一显示）
                self.logger.info("=" * 80)
                self.logger.info("【Step1 召回路径】Key → Entity 映射 (一对一):")
                self.logger.info("-" * 80)

                for key in key_final:
                    key_id = key.get("key_id") or key.get("id")
                    key_display = f"{key['name']}({key['type']})"

                    # 找到对应的 entity
                    entity = next(
                        (e for e in found_entities if e.id == key_id), None)
                    if entity:
                        entity_display = f"{entity.name}({entity.type})"
                        self.logger.info(
                            f"  Key '{key_display}' [id={key_id[:8]}...] → Entity '{entity_display}'")
                    else:
                        self.logger.warning(
                            f"  Key '{key_display}' [id={key_id[:8]}...] → ❌ 未找到对应实体")

                self.logger.info("-" * 80)
                self.logger.info(
                    f"  总计: {len(key_final)} 个Key → {len(found_entities)} 个Entity")
                self.logger.info("=" * 80)

                # 调试：显示每个匹配的实体
                for entity in found_entities:
                    self.logger.debug(
                        f"  实体: {entity.name} (type={entity.type}, id={entity.id[:8]}...)"
                    )

                # 4. 通过EventEntity查找相关事件（限制在指定source_config_ids内）
                event_entity_query = (
                    select(EventEntity.event_id,
                           EventEntity.entity_id, EventEntity.weight)
                    .join(SourceEvent, EventEntity.event_id == SourceEvent.id)
                    .where(
                        and_(
                            SourceEvent.source_config_id.in_(source_config_ids),
                            EventEntity.entity_id.in_(entity_ids)
                        )
                    )
                    .distinct()
                )

                event_result = await session.execute(event_entity_query)
                event_entities = event_result.fetchall()

                if not event_entities:
                    self.logger.warning("未找到相关事件")
                    return []

                # 5. 计算每个事件的权重，并记录事件到实体的映射
                event_weights = {}
                event_to_entities = {}  # 事件ID -> 实体ID列表的映射

                # 创建 entity_id -> key 对象的映射（用于后续构建 clues）
                entity_to_key = {}
                for key in key_final:
                    key_id = key.get("key_id") or key.get("id")
                    if key_id:
                        # 创建 key 的副本，将 key_id 重命名为 id
                        key_copy = key.copy()
                        if "key_id" in key_copy:
                            key_copy["id"] = key_copy.pop("key_id")
                        elif "id" not in key_copy:
                            key_copy["id"] = key_id
                        entity_to_key[key_id] = key_copy

                for event_entity in event_entities:
                    event_id = event_entity.event_id
                    entity_id = event_entity.entity_id
                    event_entity_weight = event_entity.weight or 1.0
                    entity_weight = entity_weight_map.get(
                        entity_id, 1.0)  # 🆕 直接用 entity_id 查找

                    # 综合权重 = 实体权重 × 关联权重
                    combined_weight = float(
                        entity_weight) * float(event_entity_weight)
                    event_weights[event_id] = event_weights.get(
                        event_id, 0) + combined_weight

                    # 记录事件到实体的映射
                    if event_id not in event_to_entities:
                        event_to_entities[event_id] = []
                    event_to_entities[event_id].append(entity_id)

                event_ids = list(event_weights.keys())
                self.logger.info(f"找到 {len(event_ids)} 个相关事件")

                # 🆕 日志：显示 Entity → Event 映射（通过key关联）
                self.logger.info("=" * 80)
                self.logger.info("【Step1 召回路径】Entity → Event 映射:")
                self.logger.info("-" * 80)

                # 构建 entity_id → entity_name 映射
                entity_id_to_name = {
                    e.id: f"{e.name}({e.type})" for e in found_entities}

                # 按 entity 分组显示关联的 events
                for entity_id, entity_name in entity_id_to_name.items():
                    related_events = [
                        event_id for event_id, entity_ids in event_to_entities.items()
                        if entity_id in entity_ids
                    ]
                    if related_events:
                        self.logger.info(
                            f"  Entity '{entity_name}' → {len(related_events)} 个事项: "
                            f"{', '.join([eid[:8]+'...' for eid in related_events[:5]])}"
                            f"{' ...' if len(related_events) > 5 else ''}"
                        )

                self.logger.info("=" * 80)

                # 调试：显示每个事件的详细信息
                for event_id in event_ids:
                    self.logger.debug(
                        f"  事件 {event_id[:8]}... 权重={event_weights[event_id]:.3f}"
                    )

                # 6. 通过事件的 chunk_id 查找对应的原文块（SourceChunk）
                # 🆕 新架构：SourceEvent.chunk_id → SourceChunk（一对一关系）

                # 首先获取所有事件的详细信息（包括 chunk_id 字段）
                event_detail_query = (
                    select(SourceEvent)
                    .where(
                        and_(
                            SourceEvent.source_config_id.in_(source_config_ids),
                            SourceEvent.id.in_(event_ids)
                        )
                    )
                )
                event_detail_result = await session.execute(event_detail_query)
                events = event_detail_result.scalars().all()

                # 收集所有事件的 chunk_id
                chunk_ids = set()
                event_to_chunk = {}  # 事件ID -> chunk_id 的映射

                for event in events:
                    if event.chunk_id:
                        event_to_chunk[event.id] = event.chunk_id
                        chunk_ids.add(event.chunk_id)
                        self.logger.debug(
                            f"事件 {event.id[:8]}... 关联到 chunk {event.chunk_id[:8]}..."
                        )
                    else:
                        self.logger.warning(
                            f"事件 {event.id[:8]}... 没有 chunk_id 字段"
                        )

                if not chunk_ids:
                    self.logger.warning("所有事件都没有关联到原文块")
                    return []

                self.logger.info(
                    f"收集到 {len(chunk_ids)} 个原文块ID（来自 {len(events)} 个事件）")

                # 从 MySQL 查询 SourceChunk 的基本信息
                chunk_query = (
                    select(SourceChunk)
                    .where(
                        and_(
                            SourceChunk.source_config_id.in_(source_config_ids),
                            SourceChunk.id.in_(list(chunk_ids))
                        )
                    )
                    .order_by(SourceChunk.rank)
                )

                chunk_result = await session.execute(chunk_query)
                chunks = chunk_result.scalars().all()

                if not chunks:
                    self.logger.warning("未找到相关原文块")
                    return []

                self.logger.info(f"从 MySQL 找到 {len(chunks)} 个原文块")

                # 7. 构建原文块数据
                # 使用字典存储：chunk_id -> chunk data
                chunks_dict = {}  # key: chunk_id, value: chunk data

                # 反向映射：chunk_id -> [event_ids]
                chunk_to_events = {}
                for event_id, chunk_id in event_to_chunk.items():
                    if chunk_id not in chunk_to_events:
                        chunk_to_events[chunk_id] = []
                    chunk_to_events[chunk_id].append(event_id)

                self.logger.debug(f"构建了 {len(chunk_to_events)} 个原文块到事件的映射")

                # 🆕 日志：显示 Event → Chunk 映射
                self.logger.info("=" * 80)
                self.logger.info("【Step1 召回路径】Event → Chunk 映射:")
                self.logger.info("-" * 80)

                # 显示每个事项关联的原文块
                for event in events[:10]:  # 只显示前10个事项
                    if event.chunk_id:
                        self.logger.info(
                            f"  Event {event.id[:8]}... ('{event.title[:30]}') → Chunk {event.chunk_id[:8]}..."
                        )

                if len(events) > 10:
                    self.logger.info(f"  ... (还有 {len(events) - 10} 个事项未显示)")

                self.logger.info("=" * 80)

                # 遍历所有原文块，构建原文块数据
                for chunk in chunks:
                    chunk_id = chunk.id

                    # 找到引用该原文块的所有事件
                    related_event_ids = chunk_to_events.get(chunk_id, [])

                    if not related_event_ids:
                        self.logger.warning(
                            f"原文块 {chunk_id[:8]}... 没有找到关联的事件")
                        continue

                    # 计算该原文块的综合权重（所有关联事件的权重之和）
                    total_event_weight = sum(event_weights.get(
                        eid, 1.0) for eid in related_event_ids)

                    # 收集该原文块的 clues（从关联的事件中收集关联的实体）
                    chunk_clues = []
                    seen_entity_ids = set()
                    for event_id in related_event_ids:
                        # 获取该事件关联的所有实体
                        entity_ids = event_to_entities.get(event_id, [])
                        for entity_id in entity_ids:
                            if entity_id not in seen_entity_ids:
                                # 获取对应的 key 对象
                                key = entity_to_key.get(entity_id)
                                if key:
                                    chunk_clues.append(key)
                                    seen_entity_ids.add(entity_id)

                    # 创建原文块数据
                    chunk_data = {
                        "chunk_id": chunk_id,
                        "source_id": chunk.source_id,
                        "event_ids": related_event_ids,  # 所有关联的事件ID
                        "rank": chunk.rank,
                        "heading": chunk.heading,
                        "content": chunk.content,
                        "content_vector": None,  # 将在后面从 ES 获取
                        "entity_weight": total_event_weight,
                        "created_time": chunk.created_time,
                        "extra_data": chunk.extra_data or {},
                        "clues": chunk_clues,  # 召回该原文块的 key 列表
                        "references": chunk.references or []  # SourceChunk 引用的 ArticleSection ID列表
                    }
                    chunks_dict[chunk_id] = chunk_data

                    self.logger.debug(
                        f"原文块 {chunk_id[:8]}... 关联了 {len(related_event_ids)} 个事件，"
                        f"综合权重={total_event_weight:.3f}，clues数={len(chunk_clues)}"
                    )

                # 转换为列表
                chunks_data = list(chunks_dict.values())

                # 统计信息
                multi_event_chunks = [
                    c for c in chunks_data if len(c["event_ids"]) > 1]
                self.logger.info(
                    f"构建了 {len(chunks_data)} 个唯一原文块数据"
                )
                if multi_event_chunks:
                    self.logger.info(
                        f"其中 {len(multi_event_chunks)} 个原文块关联了多个事件"
                    )

                if not chunks_data:
                    self.logger.warning("过滤后没有找到真正关联的原文块")
                    return []

                # 🆕 日志：汇总显示完整召回路径统计
                self.logger.info("=" * 80)
                self.logger.info("【Step1 召回路径汇总】完整召回链:")
                self.logger.info("-" * 80)
                self.logger.info(f"  Key数量: {len(key_final)}")
                self.logger.info(
                    f"  → Entity数量: {len(found_entities)} (通过Key匹配)")
                self.logger.info(f"  → Event数量: {len(events)} (通过Entity关联)")
                self.logger.info(
                    f"  → Chunk数量: {len(chunks_data)} (通过Event.chunk_id)")
                self.logger.info("-" * 80)
                self.logger.info(f"  召回路径: Key → Entity → Event → Chunk")
                self.logger.info("=" * 80)

                # 7.5. 从 ES 批量获取原文块的预存向量
                chunk_ids_list = list(chunks_dict.keys())
                self.logger.info(
                    f"从 ES 批量获取 {len(chunk_ids_list)} 个原文块的预存向量...")

                es_chunks_data = await self.content_repo.get_chunks_by_ids(
                    chunk_ids=chunk_ids_list,
                    include_vectors=True
                )

                # 构建 chunk_id -> content_vector 的映射
                chunk_vector_map = {}
                for es_chunk in es_chunks_data:
                    chunk_id = es_chunk.get('chunk_id')
                    content_vector = es_chunk.get('content_vector')
                    if chunk_id and content_vector:
                        chunk_vector_map[chunk_id] = content_vector

                self.logger.info(
                    f"从 ES 获取到 {len(chunk_vector_map)} 个原文块的预存向量 "
                    f"(请求了 {len(chunk_ids_list)} 个)"
                )

                # 将获取到的向量填充到原文块数据中
                filled_count = 0
                for chunk in chunks_data:
                    chunk_id = chunk['chunk_id']
                    if chunk_id in chunk_vector_map:
                        chunk['content_vector'] = chunk_vector_map[chunk_id]
                        filled_count += 1
                    else:
                        self.logger.warning(
                            f"原文块 {chunk_id[:8]}... 在 ES 中未找到预存向量，将现场生成"
                        )

                self.logger.info(
                    f"成功填充 {filled_count}/{len(chunks_data)} 个原文块的预存向量"
                )

                # 8. 计算向量相似度得分
                similarity_scores = await self._calculate_cosine_scores(
                    query_vector=query_vector,
                    paragraphs=chunks_data  # 这里保持参数名为 paragraphs，因为方法内部使用这个名称
                )
                self.logger.debug(f"余弦相似度计算完成")

                # 9. 使用余弦相似度作为最终得分
                content_results = []

                # 添加详细得分日志（与步骤2对应）
                self.logger.info("=" * 80)
                self.logger.info("步骤1 得分详情（纯余弦相似度，使用ES预存向量）：")
                self.logger.info("-" * 80)

                for idx, chunk in enumerate(chunks_data, start=1):
                    chunk_id = chunk["chunk_id"]

                    # 直接使用余弦相似度作为得分（范围通常在[0, 1]之间）
                    cosine_score = similarity_scores.get(chunk_id, 0.0)

                    # 获取该原文块关联的所有事件ID
                    event_ids = chunk["event_ids"]

                    # 详细日志：显示每个原文块的得分和关联的事件数
                    heading_preview = chunk.get("heading", "")[:40]
                    self.logger.info(
                        f"原文块 {chunk_id[:8]}... | "
                        f"Cosine={cosine_score:.4f} | "
                        f"关联事件数={len(event_ids)} | "
                        f"标题: {heading_preview}"
                    )

                    # DEBUG级别：显示所有关联的事件ID
                    if len(event_ids) > 1:
                        event_ids_preview = [
                            eid[:8] + "..." for eid in event_ids]
                        self.logger.debug(f"  事件ID列表: {event_ids_preview}")

                    # 创建 ContentSearchResult 对象，添加编号和 clues
                    result = ContentSearchResult(
                        search_type=f"SQL-{idx}",
                        source_config_id=source_config_ids[0] if source_config_ids else "",
                        source_id=chunk["source_id"],  # SourceChunk.source_id (文章ID)
                        chunk_id=chunk_id,  # SourceChunk.id
                        rank=chunk["rank"],
                        heading=chunk["heading"],
                        content=chunk["content"],
                        score=cosine_score,
                        event_ids=event_ids,  # 记录所有关联的事件ID列表
                        clues=chunk.get("clues", []),  # 记录召回该原文块的 key 列表
                    )

                    content_results.append(result)

                self.logger.info("=" * 80)

                # 10. 按余弦相似度排序
                content_results.sort(key=lambda x: x.score, reverse=True)

                # 11. 使用 config.rerank.score_threshold 过滤低相似度结果
                original_count = len(content_results)
                if config and config.rerank.score_threshold:
                    filtered_results = [
                        r for r in content_results if r.score >= config.rerank.score_threshold]

                    if len(filtered_results) < original_count:
                        self.logger.info(
                            f"相似度过滤: {original_count} -> {len(filtered_results)} 个原文块 "
                            f"(阈值={config.rerank.score_threshold:.2f})"
                        )

                        # 展示过滤后保留的段落信息
                        if filtered_results:
                            self.logger.info("=" * 80)
                            self.logger.info(
                                f"过滤后保留的 {len(filtered_results)} 个段落：")
                            self.logger.info("-" * 80)
                            for result in filtered_results:
                                heading_preview = result.heading[:
                                                                 40] if result.heading else "无标题"
                                self.logger.info(
                                    f"段落 {result.chunk_id[:8]}... | "
                                    f"Cosine={result.score:.4f} | "
                                    f"标题: {heading_preview}"
                                )
                            self.logger.info("=" * 80)

                    content_results = filtered_results
                else:
                    self.logger.warning("未设置阈值或config为空，跳过相似度过滤")

                # 🆕 根据 max_key_recall_results 截断（在构建线索前，按相似度排序）
                max_key_results = config.rerank.max_key_recall_results if config else 30
                if len(content_results) > max_key_results:
                    self.logger.warning(
                        f"⚠️  [段落级Step1] Key召回段落数({len(content_results)})超过max_key_recall_results({max_key_results})，"
                        f"将按相似度排序后截断"
                    )

                    # 已经按相似度降序排序了（第820行），直接截断
                    truncated_results = content_results[:max_key_results]

                    self.logger.info(
                        f"📊 [段落级Step1] 截断统计: "
                        f"保留{len(truncated_results)}个, "
                        f"丢弃{len(content_results) - len(truncated_results)}个"
                    )

                    content_results = truncated_results

                # 🆕 日志：显示过滤后的有效映射关系
                if content_results:
                    # 收集过滤后的所有 chunk_ids 和 event_ids
                    filtered_chunk_ids = {
                        r.chunk_id for r in content_results}
                    filtered_event_ids = set()
                    for r in content_results:
                        filtered_event_ids.update(r.event_ids)

                    self.logger.info("=" * 80)
                    self.logger.info("【Step1 召回路径过滤后】有效映射关系:")
                    self.logger.info("-" * 80)

                    # 1. Entity → Event 映射（只显示有保留段落的事项）
                    self.logger.info("  1️⃣ Entity → Event (仅保留有效事项):")
                    entity_event_count = {}

                    # 🆕 创建 event_id → event_title 的映射
                    event_title_map = {
                        event.id: event.title for event in events}

                    for entity_id, entity_name in entity_id_to_name.items():
                        # 找到该实体关联的、且有保留段落的事项
                        valid_events = [
                            event_id for event_id, entity_ids in event_to_entities.items()
                            if entity_id in entity_ids and event_id in filtered_event_ids
                        ]
                        if valid_events:
                            entity_event_count[entity_name] = len(valid_events)

                            # 🆕 显示事项ID和标题
                            events_preview_parts = []
                            for eid in valid_events[:3]:
                                event_title = event_title_map.get(eid, "")
                                title_preview = event_title[:30] if event_title else "无标题"
                                events_preview_parts.append(
                                    f"{eid[:8]}...({title_preview})")

                            events_preview = ', '.join(events_preview_parts)
                            if len(valid_events) > 3:
                                events_preview += f' ... (共{len(valid_events)}个)'

                            self.logger.info(
                                f"     {entity_name} → {len(valid_events)} 个事项: {events_preview}")

                    # 2. Event → Chunk 映射（只显示保留的原文块）
                    self.logger.info("")
                    self.logger.info("  2️⃣ Event → Chunk (仅保留原文块):")
                    event_section_count = {}
                    displayed_count = 0

                    # 🆕 创建 chunk_id → heading 的映射
                    chunk_heading_map = {}
                    for content in content_results:
                        chunk_heading_map[content.chunk_id] = content.heading or ""

                    # 🆕 只遍历有效的事项（在 filtered_event_ids 中的）
                    for event in events:
                        if event.id in filtered_event_ids:
                            # 找到该事项关联的、且被保留的段落
                            valid_sections = [
                                sid for sid in (event.references or [])
                                if sid in filtered_chunk_ids
                            ]
                            if valid_sections:
                                event_section_count[event.id] = len(
                                    valid_sections)

                                # 🆕 显示段落ID和标题
                                sections_preview_parts = []
                                for sid in valid_sections[:3]:
                                    section_heading = chunk_heading_map.get(
                                        sid, "")
                                    heading_preview = section_heading[:
                                                                      30] if section_heading else "无标题"
                                    sections_preview_parts.append(
                                        f"{sid[:8]}...({heading_preview})")

                                sections_preview = ', '.join(
                                    sections_preview_parts)
                                if len(valid_sections) > 3:
                                    sections_preview += f' ... (共{len(valid_sections)}个)'

                                self.logger.info(
                                    f"     Event {event.id[:8]}... ('{event.title[:30]}') "
                                    f"→ {len(valid_sections)} 个段落: {sections_preview}"
                                )
                                displayed_count += 1

                                # 限制显示数量（避免日志过长）
                                if displayed_count >= 10:
                                    break

                    # 统计所有有效事项
                    total_valid_events = sum(
                        1 for e in events if e.id in filtered_event_ids)
                    if displayed_count < total_valid_events:
                        self.logger.info(
                            f"     ... (还有 {total_valid_events - displayed_count} 个有效事项未显示)")

                    # 3. 统计汇总
                    self.logger.info("")
                    self.logger.info("  📊 过滤效果统计:")
                    self.logger.info(
                        f"     过滤前: {len(found_entities)} 个Entity → {len(events)} 个Event → {len(chunks_data)} 个Chunk")
                    self.logger.info(
                        f"     过滤后: {len(entity_event_count)} 个有效Entity → {len(filtered_event_ids)} 个有效Event → {len(filtered_chunk_ids)} 个有效Chunk")
                    self.logger.info(
                        f"     过滤率: Entity={len(entity_event_count)/len(found_entities)*100:.1f}%, Event={len(filtered_event_ids)/len(events)*100:.1f}%, Chunk={len(filtered_chunk_ids)/len(chunks_data)*100:.1f}%")

                    self.logger.info("=" * 80)

                    # 🆕 构建 Step1 阶段的线索
                    from sag.modules.search.tracker import Tracker
                    tracker = Tracker(config)

                    # 准备数据映射
                    # 1. entity_id → entity_weight 映射
                    entity_weight_map = {key["key_id"]: key.get(
                        "weight", 0.0) for key in key_final}

                    # 2. 
                    chunk_data_map = {}
                    for content in content_results:
                        chunk_data_map[content.chunk_id] = {
                            "chunk_id": content.chunk_id,
                            "id": content.chunk_id,
                            "heading": content.heading or "",
                            "content": content.content or "",
                            "summary": "",
                            "section_type": getattr(content, 'search_type', ''),
                            "score": content.score  # 余弦相似度
                        }

                    # A. 构建 Entity → Event 线索
                    entity_event_clue_count = 0
                    for entity in found_entities:
                        entity_id = entity.id
                        entity_weight = entity_weight_map.get(entity_id, 0.0)

                        # 找到这个实体关联的所有有效事项
                        for event in events:
                            if event.id in filtered_event_ids:
                                # 检查这个事项是否关联这个实体
                                if entity_id in event_to_entities.get(event.id, []):
                                    # 构建节点
                                    entity_node = Tracker.build_entity_node({
                                        "key_id": entity_id,
                                        "id": entity_id,
                                        "name": entity.name,
                                        "type": entity.type,
                                        "description": entity.description or ""
                                    })
                                    # 🆕 使用 tracker 实例方法，指定召回方式为 "entity"
                                    event_node = tracker.get_or_create_event_node(
                                        event, "rerank", recall_method="entity")

                                    # 添加线索（置信度用实体权重）
                                    tracker.add_clue(
                                        stage="rerank",
                                        from_node=entity_node,
                                        to_node=event_node,
                                        confidence=entity_weight,
                                        relation="实体召回",
                                        metadata={
                                            "method": "entity_recall",
                                            "entity_weight": entity_weight,
                                            "step": "step1"
                                        }
                                    )
                                    entity_event_clue_count += 1

                    # B. 构建 Event → Chunk 线索
                    event_chunk_clue_count = 0
                    for event in events:
                        if event.id in filtered_event_ids and event.chunk_id:
                            # 检查该事项的chunk是否在过滤后的结果中
                            if event.chunk_id in filtered_chunk_ids:
                                # 获取chunk数据
                                chunk_data = chunk_data_map.get(event.chunk_id)
                                if chunk_data:
                                    # 构建节点
                                    event_node = tracker.get_or_create_event_node(
                                        event, "rerank", recall_method="entity")
                                    chunk_node = Tracker.build_section_node(chunk_data)

                                    # 添加线索（置信度用chunk的余弦相似度）
                                    tracker.add_clue(
                                        stage="rerank",
                                        from_node=event_node,
                                        to_node=chunk_node,
                                        confidence=chunk_data["score"],
                                        relation="原文块召回",
                                        metadata={
                                            "method": "chunk_recall",
                                            "chunk_score": chunk_data["score"],
                                            "step": "step1"
                                        }
                                    )
                                    event_chunk_clue_count += 1

                    self.logger.info("=" * 80)
                    self.logger.info(
                        f"🔗 [Step1 线索构建] Entity→Event={entity_event_clue_count}条, "
                        f"Event→Chunk={event_chunk_clue_count}条"
                    )
                    self.logger.info("=" * 80)

                self.logger.info(
                    f"步骤1完成: 处理了 {len(content_results)} 个段落",
                    extra={
                        "avg_cosine_score": np.mean([r.score for r in content_results]) if content_results else 0.0
                    }
                )

                # 显示Top 5结果
                top_results = content_results[:5]
                for i, result in enumerate(top_results, 1):
                    self.logger.debug(
                        f"Top {i}: {result.heading[:50]} - "
                        f"Cosine:{result.score:.3f}"
                    )

                # 转换为字典列表返回
                return [r.to_dict() for r in content_results]

        except Exception as e:
            self.logger.error(f"步骤1执行失败: {e}", exc_info=True)
            return []

    async def _step2_query_to_contents(
        self,
        query: str,
        source_config_ids: List[str],
        k: int = 30,  # 🆕 默认值改为30，与max_query_recall_results一致
        query_vector: Optional[List[float]] = None,  # 可选的查询向量
        config: Optional[SearchConfig] = None  # 添加config参数用于缓存和阈值
    ) -> List[Dict[str, Any]]:
        """
        步骤2: query找content（语义匹配）
        通过向量相似度在向量数据库找到原文块[content-query-related]，计算余弦相似度作为得分

        Args:
            query: 查询文本
            source_config_ids: 数据源ID列表
            k: ES召回数量（建议使用config.rerank.max_query_recall_results）
            query_vector: 可选的查询向量，如果为None则自动生成
            config: 搜索配置（用于缓存和阈值过滤）

        Returns:
            相关段落列表（ContentSearchResult.to_dict()格式）
        """
        try:
            self.logger.info(
                f"步骤2开始: query='{query}', source_config_ids={source_config_ids}")

            # 1. 生成查询向量（如果没有传入）
            if query_vector is None:
                query_vector = await self._generate_query_vector(query, config)
            if config and config.has_query_embedding:
                self.logger.info(f"使用缓存的query向量，维度: {len(query_vector)}")
            else:
                self.logger.info(f"查询向量生成成功，维度: {len(query_vector)}")

            # 2. 使用KNN搜索查找最相似的文本片段
            similar_paragraphs = await self._search_similar_paragraphs(
                query_vector=query_vector,
                source_config_ids=source_config_ids,
                k=k
            )
            self.logger.info(f"KNN搜索找到 {len(similar_paragraphs)} 个相似段落")

            if not similar_paragraphs:
                self.logger.warning("未找到相似段落")
                return []

            # 3. 计算余弦相似度得分
            cosine_scores = await self._calculate_cosine_scores(
                query_vector=query_vector,
                paragraphs=similar_paragraphs
            )

            # 4. 为每个原文块添加得分信息并创建 ContentSearchResult 对象
            content_results = []

            # 添加详细得分日志
            self.logger.info("=" * 80)
            self.logger.info("步骤2 得分详情（纯余弦相似度）：")
            self.logger.info("-" * 80)

            for idx, chunk in enumerate(similar_paragraphs, start=1):
                # ES 返回的是 chunk_id 和 source_id
                chunk_id = chunk.get("chunk_id")
                cosine_score = cosine_scores.get(chunk_id, 0.0)

                # 详细日志：显示每个原文块的得分
                heading_preview = chunk.get("heading", "")[:40]
                self.logger.info(
                    f"原文块 {chunk_id[:8]}... | "
                    f"Cosine={cosine_score:.4f} | "
                    f"标题: {heading_preview}"
                )

                # 创建 ContentSearchResult 对象，添加编号和 clues
                # Embedding 搜索使用 query 作为 clue
                query_clue = {
                    "type": "query",
                    "name": query,
                    "weight": 1.0,
                    "source": "embedding"
                }

                # 创建 ContentSearchResult 对象，添加编号和 clues
                # Embedding 搜索使用 query 作为 clue
                result = ContentSearchResult(
                    search_type=f"embedding-{idx}",
                    source_config_id=source_config_ids[0] if source_config_ids else "",
                    source_id=chunk.get("source_id", ""),  # SourceChunk.source_id (文章ID)
                    chunk_id=chunk_id,  # SourceChunk.id
                    rank=chunk.get("rank", 0),
                    heading=chunk.get("heading", ""),
                    content=chunk.get("content", ""),
                    score=cosine_score,  # 直接使用余弦相似度作为得分
                    event_ids=[],  # embedding搜索没有直接关联的event_id
                    clues=[query_clue],  # 使用 query 作为召回线索
                )
                content_results.append(result)

            self.logger.info("=" * 80)

            # 5. 按余弦相似度排序
            content_results.sort(key=lambda x: x.score, reverse=True)

            # 6. 使用 config.rerank.score_threshold 过滤低相似度结果
            original_count = len(content_results)
            if config and config.rerank.score_threshold:
                filtered_results = [
                    r for r in content_results if r.score >= config.rerank.score_threshold]

                if len(filtered_results) < original_count:
                    self.logger.info(
                        f"相似度过滤: {original_count} -> {len(filtered_results)} 个段落 "
                        f"(阈值={config.rerank.score_threshold:.2f})"
                    )

                    # 展示过滤后保留的段落信息
                    if filtered_results:
                        self.logger.info("=" * 80)
                        self.logger.info(
                            f"过滤后保留的 {len(filtered_results)} 个段落：")
                        self.logger.info("-" * 80)
                        for result in filtered_results:
                            heading_preview = result.heading[:
                                                             40] if result.heading else "无标题"
                            self.logger.info(
                                f"段落 {result.chunk_id[:8]}... | "
                                f"Cosine={result.score:.4f} | "
                                f"标题: {heading_preview}"
                            )
                        self.logger.info("=" * 80)

                content_results = filtered_results
            else:
                self.logger.warning("未设置阈值或config为空，跳过相似度过滤")

            self.logger.info(
                f"步骤2完成: 处理了 {len(content_results)} 个段落",
                extra={
                    "avg_cosine_score": np.mean([r.score for r in content_results]) if content_results else 0.0
                }
            )

            # 显示Top 5结果
            top_results = content_results[:5]
            for i, result in enumerate(top_results, 1):
                self.logger.debug(
                    f"Top {i}: {result.heading[:50]} - "
                    f"Cosine:{result.score:.3f}"
                )

            # 转换为字典列表返回
            return [r.to_dict() for r in content_results]

        except Exception as e:
            self.logger.error(f"步骤2执行失败: {e}", exc_info=True)
            return []

    async def _generate_query_vector(self, query: str, config: SearchConfig = None) -> List[float]:
        """
        将query转化成向量

        Args:
            query: 查询文本
            config: 搜索配置（用于缓存query_vector）

        Returns:
            查询向量
        """
        try:
            # 检查是否已有缓存的query_vector（如果有config传入）
            if config and config.has_query_embedding and config.query_embedding:
                self.logger.debug(
                    f"📦 使用缓存的query向量，维度: {len(config.query_embedding)}")
                return config.query_embedding

            # 使用processor生成向量
            query_vector = await self.processor.generate_embedding(query)
            self.logger.debug(f"Query向量生成成功，维度: {len(query_vector)}")

            # 如果有config，缓存query_vector
            if config:
                config.query_embedding = query_vector
                config.has_query_embedding = True
                self.logger.debug("📦 Query向量已缓存到config中")

            return query_vector
        except Exception as e:
            self.logger.error(f"查询向量生成失败: {e}")
            raise AIError(f"查询向量生成失败: {e}") from e

    async def _search_similar_paragraphs(
        self,
        query_vector: List[float],
        source_config_ids: List[str],
        k: int
    ) -> List[Dict[str, Any]]:
        """
        使用KNN搜索查找相似的文本片段

        遍历所有数据源，合并搜索结果

        Args:
            query_vector: 查询向量
            source_config_ids: 数据源ID列表
            k: 每个数据源返回的数量

        Returns:
            相似段落列表（合并所有数据源的结果）
        """
        try:
            all_paragraphs = []

            # 如果没有指定数据源，则搜索所有数据源
            if not source_config_ids:
                self.logger.info("未指定数据源，搜索所有数据源")
                similar_paragraphs = await self.content_repo.search_similar_by_content(
                    query_vector=query_vector,
                    k=k,
                    source_id=None
                )
                all_paragraphs.extend(similar_paragraphs)
            else:
                # 遍历每个数据源进行搜索
                self.logger.info(f"遍历 {len(source_config_ids)} 个数据源进行KNN搜索")
                for source_id in source_config_ids:
                    try:
                        similar_paragraphs = await self.content_repo.search_similar_by_content(
                            query_vector=query_vector,
                            k=k,
                            source_id=source_id  # 使用单个 source_id
                        )
                        all_paragraphs.extend(similar_paragraphs)
                        self.logger.debug(
                            f"数据源 {source_id[:8]}... 找到 {len(similar_paragraphs)} 个相似段落")
                    except Exception as e:
                        self.logger.warning(
                            f"数据源 {source_id[:8]}... 搜索失败: {e}")
                        continue

            self.logger.info(f"KNN搜索完成，共找到 {len(all_paragraphs)} 个相似段落")
            return all_paragraphs

        except Exception as e:
            self.logger.error(f"KNN搜索失败: {e}")
            raise AIError(f"KNN搜索失败: {e}") from e

    async def _calculate_cosine_scores(
        self,
        query_vector: List[float],
        paragraphs: List[Dict[str, Any]]
    ) -> Dict[str, float]:
        """
        计算余弦相似度得分

        Args:
            query_vector: 查询向量
            paragraphs: 段落列表（包含 chunk_id  字段）

        Returns:
            余弦相似度得分字典 {chunk_id: score}
        """
        try:
            if not paragraphs:
                return {}

            cosine_scores = {}

            for paragraph in paragraphs:
                # 🔑 提取chunk_id（优先使用新架构的chunk_id字段）
                chunk_id = paragraph.get("chunk_id") 

                # 从段落中获取预存的向量
                content_vector = paragraph.get("content_vector")

                if content_vector:
                    # 直接使用ES返回的预存向量
                    pass
                else:
                    # 如果没有预存向量，现场生成（只对content生成，不包含标题）
                    content = paragraph.get(
                        'section_content') or paragraph.get('content', '')
                    if not content.strip():
                        # 内容为空，跳过
                        self.logger.warning(f"段落 {chunk_id} 内容为空且无预存向量，跳过")
                        continue

                    content_vector = await self.processor.generate_embedding(content)

                # 计算余弦相似度
                similarity = await self._cosine_similarity(query_vector, content_vector)
                cosine_scores[chunk_id] = similarity

            self.logger.info(
                f"余弦相似度计算完成 - 共 {len(cosine_scores)} 个段落, "
                f"平均相似度: {np.mean(list(cosine_scores.values())):.4f}"
            )

            return cosine_scores

        except Exception as e:
            self.logger.error(f"余弦相似度计算失败: {e}")
            import traceback
            traceback.print_exc()
            return {}

    async def _cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """
        计算两个向量的余弦相似度

        Args:
            vec1: 向量1
            vec2: 向量2

        Returns:
            余弦相似度
        """
        try:
            # 转换为numpy数组
            v1 = np.array(vec1)
            v2 = np.array(vec2)

            # 计算余弦相似度
            dot_product = np.dot(v1, v2)
            norm_v1 = np.linalg.norm(v1)
            norm_v2 = np.linalg.norm(v2)

            if norm_v1 == 0 or norm_v2 == 0:
                return 0.0

            similarity = dot_product / (norm_v1 * norm_v2)
            return float(similarity)

        except Exception as e:
            self.logger.error(f"余弦相似度计算错误: {e}")
            return 0.0

    def _batch_cosine_similarity(
        self,
        query_vector: List[float],
        target_vectors: List[List[float]]
    ) -> np.ndarray:
        """
        批量计算query向量与多个目标向量的余弦相似度

        Args:
            query_vector: 查询向量
            target_vectors: 目标向量列表

        Returns:
            余弦相似度数组
        """
        try:
            # 转换为numpy数组
            query_array = np.array(query_vector)
            target_array = np.array(target_vectors)

            # 计算点积
            dot_products = np.dot(target_array, query_array)

            # 计算范数
            query_norm = np.linalg.norm(query_array)
            target_norms = np.linalg.norm(target_array, axis=1)

            # 计算相似度（避免除以零）
            denominators = target_norms * query_norm
            similarities = np.where(
                denominators > 0,
                dot_products / denominators,
                0.0
            )

            return similarities

        except Exception as e:
            self.logger.error(f"批量余弦相似度计算错误: {e}")
            return np.zeros(len(target_vectors))

    
    async def _step3_merge_result(
        self,
        step1_results: List[Dict[str, Any]],
        step2_results: List[Dict[str, Any]],
        config: SearchConfig
    ) -> List[Dict[str, Any]]:
        """
        步骤3: 合并步骤1和步骤2的结果，并去重

        去重规则：如果 source_id + chunk_id 相同，只保留 SQL 搜索的结果（step1）

        Args:
            step1_results: 步骤1的结果（SQL搜索）
            step2_results: 步骤2的结果（Embedding搜索）
            config: 搜索配置（用于构建线索）

        Returns:
            合并并去重后的结果列表（按得分降序排序）
        """
        self.logger.info(
            f"步骤3开始: 合并 step1({len(step1_results)}个) + step2({len(step2_results)}个) 并去重"
        )

        # 1. 先记录 SQL 搜索结果中所有的 (source_id, chunk_id)
        sql_chunks = set()
        for result in step1_results:
            source_id = result.get('source_id')
            chunk_id = result.get('chunk_id')
            sql_chunks.add((source_id, chunk_id))

        self.logger.debug(f"SQL 搜索找到 {len(sql_chunks)} 个唯一原文块")

        # 2. 遍历 embedding 结果，过滤掉已经在 SQL 结果中的原文块
        filtered_embedding_results = []
        duplicate_count = 0

        for result in step2_results:
            source_id = result.get('source_id')
            chunk_id = result.get('chunk_id')
            chunk_key = (source_id, chunk_id)

            if chunk_key in sql_chunks:
                # 这个原文块已经在 SQL 结果中，跳过
                duplicate_count += 1
                self.logger.debug(
                    f"原文块 {chunk_id[:8]}... 在 SQL 和 Embedding 中都找到，保留 SQL 结果"
                )
            else:
                # 这是新原文块，保留
                filtered_embedding_results.append(result)

        self.logger.info(
            f"去重统计: Embedding 结果中有 {duplicate_count} 个与 SQL 重复的原文块已移除"
        )

        # 🆕 显示 Embedding 中进入下一轮的段落（补充作用）
        if filtered_embedding_results:
            self.logger.info("=" * 80)
            self.logger.info(
                f"【Step2 扩展补充】{len(filtered_embedding_results)} 个 Embedding 原文块进入下一轮:")
            self.logger.info("-" * 80)
            for r in filtered_embedding_results[:10]:  # 最多显示10个
                chunk_id = r.get('chunk_id', '')
                heading = r.get('heading', '')
                score = r.get('score', 0.0)
                search_type = r.get('search_type', '')
                heading_preview = heading[:40] if heading else "无标题"
                self.logger.info(
                    f"  {chunk_id[:8]}... | Cosine={score:.4f} | Type={search_type} | {heading_preview}"
                )
            if len(filtered_embedding_results) > 10:
                self.logger.info(
                    f"  ... (还有 {len(filtered_embedding_results) - 10} 个)")
            self.logger.info("=" * 80)

            # 🆕 构建 query → chunk 线索（Step2 embedding召回的原文块）
            from sag.modules.search.tracker import Tracker
            tracker = Tracker(config)

            query_chunk_clue_count = 0
            for r in filtered_embedding_results:
                chunk_id = r.get('chunk_id', '')
                if not chunk_id:
                    continue

                # 构建 query 节点
                query_node = Tracker.build_query_node(config)

                # 构建 chunk 节点（使用section node方法，因为数据结构兼容）
                chunk_node = Tracker.build_section_node({
                    "chunk_id": chunk_id,
                    "id": chunk_id,
                    "heading": r.get('heading', ''),
                    "content": r.get('content', ''),
                    "summary": "",
                    "section_type": r.get('search_type', '')
                })

                # 添加线索（置信度用余弦相似度）
                tracker.add_clue(
                    stage="rerank",
                    from_node=query_node,
                    to_node=chunk_node,
                    confidence=r.get('score', 0.0),
                    relation="语义召回",
                    metadata={
                        "method": "embedding",
                        "search_type": r.get('search_type', ''),
                        "step": "step2"
                    }
                )
                query_chunk_clue_count += 1

            self.logger.info(
                f"🔗 [Step2 线索构建] Query→Chunk={query_chunk_clue_count}条")

        # 3. 合并 SQL 结果和过滤后的 embedding 结果
        merged_list = step1_results + filtered_embedding_results

        return merged_list

    async def _step4_calculate_weight_of_contents(
        self,
        key_final: List[Dict[str, Any]],
        content_related: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        步骤4: 计算[content-related]的初始权重向量

        根据key_final中的关键实体信息，计算每个段落的权重，
        并将计算出的权重赋值给每个段落的 weight 字段

        Args:
            key_final: 从Recall返回的key_final数据
            content_related: 从step3合并后的段落列表

        Returns:
            更新了weight字段的段落列表
        """
        try:
            self.logger.info(f"步骤4开始: 计算 {len(content_related)} 个段落的权重")

            # 第一层循环：遍历所有段落
            for content in content_related:
                search_type = content.get("search_type")  # 使用search_type
                content_text = content.get("content", "")
                heading = content.get("heading", "")
                full_text = f"{heading} {content_text}"  # 合并标题和内容用于搜索key

                # 1. 获取段落与query的相似性得分（来自step1或step2的score）
                similarity_score = content.get("score", 0.0)

                # 2. 初始化key权重累加和
                key_weight_sum = 0.0

                # 第二层循环：遍历所有关键实体
                for key in key_final:
                    key_name = key.get("name", "")
                    key_weight = key.get("weight", 0.0)
                    key_steps = key.get("steps", [1])  # 例如 [1] 或 [2]

                    # 计算step值（取第一个step值）
                    step = key_steps[0] if key_steps else 1

                    # 统计key在段落中出现的次数
                    count = full_text.count(key_name)

                    if count > 0:
                        # 计算该key的贡献：key_weight * ln(1 + count) / step
                        key_contribution = key_weight * \
                            math.log(1 + count) / step
                        key_weight_sum += key_contribution

                        self.logger.debug(
                            f"段落 {search_type} 包含key '{key_name}': "
                            f"count={count}, weight={key_weight:.3f}, step={step}, "
                            f"contribution={key_contribution:.4f}"
                        )

                # 3. 计算最终权重 = 0.5 * similarity_score + ln(1 + key_weight_sum)
                total_weight = 0.5 * similarity_score + \
                    math.log(1 + key_weight_sum)

                # 4. 将权重赋值给该段落的 weight 字段
                content["weight"] = total_weight

                self.logger.info(
                    f"段落 {search_type} 权重计算: "
                    f"similarity={similarity_score:.4f}, key_sum={key_weight_sum:.4f}, "
                    f"total={total_weight:.4f}"
                )

            self.logger.info(f"步骤4完成: 计算了 {len(content_related)} 个段落的权重")

            return content_related

        except Exception as e:
            self.logger.error(f"步骤4执行失败: {e}", exc_info=True)
            return []

    async def _step5_pageRank_of_contents(
        self,
        content_related: List[Dict[str, Any]],
        key_final: List[Dict[str, Any]] = None,
        damping: float = 0.85,
        iterations: int = 100,
        tolerance: float = 1e-6
    ) -> List[Dict[str, Any]]:
        """
        步骤5: PageRank重排序

        构建段落关系图并使用PageRank算法进行排序
        - 初始权重：使用step4的weight作为初始PageRank值
        - 两种关联关系：
          1. 段落关联（权重0.5）：同一文章内相邻的段落之间有边
          2. 实体关联（权重0.5）：包含相同key_final实体的段落之间有边

        Args:
            content_related: 从step4计算完权重的段落列表
            key_final: 从Recall返回的关键实体列表（用于实体关联）
            damping: PageRank阻尼系数，默认0.85
            iterations: 最大迭代次数，默认30
            tolerance: 收敛阈值，默认1e-6

        Returns:
            按PageRank值排序后的段落列表
        """
        try:
            n = len(content_related)
            self.logger.info(f"步骤5开始: 对 {n} 个段落使用PageRank算法进行排序")

            if n == 0:
                return []

            # ===== DEBUG: 记录直接按权重排序的结果 =====
            self.logger.debug("=" * 80)
            self.logger.debug("【对比】直接按权重排序的结果（Top 10）：")
            self.logger.debug("-" * 80)

            weight_sorted = sorted(
                enumerate(content_related),
                key=lambda x: x[1].get('weight', 0.0),
                reverse=True
            )

            for rank, (idx, content) in enumerate(weight_sorted[:10], 1):
                search_type = content.get('search_type', 'N/A')
                weight = content.get('weight', 0.0)
                score = content.get('score', 0.0)
                heading = content.get('heading', '')[:40]
                chunk_id = content.get('chunk_id', '')[:8]
                event_count = len(content.get('event_ids', []))

                self.logger.debug(
                    f"Rank {rank:2d} [idx={idx:3d}]: {search_type:12s} | "
                    f"weight={weight:.4f}, score={score:.4f} | "
                    f"events={event_count} | chunk={chunk_id}... | "
                    f"{heading}"
                )
            self.logger.debug("=" * 80)

            # 1. 初始化PageRank值（使用step4的weight归一化后作为初始值）
            weights = np.array([c.get('weight', 0.0) for c in content_related])
            if weights.sum() > 0:
                pagerank = weights / weights.sum()
                self.logger.info(f"使用step4的权重作为初始PageRank值（已归一化）")
            else:
                pagerank = np.ones(n) / n
                self.logger.warning(f"所有权重为0，使用均匀分布作为初始PageRank值")

            # 为每个段落创建索引映射
            chunk_to_idx = {c['chunk_id']: i for i,
                              c in enumerate(content_related)}

            # 2. 构建关系图（使用字典存储边和权重）
            # graph[i] = [(j, weight), ...] 表示从节点i指向节点j的边及其权重
            graph = defaultdict(list)

            # 2.1 实体关联 - 直接使用实体权重构建段落间关系
            entity_edges_count = 0
            if key_final:
                self.logger.info("构建实体关联边（使用实体权重）...")
                # 为每个段落找到它包含的实体及其权重
                section_entities = defaultdict(list)  # section_idx -> [(entity_name, entity_weight), ...]
                for i, content in enumerate(content_related):
                    full_text = f"{content.get('heading', '')} {content.get('content', '')}"
                    for key in key_final:
                        key_name = key.get('name', '')
                        key_weight = key.get('weight', 0.0)
                        if key_name and key_name in full_text:
                            section_entities[i].append((key_name, key_weight))

                # 基于key-final构建段落间关系：方向性权重体现重要性差异
                for i in range(n):
                    for j in range(i + 1, n):
                        # 获取两个段落共享的实体
                        entities_i = {entity_name for entity_name, _ in section_entities[i]}
                        entities_j = {entity_name for entity_name, _ in section_entities[j]}
                        common_entities = entities_i & entities_j

                        if common_entities:
                            # 计算方向性权重
                            weight_i_to_j = 0.0  # 段落i→段落j的权重
                            weight_j_to_i = 0.0  # 段落j→段落i的权重

                            # 获取两个段落的内容
                            content_i = f"{content_related[i].get('heading', '')} {content_related[i].get('content', '')}"
                            content_j = f"{content_related[j].get('heading', '')} {content_related[j].get('content', '')}"

                            for entity_name in common_entities:
                                # 找到实体在key_final中的权重（任意一个段落的权重即可）
                                key_weight = next(w for name, w in section_entities[i] if name == entity_name)

                                # 计算key在两个段落中的出现次数
                                count_i = content_i.count(entity_name)
                                count_j = content_j.count(entity_name)

                                # 方向性权重：投票权重基于目标段落的重要性
                                weight_i_to_j += key_weight * count_j  # i→j的权重基于j中的出现次数
                                weight_j_to_i += key_weight * count_i  # j→i的权重基于i中的出现次数

                            # 建立方向性边
                            if weight_i_to_j > 0:
                                graph[i].append((j, weight_i_to_j))
                            if weight_j_to_i > 0:
                                graph[j].append((i, weight_j_to_i))

                            # 记录边的数量（只计算实际建立的边）
                            if weight_i_to_j > 0 or weight_j_to_i > 0:
                                entity_edges_count += 1

                self.logger.info(f"实体关联: 添加了 {entity_edges_count} 条有向边（基于key-final和出现次数）")
            else:
                self.logger.warning("未提供key_final，跳过实体关联边的构建")

            total_edges = entity_edges_count
            self.logger.info(f"关系图构建完成: 节点数={n}, 总边数={total_edges} (仅实体关联)")

            # ===== DEBUG: 展示图结构统计信息 =====
            self.logger.debug("=" * 80)
            self.logger.debug("【图结构统计】：")

            # 计算每个节点的出度和入度
            out_degrees = {i: len(graph.get(i, [])) for i in range(n)}
            in_degrees = defaultdict(int)
            for i in range(n):
                for j, _ in graph.get(i, []):
                    in_degrees[j] += 1

            # 统计出度分布
            out_degree_values = list(out_degrees.values())
            in_degree_values = [in_degrees.get(i, 0) for i in range(n)]

            self.logger.debug(
                f"出度统计: 平均={np.mean(out_degree_values):.2f}, "
                f"最大={max(out_degree_values)}, "
                f"最小={min(out_degree_values)}, "
                f"孤立节点={sum(1 for d in out_degree_values if d == 0)}"
            )
            self.logger.debug(
                f"入度统计: 平均={np.mean(in_degree_values):.2f}, "
                f"最大={max(in_degree_values)}, "
                f"最小={min(in_degree_values)}"
            )

            # 展示度数最高的前5个节点（基于实体关联）
            top_out_degree = sorted(
                out_degrees.items(), key=lambda x: x[1], reverse=True)[:5]
            self.logger.debug("出度最高的5个节点（基于实体关联）：")
            for idx, degree in top_out_degree:
                content = content_related[idx]
                chunk_id = content.get('chunk_id', '')[:8]
                heading = content.get('heading', '')[:30]
                self.logger.debug(
                    f"  节点{idx:3d} (chunk={chunk_id}...): 出度={degree}, 标题={heading}")

            self.logger.debug("=" * 80)

            # 3. 预计算每个节点的总出权重（避免重复计算）
            out_weights = {}
            for j in range(n):
                edges = graph.get(j, [])
                out_weights[j] = sum(w for _, w in edges) if edges else 0.0

            nodes_with_edges = sum(1 for w in out_weights.values() if w > 0)
            self.logger.debug(
                f"预计算完成: {nodes_with_edges}/{n} 个节点有出边")

            # 4. PageRank迭代计算（优化版：反向遍历图，避免O(n²)复杂度）
            self.logger.info(
                f"开始PageRank迭代（阻尼系数={damping}, 最大迭代={iterations}）...")

            for iteration in range(iterations):
                # 初始化为基础值 (1-d)/n
                new_pagerank = np.ones(n) * (1 - damping) / n

                # 遍历所有源节点j（而不是遍历目标节点i）
                for j in range(n):
                    # 跳过没有PageRank值或没有出边的节点
                    if pagerank[j] == 0 or out_weights[j] == 0:
                        continue

                    # 计算j对每条出边的单位权重贡献
                    # 贡献 = d × PR(j) × edge_weight / total_out_weight
                    contribution_per_weight = damping * pagerank[j] / out_weights[j]

                    # 遍历j的所有出边，将贡献分配给目标节点
                    for target, edge_weight in graph.get(j, []):
                        new_pagerank[target] += contribution_per_weight * edge_weight

                # 检查收敛
                diff = np.abs(new_pagerank - pagerank).sum()
                if diff < tolerance:
                    self.logger.info(
                        f"PageRank在第{iteration+1}次迭代后收敛（差异={diff:.8f}）")
                    pagerank = new_pagerank
                    break

                pagerank = new_pagerank

                # 每10次迭代输出一次日志
                if (iteration + 1) % 10 == 0:
                    self.logger.debug(
                        f"迭代 {iteration+1}/{iterations}, 差异={diff:.8f}")

            else:
                self.logger.warning(f"PageRank达到最大迭代次数{iterations}，未完全收敛")

            # 4. 将PageRank值赋值给每个段落
            for i, content in enumerate(content_related):
                content['pagerank'] = float(pagerank[i])

            # 5. 按PageRank值排序（从大到小）
            sorted_contents = sorted(
                content_related,
                key=lambda x: x.get('pagerank', 0.0),
                reverse=True
            )

            # ===== DEBUG: 记录PageRank排序结果并对比权重排序 =====
            self.logger.debug("=" * 80)
            self.logger.debug("【对比】PageRank排序的结果（Top 10）：")
            self.logger.debug("-" * 80)

            # 创建 chunk_id 到原始索引的映射（用于对比排名变化）
            chunk_to_original_idx = {
                c['chunk_id']: i for i, c in enumerate(content_related)}

            # 创建权重排名映射
            weight_rank_map = {
                content[1]['chunk_id']: rank for rank, content in enumerate(weight_sorted, 1)}

            for rank, content in enumerate(sorted_contents[:10], 1):
                search_type = content.get('search_type', 'N/A')
                pagerank_val = content.get('pagerank', 0.0)
                weight = content.get('weight', 0.0)
                score = content.get('score', 0.0)
                heading = content.get('heading', '')[:40]
                chunk_id = content.get('chunk_id', '')
                event_count = len(content.get('event_ids', []))

                # 获取在权重排序中的排名
                weight_rank = weight_rank_map.get(chunk_id, -1)
                rank_change = weight_rank - rank if weight_rank > 0 else 0

                # 排名变化标记
                if rank_change > 0:
                    change_mark = f"↑{rank_change:+d}"  # 上升
                elif rank_change < 0:
                    change_mark = f"↓{rank_change:+d}"  # 下降
                else:
                    change_mark = " ━  "  # 不变

                original_idx = chunk_to_original_idx.get(chunk_id, -1)

                self.logger.debug(
                    f"Rank {rank:2d} [idx={original_idx:3d}] {change_mark:>5s} (was #{weight_rank:2d}): {search_type:12s} | "
                    f"PR={pagerank_val:.6f}, weight={weight:.4f}, score={score:.4f} | "
                    f"events={event_count} | chunk={chunk_id[:8]}... | "
                    f"{heading}"
                )

            self.logger.debug("=" * 80)
            self.logger.debug("【排序变化统计】：")

            # 统计排名变化
            rank_changes = []
            for rank, content in enumerate(sorted_contents, 1):
                chunk_id = content['chunk_id']
                weight_rank = weight_rank_map.get(chunk_id, -1)
                if weight_rank > 0:
                    change = weight_rank - rank
                    rank_changes.append(abs(change))

            if rank_changes:
                avg_change = np.mean(rank_changes)
                max_change = max(rank_changes)
                unchanged_count = sum(1 for c in rank_changes if c == 0)
                self.logger.debug(
                    f"平均排名变化: {avg_change:.2f} 位 | "
                    f"最大排名变化: {max_change} 位 | "
                    f"排名不变: {unchanged_count}/{len(rank_changes)} 个"
                )

            self.logger.debug("=" * 80)

            # 记录排序后的前几个结果（INFO级别）
            self.logger.info("=" * 80)
            self.logger.info("步骤5排序结果（Top 5 by PageRank）：")
            self.logger.info("-" * 80)

            for i, content in enumerate(sorted_contents[:5], 1):
                search_type = content.get('search_type', 'N/A')
                pagerank_val = content.get('pagerank', 0.0)
                weight = content.get('weight', 0.0)
                score = content.get('score', 0.0)
                heading = content.get('heading', '')[:40]

                self.logger.info(
                    f"Rank {i}: {search_type} | "
                    f"PageRank={pagerank_val:.6f}, weight={weight:.4f}, score={score:.4f} | "
                    f"标题: {heading}"
                )

            self.logger.info("=" * 80)
            self.logger.info(
                f"步骤5完成: 排序了 {len(sorted_contents)} 个段落 "
                f"(平均PageRank={pagerank.mean():.6f})"
            )

            return sorted_contents

        except Exception as e:
            self.logger.error(f"步骤5执行失败: {e}", exc_info=True)
            return []

    async def _step6_get_topn_sections(
        self,
        sorted_contents: List[Dict[str, Any]],
        top_k: int,
        config: Optional[SearchConfig] = None
    ) -> List[Dict[str, Any]]:
        """
        步骤6: 取Top-N段落并返回

        处理流程：
        1. 取Top-k：从排序后的结果中取前 k 个段落
        2. 直接返回这些段落，不再进行段落→事项的转换

        Args:
            sorted_contents: 从step5排序后的段落列表（已按PageRank降序排序）
            top_k: 取前k个结果
            config: 搜索配置

        Returns:
            List[Dict[str, Any]]: 段落列表，每个段落包含：
                - chunk_id: 原文块ID
                - heading: 段落标题
                - content: 段落内容
                - pagerank: PageRank得分
                - weight: 权重得分
                - clues: 线索列表（召回该段落的实体）
        """
        try:
            self.logger.info(
                f"[段落级Step6] 开始: 从 {len(sorted_contents)} 个段落中取Top-{top_k}")

            # 1. 取Top-k段落
            topk_sections = sorted_contents[:top_k]
            self.logger.info(f"✓ [段落级Step6] 提取了Top-{len(topk_sections)}个段落")

            # 2. 显示Top-10段落信息
            self.logger.info("=" * 80)
            self.logger.info(
                f"【段落级Step6】Top-{min(len(topk_sections), 10)}段落详情:")
            self.logger.info("-" * 80)

            for idx, section in enumerate(topk_sections[:10], 1):
                heading = section.get('heading', '')[:50]
                pagerank = section.get('pagerank', 0.0)
                weight = section.get('weight', 0.0)
                chunk_id = section.get('chunk_id', '')[:8]

                self.logger.info(
                    f"  段落{idx}: {chunk_id}... | PR={pagerank:.4f}, W={weight:.3f} | '{heading}'"
                )

            if len(topk_sections) > 10:
                self.logger.info(
                    f"  ... (还有 {len(topk_sections) - 10} 个段落未显示)")

            self.logger.info("=" * 80)
            self.logger.info(f"✓ [段落级Step6] 完成: 返回 {len(topk_sections)} 个段落")

            return topk_sections

        except Exception as e:
            self.logger.error(f"[段落级Step6] 执行失败: {e}", exc_info=True)
            return []  # 失败时返回空列表

    def _build_response(
        self,
        config: SearchConfig,
        key_final: List[Dict[str, Any]],
        events: List[SourceEvent],
        event_to_clues: Dict[str, List[Dict]]
    ) -> Dict[str, Any]:
        """
        构建新的响应格式

        Args:
            config: 搜索配置对象
            key_final: 召回的实体列表（key-final）
            events: 事项列表
            event_to_clues: 事项ID到实体列表的映射 {event_id: [entity1, entity2, ...]}

        Returns:
            Dict[str, Any]: 包含以下字段的字典：
                - events: 事项对象列表
                - clues: 召回线索信息
                    - origin_query: 原始查询
                    - final_query: LLM重写后的查询（如果没有重写则为None）
                    - query_entities: 查询召回的实体列表（key_id改为id）
                    - recall_entities: 召回的实体列表（key_id改为id，去除query_entities中的值）
                    - event_entities: 事项与实体的关联映射表 {event_id: [entity1, entity2, ...]}
        """
        # 1. 处理 query_entities：将 config.query_recalled_keys 中的 key_id 改为 id
        query_entities = []
        query_key_ids = set()  # 用于后续过滤

        for key in config.query_recalled_keys:
            key_copy = key.copy()
            if "key_id" in key_copy:
                key_id = key_copy.pop("key_id")
                key_copy["id"] = key_id
                query_key_ids.add(key_id)
            query_entities.append(key_copy)

        # 2. 处理 recall_entities：将 key_final 中的 key_id 改为 id，并过滤掉 query_entities 中的值
        recall_entities = []

        for key in key_final:
            # 获取 key_id 用于过滤判断
            key_id = key.get("key_id")

            # 如果这个 key_id 在 query_recalled_keys 中，则跳过
            if key_id in query_key_ids:
                continue

            # 复制并重命名 key_id 为 id
            key_copy = key.copy()
            if "key_id" in key_copy:
                key_copy["id"] = key_copy.pop("key_id")
            recall_entities.append(key_copy)

        # 3. 判断是否应该返回 final_query
        # 如果启用了query重写功能（enable_query_rewrite=True），则返回重写后的query
        # 否则返回 None
        final_query = config.query if config.enable_query_rewrite and config.recall.use_fast_mode == False else None

        # 4. 过滤 event_to_clues，只保留最终返回的事项
        final_event_ids = {event.id for event in events}
        filtered_event_entities = {
            event_id: clues
            for event_id, clues in event_to_clues.items()
            if event_id in final_event_ids
        }

        # 5. 构建响应
        response = {
            "events": events,  # 事项列表
            "clues": {
                "origin_query": config.original_query,  # 原始query（重写前）
                "final_query": final_query,  # 重写后的query（没有重写则为None）
                "query_entities": query_entities,
                "recall_entities": recall_entities,
                "event_entities": filtered_event_entities  # 只包含最终返回事项的溯源信息
            }
        }

        self.logger.info(
            f"响应构建完成: origin_query='{config.original_query}', "
            f"final_query='{final_query}', "
            f"query_entities={len(query_entities)}个, "
            f"recall_entities={len(recall_entities)}个, "
            f"events={len(events)}个, "
            f"event_entities映射={len(filtered_event_entities)}个 (过滤前={len(event_to_clues)}个)"
        )

        return response

