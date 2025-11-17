"""
搜索 RRF 模块

实现从 keys 直接查找关联事项的功能，使用三阶段策略（BM25+RRF绑定执行）：
1. Embedding 相似度阈值过滤（粗排）：使用预存向量计算与query的余弦相似度，过滤低相关事项
2. BM25 重排序（精排）：对通过阈值的事项使用 BM25 算法进行关键词匹配排序
3. RRF 融合（自动）：使用 Reciprocal Rank Fusion 融合 Embedding 和 BM25 两种排序结果

处理流程（优化版 - 直接使用 key_id）：
1. 直接使用 key_id：key_final 中的 key_id 就是 Entity 表的 id，无需查询 Entity 表
2. 事件关联：通过 EventEntity 表找到与这些实体相关的事件
3. 事项去重：基于事项ID去重，防止同一个事项多次返回
4. 向量相似度计算（粗排）：从 ES 批量获取事项向量，计算与 query 的加权余弦相似度
5. 阈值过滤：过滤掉相似度低于阈值的事项
6. BM25 计算：对通过阈值的事项使用 BM25 算法计算分数（使用 fast_mode 跳过 spaCy，只用 jieba 分词）
7. RRF 融合：融合 Embedding 排序和 BM25 排序，计算 RRF 分数
8. Top-N 限制：返回前 N 个事项

RRF 融合算法：
RRF_score(d) = Σ 1/(k + rank_i(d))
在我们的场景中：
RRF_score(event) = 1/(k + embedding_rank) + 1/(k + bm25_rank)
其中 k 固定为 60，用于平衡不同排序系统的影响

配置参数：
- config.query: 查询文本
- config.query_embedding: 查询向量（缓存）
- config.source_config_id: 数据源ID
- config.rerank.score_threshold: Embedding 相似度阈值（默认0.5）
- config.rerank.max_results: 返回事项数量（默认8）

返回格式：
Dict[str, Any]: 包含以下字段的字典：
    - events (List[SourceEvent]): 事项对象列表，每个对象附加属性:
        - similarity_score (float): Embedding 余弦相似度（粗排分数）
        - embedding_rank (int): Embedding 排名（RRF 使用）
        - bm25_score (float): BM25 分数（精排分数）
        - bm25_rank (int): BM25 排名（RRF 使用）
        - rrf_score (float): RRF 融合分数（最终排序依据）
        - clues (List[Dict]): 召回该事项的实体线索列表（来自 key_final）
    - clues (Dict): 召回线索信息
        - origin_query (str): 原始查询（重写前）
        - final_query (str): LLM重写后的查询（重写后）
        - query_entities (List[Dict]): 查询召回的实体列表（key_id改为id）
        - recall_entities (List[Dict]): 召回的实体列表（key_id改为id，过滤掉query_entities中的值）
"""

from typing import Any, Dict, List, Tuple
import time
import numpy as np

from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload

from sag.core.storage.elasticsearch import get_es_client
from sag.core.storage.repositories.event_repository import EventVectorRepository
from sag.core.ai.tokensize import get_mixed_tokenizer
from sag.db import SourceEvent, EventEntity, SourceConfig, Article, get_session_factory
from sag.modules.search.config import SearchConfig
from sag.modules.search.tracker import Tracker  # 🆕 添加线索追踪器
from sag.utils import get_logger

logger = get_logger("search.rerank.rrf")


