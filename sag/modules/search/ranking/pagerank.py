"""
搜索 Rerank 模块 - 事项级 PageRank 实现

实现6步骤的查找最重要事项的方法：
1. key找event：根据[key-final]从SQL中提取相关事项，计算事项向量与query的余弦相似度作为得分
2. query找event：通过向量相似度（KNN+余弦相似度）在向量数据库找到相似事项
3. 合并event去重：优先保留step1结果（实体召回的事项）
4. 计算事项权重向量：使用公式 weight = 0.5*相似度 + ln(1 + Σ(key权重 × ln(1+出现次数) / step))
5. 构建事项关系图 + PageRank排序：
   - 实体关联：方向性权重，事项i→事项j的权重 = key权重 × key在事项j的出现次数
   - 类别关联：方向性权重，基于目标事项的内容丰富度
6. 选择Top-N事项：保留溯源信息，按PageRank得分排序

返回格式：
Dict[str, Any]: 包含以下字段的字典：
    - events (List[SourceEvent]): 事项对象列表（按 PageRank 顺序排列）
    - clues (Dict): 召回线索信息
        - origin_query (str): 原始查询（重写前）
        - final_query (str): LLM重写后的查询（重写后）
        - query_entities (List[Dict]): 查询召回的实体列表（key_id改为id）
        - recall_entities (List[Dict]): 召回的实体列表（key_id改为id，过滤掉query_entities中的值）

关键特性：
- 方向性PageRank：能区分事项重要性，重要事项投票权重更大
- 内容感知：权重基于key在事项内容中的实际出现频次
- 多维度关联：实体关联 + 类别关联，构建完整的关系图

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
from sqlalchemy.orm import selectinload, joinedload

from sag.core.storage.elasticsearch import get_es_client
from sag.core.storage.repositories.source_chunk_repository import SourceChunkRepository
from sag.core.storage.repositories.event_repository import EventVectorRepository
from sag.db import SourceEvent, Entity, EventEntity, ArticleSection, Article, SourceConfig, get_session_factory
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
    source_config_id: str        # 数据源ID (UUID)
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
            "source_config_id": self.source_config_id,
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
            source_config_id=data["source_config_id"],
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
        Rerank 搜索主方法（事项级 PageRank）

        整合步骤1-6，统一进行query向量化，避免重复计算

        步骤流程：
          1. key找event（向量相似度过滤）：基于实体关联找到相关事项
          2. query找event（向量相似度过滤）：基于语义相似度找到相关事项
          3. 合并event去重（优先保留step1结果）：实体召回的事项优先级更高
          4. 计算事项权重向量：结合相似度和实体权重计算初始权重
          5. 构建事项关系图 + 方向性PageRank排序：
             - 实体关联：方向性权重，key权重 × 目标事项出现次数
             - 类别关联：方向性权重，基于目标事项内容丰富度
          6. 选择Top-N事项（保留溯源）：按PageRank得分排序并保留线索

        Args:
            key_final: 从Recall返回的关键实体列表
            config: Rerank搜索配置

        Returns:
            Dict[str, Any]: 包含以下字段的字典：
                - events (List[SourceEvent]): 事项对象列表（按 PageRank 顺序排列）
                - clues (Dict): 召回线索信息
                    - origin_query (str): 原始查询（重写前）
                    - final_query (str): LLM重写后的查询（重写后）
                    - query_entities (List[Dict]): 查询召回的实体列表（key_id改为id）
                    - recall_entities (List[Dict]): 召回的实体列表（key_id改为id，过滤掉query_entities中的值）
        """
        try:
            # 记录总体开始时间
            overall_start = time.perf_counter()

            self.logger.info("=" * 80)
            self.logger.info(f"【事项级 PageRank】Rerank搜索开始")
            self.logger.info(f"Query: '{config.query}'")
            self.logger.info(f"Source IDs: {config.get_source_config_ids()}")
            self.logger.info("=" * 80)

            # 初始化 Tracker
            from sag.modules.search.tracker import Tracker
            tracker = Tracker(config)

            # 统一进行query向量化（避免在step1和step2中重复计算）
            vector_start = time.perf_counter()
            query_vector = await self._generate_query_vector(config.query, config)
            vector_time = time.perf_counter() - vector_start
            if config.has_query_embedding:
                self.logger.info(
                    f"✓ 使用缓存的query向量，维度: {len(query_vector)}, 耗时: {vector_time:.3f}秒")
            else:
                self.logger.info(
                    f"✓ 查询向量生成成功，维度: {len(query_vector)}, 耗时: {vector_time:.3f}秒")

            # 用于记录各步骤耗时
            step_times = {}

            # 步骤1和2可以并行执行（互不依赖）
            self.logger.info("\n" + "=" * 80)
            self.logger.info("【Step1 & Step2】并行执行...")
            self.logger.info("=" * 80)
            parallel_start = time.perf_counter()

            # 并行执行 step1 和 step2（事项级）
            step1_task = self._step1_keys_to_events(
                key_final=key_final,
                query=config.query,
                source_config_ids=config.get_source_config_ids(),
                query_vector=query_vector,
                config=config
            )

            step2_task = self._step2_query_to_events(
                query=config.query,
                source_config_ids=config.get_source_config_ids(),
                k=config.rerank.max_query_recall_results,  # 使用max_query_recall_results控制召回数量
                query_vector=query_vector,
                config=config
            )

            # 等待两个任务都完成
            step1_events, step2_events = await asyncio.gather(step1_task, step2_task)

            parallel_time = time.perf_counter() - parallel_start
            step_times['Step1&2_并行执行'] = parallel_time

            self.logger.info(
                f"✓ Step1&2 并行完成: "
                f"Step1={len(step1_events)}个事项, "
                f"Step2={len(step2_events)}个事项, "
                f"耗时: {parallel_time:.3f}秒"
            )

            # 步骤3: 合并事项去重
            step3_start = time.perf_counter()
            merged_events = await self._step3_merge_events(
                step1_events, step2_events)
            step3_time = time.perf_counter() - step3_start
            step_times['Step3_合并去重'] = step3_time
            self.logger.info(
                f"✓ Step3 完成: 合并后 {len(merged_events)} 个事项, 耗时: {step3_time:.3f}秒")

            # 步骤4: 计算事项权重
            step4_start = time.perf_counter()
            event_weights = await self._step4_calculate_event_weights(merged_events)
            step4_time = time.perf_counter() - step4_start
            step_times['Step4_权重计算'] = step4_time
            self.logger.info(
                f"✓ Step4 完成: 计算了 {len(event_weights)} 个事项的权重, 耗时: {step4_time:.3f}秒")

            # 步骤5: 构建事项关系图 + PageRank排序
            step5_start = time.perf_counter()
            sorted_events = await self._step5_build_event_graph_and_pagerank(
                merged_events=merged_events,
                event_weights=event_weights,
                key_final=key_final,  # 传入key_final以获取实体权重
                damping=0.85,
                max_iterations=100
            )
            step5_time = time.perf_counter() - step5_start
            step_times['Step5_PageRank排序'] = step5_time
            self.logger.info(
                f"✓ Step5 完成: 排序了 {len(sorted_events)} 个事项, 耗时: {step5_time:.3f}秒")

            # 步骤6: 选择Top-N事项并生成溯源线索
            step6_start = time.perf_counter()
            final_events = await self._step6_get_topn_events(
                sorted_events=sorted_events,
                key_final=key_final,
                config=config,
                tracker=tracker
            )
            step6_time = time.perf_counter() - step6_start
            step_times['Step6_Top-N筛选'] = step6_time
            self.logger.info(
                f"✓ Step6 完成: 最终返回 {len(final_events)} 个事项, 耗时: {step6_time:.3f}秒")

            # 计算总耗时
            overall_time = time.perf_counter() - overall_start

            # 输出耗时统计汇总
            self.logger.info("\n" + "=" * 80)
            self.logger.info("【事项级 PageRank】各步骤耗时汇总:")
            self.logger.info("-" * 80)
            self.logger.info(
                f"查询向量生成: {vector_time:.3f}秒 ({vector_time/overall_time*100:.1f}%)")
            for step_name, step_time in step_times.items():
                self.logger.info(
                    f"{step_name}: {step_time:.3f}秒 ({step_time/overall_time*100:.1f}%)")
            self.logger.info("-" * 80)
            self.logger.info(f"✓ 总耗时: {overall_time:.3f}秒")
            self.logger.info("=" * 80)

            # 构建 event_to_clues（从tracker获取）
            # 这里简化处理，直接从 sorted_events 构建
            event_to_clues = {}
            for event_data in sorted_events[:config.rerank.max_results]:
                event_id = event_data["event_id"]
                source_entities = event_data.get("source_entities", [])

                # 将实体ID列表转换为实体对象列表
                entity_objects = []
                for entity_id in source_entities:
                    # 从 key_final 中查找对应的实体信息
                    for key in key_final:
                        if key.get("key_id") == entity_id or key.get("id") == entity_id:
                            entity_objects.append({
                                "id": entity_id,
                                "name": key.get("name", ""),
                                "type": key.get("type", ""),
                                "weight": key.get("weight", 0.0)
                            })
                            break

                event_to_clues[event_id] = entity_objects

            # 构建并返回新的响应格式
            return await self._build_response(config, key_final, final_events, event_to_clues)

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

    async def _step1_keys_to_events(
        self,
        key_final: List[Dict[str, Any]],
        query: str,
        source_config_ids: List[str],
        query_vector: Optional[List[float]] = None,
        config: Optional[SearchConfig] = None
    ) -> List[Dict[str, Any]]:
        """
        步骤1（事项级）: key找event（向量相似度过滤）

        根据[key-final]从SQL中提取相关事项，然后从ES获取事项向量并计算与query的余弦相似度作为得分

        流程：
        1. 提取实体ID和权重
        2. 通过EventEntity表找到与这些实体相关的事件
        3. 从ES批量获取事项的content_vector
        4. 计算每个事项与query的余弦相似度
        5. 根据相似度阈值过滤事项
        6. 保留source_entities用于溯源

        Args:
            key_final: 从Recall返回的key_final数据
            query: 查询文本
            source_config_ids: 数据源配置ID列表
            query_vector: 可选的查询向量，如果为None则自动生成
            config: 搜索配置（用于缓存和阈值过滤）

        Returns:
            事项结果列表，每个事项包含：
            {
                "event_id": str,
                "event": SourceEvent对象,
                "similarity_score": float,  # 余弦相似度
                "source_entities": List[str],  # 召回该事项的实体ID列表
                "entity_weights": Dict[str, float]  # 实体ID -> 权重映射
            }
        """
        try:
            self.logger.info(
                f"[事项级Step1] 开始: 处理 {len(key_final)} 个key, query='{query}'")

            if not key_final:
                return []

            # 1. 生成查询向量（如果没有传入）
            if query_vector is None:
                query_vector = await self._generate_query_vector(query, config)
                if config and config.has_query_embedding:
                    self.logger.info(f"使用缓存的query向量，维度: {len(query_vector)}")
                else:
                    self.logger.info(f"查询向量生成成功，维度: {len(query_vector)}")
            else:
                self.logger.info(f"使用传入的查询向量，维度: {len(query_vector)}")

            # 2. 提取实体ID和权重
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
                # 3. 通过EventEntity查找相关事件（限制在指定source_config_ids内）
                event_entity_query = (
                    select(EventEntity.event_id,
                           EventEntity.entity_id, EventEntity.weight)
                    .join(SourceEvent, EventEntity.event_id == SourceEvent.id)
                    .where(
                        and_(
                            SourceEvent.source_config_id.in_(
                                source_config_ids),
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

                # 4. 记录事件到实体的映射关系
                event_to_entities = {}  # 事件ID -> 实体ID列表
                event_entity_weights = {}  # (事件ID, 实体ID) -> EventEntity权重

                for event_entity in event_entities:
                    event_id = event_entity.event_id
                    entity_id = event_entity.entity_id
                    event_entity_weight = event_entity.weight or 1.0

                    # 记录映射关系
                    if event_id not in event_to_entities:
                        event_to_entities[event_id] = []
                    event_to_entities[event_id].append(entity_id)

                    # 记录EventEntity权重
                    event_entity_weights[(event_id, entity_id)
                                         ] = event_entity_weight

                event_ids = list(event_to_entities.keys())
                self.logger.info(f"找到 {len(event_ids)} 个相关事件")

                # 5. 获取事件的详细信息（预加载 source 和 article 关系）
                event_detail_query = (
                    select(SourceEvent)
                    .options(
                        selectinload(SourceEvent.source),  # 预加载 SourceConfig
                        selectinload(SourceEvent.article)  # 预加载 Article
                    )
                    .where(
                        and_(
                            SourceEvent.source_config_id.in_(
                                source_config_ids),
                            SourceEvent.id.in_(event_ids)
                        )
                    )
                )
                event_detail_result = await session.execute(event_detail_query)
                events = event_detail_result.scalars().all()

                if not events:
                    self.logger.warning("未找到事件详情")
                    return []

                self.logger.info(f"获取到 {len(events)} 个事件的详细信息")

                # 6. 为每个事项添加 document_name 和 source_name 属性
                for event in events:
                    # 添加 source_name (from SourceConfig.name)
                    event.source_name = event.source.name if event.source else ""

                    # 添加 document_name (from Article.title)
                    event.document_name = event.article.title if event.article else ""

                # 7. 从ES批量获取事项的content_vector
                self.logger.info(f"从 ES 批量获取 {len(event_ids)} 个事项的向量...")

                es_events_data = await self.event_repo.get_events_by_ids(event_ids=event_ids)

                # 构建 event_id -> content_vector 的映射
                event_vector_map = {}
                for es_event in es_events_data:
                    event_id = es_event.get('event_id')
                    content_vector = es_event.get('content_vector')
                    if event_id and content_vector:
                        event_vector_map[event_id] = content_vector

                self.logger.info(
                    f"从 ES 获取到 {len(event_vector_map)} 个事项的向量 "
                    f"(请求了 {len(event_ids)} 个)"
                )

                # 8. 计算余弦相似度得分
                self.logger.info("=" * 80)
                self.logger.info("[事项级Step1] 余弦相似度计算:")
                self.logger.info("-" * 80)

                event_results = []

                for event in events:
                    event_id = event.id

                    # 获取事项向量
                    event_vector = event_vector_map.get(event_id)

                    if not event_vector:
                        self.logger.warning(
                            f"事项 {event_id[:8]}... 在ES中未找到向量，跳过")
                        continue

                    # 计算余弦相似度
                    try:
                        # 转换为numpy数组
                        query_np = np.array(query_vector, dtype=np.float32)
                        event_np = np.array(event_vector, dtype=np.float32)

                        # 计算余弦相似度
                        cosine_score = float(
                            np.dot(query_np, event_np) /
                            (np.linalg.norm(query_np) * np.linalg.norm(event_np))
                        )
                    except Exception as e:
                        self.logger.warning(
                            f"事项 {event_id[:8]}... 相似度计算失败: {e}")
                        cosine_score = 0.0

                    # 获取召回该事项的实体列表
                    source_entities = event_to_entities.get(event_id, [])

                    # 构建实体权重映射（包含key权重和EventEntity权重）
                    entity_weights = {}
                    for entity_id in source_entities:
                        key_weight = entity_weight_map.get(entity_id, 1.0)
                        ee_weight = event_entity_weights.get(
                            (event_id, entity_id), 1.0)
                        entity_weights[entity_id] = float(
                            key_weight) * float(ee_weight)

                    # 记录结果
                    event_result = {
                        "event_id": event_id,
                        "event": event,
                        "similarity_score": cosine_score,
                        "source_entities": source_entities,
                        "entity_weights": entity_weights
                    }
                    event_results.append(event_result)

                    # 日志输出
                    title_preview = event.title[:40] if event.title else "无标题"
                    self.logger.info(
                        f"事项 {event_id[:8]}... | "
                        f"Cosine={cosine_score:.4f} | "
                        f"关联实体数={len(source_entities)} | "
                        f"标题: {title_preview}"
                    )

                self.logger.info("=" * 80)

                # 8. 按相似度排序
                event_results.sort(
                    key=lambda x: x["similarity_score"], reverse=True)

                # 9. 使用 config.rerank.score_threshold 过滤低相似度结果
                original_count = len(event_results)
                if config and config.rerank.score_threshold:
                    filtered_results = [
                        r for r in event_results
                        if r["similarity_score"] >= config.rerank.score_threshold
                    ]

                    if len(filtered_results) < original_count:
                        self.logger.info(
                            f"相似度过滤: {original_count} -> {len(filtered_results)} 个事项 "
                            f"(阈值={config.rerank.score_threshold:.2f})"
                        )

                        # 展示过滤后保留的事项信息
                        if filtered_results:
                            self.logger.info("=" * 80)
                            self.logger.info(
                                f"过滤后保留的 {len(filtered_results)} 个事项:")
                            self.logger.info("-" * 80)
                            for result in filtered_results:
                                title_preview = result["event"].title[:
                                                                      40] if result["event"].title else "无标题"
                                self.logger.info(
                                    f"事项 {result['event_id'][:8]}... | "
                                    f"Cosine={result['similarity_score']:.4f} | "
                                    f"标题: {title_preview}"
                                )
                            self.logger.info("=" * 80)

                    event_results = filtered_results
                else:
                    self.logger.warning("未设置阈值或config为空，跳过相似度过滤")

                self.logger.info(
                    f"[事项级Step1] 完成: 处理了 {len(event_results)} 个事项",
                    extra={
                        "avg_cosine_score": np.mean([r["similarity_score"] for r in event_results]) if event_results else 0.0
                    }
                )

                # 🆕 根据 max_key_recall_results 截断（按相似度排序）
                max_key_results = config.rerank.max_key_recall_results if config else 30
                if len(event_results) > max_key_results:
                    self.logger.warning(
                        f"⚠️  [事项级Step1] Key召回事项数({len(event_results)})超过max_key_recall_results({max_key_results})，"
                        f"将按相似度排序后截断"
                    )

                    # 按相似度降序排序
                    event_results.sort(
                        key=lambda x: x["similarity_score"],
                        reverse=True
                    )

                    # 截断
                    truncated_results = event_results[:max_key_results]

                    self.logger.info(
                        f"📊 [事项级Step1] 截断统计: "
                        f"保留{len(truncated_results)}个, "
                        f"丢弃{len(event_results) - len(truncated_results)}个"
                    )

                    event_results = truncated_results

                # 显示Top 5结果
                top_results = event_results[:5]
                for i, result in enumerate(top_results, 1):
                    self.logger.debug(
                        f"Top {i}: {result['event'].title[:50]} - "
                        f"Cosine:{result['similarity_score']:.3f}"
                    )

                return event_results

        except Exception as e:
            self.logger.error(f"[事项级Step1] 执行失败: {e}", exc_info=True)
            return []

    async def _step2_query_to_events(
        self,
        query: str,
        source_config_ids: List[str],
        k: int = 30,  # 默认值改为30，与max_query_recall_results一致
        query_vector: Optional[List[float]] = None,
        config: Optional[SearchConfig] = None
    ) -> List[Dict[str, Any]]:
        """
        步骤2（事项级）: query找event（向量相似度过滤）

        通过向量相似度在ES中找到相似事项，计算余弦相似度作为得分

        流程：
        1. 生成查询向量（如果没有传入）
        2. 使用ES KNN搜索查找最相似的事项
        3. 计算每个事项与query的余弦相似度
        4. 根据相似度阈值过滤事项
        5. 返回事项结果

        Args:
            query: 查询文本
            source_config_ids: 数据源ID列表
            k: ES召回数量（建议使用config.rerank.max_query_recall_results）
            query_vector: 可选的查询向量，如果为None则自动生成
            config: 搜索配置（用于缓存和阈值过滤）

        Returns:
            事项结果列表，每个事项包含：
            {
                "event_id": str,
                "event": SourceEvent对象（从SQL查询获取）,
                "similarity_score": float,  # 余弦相似度
                "source": str  # "query"（用于区分来自Step1还是Step2）
            }
        """
        try:
            self.logger.info(
                f"[事项级Step2] 开始: query='{query}', source_config_ids={source_config_ids}")

            # 1. 生成查询向量（如果没有传入）
            if query_vector is None:
                query_vector = await self._generate_query_vector(query, config)
            if config and config.has_query_embedding:
                self.logger.info(f"使用缓存的query向量，维度: {len(query_vector)}")
            else:
                self.logger.info(f"查询向量生成成功，维度: {len(query_vector)}")

            # 2. 使用ES KNN搜索查找最相似的事项
            self.logger.info(
                f"从 ES 搜索相似事项，source_config_ids={source_config_ids}, k={k}")

            similar_events = await self.event_repo.search_similar_by_content(
                query_vector=query_vector,
                k=k,
                source_config_ids=source_config_ids
            )

            self.logger.info(f"ES搜索找到 {len(similar_events)} 个相似事项")

            if not similar_events:
                self.logger.warning("未找到相似事项")
                return []

            # 3. 提取事项ID列表，并从SQL查询完整事项信息
            event_ids_from_es = [e.get("event_id")
                                 for e in similar_events if e.get("event_id")]

            if not event_ids_from_es:
                self.logger.warning("ES返回的事项中没有有效的event_id")
                return []

            self.logger.info(f"从SQL查询 {len(event_ids_from_es)} 个事项的详细信息...")

            async with self.session_factory() as session:
                # 从SQL获取事项详细信息（预加载 source 和 article 关系）
                event_detail_query = (
                    select(SourceEvent)
                    .options(
                        selectinload(SourceEvent.source),  # 预加载 SourceConfig
                        selectinload(SourceEvent.article)  # 预加载 Article
                    )
                    .where(
                        and_(
                            SourceEvent.source_config_id.in_(
                                source_config_ids),
                            SourceEvent.id.in_(event_ids_from_es)
                        )
                    )
                )
                event_detail_result = await session.execute(event_detail_query)
                events_from_sql = event_detail_result.scalars().all()

                if not events_from_sql:
                    self.logger.warning("SQL中未找到对应的事项")
                    return []

                # 为每个事项添加 document_name 和 source_name 属性
                for event in events_from_sql:
                    # 添加 source_name (from SourceConfig.name)
                    event.source_name = event.source.name if event.source else ""

                    # 添加 document_name (from Article.title)
                    event.document_name = event.article.title if event.article else ""

                # 构建 event_id -> SourceEvent 映射
                event_map = {event.id: event for event in events_from_sql}

                self.logger.info(f"从SQL获取到 {len(event_map)} 个事项的详细信息")

            # 4. 构建 event_id -> content_vector 映射
            event_vector_map = {}
            for es_event in similar_events:
                event_id = es_event.get("event_id")
                content_vector = es_event.get("content_vector")
                if event_id and content_vector:
                    event_vector_map[event_id] = content_vector

            # 5. 计算余弦相似度得分
            self.logger.info("=" * 80)
            self.logger.info("[事项级Step2] 余弦相似度计算:")
            self.logger.info("-" * 80)

            event_results = []

            for es_event in similar_events:
                event_id = es_event.get("event_id")

                # 检查该事项是否在SQL中找到
                if event_id not in event_map:
                    self.logger.debug(
                        f"事项 {event_id[:8] if event_id else 'None'}... 在SQL中未找到，跳过")
                    continue

                event = event_map[event_id]

                # 获取事项向量
                event_vector = event_vector_map.get(event_id)

                if not event_vector:
                    self.logger.warning(f"事项 {event_id[:8]}... 没有向量信息，跳过")
                    continue

                # 计算余弦相似度
                try:
                    # 转换为numpy数组
                    query_np = np.array(query_vector, dtype=np.float32)
                    event_np = np.array(event_vector, dtype=np.float32)

                    # 计算余弦相似度
                    cosine_score = float(
                        np.dot(query_np, event_np) /
                        (np.linalg.norm(query_np) * np.linalg.norm(event_np))
                    )
                except Exception as e:
                    self.logger.warning(f"事项 {event_id[:8]}... 相似度计算失败: {e}")
                    cosine_score = 0.0

                # 记录结果
                event_result = {
                    "event_id": event_id,
                    "event": event,
                    "similarity_score": cosine_score,
                    "source": "query"  # 标记来自query直接召回
                }
                event_results.append(event_result)

                # 日志输出
                title_preview = event.title[:40] if event.title else "无标题"
                self.logger.info(
                    f"事项 {event_id[:8]}... | "
                    f"Cosine={cosine_score:.4f} | "
                    f"标题: {title_preview}"
                )

            self.logger.info("=" * 80)

            # 6. 按相似度排序
            event_results.sort(
                key=lambda x: x["similarity_score"], reverse=True)

            # 7. 使用 config.rerank.score_threshold 过滤低相似度结果
            original_count = len(event_results)
            if config and config.rerank.score_threshold:
                filtered_results = [
                    r for r in event_results
                    if r["similarity_score"] >= config.rerank.score_threshold
                ]

                if len(filtered_results) < original_count:
                    self.logger.info(
                        f"相似度过滤: {original_count} -> {len(filtered_results)} 个事项 "
                        f"(阈值={config.rerank.score_threshold:.2f})"
                    )

                    # 展示过滤后保留的事项信息
                    if filtered_results:
                        self.logger.info("=" * 80)
                        self.logger.info(
                            f"过滤后保留的 {len(filtered_results)} 个事项:")
                        self.logger.info("-" * 80)
                        for result in filtered_results:
                            title_preview = result["event"].title[:
                                                                  40] if result["event"].title else "无标题"
                            self.logger.info(
                                f"事项 {result['event_id'][:8]}... | "
                                f"Cosine={result['similarity_score']:.4f} | "
                                f"标题: {title_preview}"
                            )
                        self.logger.info("=" * 80)

                event_results = filtered_results
            else:
                self.logger.warning("未设置阈值或config为空，跳过相似度过滤")

            self.logger.info(
                f"[事项级Step2] 完成: 处理了 {len(event_results)} 个事项",
                extra={
                    "avg_cosine_score": np.mean([r["similarity_score"] for r in event_results]) if event_results else 0.0
                }
            )

            # 显示Top 5结果
            top_results = event_results[:5]
            for i, result in enumerate(top_results, 1):
                self.logger.debug(
                    f"Top {i}: {result['event'].title[:50]} - "
                    f"Cosine:{result['similarity_score']:.3f}"
                )

            return event_results

        except Exception as e:
            self.logger.error(f"[事项级Step2] 执行失败: {e}", exc_info=True)
            return []

    async def _step3_merge_events(
        self,
        step1_events: List[Dict[str, Any]],
        step2_events: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        步骤3（事项级）: 合并event去重（保留step1结果）

        合并Step1和Step2的事项结果，如果同一事项同时出现在两个步骤中，只保留Step1的结果

        流程：
        1. 以Step1结果为基础
        2. 添加Step2中不在Step1中的事项
        3. 记录每个事项的召回来源

        Args:
            step1_events: Step1返回的事项列表
            step2_events: Step2返回的事项列表

        Returns:
            合并后的事项列表，每个事项包含：
            {
                "event_id": str,
                "event": SourceEvent对象,
                "similarity_score": float,  # 来自Step1或Step2的相似度
                "source": str,  # "entity" 或 "query"
                "source_entities": List[str],  # 仅Step1有，召回该事项的实体ID列表
                "entity_weights": Dict[str, float]  # 仅Step1有，实体ID -> 权重映射
            }
        """
        try:
            self.logger.info(
                f"[事项级Step3] 开始合并事项: Step1={len(step1_events)}个, Step2={len(step2_events)}个")

            # 1. 以Step1为基础，构建event_id集合
            step1_event_ids = {e["event_id"] for e in step1_events}

            # 2. 创建合并后的列表，先添加所有Step1结果
            merged_events = []

            for event_data in step1_events:
                # Step1的事项，source应为"entity"
                merged_event = {
                    "event_id": event_data["event_id"],
                    "event": event_data["event"],
                    "similarity_score": event_data["similarity_score"],
                    "source": "entity",  # Step1来自实体召回
                    "source_entities": event_data.get("source_entities", []),
                    "entity_weights": event_data.get("entity_weights", {})
                }
                merged_events.append(merged_event)

            # 3. 添加Step2中不在Step1中的事项
            added_from_step2 = 0
            for event_data in step2_events:
                event_id = event_data["event_id"]

                # 如果该事项已经在Step1中，跳过（保留Step1结果）
                if event_id in step1_event_ids:
                    self.logger.debug(
                        f"事项 {event_id[:8]}... 已在Step1中，跳过Step2结果")
                    continue

                # Step2独有的事项
                merged_event = {
                    "event_id": event_id,
                    "event": event_data["event"],
                    "similarity_score": event_data["similarity_score"],
                    "source": "query",  # Step2来自query召回
                    "source_entities": [],  # Step2没有实体信息
                    "entity_weights": {}
                }
                merged_events.append(merged_event)
                added_from_step2 += 1

            self.logger.info("=" * 80)
            self.logger.info(f"[事项级Step3] 合并统计:")
            self.logger.info("-" * 80)
            self.logger.info(f"  Step1事项数: {len(step1_events)}")
            self.logger.info(f"  Step2事项数: {len(step2_events)}")
            self.logger.info(f"  Step2独有事项数: {added_from_step2}")
            self.logger.info(
                f"  Step1+Step2重复事项数: {len(step2_events) - added_from_step2}")
            self.logger.info(f"  合并后总事项数: {len(merged_events)}")
            self.logger.info("-" * 80)

            # 统计召回来源
            entity_source_count = sum(
                1 for e in merged_events if e["source"] == "entity")
            query_source_count = sum(
                1 for e in merged_events if e["source"] == "query")

            self.logger.info(f"  按来源统计:")
            self.logger.info(f"    实体召回(entity): {entity_source_count} 个")
            self.logger.info(f"    查询召回(query): {query_source_count} 个")
            self.logger.info("=" * 80)

            return merged_events

        except Exception as e:
            self.logger.error(f"[事项级Step3] 执行失败: {e}", exc_info=True)
            return []

    async def _step4_calculate_event_weights(
        self,
        merged_events: List[Dict[str, Any]]
    ) -> Dict[str, float]:
        """
        步骤4（事项级）: 计算事项权重向量

        使用公式计算每个事项的初始权重，用于后续PageRank

        公式（参考原段落权重计算）:
        weight = 0.5 × similarity_score + ln(1 + Σ(entity_weight))

        说明：
        - similarity_score: 事项与query的余弦相似度
        - entity_weight: 召回该事项的实体权重（仅Step1有，Step2为0）
        - 对于Step1事项：使用entity_weights中的权重求和
        - 对于Step2事项：仅使用similarity_score

        Args:
            merged_events: Step3合并后的事项列表

        Returns:
            事项权重字典 {event_id: weight}
        """
        try:
            self.logger.info(f"[事项级Step4] 开始计算事项权重，共 {len(merged_events)} 个事项")

            event_weights = {}

            self.logger.info("=" * 80)
            self.logger.info("[事项级Step4] 权重计算详情:")
            self.logger.info("-" * 80)

            for event_data in merged_events:
                event_id = event_data["event_id"]
                similarity_score = event_data["similarity_score"]
                source = event_data["source"]
                entity_weights = event_data.get("entity_weights", {})

                # 计算实体权重总和
                if entity_weights:
                    entity_weight_sum = sum(entity_weights.values())
                else:
                    entity_weight_sum = 0.0

                # 使用公式计算权重
                # weight = 0.5 × similarity_score + ln(1 + entity_weight_sum)
                weight = 0.5 * similarity_score + \
                    math.log(1 + entity_weight_sum)

                event_weights[event_id] = weight

                # 日志输出（显示前10个）
                if len(event_weights) <= 10:
                    title_preview = event_data["event"].title[:
                                                              30] if event_data["event"].title else "无标题"
                    self.logger.info(
                        f"事项 {event_id[:8]}... | "
                        f"来源={source} | "
                        f"Sim={similarity_score:.4f} | "
                        f"Entity权重和={entity_weight_sum:.4f} | "
                        f"最终权重={weight:.4f} | "
                        f"标题: {title_preview}"
                    )

            if len(merged_events) > 10:
                self.logger.info(f"... (还有 {len(merged_events) - 10} 个事项未显示)")

            self.logger.info("-" * 80)

            # 统计信息
            weights_list = list(event_weights.values())
            if weights_list:
                avg_weight = np.mean(weights_list)
                max_weight = np.max(weights_list)
                min_weight = np.min(weights_list)

                self.logger.info(f"权重统计:")
                self.logger.info(f"  平均权重: {avg_weight:.4f}")
                self.logger.info(f"  最大权重: {max_weight:.4f}")
                self.logger.info(f"  最小权重: {min_weight:.4f}")

            self.logger.info("=" * 80)

            self.logger.info(f"[事项级Step4] 完成: 计算了 {len(event_weights)} 个事项的权重")

            return event_weights

        except Exception as e:
            self.logger.error(f"[事项级Step4] 执行失败: {e}", exc_info=True)
            return {}

    async def _step5_build_event_graph_and_pagerank(
        self,
        merged_events: List[Dict[str, Any]],
        event_weights: Dict[str, float],
        key_final: List[Dict[str, Any]],
        damping: float = 0.85,
        max_iterations: int = 100
    ) -> List[Dict[str, Any]]:
        """
        步骤5（事项级）: 构建事项关系图 + 方向性PageRank排序

        使用事项之间的方向性关联关系构建图，实现更精细的PageRank排序：
        - 能区分事项重要性差异：重要事项投票权重更大
        - 基于内容的方向性：投票权重考虑目标事项的内容丰富度

        方向性关系图构建：
        1. 实体关联（方向性权重）：
           - 事项i→事项j的权重 = key权重 × key在事项j的出现次数
           - 事项j→事项i的权重 = key权重 × key在事项i的出现次数
           - 体现"重要事项投票更有价值"的思想
        2. 类别关联（方向性权重）：
           - 相同category的事项之间建立有向边
           - 权重基于目标事项的内容丰富度比例
           - 内容更丰富的事项获得更多投票权重

        Args:
            merged_events: Step3合并后的事项列表
            event_weights: Step4计算的事项权重字典
            key_final: Recall阶段的关键实体列表（包含实体权重）
            damping: 阻尼系数（默认0.85）
            max_iterations: 最大迭代次数（默认100）

        Returns:
            排序后的事项列表（按PageRank得分降序）
        """
        try:
            n = len(merged_events)

            if n == 0:
                self.logger.warning("[事项级Step5] 事项列表为空")
                return []

            self.logger.info(f"[事项级Step5] 开始构建事项关系图，共 {n} 个事项")

            # 1. 初始化PageRank值（使用Step4的权重作为初始值）
            weights = np.array([event_weights.get(e["event_id"], 0.0)
                               for e in merged_events])

            if weights.sum() > 0:
                pagerank = weights / weights.sum()  # 归一化
            else:
                pagerank = np.ones(n) / n  # 均匀分布

            self.logger.info(
                f"初始PageRank分布: min={pagerank.min():.6f}, max={pagerank.max():.6f}, sum={pagerank.sum():.6f}")

            # 2. 构建关系图
            # graph[i] = [(j, weight), ...] 表示从节点i到节点j的边及其权重
            graph = defaultdict(list)

            # 构建 entity_id -> 实体权重 的映射
            entity_weight_map = {}
            for key in key_final:
                key_id = key.get("key_id") or key.get("id")
                if key_id:
                    entity_weight_map[key_id] = key.get("weight", 1.0)

            # 2.1 实体关联（方向性权重）：基于key在事项内容中的出现次数构建有向边
            # 核心思想：重要事项应该"投票"给内容更丰富的事项
            # 事项i→事项j的权重 = key权重 × key在事项j的出现次数
            # 这样，内容中包含更多key的事项会获得更多投票权重
            entity_to_events = defaultdict(list)
            for i, event in enumerate(merged_events):
                for entity_id in event.get("source_entities", []):
                    entity_to_events[entity_id].append(i)

            # 构建key名称到权重的映射，用于后续内容匹配
            key_name_to_weight = {}
            for key in key_final:
                key_name = key.get("name", "")
                key_weight = key.get("weight", 1.0)
                if key_name:
                    key_name_to_weight[key_name] = key_weight

            entity_edges = 0
            for entity_id, event_indices in entity_to_events.items():
                if len(event_indices) > 1:
                    # 获取该实体的权重
                    entity_weight = entity_weight_map.get(entity_id, 1.0)

                    # 找到对应的key名称（用于内容匹配）
                    # 通过entity_id反向查找key_final中的对应实体名称
                    key_name = None
                    for key in key_final:
                        key_id = key.get("key_id") or key.get("id")
                        if key_id == entity_id:
                            key_name = key.get("name", "")
                            break

                    if not key_name:
                        continue  # 找不到key名称，跳过内容匹配

                    # 计算每个事项中key的出现次数
                    # 统计范围：标题 + 摘要 + 内容，全面反映事项与key的关联强度
                    event_key_counts = {}
                    for event_idx in event_indices:
                        event_obj = merged_events[event_idx]["event"]  # 获取SourceEvent对象
                        # 合并标题、摘要和内容进行统计
                        event_text = f"{event_obj.title} {event_obj.summary} {event_obj.content}"
                        key_count = event_text.count(key_name)
                        event_key_counts[event_idx] = key_count

                    # 建立方向性边：实现"重要事项投票给内容更丰富事项"的思想
                    # 事项i→事项j的权重 = key权重 × key在事项j的出现次数
                    # 这样，内容中key出现次数多的事项会获得更多投票权重
                    # 使用 idx_i < idx_j 避免重复遍历
                    for idx_i, i in enumerate(event_indices):
                        for idx_j in range(idx_i + 1, len(event_indices)):
                            j = event_indices[idx_j]

                            # 计算双向权重
                            # i→j的权重基于j中key的出现次数
                            weight_i_to_j = entity_weight * event_key_counts[j]
                            # j→i的权重基于i中key的出现次数
                            weight_j_to_i = entity_weight * event_key_counts[i]

                            # 只建立权重>0的边，避免无效连接
                            # 这种机制确保：
                            # 1. 内容中不包含key的事项不会参与投票
                            # 2. 投票权重与内容丰富度成正比
                            # 3. 实现真正的"内容重要性"驱动的PageRank
                            if weight_i_to_j > 0:
                                graph[i].append((j, weight_i_to_j))
                                entity_edges += 1
                            if weight_j_to_i > 0:
                                graph[j].append((i, weight_j_to_i))
                                entity_edges += 1

            self.logger.info(
                f"实体关联: {entity_edges} 条有向边，涉及 {len([e for e in entity_to_events.values() if len(e) > 1])} 个实体")

            # 2.2 类别关联（方向性权重）：相同category的event之间建边，权重考虑内容相关性
            # 核心思想：同类事项中，内容更丰富的事项应该获得更多投票权重
            # 权重计算：基于目标事项的内容长度比例，内容越长权重越大
            # 这样确保重要的同类事项在PageRank中获得更高排名
            category_to_events = defaultdict(list)
            for i, event in enumerate(merged_events):
                category = event["event"].category
                if category:
                    category_to_events[category].append(i)

            category_edges = 0
            for category, event_indices in category_to_events.items():
                if len(event_indices) > 1:
                    # 计算每个事项的内容长度（用于权重分配）
                    event_content_lengths = {}
                    for event_idx in event_indices:
                        event_obj = merged_events[event_idx]["event"]
                        content_length = len(f"{event_obj.title} {event_obj.summary} {event_obj.content}")
                        event_content_lengths[event_idx] = max(content_length, 1)  # 避免除零

                    # 建立方向性边：权重基于目标事项的内容丰富度
                    # 使用 idx_i < idx_j 避免重复遍历
                    total_length = sum(event_content_lengths.values())
                    if total_length > 0:
                        for idx_i, i in enumerate(event_indices):
                            for idx_j in range(idx_i + 1, len(event_indices)):
                                j = event_indices[idx_j]

                                # 计算双向权重：使用目标事项的内容长度比例作为权重
                                # i→j的权重基于j的内容长度
                                weight_i_to_j = 0.1 * (event_content_lengths[j] / total_length)
                                # j→i的权重基于i的内容长度
                                weight_j_to_i = 0.1 * (event_content_lengths[i] / total_length)

                                # 建立双向边
                                if weight_i_to_j > 0:
                                    graph[i].append((j, weight_i_to_j))
                                    category_edges += 1
                                if weight_j_to_i > 0:
                                    graph[j].append((i, weight_j_to_i))
                                    category_edges += 1

            self.logger.info(
                f"类别关联: {category_edges} 条有向边，涉及 {len([c for c in category_to_events.values() if len(c) > 1])} 个类别")

            total_edges = entity_edges + category_edges
            self.logger.info(f"关系图构建完成: 总边数={total_edges}")

            # 3. 预计算每个节点的总出权重（避免重复计算）
            out_weights = {}
            for j in range(n):
                edges = graph.get(j, [])
                out_weights[j] = sum(w for _, w in edges) if edges else 0.0

            nodes_with_edges = sum(1 for w in out_weights.values() if w > 0)
            self.logger.debug(
                f"预计算完成: {nodes_with_edges}/{n} 个节点有出边")

            # 4. PageRank迭代（优化版：反向遍历图，避免O(n²)复杂度）
            self.logger.info("=" * 80)
            self.logger.info("[事项级Step5] 开始PageRank迭代")
            self.logger.info("-" * 80)

            for iteration in range(max_iterations):
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

                if (iteration + 1) % 10 == 0:
                    self.logger.debug(f"迭代 {iteration + 1}: diff={diff:.8f}")

                if diff < 1e-6:
                    self.logger.info(
                        f"PageRank在第 {iteration + 1} 次迭代收敛 (diff={diff:.8f})")
                    pagerank = new_pagerank
                    break

                pagerank = new_pagerank

            else:
                self.logger.warning(f"PageRank达到最大迭代次数 {max_iterations}，未完全收敛")

            self.logger.info(
                f"最终PageRank分布: min={pagerank.min():.6f}, max={pagerank.max():.6f}, sum={pagerank.sum():.6f}")
            self.logger.info("=" * 80)

            # 4. 将PageRank值赋给事项
            for i, event in enumerate(merged_events):
                event["pagerank"] = float(pagerank[i])

            # 5. 按PageRank排序（降序）
            sorted_events = sorted(
                merged_events, key=lambda x: x["pagerank"], reverse=True)

            # 6. 日志：显示Top 10
            self.logger.info("=" * 80)
            self.logger.info("[事项级Step5] PageRank排序结果 (Top 10):")
            self.logger.info("-" * 80)

            for rank, event in enumerate(sorted_events[:10], 1):
                title_preview = event["event"].title[:
                                                     40] if event["event"].title else "无标题"
                self.logger.info(
                    f"Rank {rank}: {event['event_id'][:8]}... | "
                    f"PageRank={event['pagerank']:.6f} | "
                    f"Weight={event_weights.get(event['event_id'], 0.0):.4f} | "
                    f"来源={event['source']} | "
                    f"标题: {title_preview}"
                )

            if len(sorted_events) > 10:
                self.logger.info(f"... (还有 {len(sorted_events) - 10} 个事项未显示)")

            self.logger.info("=" * 80)

            self.logger.info(f"[事项级Step5] 完成: 排序了 {len(sorted_events)} 个事项")

            return sorted_events

        except Exception as e:
            self.logger.error(f"[事项级Step5] 执行失败: {e}", exc_info=True)
            return []

    async def _step6_get_topn_events(
        self,
        sorted_events: List[Dict[str, Any]],
        key_final: List[Dict[str, Any]],
        config: SearchConfig,
        tracker: Tracker
    ) -> List[SourceEvent]:
        """
        步骤6（事项级）: 选择Top-N事项（保留溯源）

        从PageRank排序后的事项中选择Top-N个，并为事项生成溯源线索

        流程：
        1. 为 Top-K×3 的事项生成 intermediate 线索（普通模式可见）
        2. 为 Top-K 的事项额外生成 final 线索（精简模式高亮显示）
        3. 返回 Top-K 事项列表（按PageRank顺序）

        Args:
            sorted_events: Step5排序后的事项列表
            key_final: Recall阶段的key列表（用于构建实体节点）
            config: 搜索配置
            tracker: 线索追踪器

        Returns:
            Top-N事项对象列表（SourceEvent）
        """
        try:
            topn = config.rerank.max_results
            # 所有事项都生成 intermediate 线索
            intermediate_events = sorted_events  # 改为所有事项
            # Top-K 用于生成 final 线索和最终返回
            final_events = sorted_events[:topn]

            self.logger.info(f"[事项级Step6] 开始处理事项")
            self.logger.info(
                f"  所有 {len(intermediate_events)} 个事项生成 intermediate 线索")
            self.logger.info(f"  Top-{topn} 事项生成 final 线索")

            # 1. 构建 entity_id -> key 对象的映射
            entity_to_key = {}
            for key in key_final:
                key_id = key.get("key_id") or key.get("id")
                if key_id:
                    entity_to_key[key_id] = key

            # ========== 第一步：为所有事项生成 intermediate 线索 ==========
            self.logger.info("")
            self.logger.info("=" * 80)
            self.logger.info(
                f"[事项级Step6] 生成 Intermediate 线索 (所有 {len(intermediate_events)} 个事项)")
            self.logger.info("-" * 80)

            intermediate_entity_clue_count = 0
            intermediate_query_clue_count = 0

            for rank, event_data in enumerate(intermediate_events, 1):
                event_obj = event_data["event"]
                event_id = event_data["event_id"]
                source = event_data["source"]
                source_entities = event_data.get("source_entities", [])

                if source == "entity":
                    # Step1召回的事项：为每个source_entity生成线索（entity → event）
                    for entity_id in source_entities:
                        # 从key_final中获取实体信息
                        key_info = entity_to_key.get(entity_id)
                        if not key_info:
                            self.logger.warning(
                                f"实体 {entity_id[:8]}... 在key_final中未找到")
                            continue

                        # 构建实体节点
                        entity_node = Tracker.build_entity_node({
                            "key_id": entity_id,
                            "id": entity_id,
                            "name": key_info.get("name", ""),
                            "type": key_info.get("type", ""),
                            "description": key_info.get("description", "")
                        })

                        # 构建事项节点（使用tracker实例方法）
                        event_node = tracker.get_or_create_event_node(
                            event_obj,
                            "rerank",
                            recall_method="entity"
                        )

                        # 添加 intermediate 线索
                        tracker.add_clue(
                            stage="rerank",
                            from_node=entity_node,
                            to_node=event_node,
                            confidence=event_data["similarity_score"],
                            relation="实体召回",
                            display_level="intermediate",  # intermediate 级别
                            metadata={
                                "method": "pagerank_entity",
                                "pagerank_score": event_data["pagerank"],
                                "similarity_score": event_data["similarity_score"],
                                "rank": rank
                            }
                        )
                        intermediate_entity_clue_count += 1

                elif source == "query":
                    # Step2召回的事项：生成query → event线索
                    # 构建query节点
                    query_node = Tracker.build_query_node(config)

                    # 构建事项节点
                    event_node = tracker.get_or_create_event_node(
                        event_obj,
                        "rerank",
                        recall_method="query"
                    )

                    # 添加 intermediate 线索
                    tracker.add_clue(
                        stage="rerank",
                        from_node=query_node,
                        to_node=event_node,
                        confidence=event_data["similarity_score"],
                        relation="查询召回",
                        display_level="intermediate",  # intermediate 级别
                        metadata={
                            "method": "pagerank_query",
                            "pagerank_score": event_data["pagerank"],
                            "similarity_score": event_data["similarity_score"],
                            "rank": rank
                        }
                    )
                    intermediate_query_clue_count += 1

                # 日志（只显示前10个）
                if rank <= 10:
                    title_preview = event_obj.title[:
                                                    40] if event_obj.title else "无标题"
                    self.logger.info(
                        f"  Rank {rank}: {event_id[:8]}... | "
                        f"来源={source} | "
                        f"实体数={len(source_entities)} | "
                        f"标题: {title_preview}"
                    )

            if len(intermediate_events) > 10:
                self.logger.info(
                    f"  ... (还有 {len(intermediate_events) - 10} 个事项)")

            self.logger.info("-" * 80)
            self.logger.info(f"Intermediate 线索统计:")
            self.logger.info(f"  实体→事项线索: {intermediate_entity_clue_count} 条")
            self.logger.info(f"  查询→事项线索: {intermediate_query_clue_count} 条")
            self.logger.info(
                f"  总线索数: {intermediate_entity_clue_count + intermediate_query_clue_count} 条")
            self.logger.info("=" * 80)

            # ========== 第二步：为 Top-K 生成 final 线索 ==========
            self.logger.info("")
            self.logger.info(
                "🎯 [事项级 Rerank Final] 生成最终线索 (display_level=final)")
            self.logger.info(f"   为 Top-{topn} 事项生成 final 线索")

            final_clue_count = 0

            for rank, event_data in enumerate(final_events, 1):
                event_obj = event_data["event"]
                event_id = event_data["event_id"]
                source = event_data["source"]
                source_entities = event_data.get("source_entities", [])

                if source == "entity":
                    # 为 entity 召回的事项生成 final 线索（entity → event）
                    for entity_id in source_entities:
                        key_info = entity_to_key.get(entity_id)
                        if not key_info:
                            continue

                        # 构建实体节点
                        entity_node = Tracker.build_entity_node({
                            "key_id": entity_id,
                            "id": entity_id,
                            "name": key_info.get("name", ""),
                            "type": key_info.get("type", ""),
                            "description": key_info.get("description", "")
                        })

                        # 构建事项节点
                        event_node = tracker.get_or_create_event_node(
                            event_obj,
                            "rerank",
                            recall_method="entity"
                        )

                        # 添加 final 线索
                        tracker.add_clue(
                            stage="rerank",
                            from_node=entity_node,
                            to_node=event_node,
                            confidence=event_data["similarity_score"],
                            relation="最终事项",
                            display_level="final",  # final 级别
                            metadata={
                                "method": "final_result",
                                "step": "step6",
                                "pagerank_score": event_data["pagerank"],
                                "similarity_score": event_data["similarity_score"],
                                "rank": rank,
                                "source": "entity"
                            }
                        )
                        final_clue_count += 1

                        self.logger.debug(
                            f"  Final: {entity_id[:8]}... ('{key_info.get('name', '')[:20]}') "
                            f"→ {event_id[:8]}... ('{event_obj.title[:30]}', PR={event_data['pagerank']:.4f})"
                        )

                elif source == "query":
                    # 为 query 召回的事项生成 final 线索（query → event）
                    query_node = Tracker.build_query_node(config)

                    event_node = tracker.get_or_create_event_node(
                        event_obj,
                        "rerank",
                        recall_method="query"
                    )

                    # 添加 final 线索
                    tracker.add_clue(
                        stage="rerank",
                        from_node=query_node,
                        to_node=event_node,
                        confidence=event_data["similarity_score"],
                        relation="最终事项",
                        display_level="final",  # final 级别
                        metadata={
                            "method": "final_result",
                            "step": "step6",
                            "pagerank_score": event_data["pagerank"],
                            "similarity_score": event_data["similarity_score"],
                            "rank": rank,
                            "source": "query"
                        }
                    )
                    final_clue_count += 1

                    self.logger.debug(
                        f"  Final: query '{config.query[:20]}...' "
                        f"→ {event_id[:8]}... ('{event_obj.title[:30]}', PR={event_data['pagerank']:.4f})"
                    )

            self.logger.info(
                f"✅ [事项级 Rerank Final] 生成了 {final_clue_count} 条最终线索"
            )
            self.logger.info(
                f"✅ [事项级 Rerank Final] 前端可根据这些 final 线索反推完整推理路径："
            )
            self.logger.info(f"   - Entity召回: query → entity → event")
            self.logger.info(f"   - Query召回: query → event")
            self.logger.info("")

            # 3. 提取事项对象列表（保持PageRank顺序，只返回Top-K）
            result_events = [e["event"] for e in final_events]

            self.logger.info(f"[事项级Step6] 完成: 返回 Top-{len(result_events)} 个事项")

            return result_events

        except Exception as e:
            self.logger.error(f"[事项级Step6] 执行失败: {e}", exc_info=True)
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
    
    async def _build_response(
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
