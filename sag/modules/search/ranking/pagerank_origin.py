"""
搜索 Rerank 模块

实现6步骤的查找最重要段落的方法：
1. key找content：根据[key-final]从sql中提取原文块[content-key-related]，从ES获取预存向量并计算和query的余弦相似度（记录event_id）
2. query找content：通过向量相似度（KNN+余弦相似度）在向量数据库找到原文块[content-query-related]（event_ids为空）
3. content合并+去重：合并[content-key-related]和[content-query-related]，如果同一段落（article_id+section_id相同）同时出现在SQL和Embedding结果中，只保留SQL的结果
4. 制作[content-related]权重向量：使用公式 weight = 0.5*相似度 + ln(1 + Σ(key权重 × ln(1+出现次数) / step))
5. PageRank重排序：根据权重对段落排序（从大到小）
6. 取Top-N并返回：从Top-N段落中提取关联的事项列表

返回格式：
Dict[str, Any]: 包含以下字段的字典：
    - events (List[SourceEvent]): 事项对象列表（按段落 PageRank 顺序排列，已去重）
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
from sag.db import SourceEvent, Entity, EventEntity, ArticleSection, Article, get_session_factory
from sag.exceptions import AIError
from sag.modules.load.processor import DocumentProcessor
from sag.modules.search.config import SearchConfig
from sag.modules.search.tracker import Tracker  # 🆕 添加线索追踪器
from sag.utils import get_logger

logger = get_logger("search.rerank.pagerank")


@dataclass
class ContentSearchResult:
    """
    搜索结果的统一返回格式

    用于表示从SQL数据库或Embedding向量数据库搜索到的内容
    """
    # 必需字段
    search_type: str      # "sql", "embedding" 或带编号的格式如 "SQL-1", "embedding-2"
    source_id: str        # 数据源ID (UUID)
    article_id: str       # 文章ID (UUID)
    section_id: str       # 段落ID (UUID)
    rank: int             # 段落在文章中的排序
    heading: str          # 段落标题
    content: str          # 段落内容
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
            "source_id": self.source_id,
            "article_id": self.article_id,
            "section_id": self.section_id,
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
            source_id=data["source_id"],
            article_id=data["article_id"],
            section_id=data["section_id"],
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
            f"section_id={self.section_id}, "
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
                k=config.rerank.pagerank_section_top_k,
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

            # 步骤6: 提取关联的事项列表并计算相似度
            step6_start = time.perf_counter()
            results, event_to_clues = await self._step6_get_topn_of_contents(
                sorted_contents=sorted_results,
                top_k=config.rerank.max_results * 2,
                source_config_ids=config.get_source_config_ids(),
                query_vector=query_vector,
                config=config
            )
            step6_time = time.perf_counter() - step6_start
            step_times['step6_事项召回与过滤'] = step6_time
            self.logger.info(
                f"步骤6完成，最终返回 {len(results)} 个事项, 耗时: {step6_time:.3f}秒"
            )

            # 计算总耗时
            overall_time = time.perf_counter() - overall_start

            # 输出耗时统计汇总
            self.logger.info("=" * 80)
            self.logger.info("【Rerank 搜索】各步骤耗时汇总:")
            self.logger.info("-" * 80)
            self.logger.info(
                f"查询向量生成: {vector_time:.3f}秒 ({vector_time/overall_time*100:.1f}%)")
            for step_name, step_time in step_times.items():
                self.logger.info(
                    f"{step_name}: {step_time:.3f}秒 ({step_time/overall_time*100:.1f}%)")
            self.logger.info("-" * 80)
            self.logger.info(f"总耗时: {overall_time:.3f}秒")
            self.logger.info("=" * 80)

            # === 构建Rerank阶段线索 ===
            # rerank_clues = self._build_rerank_clues(config, key_final, results, event_to_clues, sorted_results)
            # config.rerank_clues = rerank_clues
            self.logger.info(f"✨ Rerank线索已构建 (entity→section→event拆分为2条线索)")

            # 构建并返回新的响应格式
            return self._build_response(config, key_final, results, event_to_clues)

        except Exception as e:
            self.logger.error(f"Rerank搜索失败: {e}", exc_info=True)
            # 判断是否应该返回 final_query
            # 如果启用了query重写功能（enable_query_rewrite=True），则返回重写后的query
            # 否则返回 None
            final_query = config.query if config.enable_query_rewrite else None
            return {
                "events": [],
                "clues": {
                    "origin_query": config.original_query,
                    "final_query": final_query,
                    "query_entities": [],
                    "recall_entities": []
                }
            }  # 失败时返回空字典

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
                            Entity.source_id.in_(source_config_ids),
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
                            SourceEvent.source_id.in_(source_config_ids),
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

                # 6. 通过事件查找对应的段落
                # 新方法：直接使用事件的 references 字段来查找段落

                # 首先获取所有事件的详细信息（包括 references 字段）
                event_detail_query = (
                    select(SourceEvent)
                    .where(
                        and_(
                            SourceEvent.source_id.in_(source_config_ids),
                            SourceEvent.id.in_(event_ids)
                        )
                    )
                )
                event_detail_result = await session.execute(event_detail_query)
                events = event_detail_result.scalars().all()

                # 收集所有事件引用的段落ID
                all_referenced_section_ids = set()
                event_to_sections = {}  # 事件ID -> 段落ID列表的映射

                for event in events:
                    if event.references:
                        event_to_sections[event.id] = event.references
                        all_referenced_section_ids.update(event.references)
                        self.logger.debug(
                            f"事件 {event.id}... 引用了 {len(event.references)} 个段落"
                        )
                    else:
                        # 如果没有 references，使用文章的所有段落（向后兼容）
                        event_to_sections[event.id] = None
                        self.logger.warning(
                            f"事件 {event.id}... 没有 references 字段"
                        )

                if not all_referenced_section_ids:
                    self.logger.warning("所有事件都没有引用任何段落")
                    return []

                self.logger.info(
                    f"收集到 {len(all_referenced_section_ids)} 个被引用的段落ID")

                # 查询这些段落的详细信息
                article_section_query = (
                    select(ArticleSection, Article)
                    .join(Article, ArticleSection.article_id == Article.id)
                    .where(
                        and_(
                            Article.source_id.in_(source_config_ids),
                            ArticleSection.id.in_(
                                list(all_referenced_section_ids))
                        )
                    )
                    .order_by(ArticleSection.rank)
                )

                section_result = await session.execute(article_section_query)
                sections_data = section_result.fetchall()

                if not sections_data:
                    self.logger.warning("未找到相关段落")
                    return []

                self.logger.info(f"找到 {len(sections_data)} 个被引用的段落")

                # 7. 构建段落数据并计算相似度
                # 使用字典来合并同一段落的多个事件ID
                paragraphs_dict = {}  # key: section_id, value: paragraph data

                # 反向映射：section_id -> [event_ids]
                section_to_events = {}
                for event_id, section_ids in event_to_sections.items():
                    if section_ids:  # 如果有 references
                        for section_id in section_ids:
                            if section_id not in section_to_events:
                                section_to_events[section_id] = []
                            section_to_events[section_id].append(event_id)

                self.logger.debug(f"构建了 {len(section_to_events)} 个段落到事件的映射")

                # 🆕 日志：显示 Event → Section 映射
                self.logger.info("=" * 80)
                self.logger.info("【Step1 召回路径】Event → Section 映射:")
                self.logger.info("-" * 80)

                # 显示每个事项引用的段落
                for event in events[:10]:  # 只显示前10个事项
                    if event.references:
                        section_count = len(event.references)
                        section_preview = ', '.join(
                            [sid[:8]+'...' for sid in event.references[:3]])
                        if section_count > 3:
                            section_preview += f' ... (共{section_count}个)'
                        self.logger.info(
                            f"  Event {event.id[:8]}... ('{event.title[:30]}') → {section_count} 个段落: {section_preview}"
                        )

                if len(events) > 10:
                    self.logger.info(f"  ... (还有 {len(events) - 10} 个事项未显示)")

                self.logger.info("=" * 80)

                # 遍历所有段落，构建段落数据
                for section, article in sections_data:
                    section_id = section.id

                    # 找到引用该段落的所有事件
                    related_event_ids = section_to_events.get(section_id, [])

                    if not related_event_ids:
                        self.logger.warning(
                            f"段落 {section_id[:8]}... 没有找到关联的事件")
                        continue

                    # 计算该段落的综合权重（所有关联事件的权重之和）
                    total_event_weight = sum(event_weights.get(
                        eid, 1.0) for eid in related_event_ids)

                    # 收集该段落的 clues（从关联的事件中收集关联的实体）
                    paragraph_clues = []
                    seen_entity_ids = set()
                    for event_id in related_event_ids:
                        # 获取该事件关联的所有实体
                        entity_ids = event_to_entities.get(event_id, [])
                        for entity_id in entity_ids:
                            if entity_id not in seen_entity_ids:
                                # 获取对应的 key 对象
                                key = entity_to_key.get(entity_id)
                                if key:
                                    paragraph_clues.append(key)
                                    seen_entity_ids.add(entity_id)

                    # 创建段落数据
                    paragraph = {
                        "section_id": section_id,
                        "article_id": article.id,
                        "event_ids": related_event_ids,  # 所有关联的事件ID
                        "article_title": article.title,
                        "article_category": article.category,
                        "section_rank": section.rank,
                        "section_heading": section.heading,
                        "section_content": section.content,
                        "content_vector": None,  # 将在后面获取
                        "entity_weight": total_event_weight,
                        "created_time": section.created_time,
                        "extra_data": section.extra_data or {},
                        "clues": paragraph_clues  # 召回该段落的 key 列表
                    }
                    paragraphs_dict[section_id] = paragraph

                    self.logger.debug(
                        f"段落 {section_id[:8]}... 关联了 {len(related_event_ids)} 个事件，"
                        f"综合权重={total_event_weight:.3f}，clues数={len(paragraph_clues)}"
                    )

                # 转换为列表
                paragraphs_data = list(paragraphs_dict.values())

                # 统计信息
                multi_event_sections = [
                    p for p in paragraphs_data if len(p["event_ids"]) > 1]
                self.logger.info(
                    f"构建了 {len(paragraphs_data)} 个唯一段落数据"
                )
                if multi_event_sections:
                    self.logger.info(
                        f"其中 {len(multi_event_sections)} 个段落关联了多个事件"
                    )

                if not paragraphs_data:
                    self.logger.warning("过滤后没有找到真正关联的段落")
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
                    f"  → Section数量: {len(paragraphs_data)} (通过Event.references)")
                self.logger.info("-" * 80)
                self.logger.info(f"  召回路径: Key → Entity → Event → Section")
                self.logger.info("=" * 80)

                # 7.5. 从 ES 批量获取段落的预存向量
                section_ids_list = list(paragraphs_dict.keys())
                self.logger.info(
                    f"从 ES 批量获取 {len(section_ids_list)} 个段落的预存向量...")

                es_sections_data = await self.content_repo.get_chunks_by_ids(
                    chunk_ids=section_ids_list,
                    include_vectors=True
                )

                # 构建 section_id -> content_vector 的映射
                section_vector_map = {}
                for es_section in es_sections_data:
                    chunk_id = es_section.get('chunk_id')
                    content_vector = es_section.get('content_vector')
                    if chunk_id and content_vector:
                        section_vector_map[chunk_id] = content_vector

                self.logger.info(
                    f"从 ES 获取到 {len(section_vector_map)} 个段落的预存向量 "
                    f"(请求了 {len(section_ids_list)} 个)"
                )

                # 将获取到的向量填充到段落数据中
                filled_count = 0
                for paragraph in paragraphs_data:
                    section_id = paragraph['section_id']
                    if section_id in section_vector_map:
                        paragraph['content_vector'] = section_vector_map[section_id]
                        filled_count += 1
                    else:
                        self.logger.warning(
                            f"段落 {section_id[:8]}... 在 ES 中未找到预存向量，将现场生成"
                        )

                self.logger.info(
                    f"成功填充 {filled_count}/{len(paragraphs_data)} 个段落的预存向量"
                )

                # 8. 计算向量相似度得分
                similarity_scores = await self._calculate_cosine_scores(
                    query_vector=query_vector,
                    paragraphs=paragraphs_data
                )
                self.logger.debug(f"余弦相似度计算完成")

                # 9. 使用余弦相似度作为最终得分
                content_results = []

                # 添加详细得分日志（与步骤2对应）
                self.logger.info("=" * 80)
                self.logger.info("步骤1 得分详情（纯余弦相似度，使用ES预存向量）：")
                self.logger.info("-" * 80)

                for idx, paragraph in enumerate(paragraphs_data, start=1):
                    section_id = paragraph["section_id"]

                    # 直接使用余弦相似度作为得分（范围通常在[0, 1]之间）
                    cosine_score = similarity_scores.get(section_id, 0.0)

                    # 获取该段落关联的所有事件ID
                    event_ids = paragraph["event_ids"]

                    # 详细日志：显示每个段落的得分和关联的事件数
                    heading_preview = paragraph.get("section_heading", "")[:40]
                    self.logger.info(
                        f"段落 {section_id[:8]}... | "
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
                        search_type=f"SQL-{idx}",  # 添加编号
                        # 使用第一个source_id作为主source_id
                        source_id=source_config_ids[0] if source_config_ids else "",
                        article_id=paragraph["article_id"],
                        section_id=section_id,
                        rank=paragraph["section_rank"],
                        heading=paragraph["section_heading"],
                        content=paragraph["section_content"],
                        score=cosine_score,
                        event_ids=event_ids,  # 记录所有关联的事件ID列表
                        clues=paragraph.get("clues", []),  # 记录召回该段落的 key 列表
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
                                    f"段落 {result.section_id[:8]}... | "
                                    f"Cosine={result.score:.4f} | "
                                    f"标题: {heading_preview}"
                                )
                            self.logger.info("=" * 80)

                    content_results = filtered_results
                else:
                    self.logger.warning("未设置阈值或config为空，跳过相似度过滤")

                # 🆕 日志：显示过滤后的有效映射关系
                if content_results:
                    # 收集过滤后的所有 section_ids 和 event_ids
                    filtered_section_ids = {
                        r.section_id for r in content_results}
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

                    # 2. Event → Section 映射（只显示保留的段落）
                    self.logger.info("")
                    self.logger.info("  2️⃣ Event → Section (仅保留段落):")
                    event_section_count = {}
                    displayed_count = 0

                    # 🆕 创建 section_id → heading 的映射
                    section_heading_map = {}
                    for content in content_results:
                        section_heading_map[content.section_id] = content.heading or ""

                    # 🆕 只遍历有效的事项（在 filtered_event_ids 中的）
                    for event in events:
                        if event.id in filtered_event_ids:
                            # 找到该事项关联的、且被保留的段落
                            valid_sections = [
                                sid for sid in (event.references or [])
                                if sid in filtered_section_ids
                            ]
                            if valid_sections:
                                event_section_count[event.id] = len(
                                    valid_sections)

                                # 🆕 显示段落ID和标题
                                sections_preview_parts = []
                                for sid in valid_sections[:3]:
                                    section_heading = section_heading_map.get(
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
                        f"     过滤前: {len(found_entities)} 个Entity → {len(events)} 个Event → {len(paragraphs_data)} 个Section")
                    self.logger.info(
                        f"     过滤后: {len(entity_event_count)} 个有效Entity → {len(filtered_event_ids)} 个有效Event → {len(filtered_section_ids)} 个有效Section")
                    self.logger.info(
                        f"     过滤率: Entity={len(entity_event_count)/len(found_entities)*100:.1f}%, Event={len(filtered_event_ids)/len(events)*100:.1f}%, Section={len(filtered_section_ids)/len(paragraphs_data)*100:.1f}%")

                    self.logger.info("=" * 80)

                    # 🆕 构建 Step1 阶段的线索
                    from sag.modules.search.tracker import Tracker
                    tracker = Tracker(config)

                    # 准备数据映射
                    # 1. entity_id → entity_weight 映射
                    entity_weight_map = {key["key_id"]: key.get(
                        "weight", 0.0) for key in key_final}

                    # 2. section_id → section_data 映射（包含 score）
                    section_data_map = {}
                    for content in content_results:
                        section_data_map[content.section_id] = {
                            "section_id": content.section_id,
                            "id": content.section_id,
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

                    # B. 构建 Event → Section 线索
                    event_section_clue_count = 0
                    for event in events:
                        if event.id in filtered_event_ids:
                            # 遍历这个事项引用的所有段落
                            for section_id in (event.references or []):
                                if section_id in filtered_section_ids:
                                    # 获取段落数据
                                    section_data = section_data_map.get(
                                        section_id)
                                    if section_data:
                                        # 构建节点
                                        # 🆕 使用 tracker 实例方法，指定召回方式为 "entity"（因为是通过entity找到的event）
                                        event_node = tracker.get_or_create_event_node(
                                            event, "rerank", recall_method="entity")
                                        section_node = Tracker.build_section_node(
                                            section_data)

                                        # 添加线索（置信度用段落的余弦相似度）
                                        tracker.add_clue(
                                            stage="rerank",
                                            from_node=event_node,
                                            to_node=section_node,
                                            confidence=section_data["score"],
                                            relation="段落召回",
                                            metadata={
                                                "method": "section_recall",
                                                "section_score": section_data["score"],
                                                "step": "step1"
                                            }
                                        )
                                        event_section_clue_count += 1

                    self.logger.info("=" * 80)
                    self.logger.info(
                        f"🔗 [Step1 线索构建] Entity→Event={entity_event_clue_count}条, "
                        f"Event→Section={event_section_clue_count}条"
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
        k: int = 20,
        query_vector: Optional[List[float]] = None,  # 可选的查询向量
        config: Optional[SearchConfig] = None  # 添加config参数用于缓存和阈值
    ) -> List[Dict[str, Any]]:
        """
        步骤2: query找content（语义匹配）
        通过向量相似度在向量数据库找到原文块[content-query-related]，计算余弦相似度作为得分

        Args:
            query: 查询文本
            source_id: 数据源ID
            k: 返回结果数量
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

            # 4. 为每个段落添加得分信息并创建 ContentSearchResult 对象
            content_results = []

            # 添加详细得分日志
            self.logger.info("=" * 80)
            self.logger.info("步骤2 得分详情（纯余弦相似度）：")
            self.logger.info("-" * 80)

            for idx, paragraph in enumerate(similar_paragraphs, start=1):
                section_id = paragraph.get("section_id")
                cosine_score = cosine_scores.get(section_id, 0.0)

                # 详细日志：显示每个段落的得分
                heading_preview = paragraph.get("heading", "")[:40]
                self.logger.info(
                    f"段落 {section_id[:8]}... | "
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

                result = ContentSearchResult(
                    search_type=f"embedding-{idx}",  # 添加编号
                    # 使用第一个source_id作为主source_id
                    source_id=source_config_ids[0] if source_config_ids else "",
                    article_id=paragraph.get("article_id", ""),
                    section_id=section_id,
                    rank=paragraph.get("rank", 0),
                    heading=paragraph.get("heading", ""),
                    content=paragraph.get("content", ""),
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
                                f"段落 {result.section_id[:8]}... | "
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
            paragraphs: 段落列表

        Returns:
            余弦相似度得分字典 {section_id: score}
        """
        try:
            if not paragraphs:
                return {}

            cosine_scores = {}

            for paragraph in paragraphs:
                section_id = paragraph.get("section_id")

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
                        self.logger.warning(f"段落 {section_id} 内容为空且无预存向量，跳过")
                        continue

                    content_vector = await self.processor.generate_embedding(content)

                # 计算余弦相似度
                similarity = await self._cosine_similarity(query_vector, content_vector)
                cosine_scores[section_id] = similarity

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

    async def _tokenize_text(self, text: str) -> List[str]:
        """
        使用jieba进行中文分词

        Args:
            text: 输入文本

        Returns:
            分词列表
        """
        if not text or not text.strip():
            return []

        # 转换为小写并移除标点符号
        text = re.sub(r'[^\w\s\u4e00-\u9fff]', ' ', text.lower())

        # 使用jieba分词（延迟导入）
        try:
            import jieba
            tokens = list(jieba.cut(text))
        except ImportError:
            # 如果jieba未安装，使用简单的空格分词
            self.logger.warning("jieba未安装，使用简单分词")
            tokens = text.split()

        # 只过滤空白token，保留单字符（中文单字也可能有意义）
        tokens = [t.strip() for t in tokens if t.strip()]

        return tokens

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

    def _calculate_weighted_similarity(
        self,
        query_vector: List[float],
        title_vectors: List[Optional[List[float]]],
        content_vectors: List[Optional[List[float]]],
        title_weight: float = 0.2,
        content_weight: float = 0.8
    ) -> np.ndarray:
        """
        计算加权相似度：query分别与title和content批量计算相似度，然后按权重合并

        Args:
            query_vector: 查询向量
            title_vectors: 标题向量列表（可能包含None）
            content_vectors: 内容向量列表（可能包含None）
            title_weight: 标题相似度权重，默认0.2
            content_weight: 内容相似度权重，默认0.8

        Returns:
            加权相似度数组
        """
        try:
            num_items = len(title_vectors)

            # 初始化相似度数组
            title_similarities = np.zeros(num_items)
            content_similarities = np.zeros(num_items)

            # 1. 批量计算标题相似度
            # 收集所有非None的标题向量及其索引
            valid_title_indices = []
            valid_title_vectors = []
            for i, vec in enumerate(title_vectors):
                if vec is not None:
                    valid_title_indices.append(i)
                    valid_title_vectors.append(vec)

            # 批量计算所有有效标题向量的相似度
            if valid_title_vectors:
                batch_title_sims = self._batch_cosine_similarity(
                    query_vector,
                    valid_title_vectors
                )
                # 将结果填回对应的索引位置
                for idx, sim in zip(valid_title_indices, batch_title_sims):
                    title_similarities[idx] = sim

            # 2. 批量计算内容相似度
            # 收集所有非None的内容向量及其索引
            valid_content_indices = []
            valid_content_vectors = []
            for i, vec in enumerate(content_vectors):
                if vec is not None:
                    valid_content_indices.append(i)
                    valid_content_vectors.append(vec)

            # 批量计算所有有效内容向量的相似度
            if valid_content_vectors:
                batch_content_sims = self._batch_cosine_similarity(
                    query_vector,
                    valid_content_vectors
                )
                # 将结果填回对应的索引位置
                for idx, sim in zip(valid_content_indices, batch_content_sims):
                    content_similarities[idx] = sim

            # 3. 向量化加权合并
            weighted_similarities = (
                title_weight * title_similarities + content_weight * content_similarities
            )

            return weighted_similarities

        except Exception as e:
            self.logger.error(f"加权相似度计算错误: {e}")
            return np.zeros(num_items)

    async def _step3_merge_result(
        self,
        step1_results: List[Dict[str, Any]],
        step2_results: List[Dict[str, Any]],
        config: SearchConfig
    ) -> List[Dict[str, Any]]:
        """
        步骤3: 合并步骤1和步骤2的结果，并去重

        去重规则：如果 article_id + section_id 相同，只保留 SQL 搜索的结果（step1）

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

        # 1. 先记录 SQL 搜索结果中所有的 (article_id, section_id)
        sql_sections = set()
        for result in step1_results:
            article_id = result.get('article_id')
            section_id = result.get('section_id')
            sql_sections.add((article_id, section_id))

        self.logger.debug(f"SQL 搜索找到 {len(sql_sections)} 个唯一段落")

        # 2. 遍历 embedding 结果，过滤掉已经在 SQL 结果中的段落
        filtered_embedding_results = []
        duplicate_count = 0

        for result in step2_results:
            article_id = result.get('article_id')
            section_id = result.get('section_id')
            section_key = (article_id, section_id)

            if section_key in sql_sections:
                # 这个段落已经在 SQL 结果中，跳过
                duplicate_count += 1
                self.logger.debug(
                    f"段落 {section_id[:8]}... 在 SQL 和 Embedding 中都找到，保留 SQL 结果"
                )
            else:
                # 这是新段落，保留
                filtered_embedding_results.append(result)

        self.logger.info(
            f"去重统计: Embedding 结果中有 {duplicate_count} 个与 SQL 重复的段落已移除"
        )

        # 🆕 显示 Embedding 中进入下一轮的段落（补充作用）
        if filtered_embedding_results:
            self.logger.info("=" * 80)
            self.logger.info(
                f"【Step2 扩展补充】{len(filtered_embedding_results)} 个 Embedding 段落进入下一轮:")
            self.logger.info("-" * 80)
            for r in filtered_embedding_results[:10]:  # 最多显示10个
                section_id = r.get('section_id', '')
                heading = r.get('heading', '')
                score = r.get('score', 0.0)
                search_type = r.get('search_type', '')
                heading_preview = heading[:40] if heading else "无标题"
                self.logger.info(
                    f"  {section_id[:8]}... | Cosine={score:.4f} | Type={search_type} | {heading_preview}"
                )
            if len(filtered_embedding_results) > 10:
                self.logger.info(
                    f"  ... (还有 {len(filtered_embedding_results) - 10} 个)")
            self.logger.info("=" * 80)

            # 🆕 构建 query → section 线索（Step2 embedding召回的段落）
            from sag.modules.search.tracker import Tracker
            tracker = Tracker(config)

            query_section_clue_count = 0
            for r in filtered_embedding_results:
                section_id = r.get('section_id', '')
                if not section_id:
                    continue

                # 构建 query 节点
                query_node = Tracker.build_query_node(config)

                # 构建 section 节点
                section_node = Tracker.build_section_node({
                    "section_id": section_id,
                    "id": section_id,
                    "heading": r.get('heading', ''),
                    "content": r.get('content', ''),
                    "summary": "",
                    "section_type": r.get('search_type', '')
                })

                # 添加线索（置信度用余弦相似度）
                tracker.add_clue(
                    stage="rerank",
                    from_node=query_node,
                    to_node=section_node,
                    confidence=r.get('score', 0.0),
                    relation="语义召回",
                    metadata={
                        "method": "embedding",
                        "search_type": r.get('search_type', ''),
                        "step": "step2"
                    }
                )
                query_section_clue_count += 1

            self.logger.info(
                f"🔗 [Step2 线索构建] Query→Section={query_section_clue_count}条")

        # 3. 合并 SQL 结果和过滤后的 embedding 结果
        merged_list = step1_results + filtered_embedding_results

        # 4. 按得分降序排序
        merged_list.sort(key=lambda x: x['score'], reverse=True)

        self.logger.info(
            f"步骤3完成: step1={len(step1_results)}个, "
            f"step2={len(step2_results)}个(去重后保留{len(filtered_embedding_results)}个), "
            f"合并后={len(merged_list)}个"
        )

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
        - 三种关联关系：
          1. 事件关联（权重0.6）：共享相同event_id的段落之间有边
          2. 段落关联（权重0.2）：同一文章内相邻的段落之间有边
          3. 实体关联（权重0.2）：包含相同key_final实体的段落之间有边

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
                section_id = content.get('section_id', '')[:8]
                event_count = len(content.get('event_ids', []))

                self.logger.debug(
                    f"Rank {rank:2d} [idx={idx:3d}]: {search_type:12s} | "
                    f"weight={weight:.4f}, score={score:.4f} | "
                    f"events={event_count} | section={section_id}... | "
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
            section_to_idx = {c['section_id']: i for i,
                              c in enumerate(content_related)}

            # 2. 构建关系图（使用字典存储边和权重）
            # graph[i] = [(j, weight), ...] 表示从节点i指向节点j的边及其权重
            graph = defaultdict(list)

            # 2.1 事件关联（权重0.6）
            self.logger.info("构建事件关联边...")
            event_to_sections = defaultdict(list)
            for i, content in enumerate(content_related):
                for event_id in content.get('event_ids', []):
                    event_to_sections[event_id].append(i)

            event_edges_count = 0
            for event_id, sections in event_to_sections.items():
                if len(sections) > 1:
                    # 共享相同事件的段落之间互相关联
                    for i in sections:
                        for j in sections:
                            if i != j:
                                graph[i].append((j, 0.6))
                                event_edges_count += 1

            self.logger.info(f"事件关联: 添加了 {event_edges_count} 条边")

            # 2.2 段落关联（权重0.2）- 同一文章内相邻段落
            self.logger.info("构建段落关联边（相邻段落）...")
            article_sections = defaultdict(list)
            for i, content in enumerate(content_related):
                article_id = content['article_id']
                rank = content['rank']
                article_sections[article_id].append((i, rank))

            paragraph_edges_count = 0
            for article_id, sections in article_sections.items():
                # 按rank排序
                sections.sort(key=lambda x: x[1])
                # 相邻段落之间建立双向边
                for k in range(len(sections) - 1):
                    i, rank_i = sections[k]
                    j, rank_j = sections[k + 1]
                    if rank_j - rank_i == 1:  # 严格相邻
                        graph[i].append((j, 0.2))
                        graph[j].append((i, 0.2))
                        paragraph_edges_count += 2

            self.logger.info(f"段落关联: 添加了 {paragraph_edges_count} 条边")

            # 2.3 实体关联（权重0.2）- 包含相同key_final实体
            entity_edges_count = 0
            if key_final:
                self.logger.info("构建实体关联边...")
                # 为每个段落找到它包含的实体
                section_entities = defaultdict(set)
                for i, content in enumerate(content_related):
                    full_text = f"{content.get('heading', '')} {content.get('content', '')}"
                    for key in key_final:
                        key_name = key.get('name', '')
                        if key_name and key_name in full_text:
                            section_entities[i].add(key_name)

                # 共享实体的段落之间建立双向边
                for i in range(n):
                    for j in range(i + 1, n):
                        common_entities = section_entities[i] & section_entities[j]
                        if common_entities:
                            # 可以根据共享实体的数量调整权重，这里简化为固定0.2
                            graph[i].append((j, 0.2))
                            graph[j].append((i, 0.2))
                            entity_edges_count += 2

                self.logger.info(f"实体关联: 添加了 {entity_edges_count} 条边")
            else:
                self.logger.warning("未提供key_final，跳过实体关联边的构建")

            total_edges = event_edges_count + paragraph_edges_count + entity_edges_count
            self.logger.info(f"关系图构建完成: 节点数={n}, 总边数={total_edges}")

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

            # 展示度数最高的前5个节点
            top_out_degree = sorted(
                out_degrees.items(), key=lambda x: x[1], reverse=True)[:5]
            self.logger.debug("出度最高的5个节点：")
            for idx, degree in top_out_degree:
                content = content_related[idx]
                section_id = content.get('section_id', '')[:8]
                heading = content.get('heading', '')[:30]
                self.logger.debug(
                    f"  节点{idx:3d} (section={section_id}...): 出度={degree}, 标题={heading}")

            self.logger.debug("=" * 80)

            # 3. PageRank迭代计算
            self.logger.info(
                f"开始PageRank迭代（阻尼系数={damping}, 最大迭代={iterations}）...")

            for iteration in range(iterations):
                new_pagerank = np.zeros(n)

                for i in range(n):
                    # 计算指向节点i的所有节点的贡献
                    incoming_score = 0.0

                    for j in range(n):
                        # 检查是否有从j到i的边
                        edges_from_j = graph.get(j, [])
                        if not edges_from_j:
                            continue

                        for target, edge_weight in edges_from_j:
                            if target == i:
                                # 计算j对i的贡献
                                # 贡献 = PR(j) * edge_weight / sum(所有从j出发的edge_weight)
                                total_out_weight = sum(
                                    w for _, w in edges_from_j)
                                if total_out_weight > 0:
                                    incoming_score += pagerank[j] * \
                                        edge_weight / total_out_weight

                    # PageRank公式: PR(i) = (1-d)/n + d * incoming_score
                    new_pagerank[i] = (1 - damping) / n + \
                        damping * incoming_score

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

            # 创建 section_id 到原始索引的映射（用于对比排名变化）
            section_to_original_idx = {
                c['section_id']: i for i, c in enumerate(content_related)}

            # 创建权重排名映射
            weight_rank_map = {
                content[1]['section_id']: rank for rank, content in enumerate(weight_sorted, 1)}

            for rank, content in enumerate(sorted_contents[:10], 1):
                search_type = content.get('search_type', 'N/A')
                pagerank_val = content.get('pagerank', 0.0)
                weight = content.get('weight', 0.0)
                score = content.get('score', 0.0)
                heading = content.get('heading', '')[:40]
                section_id = content.get('section_id', '')
                event_count = len(content.get('event_ids', []))

                # 获取在权重排序中的排名
                weight_rank = weight_rank_map.get(section_id, -1)
                rank_change = weight_rank - rank if weight_rank > 0 else 0

                # 排名变化标记
                if rank_change > 0:
                    change_mark = f"↑{rank_change:+d}"  # 上升
                elif rank_change < 0:
                    change_mark = f"↓{rank_change:+d}"  # 下降
                else:
                    change_mark = " ━  "  # 不变

                original_idx = section_to_original_idx.get(section_id, -1)

                self.logger.debug(
                    f"Rank {rank:2d} [idx={original_idx:3d}] {change_mark:>5s} (was #{weight_rank:2d}): {search_type:12s} | "
                    f"PR={pagerank_val:.6f}, weight={weight:.4f}, score={score:.4f} | "
                    f"events={event_count} | section={section_id[:8]}... | "
                    f"{heading}"
                )

            self.logger.debug("=" * 80)
            self.logger.debug("【排序变化统计】：")

            # 统计排名变化
            rank_changes = []
            for rank, content in enumerate(sorted_contents, 1):
                section_id = content['section_id']
                weight_rank = weight_rank_map.get(section_id, -1)
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

    async def _step6_get_topn_of_contents(
        self,
        sorted_contents: List[Dict[str, Any]],
        top_k: int,
        source_config_ids: List[str],
        query_vector: List[float],
        config: Optional[SearchConfig] = None
    ) -> Tuple[List[SourceEvent], Dict[str, List[Dict]]]:
        """
        步骤6: 取Top-N段落并返回关联的事项列表

        处理流程：
        1. 取Top-k：从排序后的结果中取前 k 个段落
        2. 收集段落ID：收集所有段落的 section_id
        3. 反向查询事项：通过 SourceEvent.references 字段查找包含这些 section_id 的事项
        4. 获取事项向量：从 ES 中批量获取事项的向量数据
        5. 相似度计算：计算 query 与每个事项的余弦相似度
        6. 阈值过滤：使用 config.rerank.score_threshold 过滤低相似度事项
        7. 返回事项列表：保持段落 PageRank 顺序（事项首次出现的位置）

        注意：不再依赖 step1 的 event_ids，统一通过 references 字段反向查找

        Args:
            sorted_contents: 从step5排序后的段落列表（已按PageRank降序排序）
            top_k: 取前k个结果
            source_config_ids: 数据源ID列表
            query_vector: 查询向量
            config: 搜索配置（用于阈值过滤）

        Returns:
            Tuple[List[SourceEvent], Dict[str, List[Dict]]]:
                - events: 事项对象列表（保持段落 PageRank 顺序，已过滤和去重）
                - event_to_clues: 事项ID到实体列表的映射 {event_id: [entity1, entity2, ...]}
        """
        try:
            self.logger.info(
                f"步骤6开始: 从 {len(sorted_contents)} 个段落中取Top-{top_k}")

            # 1. 取Top-k结果
            topk_contents = sorted_contents[:top_k]
            self.logger.info(f"提取了Top-{top_k}段落（实际: {len(topk_contents)} 个）")

            # 2. 收集所有段落的 section_id
            section_ids = [content.get('section_id')
                           for content in topk_contents]
            self.logger.info(f"收集到 {len(section_ids)} 个段落ID")

            # 3. 通过 SourceEvent.references 反向查询关联的事项
            # 查询所有 references 字段包含这些 section_id 的事项
            if not section_ids:
                self.logger.warning("Top-k段落中没有有效的 section_id")
                return [], {}  # 返回空列表和空字典

            async with self.session_factory() as session:
                # 查询所有事项，然后在 Python 中过滤
                # 因为 references 是 JSON 字段（存储列表），无法直接使用数组操作符
                event_query = (
                    select(SourceEvent)
                    # 预加载关联关系
                    .options(selectinload(SourceEvent.event_associations))
                    .where(
                        and_(
                            SourceEvent.source_id.in_(source_config_ids),
                            # 排除 references 为 NULL 的事项
                            SourceEvent.references.isnot(None)
                        )
                    )
                )

                event_result = await session.execute(event_query)
                all_events = event_result.scalars().all()

                # DEBUG: 检查事项的 summary 字段
                empty_summary_count = sum(
                    1 for e in all_events if not e.summary or not e.summary.strip())
                if empty_summary_count > 0:
                    self.logger.warning(
                        f"发现 {empty_summary_count}/{len(all_events)} 个事项的 summary 字段为空"
                    )

                # 在 Python 中过滤：找出 references 与 section_ids 有交集的事项
                events = []
                for event in all_events:
                    if event.references and isinstance(event.references, list):
                        # 检查是否有交集
                        if any(section_id in event.references for section_id in section_ids):
                            events.append(event)

                self.logger.info(
                    f"从数据库查询到 {len(all_events)} 个事项，"
                    f"过滤后得到 {len(events)} 个关联的事项"
                )

            if not events:
                self.logger.warning("没有找到与段落关联的事项")
                return []

            # 4. 从 ES 批量获取事项的向量数据
            event_ids = [event.id for event in events]
            event_vectors_data = await self.event_repo.get_events_by_ids(event_ids)

            # 构建 event_id -> content_vector 的映射
            event_vector_map = {}
            for event_data in event_vectors_data:
                event_id = event_data.get('event_id')
                content_vector = event_data.get('content_vector')
                if event_id and content_vector:
                    event_vector_map[event_id] = content_vector

            self.logger.info(
                f"从ES获取到 {len(event_vector_map)} 个事项的向量数据"
            )

            # 5. 计算 query 与每个事项的余弦相似度
            event_similarity_map = {}  # event_id -> similarity_score

            for event in events:
                event_vector = event_vector_map.get(event.id)

                if event_vector:
                    # 计算余弦相似度
                    similarity = await self._cosine_similarity(query_vector, event_vector)
                    event_similarity_map[event.id] = similarity
                    self.logger.debug(
                        f"事项 {event.id[:8]}... | 相似度={similarity:.4f} | title='{event.title[:40]}'"
                    )
                else:
                    self.logger.warning(
                        f"事项 {event.id[:8]}... 在ES中未找到向量数据，跳过"
                    )

            self.logger.info(f"计算了 {len(event_similarity_map)} 个事项的相似度")

            # 🆕 日志1：记录每个段落找到了哪些事项（相似度过滤前）
            self.logger.info("=" * 80)
            self.logger.info("【Step6 日志1】每个段落找到的事项（相似度过滤前）：")
            self.logger.info("-" * 80)

            section_to_events_before = {}  # section_id -> [event_info]
            for event in events:
                if event.references:
                    for section_id in event.references:
                        if section_id in section_ids:
                            if section_id not in section_to_events_before:
                                section_to_events_before[section_id] = []
                            section_to_events_before[section_id].append({
                                'id': event.id,
                                'title': event.title,
                                'similarity': event_similarity_map.get(event.id, 0.0)
                            })

            for idx, content in enumerate(topk_contents[:10], 1):
                section_id = content.get('section_id')
                heading = content.get('heading', '')[:40]
                found_events = section_to_events_before.get(section_id, [])

                if found_events:
                    event_preview = ', '.join([
                        f"{e['id'][:8]}...(sim={e['similarity']:.2f})"
                        for e in found_events[:3]
                    ])
                    if len(found_events) > 3:
                        event_preview += f' ... (共{len(found_events)}个)'
                    self.logger.info(
                        f"  段落{idx} {section_id[:8]}... ('{heading}') "
                        f"→ {len(found_events)} 个事项: {event_preview}"
                    )
                else:
                    self.logger.info(
                        f"  段落{idx} {section_id[:8]}... ('{heading}') → 0 个事项"
                    )

            if len(topk_contents) > 10:
                self.logger.info(
                    f"  ... (还有 {len(topk_contents) - 10} 个段落未显示)")

            self.logger.info("-" * 80)
            self.logger.info(
                f"  统计: {len(topk_contents)} 个段落 → {len(events)} 个事项")
            self.logger.info("=" * 80)

            # 6. 使用 config.rerank.score_threshold 过滤低相似度事项
            original_count = len(event_similarity_map)
            if config and config.rerank.score_threshold:
                # 只保留相似度 >= threshold 的事项
                filtered_event_ids = {
                    event_id for event_id, score in event_similarity_map.items()
                    if score >= config.rerank.score_threshold
                }

                if len(filtered_event_ids) < original_count:
                    self.logger.info(
                        f"相似度过滤: {original_count} -> {len(filtered_event_ids)} 个事项 "
                        f"(阈值={config.rerank.score_threshold:.2f})"
                    )
            else:
                self.logger.warning("未设置阈值或config为空，跳过相似度过滤")
                filtered_event_ids = set(event_similarity_map.keys())

            # 🆕 日志2：记录经过相似度过滤后，每个段落还有哪些事项
            self.logger.info("=" * 80)
            self.logger.info("【Step6 日志2】经过相似度过滤后，每个段落保留的事项：")
            self.logger.info("-" * 80)

            section_to_events_after = {}  # section_id -> [event_info]
            for event in events:
                if event.id in filtered_event_ids and event.references:
                    for section_id in event.references:
                        if section_id in section_ids:
                            if section_id not in section_to_events_after:
                                section_to_events_after[section_id] = []
                            section_to_events_after[section_id].append({
                                'id': event.id,
                                'title': event.title,
                                'similarity': event_similarity_map.get(event.id, 0.0)
                            })

            for idx, content in enumerate(topk_contents[:10], 1):
                section_id = content.get('section_id')
                heading = content.get('heading', '')[:40]
                before_count = len(
                    section_to_events_before.get(section_id, []))
                after_events = section_to_events_after.get(section_id, [])
                after_count = len(after_events)

                if after_events:
                    event_preview = ', '.join([
                        f"{e['id'][:8]}...(sim={e['similarity']:.2f})"
                        for e in after_events[:3]
                    ])
                    if len(after_events) > 3:
                        event_preview += f' ... (共{after_count}个)'
                    self.logger.info(
                        f"  段落{idx} {section_id[:8]}... ('{heading}') "
                        f"→ 过滤: {before_count} → {after_count} 个事项: {event_preview}"
                    )
                else:
                    self.logger.info(
                        f"  段落{idx} {section_id[:8]}... ('{heading}') "
                        f"→ 过滤: {before_count} → 0 个事项 (全部被过滤)"
                    )

            if len(topk_contents) > 10:
                self.logger.info(
                    f"  ... (还有 {len(topk_contents) - 10} 个段落未显示)")

            self.logger.info("-" * 80)
            self.logger.info(
                f"  统计: {len(topk_contents)} 个段落 → "
                f"{len(events)} 个事项 (过滤前) → {len(filtered_event_ids)} 个事项 (过滤后)"
            )
            self.logger.info("=" * 80)

            # 7. 按段落 PageRank 顺序返回事项（保持事项首次出现的位置）
            # 为每个事项计算它第一次被引用的位置（段落索引）
            # 同时收集每个事项的 clues
            event_first_appearance = {}  # event_id -> 第一次出现的段落索引
            event_to_clues = {}  # event_id -> clues 列表

            for idx, content in enumerate(topk_contents):
                section_id = content.get('section_id')
                content_clues = content.get('clues', [])  # 获取段落的 clues

                # 遍历所有事项，检查哪些事项引用了这个段落
                for event in events:
                    # 只处理通过相似度过滤的事项
                    if event.id in filtered_event_ids:
                        if event.references and section_id in event.references:
                            # 如果这个事项还没有记录，记录它第一次出现的位置
                            if event.id not in event_first_appearance:
                                event_first_appearance[event.id] = idx
                                self.logger.debug(
                                    f"事项 {event.id[:8]}... 在第 {idx} 个段落中首次出现"
                                )

                            # 汇总该段落的 clues 到事项
                            if event.id not in event_to_clues:
                                event_to_clues[event.id] = []

                            # 将段落的 clues 添加到事项（去重）
                            for clue in content_clues:
                                # 简单去重：检查是否已存在相同的 clue
                                clue_key = (clue.get('type'), clue.get('name'))
                                existing_keys = [(c.get('type'), c.get('name'))
                                                 for c in event_to_clues[event.id]]
                                if clue_key not in existing_keys:
                                    event_to_clues[event.id].append(clue)

            # 按照第一次出现的位置排序事项（不附加 clues）
            result_events = []
            for event in sorted(events, key=lambda e: event_first_appearance.get(e.id, float('inf'))):
                # 只保留通过相似度过滤且有出现位置的事项
                if event.id in event_first_appearance:
                    result_events.append(event)

            # 输出 event_to_clues 映射表统计信息
            clues_stats = [len(clues) for clues in event_to_clues.values()]
            if clues_stats:
                self.logger.info(
                    f"✅ event_to_clues统计: 平均={sum(clues_stats)/len(clues_stats):.1f}个实体/事项, "
                    f"最多={max(clues_stats)}个, 最少={min(clues_stats)}个"
                )

            self.logger.info(
                f"步骤6完成: 返回 {len(result_events)} 个事项（保持段落 PageRank 顺序，已过滤）"
            )

            # 注意：段落→事项的线索构建已移至日志3之后（避免重复构建）

            # 🆕 日志3：记录经过top-k后，最终返回的段落和事项映射
            self.logger.info("=" * 80)
            self.logger.info(
                f"【Step6 日志3】经过top-k后 (max_results={config.rerank.max_results})，最终段落和事项映射：")
            self.logger.info("-" * 80)

            # 最终返回的事项ID集合（经过max_results限制）
            final_result_event_ids = {
                e.id for e in result_events[:config.rerank.max_results]}

            # 构建最终的段落到事项映射
            section_to_events_final = {}  # section_id -> [event_info]
            for event in result_events[:config.rerank.max_results]:
                if event.references:
                    for section_id in event.references:
                        if section_id in section_ids:
                            if section_id not in section_to_events_final:
                                section_to_events_final[section_id] = []
                            section_to_events_final[section_id].append({
                                'id': event.id,
                                'title': event.title,
                                'similarity': event_similarity_map.get(event.id, 0.0),
                                'first_pos': event_first_appearance.get(event.id, -1)
                            })

            # 显示每个段落最终关联的事项
            displayed_sections = 0
            for idx, content in enumerate(topk_contents, 1):
                section_id = content.get('section_id')
                heading = content.get('heading', '')[:40]
                pagerank = content.get('pagerank', 0.0)

                final_events = section_to_events_final.get(section_id, [])

                if final_events or displayed_sections < 15:  # 显示所有有事项的段落，或前15个
                    # 按first_pos排序
                    final_events.sort(key=lambda e: e['first_pos'])

                    if final_events:
                        # 显示段落基本信息
                        self.logger.info(
                            f"  段落{idx} (PR={pagerank:.4f}) {section_id[:8]}... ('{heading}') "
                            f"→ {len(final_events)} 个最终事项:"
                        )

                        # 显示每个事项的详细信息（缩进显示）
                        for e in final_events[:5]:  # 最多显示前5个
                            event_title_preview = e['title'][:50] if e['title'] else "无标题"
                            self.logger.info(
                                f"     ├─ {e['id'][:8]}... | pos={e['first_pos']}, sim={e['similarity']:.3f} | '{event_title_preview}'"
                            )

                        if len(final_events) > 5:
                            self.logger.info(
                                f"     └─ ... (还有 {len(final_events) - 5} 个事项)")
                    else:
                        self.logger.info(
                            f"  段落{idx} (PR={pagerank:.4f}) {section_id[:8]}... ('{heading}') "
                            f"→ 0 个最终事项"
                        )
                    displayed_sections += 1

            if displayed_sections < len(topk_contents):
                self.logger.info(
                    f"  ... (还有 {len(topk_contents) - displayed_sections} 个段落未显示)")

            self.logger.info("-" * 80)
            self.logger.info(
                f"  统计: Top-{len(topk_contents)} 段落中有 {len(section_to_events_final)} 个段落 "
                f"关联了 {len(final_result_event_ids)} 个最终事项"
            )
            self.logger.info("=" * 80)

            # 🆕 构建段落→事项的信息溯源线索（基于日志3的最终映射）
            # 🔧 修复：创建统一的 Tracker 实例，供后续所有线索构建共用，避免重复创建事项节点
            from sag.modules.search.tracker import Tracker
            tracker = Tracker(config)  # 统一的 tracker 实例

            if config and section_to_events_final:
                self.logger.info("")
                self.logger.info("🔗 【Step6 线索构建】构建段落→事项的信息溯源...")

                section_event_clue_count = 0
                clue_details = []  # 用于记录详细的线索信息

                # 遍历所有有最终事项的段落
                for section_id, events_list in section_to_events_final.items():
                    # 找到对应的段落内容
                    section_content = None
                    for content in topk_contents:
                        if content.get('section_id') == section_id:
                            section_content = content
                            break

                    if not section_content:
                        continue

                    # 构建段落节点数据
                    section_data = {
                        "section_id": section_id,
                        "id": section_id,
                        "heading": section_content.get('heading', ''),
                        "content": section_content.get('content', ''),
                        "summary": "",
                        "section_type": section_content.get('search_type', ''),
                        "score": section_content.get('score', 0.0),
                        "pagerank": section_content.get('pagerank', 0.0)
                    }

                    section_node = Tracker.build_section_node(section_data)

                    # 为这个段落的每个事项构建线索
                    for event_info in events_list:
                        event_id = event_info['id']

                        # 找到对应的事项对象
                        event_obj = next(
                            (e for e in result_events[:config.rerank.max_results] if e.id == event_id), None)
                        if not event_obj:
                            continue

                        # 🆕 使用 tracker 实例方法，指定召回方式为 "section"
                        event_node = tracker.get_or_create_event_node(
                            event_obj, "rerank", recall_method="section")

                        # 添加线索（前端根据 stage="rerank" + relation="段落召回" 渲染为紫色）
                        tracker.add_clue(
                            stage="rerank",
                            from_node=section_node,
                            to_node=event_node,
                            # 使用事项的相似度作为置信度
                            confidence=event_info['similarity'],
                            relation="段落召回",
                            metadata={
                                "method": "section_to_event",
                                "event_similarity": event_info['similarity'],
                                "event_first_pos": event_info['first_pos'],
                                "section_pagerank": section_data['pagerank'],
                                "section_score": section_data['score'],
                                "step": "step6"
                            }
                        )
                        section_event_clue_count += 1

                        # 记录线索详情（用于日志）
                        clue_details.append({
                            'section_id': section_id[:8],
                            'section_heading': section_data['heading'][:30],
                            'event_id': event_id[:8],
                            'event_title': event_info['title'][:30],
                            'similarity': event_info['similarity'],
                            'pagerank': section_data['pagerank']
                        })

                self.logger.info(f"✅ 成功构建 {section_event_clue_count} 条段落→事项线索")

                # 显示部分线索详情
                if clue_details:
                    self.logger.info("")
                    self.logger.info(f"📋 线索详情（前10条）：")
                    for idx, clue in enumerate(clue_details[:10], 1):
                        self.logger.info(
                            f"  {idx}. {clue['section_id']}... ('{clue['section_heading']}', PR={clue['pagerank']:.3f}) "
                            f"→ {clue['event_id']}... ('{clue['event_title']}', sim={clue['similarity']:.3f})"
                        )
                    if len(clue_details) > 10:
                        self.logger.info(
                            f"  ... (还有 {len(clue_details) - 10} 条线索)")

                self.logger.info("")
                self.logger.info("=" * 80)

            # === 🆕 生成最终线索 (display_level="final") ===
            # 为最终返回的事项生成 final 线索，前端可据此反推完整推理路径
            # 🔧 修复：复用上面创建的 tracker 实例，避免重复创建事项节点
            if config and result_events[:config.rerank.max_results]:
                self.logger.info("")
                self.logger.info(
                    "🎯 [Rerank Final] 生成最终线索 (display_level=final)")

                # 🔧 不再创建新的 Tracker，直接使用上面的 tracker 实例
                final_events = result_events[:config.rerank.max_results]
                final_clue_count = 0

                # 为每个最终事项生成 section → event 的 final 线索
                for event in final_events:
                    # 找到该事项首次出现的段落位置
                    first_pos = event_first_appearance.get(event.id)
                    if first_pos is not None and first_pos < len(topk_contents):
                        section_content = topk_contents[first_pos]
                        section_id = section_content.get('section_id')

                        # 构建段落节点
                        section_data = {
                            "section_id": section_id,
                            "id": section_id,
                            "heading": section_content.get('heading', ''),
                            "content": section_content.get('content', ''),
                            "summary": "",
                            "section_type": section_content.get('search_type', ''),
                            "score": section_content.get('score', 0.0),
                            "pagerank": section_content.get('pagerank', 0.0)
                        }

                        section_node = Tracker.build_section_node(section_data)
                        # 🆕 使用 tracker 实例方法，指定召回方式为 "section"（final 线索也使用 section 召回）
                        event_node = tracker.get_or_create_event_node(
                            event, "rerank", recall_method="section")

                        # 生成 section → event final 线索
                        tracker.add_clue(
                            stage="rerank",
                            from_node=section_node,
                            to_node=event_node,
                            confidence=event_similarity_map.get(event.id, 0.0),
                            relation="最终事项",
                            display_level="final",  # 🆕 标记为最终结果
                            metadata={
                                "method": "final_result",
                                "step": "step6",
                                "similarity": event_similarity_map.get(event.id, 0.0),
                                "first_position": first_pos,
                                "pagerank": section_data['pagerank'],
                                "section_score": section_data['score']
                            }
                        )
                        final_clue_count += 1

                        self.logger.debug(
                            f"  Final: {section_id[:8]}... ('{section_data['heading'][:30]}', PR={section_data['pagerank']:.3f}) "
                            f"→ {event.id[:8]}... ('{event.title[:30]}', sim={event_similarity_map.get(event.id, 0.0):.3f})"
                        )
                    else:
                        self.logger.warning(
                            f"⚠️ [Rerank Final] 事项 {event.id[:8]}... 找不到首次出现的段落位置"
                        )

                self.logger.info(
                    f"✅ [Rerank Final] 生成了 {final_clue_count} 条最终线索 (section→event)"
                )
                self.logger.info(
                    f"✅ [Rerank Final] 前端可根据这些 final 线索反推完整推理路径 (query→entity→section→event)"
                )
                self.logger.info("")

            return result_events[:config.rerank.max_results], event_to_clues

        except Exception as e:
            self.logger.error(f"步骤6执行失败: {e}", exc_info=True)
            return [], {}  # 失败时返回空列表和空字典

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

    def _build_rerank_clues(
        self,
        config: SearchConfig,
        key_final: List[Dict[str, Any]],
        events: List[SourceEvent],
        event_to_clues: Dict[str, List[Dict]],
        sorted_contents: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        构建Rerank阶段的线索（entity → section → event）

        🆕 修改：不再使用单条entity→event线索，改为拆分成两条：
        1. entity → section
        2. section → event

        这样确保中间节点（section）不会被省略，前端可以构建完整知识图谱

        Args:
            config: 搜索配置
            key_final: 最终的key列表
            events: 最终返回的事项列表
            event_to_clues: 事项ID到实体列表的映射
            sorted_contents: 排序后的段落列表

        Returns:
            Rerank阶段的线索列表（兼容性保留，实际线索已追加到config.all_clues）
        """
        # 🔕 已注释：暂时禁用 Rerank 阶段的线索构建
        # ============================================================
        # # 🆕 创建线索构建器
        # tracker = Tracker(config)
        #
        # # 创建key_id到key对象的映射，方便查找权重等信息
        # key_map = {key["key_id"]: key for key in key_final}
        #
        # # 创建event_id集合，只处理最终返回的事项
        # final_event_ids = {event.id for event in events}
        #
        # # 统计信息
        # query_section_clues = 0  # 🆕 query → section 线索计数
        # entity_section_clues = 0
        # section_event_clues = 0
        #
        # # 只处理Top-N的sections（与最终返回的events相关）
        # top_n = config.rerank.max_results * 2  # 与step6保持一致
        # for content in sorted_contents[:top_n]:
        #     section_id = content.get("section_id")
        #     section_heading = content.get("heading", "")
        #     section_weight = content.get("weight", 0.0)
        #     section_score = content.get("score", 0.0)  # 🆕 获取段落的余弦相似度
        #
        #     # 构建section字典（用于标准节点构建）
        #     section_dict = {
        #         "section_id": section_id,
        #         "id": section_id,
        #         "heading": section_heading,
        #         "content": content.get("content", ""),
        #         "summary": "",  # PageRank没有summary字段
        #         "section_type": content.get("search_type", "")
        #     }
        #
        #     # 使用标准节点构建器
        #     section_node = Tracker.build_section_node(section_dict)
        #
        #     # 1. 构建 query/entity → section 线索
        #     # content.clues 包含了导致找到这个section的所有query或entities
        #     content_clues = content.get("clues", [])
        #     for clue in content_clues:
        #         clue_type = clue.get("type")
        #
        #         # 🆕 处理 query 类型的 clue（Step2 直接召回的段落）
        #         if clue_type == "query":
        #             # 使用 Tracker.build_query_node 生成标准 query 节点（含确定性 ID）
        #             query_node = Tracker.build_query_node(config)
        #
        #             # 添加 query → section 线索
        #             tracker.add_clue(
        #                 stage="rerank",
        #                 from_node=query_node,
        #                 to_node=section_node,
        #                 confidence=section_score,  # 使用段落的余弦相似度
        #                 relation="语义召回",
        #                 metadata={
        #                     "method": "embedding"
        #                 }
        #             )
        #             query_section_clues += 1
        #             continue
        #
        #         # 处理 entity 类型的 clue（原有逻辑）
        #         # 兼容不同的ID字段名
        #         entity_id = clue.get("id") or clue.get("key_id")
        #         if not entity_id or entity_id not in key_map:
        #             continue
        #
        #         entity_info = key_map[entity_id]
        #
        #         # 构建完整的实体字典（用于标准节点构建）
        #         entity_dict = {
        #             "key_id": entity_id,
        #             "id": entity_id,
        #             "name": entity_info.get("name", ""),
        #             "type": entity_info.get("type", "unknown"),
        #             "description": entity_info.get("description", "")
        #         }
        #
        #         # 使用标准节点构建器
        #         entity_node = Tracker.build_entity_node(entity_dict)
        #
        #         # 添加 entity → section 线索（使用新的统一接口）
        #         tracker.add_clue(
        #             stage="rerank",
        #             from_node=entity_node,
        #             to_node=section_node,
        #             confidence=section_score,  # 🆕 使用余弦相似度作为置信度
        #             relation="内容重排",
        #             metadata={
        #                 "method": "pagerank",
        #                 "entity_weight": entity_info.get("weight", 0.0),
        #                 "section_weight": section_weight,  # 🆕 保留原始 PageRank 权重
        #                 "step": "step1"
        #             }
        #         )
        #         entity_section_clues += 1
        #
        #     # 2. 构建 section → event 线索
        #     # content.event_ids 包含了引用这个section的所有events
        #     content_event_ids = content.get("event_ids", [])
        #     for event_id in content_event_ids:
        #         # 只处理最终返回的events
        #         if event_id not in final_event_ids:
        #             continue
        #
        #         # 找到对应的event对象
        #         event_obj = next((e for e in events if e.id == event_id), None)
        #         if not event_obj:
        #             continue
        #
        #         # 使用标准节点构建器
        #         event_node = Tracker.build_event_node(event_obj)
        #
        #         # 获取event的PageRank分数作为置信度
        #         confidence = getattr(event_obj, 'pagerank_score', None)
        #         if confidence is None or confidence == 0.0:
        #             confidence = getattr(event_obj, 'similarity_score', 0.0)
        #
        #         # 添加 section → event 线索（使用新的统一接口）
        #         tracker.add_clue(
        #             stage="rerank",
        #             from_node=section_node,
        #             to_node=event_node,
        #             confidence=confidence,
        #             relation="内容重排",
        #             metadata={
        #                 "method": "pagerank",
        #                 "pagerank_score": getattr(event_obj, 'pagerank_score', None),
        #                 "similarity_score": getattr(event_obj, 'similarity_score', None)
        #             }
        #         )
        #         section_event_clues += 1
        #
        # self.logger.info(
        #     f"🔍 [Rerank诊断] 线索统计: "
        #     f"query→section={query_section_clues}条, "
        #     f"entity→section={entity_section_clues}条, "
        #     f"section→event={section_event_clues}条"
        # )
        # ============================================================

        self.logger.info("🔕 [Rerank] 线索构建已禁用")

        # 返回空列表（兼容性保留，实际线索已通过tracker追加到config.all_clues）
        return []