class RerankRRFSearcher:
    """RRF搜索器 - 从keys直接查找关联事项（两阶段排序）"""

    def __init__(self, llm_client=None):
        """
        初始化RRF搜索器

        Args:
            llm_client: LLM客户端（可选，暂未使用）
        """
        self.session_factory = get_session_factory()
        self.logger = get_logger("search.rerank.rrf")

        # 初始化Elasticsearch仓库
        self.es_client = get_es_client()
        self.event_repo = EventVectorRepository(self.es_client)

        self.logger.info("RRF搜索器初始化完成")

    async def search(
        self,
        key_final: List[Dict[str, Any]],
        config: SearchConfig
    ) -> Dict[str, Any]:
        """
        从 keys 直接查找关联事项（Embedding粗排 + BM25精排 + RRF融合）

        处理流程：
        1. 实体匹配：根据 key_final 中的实体名称和类型，在 Entity 表中查找匹配的实体
        2. 事件关联：通过 EventEntity 表找到与这些实体相关的事件
        3. 事项去重：基于事项ID去重，防止同一个事项多次返回
        4. Embedding 相似度计算（粗排）：从 ES 批量获取事项向量，计算与 query 的余弦相似度
        5. 阈值过滤：过滤掉相似度低于阈值的事项
        6. BM25 + RRF 融合排序（精排）：使用 BM25 和 RRF 算法融合 Embedding 和关键词两种排序
        7. Top-K 限制：返回前 K 个事项

        配置参数：
        - config.query: 查询文本
        - config.query_embedding: 查询向量（缓存）
        - config.source_config_id: 数据源ID
        - config.rerank.score_threshold: Embedding 相似度阈值（默认0.5）
        - config.rerank.max_results: 返回事项数量（默认8）

        注意：
        - BM25 和 RRF 融合绑定，无需单独配置
        - RRF 常数 k 固定为 60

        Args:
            key_final: 从Recall或Expand返回的关键实体列表
            config: 搜索配置对象

        Returns:
            Dict[str, Any]: 包含以下字段的字典：
                - events (List[SourceEvent]): 事项对象列表（按RRF分数排序，最多返回top_k个），每个对象附加属性:
                    - similarity_score (float): Embedding 余弦相似度
                    - embedding_rank (int): Embedding 排名
                    - bm25_score (float): BM25 分数
                    - bm25_rank (int): BM25 排名
                    - rrf_score (float): RRF 融合分数（最终排序依据）
                - clues (Dict): 召回线索信息
                    - origin_query (str): 原始查询（重写前）
                    - final_query (str): LLM重写后的查询（重写后）
                    - query_entities (List[Dict]): 查询召回的实体列表（key_id改为id）
                    - recall_entities (List[Dict]): 召回的实体列表（key_id改为id，过滤掉query_entities中的值）
        """
        try:
            # 从 config 中提取参数
            query = config.query
            query_vector = config.query_embedding
            source_config_ids = config.get_source_config_ids()  # 🆕 支持多信息源
            threshold = config.rerank.score_threshold  # 使用通用阈值参数
            top_k = config.rerank.max_results  # 使用通用 top_k 参数
            rrf_k = 60  # RRF 常数固定为 60

            self.logger.info(
                f"RRF搜索开始: 处理 {len(key_final)} 个keys, "
                f"query='{query}', source_config_ids={source_config_ids}, threshold={threshold}, top_k={top_k}"
            )

            if not key_final:
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
                }

            # 步骤1-3: 实体匹配 → 事件关联 → 去重
            events, event_to_clues = await self._get_events_from_keys(key_final, source_config_ids)
            if not events:
                self.logger.warning(
                    f"⚠️ RRF步骤1-3失败：未从 {len(key_final)} 个实体找到任何关联事项。"
                    f"可能原因：1) EventEntity表无数据关联 2) source_config_ids {source_config_ids} 无匹配事项"
                )
                self.logger.warning(
                    f"实体列表：{[k['name'] for k in key_final[:5]]}")
                return self._build_response(config, key_final, [], {})

            self.logger.info(f"✅ 步骤1-3: 从实体查找到 {len(events)} 个关联事项")

            # 步骤4: Embedding 相似度计算（粗排）
            events_with_scores = await self._calculate_embedding_similarity(
                events, query_vector
            )
            if not events_with_scores:
                self.logger.warning(
                    f"⚠️ RRF步骤4失败：Embedding相似度计算返回空结果。"
                    f"原始事项数: {len(events)}"
                )
                return self._build_response(config, key_final, [], {})

            self.logger.info(
                f"✅ 步骤4: Embedding计算完成，{len(events_with_scores)} 个事项有相似度分数")

            # 步骤5: 阈值过滤
            filtered_events = self._filter_by_threshold(
                events_with_scores, threshold)
            if not filtered_events:
                # 计算相似度统计
                scores = [getattr(e, 'similarity_score', 0)
                          for e in events_with_scores]
                max_score = max(scores) if scores else 0
                avg_score = sum(scores) / len(scores) if scores else 0

                self.logger.warning(
                    f"⚠️ RRF步骤5失败：所有 {len(events_with_scores)} 个事项都被阈值过滤掉了。"
                    f"\n  当前阈值: {threshold}"
                    f"\n  最高相似度: {max_score:.4f}"
                    f"\n  平均相似度: {avg_score:.4f}"
                    f"\n  建议：降低 threshold 参数（当前={threshold}）"
                )
                return self._build_response(config, key_final, [], {})

            # 步骤6-7: BM25 + RRF 融合排序（绑定执行）
            final_events = await self._rank_by_rrf(
                filtered_events, query, top_k, rrf_k
            )

            self.logger.info(
                f"RRF搜索完成: 返回 {len(final_events)} 个事项 "
                f"(原始={len(events)}, 阈值过滤后={len(filtered_events)}, "
                f"Top-K={top_k})"
            )

            # === 构建Rerank阶段线索 ===
            # 计算 top-k×3 用于生成 intermediate 线索
            intermediate_count = min(top_k * 3, len(final_events))
            intermediate_events = final_events[:intermediate_count]

            rerank_clues = self._build_rerank_clues(
                config,
                key_final,
                intermediate_events,  # 传入 top-k×3 事项（用于生成中间线索）
                final_events[:top_k], # 传入 Top-K 事项（用于生成 final 线索）
                event_to_clues
            )
            config.rerank_clues = rerank_clues
            self.logger.info(f"✨ Rerank线索已构建 (entity→event直接连接)")

            return self._build_response(config, key_final, final_events, event_to_clues)

        except Exception as e:
            self.logger.error(f"RRF搜索失败: {e}", exc_info=True)
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
            }

    async def _get_events_from_keys(
        self,
        key_final: List[Dict[str, Any]],
        source_config_ids: List[str]  # 🆕 改为列表支持多源
    ) -> Tuple[List[SourceEvent], Dict[str, List[Dict]]]:
        """
        步骤1-3: 从 keys 查找关联事项（直接使用 key_id → 事件关联 → 去重）

        优化：完全跳过 Entity 表查询，直接使用 key_id（key_id 就是 entity_id）

        新增功能：构建 event_to_clues 映射表，记录事项与实体的关联关系

        Args:
            key_final: 关键实体列表（每个 key 的 key_id 就是 Entity 表的 id）
            source_config_ids: 数据源ID列表（🆕 支持多源）

        Returns:
            Tuple[List[SourceEvent], Dict[str, List[Dict]]]:
                - events: 去重后的事项列表
                - event_to_clues: 事项ID到实体列表的映射 {event_id: [entity1, entity2, ...]}
        """
        # 1. 直接提取 key_ids（就是 entity_ids）
        entity_ids = [key["key_id"] for key in key_final]

        # 2. 创建 key_id → key 对象的映射（将 key_id 重命名为 id）
        key_info_map = {}
        for key in key_final:
            # 创建 key 的副本，将 key_id 重命名为 id
            key_copy = key.copy()
            if "key_id" in key_copy:
                key_copy["id"] = key_copy.pop("key_id")
            key_info_map[key["key_id"]] = key_copy

        self.logger.info(f"从 key_final 提取 {len(entity_ids)} 个 entity_ids")

        async with self.session_factory() as session:
            # 3. 直接通过 EventEntity 查找相关事件（跳过 Entity 表查询）
            event_entity_query = (
                select(EventEntity.event_id, EventEntity.entity_id)
                .join(SourceEvent, EventEntity.event_id == SourceEvent.id)
                .where(
                    and_(
                        SourceEvent.source_config_id.in_(source_config_ids),  # 🆕 多源过滤
                        EventEntity.entity_id.in_(entity_ids)  # 直接使用 key_ids
                    )
                )
                .distinct()
            )

            event_result = await session.execute(event_entity_query)
            event_relations = event_result.fetchall()  # 获取 (event_id, entity_id) 元组

            self.logger.info(f"查询到 {len(event_relations)} 条 event-entity 关系")

            # 4. 构建 event_id → clues 映射（一次遍历完成）
            event_to_clues = {}
            for event_id, entity_id in event_relations:
                if event_id not in event_to_clues:
                    event_to_clues[event_id] = []

                # 直接从 key_info_map 获取（entity_id 就是 key_id）
                key_info = key_info_map.get(entity_id)
                if key_info:
                    event_to_clues[event_id].append(key_info)

            # 5. 提取所有唯一的 event_ids
            event_ids = list(event_to_clues.keys())

            if not event_ids:
                self.logger.warning("未找到相关事件")
                return []

            self.logger.info(f"找到 {len(event_ids)} 个关联事件（已去重）")

            # 6. 查询所有事项的详细信息（预加载 source 和 article 关系）
            event_query = (
                select(SourceEvent)
                .options(
                    selectinload(SourceEvent.event_associations),  # 预加载关联关系
                    selectinload(SourceEvent.source),  # 预加载 SourceConfig
                    selectinload(SourceEvent.article)  # 预加载 Article
                )
                .where(
                    and_(
                        SourceEvent.source_config_id.in_(source_config_ids),  # 🆕 多源过滤
                        SourceEvent.id.in_(event_ids)
                    )
                )
            )

            event_detail_result = await session.execute(event_query)
            events = event_detail_result.scalars().all()

            original_count = len(events)
            self.logger.info(f"查询到 {original_count} 个事项详细信息")

            if not events:
                self.logger.warning("未找到事项详细信息")
                return []

            # 为每个事项添加 source_name 和 document_name 属性
            for event in events:
                event.source_name = event.source.name if event.source else ""
                event.document_name = event.article.title if event.article else ""

            # 7. 事项去重（防止同一个事项多次返回）
            unique_events = {}
            for event in events:
                event_id = event.id
                if event_id not in unique_events:
                    unique_events[event_id] = event

            events = list(unique_events.values())
            unique_count = len(events)
            duplicate_count = original_count - unique_count

            if duplicate_count > 0:
                self.logger.info(
                    f"事项去重完成: 去重前={original_count}个, "
                    f"去重后={unique_count}个, "
                    f"去除重复={duplicate_count}个"
                )

            # 8. 输出 event_to_clues 映射表统计信息
            clues_stats = [len(clues) for clues in event_to_clues.values()]
            if clues_stats:
                self.logger.info(
                    f"✅ event_to_clues统计: 平均={sum(clues_stats)/len(clues_stats):.1f}个实体/事项, "
                    f"最多={max(clues_stats)}个, 最少={min(clues_stats)}个"
                )

            return events, event_to_clues

    async def _calculate_embedding_similarity(
        self,
        events: List[SourceEvent],
        query_vector: List[float]
    ) -> List[SourceEvent]:
        """
        步骤4: 计算 Embedding 相似度（粗排）

        从 ES 批量获取事项向量，计算与 query 的加权余弦相似度
        权重: title_vector * 0.2 + content_vector * 0.8

        Args:
            events: 事项列表
            query_vector: 查询向量

        Returns:
            List[SourceEvent]: 附加了 similarity_score 属性的事项列表
        """
        self.logger.info(f"开始从ES批量获取 {len(events)} 个事项的向量...")
        vector_fetch_start = time.perf_counter()

        # 提取所有事项ID
        event_ids = [event.id for event in events]

        # 分批处理，避免一次性查询过多
        batch_size = 100  # ES可以处理更大的批次
        events_with_vectors = []
        missing_vector_count = 0

        for i in range(0, len(event_ids), batch_size):
            batch_event_ids = event_ids[i:i + batch_size]
            self.logger.debug(
                f"  处理批次 {i//batch_size + 1}: {len(batch_event_ids)} 个事项")

            # 批量获取事项数据（包含向量）
            batch_event_data = await self.event_repo.get_events_by_ids(batch_event_ids)
            # 创建 event_id 到数据的映射
            event_data_map = {
                data.get("event_id"): data
                for data in batch_event_data
                if isinstance(data, dict) and "event_id" in data
            }

            # 匹配原始 event 对象和 ES 数据
            for event in events[i:i + batch_size]:
                event_data = event_data_map.get(event.id)

                if not event_data:
                    self.logger.warning(f"事项 {event.id} 在ES中未找到数据")
                    missing_vector_count += 1
                    continue

                # 获取标题向量和内容向量
                title_vector = event_data.get("title_vector")
                content_vector = event_data.get("content_vector")

                # 至少需要一个向量
                if title_vector is None and content_vector is None:
                    self.logger.warning(f"事项 {event.id} 无向量数据")
                    missing_vector_count += 1
                    continue

                # 保存事项及其向量
                events_with_vectors.append({
                    'event': event,
                    'title_vector': title_vector,
                    'content_vector': content_vector
                })

        vector_fetch_time = time.perf_counter() - vector_fetch_start
        self.logger.info(
            f"✅ 向量获取完成，成功: {len(events_with_vectors)} 个，"
            f"缺失向量: {missing_vector_count} 个，"
            f"耗时: {vector_fetch_time:.3f}秒"
        )

        if not events_with_vectors:
            self.logger.warning("所有事项都没有向量数据，无法计算相似度")
            return []

        # 批量计算加权相似度
        self.logger.info(f"开始批量计算 {len(events_with_vectors)} 个事项的加权相似度...")
        similarity_start = time.perf_counter()

        # 提取所有标题向量和内容向量
        title_vectors = [item.get('title_vector')
                         for item in events_with_vectors]
        content_vectors = [item.get('content_vector')
                           for item in events_with_vectors]

        # 使用加权相似度计算
        similarities = self._calculate_weighted_similarity(
            query_vector=query_vector,
            title_vectors=title_vectors,
            content_vectors=content_vectors,
            title_weight=0.2,
            content_weight=0.8
        )

        similarity_time = time.perf_counter() - similarity_start
        self.logger.info(
            f"加权相似度计算完成，耗时: {similarity_time:.4f}秒，"
            f"平均每个: {similarity_time/len(similarities):.6f}秒"
        )

        # 附加相似度到事项对象
        events_with_scores = []

        for idx, (item, similarity) in enumerate(zip(events_with_vectors, similarities)):
            event = item['event']

            # 为事项对象附加相似度属性
            event.similarity_score = float(similarity)
            events_with_scores.append(event)

        return events_with_scores

    def _calculate_weighted_similarity(
        self,
        query_vector: List[float],
        title_vectors: List[List[float]],
        content_vectors: List[List[float]],
        title_weight: float = 0.2,
        content_weight: float = 0.8
    ) -> np.ndarray:
        """
        计算加权相似度：query分别与title和content批量计算相似度，然后按权重合并

        优化：使用 numpy 高级索引减少中间列表操作

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

            # 转换为 numpy 数组以便向量化操作
            query_array = np.array(query_vector)

            # 初始化结果数组
            weighted_similarities = np.zeros(num_items, dtype=np.float32)

            # 1. 批量计算标题相似度（使用列表推导式 + numpy 高级索引）
            valid_title_mask = np.array(
                [vec is not None for vec in title_vectors])
            if valid_title_mask.any():
                valid_title_vectors = np.array(
                    [vec for vec in title_vectors if vec is not None], dtype=np.float32)
                title_sims = self._batch_cosine_similarity(
                    query_array, valid_title_vectors)
                weighted_similarities[valid_title_mask] += title_weight * title_sims

            # 2. 批量计算内容相似度
            valid_content_mask = np.array(
                [vec is not None for vec in content_vectors])
            if valid_content_mask.any():
                valid_content_vectors = np.array(
                    [vec for vec in content_vectors if vec is not None], dtype=np.float32)
                content_sims = self._batch_cosine_similarity(
                    query_array, valid_content_vectors)
                weighted_similarities[valid_content_mask] += content_weight * content_sims

            return weighted_similarities

        except Exception as e:
            self.logger.error(f"加权相似度计算错误: {e}")
            return np.zeros(num_items)

    def _batch_cosine_similarity(
        self,
        query_vector: np.ndarray,
        target_vectors: np.ndarray
    ) -> np.ndarray:
        """
        批量计算query向量与多个目标向量的余弦相似度

        优化：使用 float32 减少内存占用和计算量，直接接收 numpy 数组

        Args:
            query_vector: 查询向量（numpy数组）
            target_vectors: 目标向量数组（2D numpy数组）

        Returns:
            余弦相似度数组
        """
        try:
            # 确保是 float32 类型
            if query_vector.dtype != np.float32:
                query_vector = query_vector.astype(np.float32)
            if target_vectors.dtype != np.float32:
                target_vectors = target_vectors.astype(np.float32)

            # 计算点积（使用矩阵乘法，更快）
            dot_products = np.dot(target_vectors, query_vector)

            # 计算范数（使用 axis=1 向量化计算）
            query_norm = np.linalg.norm(query_vector)
            target_norms = np.linalg.norm(target_vectors, axis=1)

            # 计算相似度（避免除以零，使用向量化操作）
            denominators = target_norms * query_norm
            similarities = np.divide(
                dot_products,
                denominators,
                out=np.zeros_like(dot_products),
                where=denominators > 1e-8  # 使用小阈值而不是0，更稳定
            )

            return similarities

        except Exception as e:
            self.logger.error(f"批量余弦相似度计算错误: {e}")
            return np.zeros(len(target_vectors), dtype=np.float32)

    def _filter_by_threshold(
        self,
        events: List[SourceEvent],
        threshold: float
    ) -> List[SourceEvent]:
        """
        步骤5: 阈值过滤

        过滤掉 Embedding 相似度低于阈值的事项

        Args:
            events: 附加了 similarity_score 的事项列表
            threshold: 相似度阈值

        Returns:
            List[SourceEvent]: 过滤后的事项列表
        """
        before_filter_count = len(events)
        filtered_events = [
            event for event in events
            if event.similarity_score >= threshold
        ]
        after_filter_count = len(filtered_events)
        filtered_count = before_filter_count - after_filter_count

        self.logger.info("=" * 80)
        self.logger.info(
            f"阈值过滤完成: 阈值={threshold:.2f}, "
            f"过滤前={before_filter_count}个, "
            f"过滤后={after_filter_count}个, "
            f"过滤掉={filtered_count}个"
        )

        if not filtered_events:
            self.logger.warning(f"阈值过滤后没有剩余事项（所有事项相似度 < {threshold:.2f}）")

        return filtered_events

    async def _rank_by_rrf(
        self,
        events: List[SourceEvent],
        query: str,
        top_k: int,
        rrf_k: int = 60
    ) -> List[SourceEvent]:
        """
        步骤7: RRF (Reciprocal Rank Fusion) 融合排序

        融合 Embedding 相似度排序和 BM25 排序的结果，计算 RRF 分数

        RRF 公式：
        RRF_score(d) = Σ 1/(k + rank_i(d))

        在我们的场景中：
        RRF_score(event) = 1/(k + embedding_rank) + 1/(k + bm25_rank)

        Args:
            events: 通过 Embedding 阈值过滤的事项列表（已有 similarity_score）
            query: 查询文本
            top_k: 返回数量
            rrf_k: RRF 常数，默认60（平衡不同排序系统的影响）

        Returns:
            List[SourceEvent]: RRF 融合排序后的事项列表（附加 rrf_score 属性）
        """
        try:
            self.logger.info(f"开始 RRF 融合排序，处理 {len(events)} 个事项，k={rrf_k}...")
            rrf_start = time.perf_counter()

            # 第1步：按 Embedding 相似度排序，获取排名
            self.logger.debug("步骤1: 按 Embedding 相似度排序...")
            embedding_sorted = sorted(
                events,
                key=lambda x: x.similarity_score,
                reverse=True
            )

            # 为每个事项附加 Embedding 排名（直接设置属性，避免创建字典）
            for rank, event in enumerate(embedding_sorted, start=1):
                event.embedding_rank = rank

            # 第2步：计算 BM25 分数并排序，获取排名
            self.logger.debug("步骤2: 计算 BM25 分数并排序...")
            events_with_bm25 = await self._calculate_bm25_scores(events, query)

            bm25_sorted = sorted(
                events_with_bm25,
                key=lambda x: x.bm25_score,
                reverse=True
            )

            # 为每个事项附加 BM25 排名
            for rank, event in enumerate(bm25_sorted, start=1):
                event.bm25_rank = rank

            # 第3步：计算 RRF 分数（向量化操作）
            self.logger.debug("步骤3: 计算 RRF 融合分数...")
            default_rank = len(events) + 1

            for event in events:
                # 直接从属性读取，避免字典查找
                embedding_rank = getattr(event, 'embedding_rank', default_rank)
                bm25_rank = getattr(event, 'bm25_rank', default_rank)

                # RRF 公式：1/(k + rank1) + 1/(k + rank2)
                rrf_score = (1.0 / (rrf_k + embedding_rank)) + \
                    (1.0 / (rrf_k + bm25_rank))
                event.rrf_score = rrf_score

            # 第4步：按 RRF 分数排序（降序）
            self.logger.debug("步骤4: 按 RRF 分数排序...")
            rrf_sorted = sorted(
                events,
                key=lambda x: x.rrf_score,
                reverse=True
            )

            # 第5步：取 Top-K
            result_events = rrf_sorted[:top_k]

            rrf_time = time.perf_counter() - rrf_start
            self.logger.info(
                f"RRF 融合排序完成，耗时: {rrf_time:.4f}秒，"
                f"返回 {len(result_events)} 个事项"
            )

            # 记录详细结果
            self.logger.info("=" * 80)
            self.logger.info(
                f"RRF 融合排序结果（Top {min(len(result_events), top_k)}）：")
            self.logger.info("-" * 80)

            for i, event in enumerate(result_events[:top_k], 1):
                title = (event.title or "无标题")[:50]
                embedding_sim = getattr(event, 'similarity_score', 0.0)
                embedding_rank = getattr(event, 'embedding_rank', 0)
                bm25_score = getattr(event, 'bm25_score', 0.0)
                bm25_rank = getattr(event, 'bm25_rank', 0)
                rrf_score = getattr(event, 'rrf_score', 0.0)

                self.logger.info(
                    f"Rank {i}: {title}\n"
                    f"  Embedding: score={embedding_sim:.4f}, rank={embedding_rank}\n"
                    f"  BM25: score={bm25_score:.4f}, rank={bm25_rank}\n"
                    f"  RRF: score={rrf_score:.6f}"
                )

            self.logger.info("=" * 80)

            return result_events

        except Exception as e:
            self.logger.error(f"RRF 融合排序失败: {e}", exc_info=True)
            # 降级方案：按 Embedding 相似度排序
            self.logger.warning("RRF 融合失败，降级为 Embedding 相似度排序")
            sorted_events = sorted(
                events,
                key=lambda x: x.similarity_score,
                reverse=True
            )
            return sorted_events[:top_k]

    async def _calculate_bm25_scores(
        self,
        events: List[SourceEvent],
        query: str
    ) -> List[SourceEvent]:
        """
        计算 BM25 分数（不进行排序和截断，仅计算分数）

        使用 fast_mode 跳过 spaCy 分词，只用 jieba + 空格分词，提升性能

        Args:
            events: 事项列表
            query: 查询文本

        Returns:
            List[SourceEvent]: 附加了 bm25_score 的事项列表
        """
        try:
            bm25_start = time.perf_counter()

            # 获取全局单例分词器
            tokenizer = get_mixed_tokenizer()

            # 预处理查询（只需一次）
            query_lower = query.lower()

            # 构建并分词文档语料库（一次性完成，减少中间列表）
            tokenize_start = time.perf_counter()
            tokenized_corpus = []

            for event in events:
                # 使用 join 代替 f-string，减少内存分配
                parts = []
                if event.title:
                    parts.append(event.title)
                if event.summary:
                    parts.append(event.summary)
                if event.content:
                    parts.append(event.content)

                # 一次性拼接并小写转换
                doc_text = ' '.join(parts).lower()

                # 直接分词并添加到结果（避免先创建 corpus 列表）
                tokenized_corpus.append(
                    tokenizer.tokenize(doc_text, fast_mode=True))

            # 分词查询
            tokenized_query = tokenizer.tokenize(query_lower, fast_mode=True)
            tokenize_time = time.perf_counter() - tokenize_start

            # 日志：展示 query 分词结果
            self.logger.info(
                f"Query 分词结果: '{query}' -> {tokenized_query} "
                f"(共 {len(tokenized_query)} 个词)"
            )

            # 计算 BM25 分数（延迟导入）
            bm25_calc_start = time.perf_counter()
            try:
                from rank_bm25 import BM25Okapi
                bm25 = BM25Okapi(tokenized_corpus)
                scores = bm25.get_scores(tokenized_query)
            except ImportError:
                self.logger.warning("rank_bm25未安装，BM25分数将使用默认值0")
                scores = [0.0] * len(events)
            bm25_calc_time = time.perf_counter() - bm25_calc_start

            # 为每个事项附加 BM25 分数
            for event, score in zip(events, scores):
                event.bm25_score = float(score)

            bm25_total_time = time.perf_counter() - bm25_start
            self.logger.debug(
                f"BM25计算耗时: 总计={bm25_total_time:.4f}秒, "
                f"分词={tokenize_time:.4f}秒, BM25计算={bm25_calc_time:.4f}秒"
            )

            return events

        except Exception as e:
            self.logger.error(f"BM25 分数计算失败: {e}", exc_info=True)
            # 降级：所有事项分数为 0
            for event in events:
                event.bm25_score = 0.0
            return events

    async def _rank_by_bm25(
        self,
        events: List[SourceEvent],
        query: str,
        top_k: int
    ) -> List[SourceEvent]:
        """
        步骤6: BM25 重排序（精排）

        使用 BM25 算法对通过 Embedding 阈值的事项进行关键词匹配重排序

        Args:
            events: 通过 Embedding 阈值过滤的事项列表
            query: 查询文本
            top_k: 返回数量

        Returns:
            List[SourceEvent]: BM25 排序后的事项列表（附加 bm25_score 和 bm25_rank 属性）
        """
        try:
            self.logger.info(f"开始 BM25 重排序，处理 {len(events)} 个事项...")
            bm25_start = time.perf_counter()

            # 获取全局单例分词器
            tokenizer = get_mixed_tokenizer()

            # 预处理查询（只需一次）
            query_lower = query.lower()

            # 构建并分词文档语料库（一次性完成）
            self.logger.debug("开始对文档语料库和查询进行分词...")
            tokenize_start = time.perf_counter()
            tokenized_corpus = []

            for event in events:
                # 使用 join 代替 f-string，减少内存分配
                parts = []
                if event.title:
                    parts.append(event.title)
                if event.summary:
                    parts.append(event.summary)
                if event.content:
                    parts.append(event.content)

                # 一次性拼接、小写转换并分词
                doc_text = ' '.join(parts).lower()
                tokenized_corpus.append(
                    tokenizer.tokenize(doc_text, fast_mode=True))

            # 分词查询
            tokenized_query = tokenizer.tokenize(query_lower, fast_mode=True)

            tokenize_time = time.perf_counter() - tokenize_start
            self.logger.debug(f"分词完成，耗时: {tokenize_time:.4f}秒")

            # 日志：展示 query 分词结果
            self.logger.info(
                f"Query 分词结果: '{query}' -> {tokenized_query} "
                f"(共 {len(tokenized_query)} 个词)"
            )

            # 计算 BM25 分数
            self.logger.debug("开始计算 BM25 分数...")
            bm25_calc_start = time.perf_counter()

            bm25 = BM25Okapi(tokenized_corpus)
            scores = bm25.get_scores(tokenized_query)

            bm25_calc_time = time.perf_counter() - bm25_calc_start
            self.logger.debug(f"BM25 分数计算完成，耗时: {bm25_calc_time:.4f}秒")

            # 为每个事项附加 BM25 分数
            events_with_bm25 = []
            for event, score in zip(events, scores):
                event.bm25_score = float(score)
                events_with_bm25.append(event)

            # 按 BM25 分数排序（降序）
            sorted_events = sorted(
                events_with_bm25,
                key=lambda x: x.bm25_score,
                reverse=True
            )

            # 为每个事项附加 BM25 排名
            for rank, event in enumerate(sorted_events, start=1):
                event.bm25_rank = rank

            # 取 Top-K
            result_events = sorted_events[:top_k]

            bm25_time = time.perf_counter() - bm25_start
            self.logger.info(
                f"BM25 重排序完成，耗时: {bm25_time:.4f}秒，"
                f"返回 {len(result_events)} 个事项"
            )

            # 记录 Top K 结果
            self.logger.info("=" * 80)
            self.logger.info(
                f"BM25 重排序结果（Top {min(len(result_events), top_k)}）：")
            self.logger.info("-" * 80)

            for i, event in enumerate(result_events[:top_k], 1):
                title = (event.title or "无标题")[:50]
                embedding_sim = getattr(event, 'similarity_score', 0.0)
                bm25_score = getattr(event, 'bm25_score', 0.0)

                self.logger.info(
                    f"Rank {i}: {title} | "
                    f"Embedding={embedding_sim:.4f}, BM25={bm25_score:.4f}"
                )

            self.logger.info("=" * 80)

            return result_events

        except Exception as e:
            self.logger.error(f"BM25 重排序失败: {e}", exc_info=True)
            # 降级方案：按 Embedding 相似度排序
            self.logger.warning("BM25 重排序失败，降级为 Embedding 相似度排序")
            sorted_events = sorted(
                events,
                key=lambda x: x.similarity_score,
                reverse=True
            )
            return sorted_events[:top_k]

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
        intermediate_events: List[SourceEvent],
        final_events: List[SourceEvent],
        event_to_clues: Dict[str, List[Dict]]
    ) -> List[Dict[str, Any]]:
        """
        构建Rerank阶段的线索（entity → event）

        🆕 修改：分两阶段生成线索
        1. 为 top-k×3 事项生成 intermediate 线索（普通模式可见）
        2. 为 Top-K 事项生成 final 线索（精简模式高亮显示）

        Args:
            config: 搜索配置
            key_final: 最终的key列表
            intermediate_events: top-k×3 事项（用于生成中间线索）
            final_events: Top-K 最终返回的事项列表（用于生成 final 线索）
            event_to_clues: 事项ID到实体列表的映射

        Returns:
            Rerank阶段的线索列表（兼容性保留，实际线索已追加到config.all_clues）
        """
        # 🆕 创建线索构建器
        tracker = Tracker(config)

        # 创建key_id到key对象的映射，方便查找权重等信息
        key_map = {key["key_id"]: key for key in key_final}

        # 创建最终事项ID集合，用于判断
        final_event_ids = {event.id for event in final_events}

        # ========== 第一步：为 top-k×3 事项生成 intermediate 线索 ==========
        self.logger.info("")
        self.logger.info("=" * 80)
        self.logger.info(f"[RRF Rerank] 生成 Intermediate 线索 (Top-{len(intermediate_events)} 事项)")
        self.logger.info("-" * 80)

        intermediate_clue_count = 0

        # 遍历 top-k×3 事项
        for rank, event in enumerate(intermediate_events, 1):
            # 获取该事项关联的实体
            source_entities = event_to_clues.get(event.id, [])

            for entity in source_entities:
                # 从key_map中获取完整的实体信息
                entity_info = key_map.get(entity["id"])
                if not entity_info:
                    # 如果找不到，使用event_to_clues中的基本信息
                    entity_info = entity

                # 构建完整的实体字典（用于标准节点构建）
                entity_dict = {
                    "key_id": entity["id"],
                    "id": entity["id"],
                    "name": entity.get("name", ""),
                    "type": entity_info.get("type", "unknown") if entity_info else "unknown",
                    "description": entity_info.get("description", "") if entity_info else ""
                }

                # 使用标准节点构建器
                entity_node = Tracker.build_entity_node(entity_dict)
                # 构建事项节点（使用tracker实例方法）
                event_node = tracker.get_or_create_event_node(
                    event,
                    "rerank",
                    recall_method="entity"
                )

                # 获取RRF分数作为置信度，fallback到similarity_score
                confidence = getattr(event, 'rrf_score', None)
                if confidence is None or confidence == 0.0:
                    # Fallback: 使用similarity_score
                    confidence = getattr(event, 'similarity_score', None)
                    if confidence is None or confidence == 0.0:
                        # 最终fallback: 使用entity权重
                        confidence = entity_info.get(
                            "weight", 0.0) if entity_info else 0.0

                # 添加 intermediate 线索
                tracker.add_clue(
                    stage="rerank",
                    from_node=entity_node,
                    to_node=event_node,
                    confidence=confidence,
                    relation="内容重排",
                    display_level="intermediate",  # intermediate 级别
                    metadata={
                        "method": "rrf",
                        "entity_weight": entity_info.get("weight", 0.0) if entity_info else 0.0,
                        "rrf_score": getattr(event, 'rrf_score', None),
                        "similarity_score": getattr(event, 'similarity_score', None),
                        "bm25_score": getattr(event, 'bm25_score', None),
                        "embedding_rank": getattr(event, 'embedding_rank', None),
                        "bm25_rank": getattr(event, 'bm25_rank', None),
                        "rank": rank
                    }
                )
                intermediate_clue_count += 1

            # 日志（只显示前10个）
            if rank <= 10:
                title_preview = event.title[:40] if event.title else "无标题"
                self.logger.info(
                    f"  Rank {rank}: {event.id[:8]}... | "
                    f"实体数={len(source_entities)} | "
                    f"标题: {title_preview}"
                )

        if len(intermediate_events) > 10:
            self.logger.info(f"  ... (还有 {len(intermediate_events) - 10} 个事项)")

        self.logger.info("-" * 80)
        self.logger.info(f"Intermediate 线索统计: entity→event={intermediate_clue_count} 条")
        self.logger.info("=" * 80)

        # ========== 第二步：为 Top-K 生成 final 线索 ==========
        self.logger.info("")
        self.logger.info("🎯 [RRF Rerank Final] 生成最终线索 (display_level=final)")
        self.logger.info(f"   为 Top-{len(final_events)} 事项生成 final 线索")

        final_clue_count = 0

        for rank, event in enumerate(final_events, 1):
            # 获取该事项关联的实体
            source_entities = event_to_clues.get(event.id, [])

            for entity in source_entities:
                # 从key_map中获取完整的实体信息
                entity_info = key_map.get(entity["id"])
                if not entity_info:
                    entity_info = entity

                # 构建完整的实体字典
                entity_dict = {
                    "key_id": entity["id"],
                    "id": entity["id"],
                    "name": entity.get("name", ""),
                    "type": entity_info.get("type", "unknown") if entity_info else "unknown",
                    "description": entity_info.get("description", "") if entity_info else ""
                }

                # 使用标准节点构建器
                entity_node = Tracker.build_entity_node(entity_dict)
                # 构建事项节点
                event_node = tracker.get_or_create_event_node(
                    event,
                    "rerank",
                    recall_method="entity"
                )

                # 获取置信度
                confidence = getattr(event, 'rrf_score', None)
                if confidence is None or confidence == 0.0:
                    confidence = getattr(event, 'similarity_score', None)
                    if confidence is None or confidence == 0.0:
                        confidence = entity_info.get("weight", 0.0) if entity_info else 0.0

                # 添加 final 线索
                tracker.add_clue(
                    stage="rerank",
                    from_node=entity_node,
                    to_node=event_node,
                    confidence=confidence,
                    relation="最终事项",
                    display_level="final",  # final 级别
                    metadata={
                        "method": "final_result",
                        "step": "step_final",
                        "entity_weight": entity_info.get("weight", 0.0) if entity_info else 0.0,
                        "rrf_score": getattr(event, 'rrf_score', None),
                        "similarity_score": getattr(event, 'similarity_score', None),
                        "bm25_score": getattr(event, 'bm25_score', None),
                        "embedding_rank": getattr(event, 'embedding_rank', None),
                        "bm25_rank": getattr(event, 'bm25_rank', None),
                        "rank": rank
                    }
                )
                final_clue_count += 1

                self.logger.debug(
                    f"  Final: {entity['id'][:8]}... ('{entity.get('name', '')[:20]}') "
                    f"→ {event.id[:8]}... ('{event.title[:30]}', RRF={getattr(event, 'rrf_score', 0.0):.4f})"
                )

        self.logger.info(
            f"✅ [RRF Rerank Final] 生成了 {final_clue_count} 条最终线索"
        )
        self.logger.info(
            f"✅ [RRF Rerank Final] 前端可根据这些 final 线索反推完整推理路径："
        )
        self.logger.info(f"   - Entity召回: query → entity → event")
        self.logger.info("")

        self.logger.info(
            f"🔍 [Rerank总计] 线索统计: intermediate={intermediate_clue_count}条, final={final_clue_count}条"
        )

        # 返回空列表（兼容性保留，实际线索已通过tracker追加到config.all_clues）
        return []
