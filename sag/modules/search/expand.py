"""
实体扩展模块（Expand）

实现多跳循环搜索算法：
1. 根据[key-final]，用sql查找到所有关联的event，得到新的[Event-key-related-2]
2. 计算原始query和新的[Event-key-related-2]的相似度，得到相似性向量(event-query-2)
3. 计算Event-key-related-2权重向量：根据每个event包含key-final的情况，将对应key的权重(key-final)相加
4. 计算event-key-query权重向量：将（event-key-2）*(event-query-2)，得到新的（event-jump-2）
5. 反向计算key权重向量：根据event权重反向得出event里所有的key的重要性

新特性：topkey 去重机制
- topkey 限制每一跳的最大新 key 数量（不是最终返回总数）
- 每一跳都会去重，去掉前面已经出现过的 key
- 最终返回所有发现的唯一 keys，最大数量为 topkey * max_jumps
"""

from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
import numpy as np
import math

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from sag.core.ai.base import BaseLLMClient
from sag.core.ai.models import LLMMessage, LLMRole
from sag.core.prompt.manager import PromptManager
from sag.core.storage.elasticsearch import get_es_client
from sag.core.storage.repositories.entity_repository import EntityVectorRepository
from sag.core.storage.repositories.event_repository import EventVectorRepository
from sag.db import SourceEvent, Entity, EventEntity, get_session_factory
from sag.exceptions import AIError
from sag.modules.load.processor import DocumentProcessor
from sag.modules.search.config import SearchConfig
from sag.modules.search.recall import RecallSearcher, RecallResult
from sag.modules.search.tracker import Tracker  # 🆕 统一使用Tracker
from sag.utils import get_logger

logger = get_logger("search.expand")


@dataclass
class ExpandResult:
    """实体扩展结果"""
    # 最终结果
    key_final: List[Dict[str, Any]]  # [{"key_id": str, "name": str, "weight": float, "steps": [int], "hop": int}, ...]
                                        # steps只包含一个数字，表示key被最早发现的步骤
                                        # steps=1: Recall中发现, steps=2: Expand第1跳发现, steps=3: Expand第2跳发现, 以此类推
                                        # hop: 跳数编号，用于前端颜色区分
                                        #      hop=0: Recall阶段 (建议最深色)
                                        #      hop=1: 第1跳 (建议深色)
                                        #      hop=2: 第2跳 (建议中等色)
                                        #      hop=N: 第N跳 (建议由深到浅渐变)
                                        # 注意：现在返回所有发现的唯一keys，不再受top_n_keys或final_key_threshold限制

    # 多跳结果
    jump_results: List[Dict[str, Any]]  # 每一跳的结果

    # 聚合统计
    total_jumps: int  # 实际跳跃次数
    convergence_reached: bool  # 是否收敛

    # 中间结果（用于调试）
    all_events_by_jump: Dict[int, List[str]]  # 每跳找到的events
    all_keys_by_jump: Dict[int, List[str]]    # 每跳计算的keys（去重后的新keys）
    weight_evolution: Dict[int, Dict[str, float]]  # 权重演化


class ExpandSearcher:
    """实体扩展搜索器 - 实现多跳循环搜索算法"""

    def __init__(
        self,
        llm_client: BaseLLMClient,
        prompt_manager: PromptManager,
        recall_searcher: RecallSearcher,
    ):
        """
        初始化实体扩展搜索器

        Args:
            llm_client: LLM客户端
            prompt_manager: 提示词管理器
            recall_searcher: 实体召回搜索器实例
        """
        self.llm_client = llm_client
        self.prompt_manager = prompt_manager
        self.recall_searcher = recall_searcher
        self.session_factory = get_session_factory()
        self.logger = get_logger("search.expand")

        # 初始化Elasticsearch仓库
        self.es_client = get_es_client()
        self.entity_repo = EntityVectorRepository(self.es_client)
        self.event_repo = EventVectorRepository(self.es_client)

        # 初始化文档处理器用于生成向量
        self.processor = DocumentProcessor(llm_client=llm_client)

        self.logger.info(
            "实体扩展搜索器初始化完成",
            extra={
                "embedding_model_name": self.processor.embedding_model_name,
            },
        )

    async def _calculate_cosine_similarity(self, vector1: List[float], vector2: List[float]) -> float:
        """
        计算两个向量的余弦相似度

        Args:
            vector1: 第一个向量
            vector2: 第二个向量

        Returns:
            余弦相似度，范围在[0, 1]之间
        """
        if not vector1 or not vector2:
            return 0.0

        # 转换为numpy数组（使用 float32 优化）
        v1 = np.array(vector1, dtype=np.float32)
        v2 = np.array(vector2, dtype=np.float32)

        # 检查向量长度是否一致
        if len(v1) != len(v2):
            return 0.0

        try:
            # 计算余弦相似度
            dot_product = np.dot(v1, v2)
            norm1 = np.linalg.norm(v1)
            norm2 = np.linalg.norm(v2)

            # 避免除零错误
            if norm1 == 0 or norm2 == 0:
                return 0.0

            similarity = dot_product / (norm1 * norm2)
            # 确保结果在[0, 1]范围内
            return max(0.0, min(1.0, float(similarity)))

        except Exception as e:
            self.logger.warning(f"计算余弦相似度时出错: {e}")
            return 0.0

    async def _batch_cosine_similarity(
        self,
        query_embedding: List[float],
        target_vectors: List[List[float]]
    ) -> np.ndarray:
        """
        批量计算 query 向量与多个目标向量的余弦相似度

        优化：使用 numpy 向量化操作，减少循环开销

        Args:
            query_embedding: 查询向量
            target_vectors: 目标向量列表

        Returns:
            余弦相似度数组
        """
        try:
            if not target_vectors:
                return np.array([])

            # 转换为 numpy 数组（使用 float32 减少内存和计算量）
            query_array = np.array(query_embedding, dtype=np.float32)
            target_array = np.array(target_vectors, dtype=np.float32)

            # 计算点积（矩阵乘法，比循环快得多）
            dot_products = np.dot(target_array, query_array)

            # 计算范数
            query_norm = np.linalg.norm(query_array)
            target_norms = np.linalg.norm(target_array, axis=1)

            # 计算相似度（向量化操作，避免除以零）
            denominators = target_norms * query_norm
            similarities = np.divide(
                dot_products,
                denominators,
                out=np.zeros_like(dot_products),
                where=denominators > 1e-8  # 使用小阈值而不是0，更稳定
            )

            # 确保结果在 [0, 1] 范围内
            similarities = np.clip(similarities, 0.0, 1.0)

            return similarities

        except Exception as e:
            self.logger.error(f"批量余弦相似度计算错误: {e}")
            return np.zeros(len(target_vectors), dtype=np.float32)

    async def search(self, config: SearchConfig, recall_result: Optional[RecallResult] = None) -> ExpandResult:
        """
        执行多跳循环搜索算法

        Args:
            config: 搜索配置
            recall_result: 实体召回结果（可选，如果不提供则会自动执行召回）

        Returns:
            实体扩展结果
        """
        try:
            self.logger.info(
                f"开始实体扩展：source_config_ids={config.source_config_ids}, query={config.query}, "
                f"max_jumps={config.expand.max_hops}"
            )

            # 如果没有提供Recall结果，先执行Recall
            if recall_result is None:
                self.logger.info("未提供Recall结果，先执行Recall搜索")
                recall_result = await self.recall_searcher.search(config)

            # 提取Recall的最终keys作为起始点
            key_final_ids = [key["key_id"] for key in recall_result.key_final]
            key_final_weights = {key["key_id"]: key["weight"] for key in recall_result.key_final}

            if not key_final_ids:
                self.logger.warning("实体召回没有产生有效的keys，无法进行实体扩展")
                return ExpandResult(
                    key_final=[],
                    jump_results=[],
                    total_jumps=0,
                    convergence_reached=False,
                    all_events_by_jump={},
                    all_keys_by_jump={},
                    weight_evolution={},
                )

            self.logger.info(f"从Recall获得 {len(key_final_ids)} 个起始keys")

            # 初始化多跳循环变量
            jump_results = []
            all_events_by_jump = {}
            all_keys_by_jump = {}
            weight_evolution = {}

            # 添加全局去重集合，记录所有发现过的keys
            all_discovered_keys = set(key_final_ids)  # 初始包含Recall的所有keys

            # ===  线索追踪：记录parent关系 ===
            key_parent_map = {}  # {child_key_id: {"parent_id": str, "parent_name": str, "parent_type": str, "hop": int}}

            # 🆕 记录第一跳中没有扩展出新实体的 recall keys
            no_expansion_recall_keys = []  # List[str]: key_ids

            current_key_ids = key_final_ids.copy()
            current_key_weights = key_final_weights.copy()
            previous_total_weight = 0.0

            # 开始多跳循环
            for jump in range(1, config.expand.max_hops + 1):
                self.logger.info(f"=== 开始第 {jump} 跳 ===")

                # 1. 根据当前keys查找到所有关联的events
                event_key_related_2 = await self._step1_keys_to_events(current_key_ids)
                all_events_by_jump[jump] = event_key_related_2

                if not event_key_related_2:
                    self.logger.warning(f"第 {jump} 跳：没有找到关联的events，停止跳跃")
                    break

                self.logger.info(f"第 {jump} 跳步骤1：找到 {len(event_key_related_2)} 个关联events")

                # 2. 计算原始query和新的events的相似度
                event_query_2, e2_weights = await self._step2_calculate_event_query_similarity(
                    config, event_key_related_2
                )

                if not event_query_2:
                    self.logger.warning(f"第 {jump} 跳：没有找到相似events，停止跳跃")
                    break

                # 📊 诊断：第1跳时检查 key-final 各个 key 的 events 过滤情况
                if jump == 1:
                    await self._diagnose_key_final_event_filtering(
                        current_key_ids,
                        current_key_weights,
                        event_key_related_2,
                        e2_weights
                    )

                self.logger.info(f"第 {jump} 跳步骤2：计算了 {len(event_query_2)} 个events的相似度")

                # 📊 日志：显示最终选定的事项
                self.logger.info(f"📊 第 {jump} 跳最终选定事项列表 (共{len(event_query_2)}个):")
                sorted_events = sorted(event_query_2, key=lambda x: x.get("similarity", 0), reverse=True)
                for i, event in enumerate(sorted_events[:5], 1):  # 显示前5个
                    event_id = event.get("event_id", "")
                    title = event.get("title", "")[:40]
                    similarity = event.get("similarity", 0)
                    self.logger.info(f"  {i}. [{event_id[:8]}] {title}... (相似度={similarity:.3f})")
                if len(event_query_2) > 5:
                    self.logger.info(f"  ... 还有 {len(event_query_2) - 5} 个事项")

                # 3. 计算event-key权重向量
                event_key_2 = await self._step3_calculate_event_key_weights(
                    event_key_related_2, current_key_ids, current_key_weights
                )

                self.logger.info(f"第 {jump} 跳步骤3：计算了 {len(event_key_2)} 个events的key权重")

                # 4. 计算event-key-query权重向量
                event_jump_2 = await self._step4_calculate_event_key_query_weights(
                    event_key_2, e2_weights
                )

                self.logger.info(f"第 {jump} 跳步骤4：计算了 {len(event_jump_2)} 个events的复合权重")

                # 5. 反向计算key权重向量 + 追踪扩展关系
                new_key_weights, key_expansion_trace = await self._step5_calculate_key_event_weights(
                    event_key_related_2, current_key_ids, event_jump_2
                )

                self.logger.info(f"第 {jump} 跳步骤5：计算了 {len(new_key_weights)} 个keys的新权重, 追踪到 {len(key_expansion_trace)} 个扩展关系")

                # 📊 诊断：第1跳时检查每个 parent key 扩展出的 child entities 情况
                if jump == 1:
                    # 先过滤出新的 entities（不包括已知的 parent keys）
                    new_unique_keys = [(key_id, weight) for key_id, weight in new_key_weights.items()
                                     if key_id not in all_discovered_keys]

                    # 按权重排序
                    sorted_new_keys = sorted(new_unique_keys, key=lambda x: x[1], reverse=True)
                    top_new_keys_preview = sorted_new_keys[:config.expand.entities_per_hop]

                    no_expansion_recall_keys = await self._diagnose_key_expansion_success(
                        current_key_ids,
                        current_key_weights,
                        key_expansion_trace,
                        new_key_weights,
                        top_new_keys_preview,
                        all_discovered_keys,
                        config.expand.entities_per_hop
                    )

                # 记录权重演化
                weight_evolution[jump] = new_key_weights.copy()

                # 检查收敛性
                current_total_weight = sum(new_key_weights.values())
                weight_change = abs(current_total_weight - previous_total_weight)
                previous_total_weight = current_total_weight

                # 收集当前跳的结果
                jump_result = {
                    "jump": jump,
                    "events_found": len(event_key_related_2),
                    "events_similar": len(event_query_2),
                    "keys_count": len(new_key_weights),
                    "total_weight": current_total_weight,
                    "weight_change": weight_change,
                }
                jump_results.append(jump_result)

                self.logger.info(f"第 {jump} 跳完成：总权重={current_total_weight:.4f}, 权重变化={weight_change:.4f}")

                # 检查收敛条件
                if weight_change < config.expand.weight_change_threshold:
                    self.logger.info(f"第 {jump} 跳：权重变化 {weight_change:.4f} 小于收敛阈值 {config.expand.weight_change_threshold}，停止跳跃")
                    convergence_reached = True
                    break

                # 更新当前keys和权重，为下一跳准备
                # 首先过滤掉已经发现过的keys，实现去重
                new_unique_keys = [(key_id, weight) for key_id, weight in new_key_weights.items()
                                 if key_id not in all_discovered_keys]

                # 按权重排序，选择权重最高的topkey个新keys
                sorted_new_keys = sorted(new_unique_keys, key=lambda x: x[1], reverse=True)
                top_new_keys = sorted_new_keys[:config.expand.entities_per_hop]

                # 📊 日志：显示最终选定的新实体
                if top_new_keys:
                    self.logger.info(f"📊 第 {jump} 跳最终选定新实体列表 (共{len(top_new_keys)}个):")
                    try:
                        key_ids = [key_id for key_id, _ in top_new_keys]
                        async with self.session_factory() as session:
                            query = select(Entity).where(Entity.id.in_(key_ids))
                            result = await session.execute(query)
                            entities = {entity.id: entity for entity in result.scalars().all()}

                        for i, (key_id, weight) in enumerate(top_new_keys, 1):
                            entity = entities.get(key_id)
                            if entity:
                                self.logger.info(f"  {i}. [{entity.type}] {entity.name} (权重={weight:.3f})")
                            else:
                                self.logger.info(f"  {i}. {key_id[:12]}... (权重={weight:.3f})")
                    except Exception as e:
                        self.logger.warning(f"查询实体名称失败: {e}")
                        for i, (key_id, weight) in enumerate(top_new_keys, 1):
                            self.logger.info(f"  {i}. {key_id[:12]}... (权重={weight:.3f})")
                else:
                    self.logger.info(f"📊 第 {jump} 跳没有选定新实体")

                # === 记录parent关系（使用真实的扩展路径） ===
                # 为每个新发现的key记录它是从哪个parent key通过哪个event扩展而来的
                for child_key_id, child_weight in top_new_keys:
                    # 从key_expansion_trace中获取真实的扩展关系
                    if child_key_id in key_expansion_trace:
                        expansion_paths = key_expansion_trace[child_key_id]
                        # 选择权重最高的扩展路径作为主parent
                        # expansion_paths: [(parent_id, event_id, event_weight), ...]
                        best_path = max(expansion_paths, key=lambda x: x[2])  # x[2]是event_weight
                        parent_id, event_id, event_weight = best_path

                        key_parent_map[child_key_id] = {
                            "parent_id": parent_id,
                            "event_id": event_id,  # 记录扩展所通过的event
                            "event_weight": event_weight,  # 记录event的权重
                            "hop": jump,  # 记录在第几跳发现的
                            "num_paths": len(expansion_paths),  # 记录总共有多少条扩展路径
                        }

                        self.logger.debug(
                            f"  ✅ 记录扩展关系: {child_key_id[:8]} ← {parent_id[:8]} "
                            f"(via event {event_id[:8]}, weight={event_weight:.3f}, "
                            f"{len(expansion_paths)}条可选路径)"
                        )
                    else:
                        # Fallback：如果没有追踪到扩展关系（理论上不应该发生）
                        self.logger.warning(
                            f"  ⚠️  未追踪到 {child_key_id[:8]} 的扩展关系，使用fallback逻辑"
                        )
                        if current_key_ids:
                            parent_id = max(current_key_ids, key=lambda k: current_key_weights.get(k, 0))
                            key_parent_map[child_key_id] = {
                                "parent_id": parent_id,
                                "hop": jump,
                                "is_fallback": True  # 标记为fallback
                            }

                # 🔍 诊断日志：记录parent关系
                self.logger.info(
                    f"🔍 [Expand诊断] 第{jump}跳parent关系: "
                    f"为{len(top_new_keys)}个新key记录parent, "
                    f"其中{sum(1 for k in [k[0] for k in top_new_keys] if k in key_expansion_trace)}个来自真实扩展路径, "
                    f"当前parent_map总数={len(key_parent_map)}"
                )

                # 更新全局去重集合，添加新发现的keys
                for key_id, _ in top_new_keys:
                    all_discovered_keys.add(key_id)

                # 设置当前跳的keys和权重
                current_key_ids = [key_id for key_id, _ in top_new_keys]
                current_key_weights = {key_id: weight for key_id, weight in top_new_keys}

                all_keys_by_jump[jump] = current_key_ids.copy()

                self.logger.info(f"第 {jump} 跳：发现 {len(new_key_weights)} 个keys，去重后选择 {len(current_key_ids)} 个新keys进入下一跳")
                self.logger.info(f"第 {jump} 跳：累计已发现 {len(all_discovered_keys)} 个唯一keys")

            # 汇总最终结果
            final_key_weights = await self._aggregate_key_weights(weight_evolution)
            key_final = await self._extract_final_keys(
                final_key_weights, config, recall_result.key_final, weight_evolution, all_discovered_keys, key_parent_map
            )

            # 🔍 诊断日志：parent_entity字段验证
            keys_with_parent = sum(1 for k in key_final if "parent_entity" in k)
            expand_keys = sum(1 for k in key_final if k.get("steps", [0])[0] >= 2)
            self.logger.info(
                f"🔍 [Expand诊断] parent_entity字段统计: "
                f"总keys={len(key_final)}, "
                f"Expand发现={expand_keys}, "
                f"有parent_entity={keys_with_parent}"
            )
            if expand_keys != keys_with_parent:
                self.logger.warning(
                    f"⚠️ [Expand诊断] parent_entity缺失: "
                    f"Expand发现了{expand_keys}个key，但只有{keys_with_parent}个有parent_entity字段！"
                )

            # 🎨 日志：按hop统计实体数量（用于前端颜色分层）
            hop_stats = {}
            for key in key_final:
                hop = key.get("hop", 0)
                hop_stats[hop] = hop_stats.get(hop, 0) + 1

            self.logger.info("🎨 [颜色分层] 按hop统计实体数量 (hop越大，建议颜色越浅):")
            for hop in sorted(hop_stats.keys()):
                hop_name = "Recall阶段" if hop == 0 else f"第{hop}跳"
                self.logger.info(f"  hop={hop} ({hop_name}): {hop_stats[hop]}个实体")

            # === 🆕 生成最终线索 (display_level="final") ===
            # 为所有key_final中的实体生成最终线索
            # 前端精简模式：只显示这些 final 线索
            # 前端可以根据 final 线索反推完整路径（包含 key→event→key）

            # 🆕 创建统一的 tracker 实例，确保整个 expand 阶段使用同一个缓存
            tracker = Tracker(config)

            if key_final:
                self.logger.info(f"🎯 [Expand Final] 生成 {len(key_final)} 条最终线索 (display_level=final)")

                # 🆕 批量查询所有需要的 event 信息（避免 N+1 查询）
                event_ids_needed = set()
                for key in key_final:
                    steps = key.get("steps", [0])[0]
                    if steps >= 2:
                        parent_info = key_parent_map.get(key["key_id"])
                        if parent_info and "event_id" in parent_info:
                            event_ids_needed.add(parent_info["event_id"])

                # 批量查询 events
                event_map = {}
                if event_ids_needed:
                    try:
                        async with self.session_factory() as session:
                            query = select(SourceEvent).where(SourceEvent.id.in_(list(event_ids_needed)))
                            result = await session.execute(query)
                            events = result.scalars().all()
                            event_map = {event.id: event for event in events}
                            self.logger.info(f"📦 [Expand Final] 批量查询了 {len(event_map)} 个event用于构建final线索")
                    except Exception as e:
                        self.logger.warning(f"⚠️ [Expand Final] 批量查询events失败: {e}，将使用简化线索")

                # 统计线索生成情况
                final_clues_count = 0
                recall_keys_count = 0
                expand_keys_count = 0
                expand_with_event_count = 0
                expand_without_event_count = 0

                for key in key_final:
                    steps = key.get("steps", [0])[0]

                    if steps == 1:
                        # Recall 阶段的 key：生成 query → entity 线索
                        recall_keys_count += 1

                        entity_dict = {
                            "id": key["key_id"],
                            "key_id": key["key_id"],
                            "name": key["name"],
                            "type": key["type"],
                            "description": key.get("description", ""),
                            "hop": key.get("hop", 0)
                        }

                        # 获取实体相似度作为confidence
                        entity_similarity = entity_dict.get("similarity", 0.0)
                        # 获取实体权重信息（如果有）
                        entity_weight = key.get("weight")
                        metadata = {
                            "method": "final_result",
                            "step": "recall",
                            "steps": key.get("steps", [1]),
                            "hop": key.get("hop", 0)
                        }
                        # 只有to节点是实体时才存储weight
                        if entity_weight is not None:
                            metadata["weight"] = entity_weight

                        tracker.add_clue(
                            stage="expand",
                            from_node=Tracker.build_query_node(config),
                            to_node=Tracker.build_entity_node(entity_dict),
                            confidence=entity_similarity,  # 统一使用similarity
                            relation="召回起点",
                            display_level="final",  # 🆕 标记为最终结果
                            metadata=metadata
                        )
                        final_clues_count += 1

                    elif steps >= 2:
                        # Expand 阶段的 key：生成 parent_entity → event → child_entity 线索（两条）
                        expand_keys_count += 1

                        if "parent_entity" in key:
                            parent_entity = key["parent_entity"]

                            parent_entity_dict = {
                                "id": parent_entity["id"],
                                "key_id": parent_entity["id"],
                                "name": parent_entity["name"],
                                "type": parent_entity["type"],
                                "description": parent_entity.get("description", ""),
                                "hop": parent_entity.get("hop", 0)
                            }

                            child_entity_dict = {
                                "id": key["key_id"],
                                "key_id": key["key_id"],
                                "name": key["name"],
                                "type": key["type"],
                                "description": key.get("description", ""),
                                "hop": key.get("hop", 0)
                            }

                            # 🆕 检查是否有 event_id（从 key_parent_map 获取）
                            parent_info = key_parent_map.get(key["key_id"])
                            if parent_info and "event_id" in parent_info:
                                # 有 event_id：生成两条线索 parent_entity → event, event → child_entity
                                event_id = parent_info["event_id"]
                                event_weight = parent_info.get("event_weight", 1.0)
                                current_hop = key.get("hop", 1)  # 🆕 获取当前跳数

                                # 从批量查询结果中获取 event 信息
                                event_obj = event_map.get(event_id)
                                if event_obj:
                                    expand_with_event_count += 1

                                    parent_node = Tracker.build_entity_node(parent_entity_dict)
                                    # 🆕 使用 tracker 实例方法，传递 stage 和 hop，确保不同跳生成不同节点
                                    event_node = tracker.get_or_create_event_node(event_obj, stage="expand", hop=current_hop)
                                    child_node = Tracker.build_entity_node(child_entity_dict)

                                    # 获取实体相似度作为confidence
                                    parent_similarity = parent_entity_dict.get("similarity", 0.0)
                                    child_similarity = child_entity_dict.get("similarity", 0.0)

                                    # 第一条线索：parent_entity → event（to节点是事件，不存储weight）
                                    metadata1 = {
                                        "method": "final_result",
                                        "step": f"expand_hop{current_hop}",
                                        "steps": key.get("steps", [2]),
                                        "hop": current_hop
                                    }

                                    tracker.add_clue(
                                        stage="expand",
                                        from_node=parent_node,
                                        to_node=event_node,
                                        confidence=parent_similarity,  # 统一使用similarity
                                        relation="共现事项",
                                        display_level="final",  # 🆕 标记为最终结果
                                        metadata=metadata1
                                    )
                                    final_clues_count += 1

                                    # 获取子实体权重信息（如果有）
                                    child_entity_weight = key.get("weight")
                                    # 第二条线索：event → child_entity（to节点是实体，需要weight）
                                    metadata2 = {
                                        "method": "final_result",
                                        "step": f"expand_hop{key.get('hop', 1)}",
                                        "steps": key.get("steps", [2]),
                                        "hop": key.get("hop", 1)
                                    }
                                    # 只有to节点是实体时才存储weight
                                    if child_entity_weight is not None:
                                        metadata2["weight"] = child_entity_weight

                                    tracker.add_clue(
                                        stage="expand",
                                        from_node=event_node,
                                        to_node=child_node,
                                        confidence=child_similarity,  # 统一使用similarity
                                        relation="扩展发现",
                                        display_level="final",  # 🆕 标记为最终结果
                                        metadata=metadata2
                                    )
                                    final_clues_count += 1
                                else:
                                    # Event 不存在，fallback 到直接连接
                                    expand_without_event_count += 1
                                    self.logger.warning(
                                        f"⚠️ [Expand Final] Event {event_id[:8]} 未在批量查询中找到，使用直接连接"
                                    )
                                    # 获取实体相似度作为confidence
                                    parent_similarity = parent_entity_dict.get("similarity", 0.0)
                                    child_similarity = child_entity_dict.get("similarity", 0.0)
                                    # 使用平均相似度作为confidence
                                    avg_similarity = (parent_similarity + child_similarity) / 2.0

                                    # 获取子实体权重信息（如果有）
                                    child_entity_weight = key.get("weight")
                                    metadata = {
                                        "method": "final_result",
                                        "step": f"expand_hop{key.get('hop', 1)}",
                                        "steps": key.get("steps", [2]),
                                        "hop": key.get("hop", 1)
                                    }
                                    # 只有to节点是实体时才存储weight
                                    if child_entity_weight is not None:
                                        metadata["weight"] = child_entity_weight

                                    tracker.add_clue(
                                        stage="expand",
                                        from_node=Tracker.build_entity_node(parent_entity_dict),
                                        to_node=Tracker.build_entity_node(child_entity_dict),
                                        confidence=avg_similarity,  # 统一使用similarity
                                        relation="扩展发现",
                                        display_level="final",
                                        metadata=metadata
                                    )
                                    final_clues_count += 1
                            else:
                                # 没有 event_id：直接生成 parent_entity → child_entity 线索
                                expand_without_event_count += 1
                                # 获取实体相似度作为confidence
                                parent_similarity = parent_entity_dict.get("similarity", 0.0)
                                child_similarity = child_entity_dict.get("similarity", 0.0)
                                # 使用平均相似度作为confidence
                                avg_similarity = (parent_similarity + child_similarity) / 2.0

                                # 获取子实体权重信息（如果有）
                                child_entity_weight = key.get("weight")
                                metadata = {
                                    "method": "final_result",
                                    "step": f"expand_hop{key.get('hop', 1)}",
                                    "steps": key.get("steps", [2]),
                                    "hop": key.get("hop", 1)
                                }
                                # 只有to节点是实体时才存储weight
                                if child_entity_weight is not None:
                                    metadata["weight"] = child_entity_weight

                                tracker.add_clue(
                                    stage="expand",
                                    from_node=Tracker.build_entity_node(parent_entity_dict),
                                    to_node=Tracker.build_entity_node(child_entity_dict),
                                    confidence=avg_similarity,  # 统一使用similarity
                                    relation="扩展发现",
                                    display_level="final",  # 🆕 标记为最终结果
                                    metadata=metadata
                                )
                                final_clues_count += 1
                        else:
                            self.logger.warning(
                                f"⚠️ [Expand Final] Expand key_id={key['key_id']} 缺少 parent_entity，无法生成最终线索"
                            )

                self.logger.info(
                    f"✅ [Expand Final] 最终线索生成完成: "
                    f"共 {final_clues_count} 条线索 "
                    f"(Recall keys={recall_keys_count}, "
                    f"Expand keys={expand_keys_count}, "
                    f"包含event={expand_with_event_count}, "
                    f"不含event={expand_without_event_count})"
                )

                # 🆕 为第一跳中没有扩展出新实体的 recall keys 也生成 final 线索
                if no_expansion_recall_keys:
                    self.logger.info(
                        f"🍃 [Expand Final] 为 {len(no_expansion_recall_keys)} 个没有扩展的recall key生成final线索"
                    )

                    # 查询这些 keys 的实体信息
                    try:
                        async with self.session_factory() as session:
                            query = select(Entity).where(Entity.id.in_(no_expansion_recall_keys))
                            result = await session.execute(query)
                            no_expansion_entities = {entity.id: entity for entity in result.scalars().all()}

                        for key_id in no_expansion_recall_keys:
                            entity = no_expansion_entities.get(key_id)
                            if not entity:
                                self.logger.warning(f"⚠️ 无法查询到实体 {key_id}，跳过")
                                continue

                            # 从 recall 结果中获取权重
                            weight = key_final_weights.get(key_id, 0.0)
                            # 获取实体相似度（如果有）
                            entity_similarity = entity_dict.get("similarity", 0.0) if hasattr(entity, 'similarity') else 0.0

                            entity_dict = {
                                "id": key_id,
                                "key_id": key_id,
                                "name": entity.name,
                                "type": entity.type,
                                "description": entity.description or "",
                                "hop": 0,  # recall 阶段 hop=0
                                "similarity": entity_similarity  # 添加相似度信息
                            }

                            # 获取实体权重信息（如果有）
                            entity_weight = weight  # weight就是实体权重
                            metadata = {
                                "method": "final_result",
                                "step": "recall_no_expansion",
                                "steps": [1],
                                "hop": 0,
                                "is_leaf": True  # 🆕 标记为叶子节点（没有扩展）
                            }
                            # 只有to节点是实体时才存储weight
                            if entity_weight > 0:
                                metadata["weight"] = entity_weight

                            tracker.add_clue(
                                stage="expand",
                                from_node=Tracker.build_query_node(config),
                                to_node=Tracker.build_entity_node(entity_dict),
                                confidence=entity_similarity,  # 统一使用similarity
                                relation="召回终点",  # 🆕 标记为没有继续扩展的终点
                                display_level="final",
                                metadata=metadata
                            )
                            final_clues_count += 1

                        self.logger.info(
                            f"✅ [Expand Final] 没有扩展的recall key处理完成，新增 {len(no_expansion_recall_keys)} 条终点线索"
                        )

                    except Exception as e:
                        self.logger.error(f"⚠️ [Expand Final] 为没有扩展的recall key生成线索失败: {e}", exc_info=True)

            # === 构建Expand阶段线索 ===
            expand_clues = await self._build_expand_clues(config, key_final, key_parent_map, tracker)
            config.expansion_clues = expand_clues
            self.logger.info(f"✨ Expand线索已构建 (entity→event→entity拆分为2条线索)")

            self.logger.info(
                f"Expand搜索完成：实际跳跃 {len(jump_results)} 次，总共发现 {len(key_final)} 个唯一keys"
            )

            return ExpandResult(
                key_final=key_final,
                jump_results=jump_results,
                total_jumps=len(jump_results),
                convergence_reached=weight_change < config.expand.weight_change_threshold if jump_results else False,
                all_events_by_jump=all_events_by_jump,
                all_keys_by_jump=all_keys_by_jump,
                weight_evolution=weight_evolution,
            )

        except Exception as e:
            self.logger.error(f"Expand搜索失败: {e}", exc_info=True)
            raise

    # === 步骤实现方法 ===

    async def _diagnose_key_final_event_filtering(
        self,
        key_ids: List[str],
        key_weights: Dict[str, float],
        all_event_ids: List[str],
        filtered_event_weights: Dict[str, float]
    ) -> None:
        """
        诊断 key-final 各个 key 召回的 events 过滤情况（仅第1跳调用）

        Args:
            key_ids: key-final 的 key_ids
            key_weights: key 权重字典
            all_event_ids: 步骤1召回的全部 events
            filtered_event_weights: 步骤2过滤后的 events 权重字典
        """
        self.logger.info(f"📊 [Key-Final过滤诊断] 开始分析 {len(key_ids)} 个key的events过滤情况")

        try:
            async with self.session_factory() as session:
                # 查询 key-event 关系
                query = (
                    select(EventEntity.entity_id, EventEntity.event_id)
                    .where(EventEntity.entity_id.in_(key_ids))
                    .where(EventEntity.event_id.in_(all_event_ids))
                )
                result = await session.execute(query)
                relations = result.fetchall()

                # 构建映射: key_id -> [event_ids]
                key_to_events = {}
                for entity_id, event_id in relations:
                    if entity_id not in key_to_events:
                        key_to_events[entity_id] = []
                    key_to_events[entity_id].append(event_id)

                # 查询 key 名称
                entity_query = select(Entity).where(Entity.id.in_(key_ids))
                entity_result = await session.execute(entity_query)
                entities = {entity.id: entity for entity in entity_result.scalars().all()}

            # 统计过滤情况
            filtered_event_ids = set(filtered_event_weights.keys())
            fully_filtered_keys = []  # 完全被过滤的 keys
            partially_filtered_keys = []  # 部分被过滤的 keys
            no_events_keys = []  # 没有召回 events 的 keys

            for key_id in key_ids:
                entity = entities.get(key_id)
                key_name = entity.name if entity else key_id[:8]
                key_type = entity.type if entity else "unknown"
                weight = key_weights.get(key_id, 0)

                recalled_events = set(key_to_events.get(key_id, []))
                retained_events = recalled_events & filtered_event_ids

                recall_count = len(recalled_events)
                retain_count = len(retained_events)

                if recall_count == 0:
                    no_events_keys.append((key_name, key_type, weight))
                    self.logger.warning(
                        f"  ⚠️  [{key_type}] {key_name}: 未召回任何events (weight={weight:.3f})"
                    )
                elif retain_count == 0:
                    fully_filtered_keys.append((key_name, key_type, weight, recall_count))
                    self.logger.warning(
                        f"  🚫 [{key_type}] {key_name}: 召回{recall_count}个events, 全部被过滤 (相似度均<0.3, weight={weight:.3f})"
                    )
                else:
                    filter_rate = (recall_count - retain_count) / recall_count
                    if filter_rate > 0.5:
                        partially_filtered_keys.append((key_name, key_type, weight, recall_count, retain_count, filter_rate))
                        self.logger.info(
                            f"  ⚠️  [{key_type}] {key_name}: 召回{recall_count}个, 保留{retain_count}个, 过滤{filter_rate:.1%} (weight={weight:.3f})"
                        )
                    else:
                        self.logger.info(
                            f"  ✅ [{key_type}] {key_name}: 召回{recall_count}个, 保留{retain_count}个, 过滤{filter_rate:.1%} (weight={weight:.3f})"
                        )

            # 汇总报告
            self.logger.info(f"📊 [Key-Final过滤诊断] 汇总:")
            self.logger.info(f"  • 总key数: {len(key_ids)}")
            self.logger.info(f"  • 未召回events: {len(no_events_keys)}个")
            self.logger.info(f"  • events全部被过滤: {len(fully_filtered_keys)}个")
            self.logger.info(f"  • events部分被过滤(>50%): {len(partially_filtered_keys)}个")

            if fully_filtered_keys:
                self.logger.warning(
                    f"⚠️ [Key-Final过滤诊断] {len(fully_filtered_keys)}个key的events全部被过滤，无法扩展新实体"
                )

        except Exception as e:
            self.logger.error(f"Key-Final过滤诊断失败: {e}", exc_info=True)

    async def _diagnose_key_expansion_success(
        self,
        parent_key_ids: List[str],
        parent_key_weights: Dict[str, float],
        key_expansion_trace: Dict[str, List[Tuple[str, str, float]]],
        all_new_key_weights: Dict[str, float],
        top_new_keys: List[Tuple[str, float]],
        already_discovered: set,
        entities_per_hop: int
    ) -> List[str]:
        """
        诊断每个 parent key 扩展出的 child entities 情况（仅第1跳调用）

        Args:
            parent_key_ids: parent keys 列表
            parent_key_weights: parent keys 权重
            key_expansion_trace: 扩展追踪信息 {child_id: [(parent_id, event_id, weight), ...]}
            all_new_key_weights: 所有新发现 entities 的权重
            top_new_keys: 选中的 topkey 列表
            already_discovered: 已发现的 keys 集合
            entities_per_hop: 每跳选择的 entities 数量

        Returns:
            没有扩展出新实体的 parent key IDs 列表
        """
        self.logger.info(f"📊 [Key扩展诊断] 开始分析 {len(parent_key_ids)} 个parent key的扩展情况")

        try:
            # 1. 反向构建映射：parent_id -> [child_ids]
            parent_to_children = {}
            for child_id, expansion_paths in key_expansion_trace.items():
                for parent_id, event_id, weight in expansion_paths:
                    if parent_id not in parent_to_children:
                        parent_to_children[parent_id] = set()
                    parent_to_children[parent_id].add(child_id)

            # 2. topkey 集合
            top_key_ids = {key_id for key_id, _ in top_new_keys}

            # 3. 查询实体名称
            async with self.session_factory() as session:
                entity_query = select(Entity).where(Entity.id.in_(parent_key_ids))
                entity_result = await session.execute(entity_query)
                entities = {entity.id: entity for entity in entity_result.scalars().all()}

            # 4. 统计每个 parent key 的扩展情况
            no_expansion_keys = []  # 没有扩展出任何 child 的 keys
            all_filtered_keys = []  # 扩展了 child 但全部未进入 topkey 的 keys
            partial_filtered_keys = []  # 部分 child 进入 topkey 的 keys
            success_keys = []  # 成功扩展的 keys

            for parent_id in parent_key_ids:
                entity = entities.get(parent_id)
                key_name = entity.name if entity else parent_id[:8]
                key_type = entity.type if entity else "unknown"
                weight = parent_key_weights.get(parent_id, 0)

                # 获取该 parent 扩展出的 children
                children = parent_to_children.get(parent_id, set())
                children_count = len(children)

                if children_count == 0:
                    # 没有扩展出任何 child
                    no_expansion_keys.append((key_name, key_type, weight))
                    self.logger.warning(
                        f"  🚫 [{key_type}] {key_name}: 未扩展出任何新实体 (weight={weight:.3f})"
                    )
                else:
                    # 扩展出了 children，检查有多少进入 topkey
                    children_in_top = children & top_key_ids
                    children_not_in_top = children - top_key_ids

                    top_count = len(children_in_top)
                    not_top_count = len(children_not_in_top)

                    if top_count == 0:
                        # 所有 children 都未进入 topkey
                        all_filtered_keys.append((key_name, key_type, weight, children_count))

                        # 显示被过滤的 children 及其权重（显示前3个权重最高的）
                        filtered_children_weights = [(cid, all_new_key_weights.get(cid, 0)) for cid in children_not_in_top]
                        filtered_children_weights.sort(key=lambda x: x[1], reverse=True)
                        top3_filtered = filtered_children_weights[:3]

                        # 计算这些 children 的排名
                        all_sorted = sorted(all_new_key_weights.items(), key=lambda x: x[1], reverse=True)
                        ranks = [i+1 for i, (kid, w) in enumerate(all_sorted) if kid in children_not_in_top]
                        min_rank = min(ranks) if ranks else "N/A"

                        self.logger.warning(
                            f"  ⚠️  [{key_type}] {key_name}: 扩展了{children_count}个新实体, 但全部未进入Top{entities_per_hop} "
                            f"(最高排名={min_rank}, weight={weight:.3f})"
                        )

                        # 显示被过滤的 top3 children
                        for i, (child_id, child_weight) in enumerate(top3_filtered, 1):
                            # 查找该 child 的排名
                            rank = next((i+1 for i, (kid, w) in enumerate(all_sorted) if kid == child_id), "N/A")
                            self.logger.debug(
                                f"    - 被过滤的child#{i}: {child_id[:8]}... (weight={child_weight:.4f}, 排名={rank})"
                            )
                    else:
                        # 部分或全部进入 topkey
                        if not_top_count > 0:
                            partial_filtered_keys.append((key_name, key_type, weight, children_count, top_count, not_top_count))
                            self.logger.info(
                                f"  ⚠️  [{key_type}] {key_name}: 扩展了{children_count}个, {top_count}个进入Top{entities_per_hop}, "
                                f"{not_top_count}个被过滤 (weight={weight:.3f})"
                            )
                            # 显示进入Top10的子实体ID
                            top_children_list = sorted(
                                [(cid, all_new_key_weights.get(cid, 0)) for cid in children_in_top],
                                key=lambda x: x[1],
                                reverse=True
                            )
                            self.logger.info(
                                f"    进入Top{entities_per_hop}的子实体: {[cid[:8] for cid, _ in top_children_list]}"
                            )
                        else:
                            success_keys.append((key_name, key_type, weight, children_count))
                            self.logger.info(
                                f"  ✅ [{key_type}] {key_name}: 扩展了{children_count}个新实体, 全部进入Top{entities_per_hop} (weight={weight:.3f})"
                            )
                            # 显示全部进入Top10的子实体ID
                            top_children_list = sorted(
                                [(cid, all_new_key_weights.get(cid, 0)) for cid in children_in_top],
                                key=lambda x: x[1],
                                reverse=True
                            )
                            self.logger.info(
                                f"    进入Top{entities_per_hop}的子实体: {[cid[:8] for cid, _ in top_children_list]}"
                            )

            # 5. 汇总报告
            self.logger.info(f"📊 [Key扩展诊断] 汇总:")
            self.logger.info(f"  • 总parent key数: {len(parent_key_ids)}")
            self.logger.info(f"  • 未扩展出新实体: {len(no_expansion_keys)}个")
            self.logger.info(f"  • 扩展的新实体全部被过滤: {len(all_filtered_keys)}个")
            self.logger.info(f"  • 扩展的新实体部分被过滤: {len(partial_filtered_keys)}个")
            self.logger.info(f"  • 成功扩展(全部进入topkey): {len(success_keys)}个")

            if no_expansion_keys or all_filtered_keys:
                failed_count = len(no_expansion_keys) + len(all_filtered_keys)
                self.logger.warning(
                    f"⚠️ [Key扩展诊断] {failed_count}个parent key未成功扩展或扩展的实体全部被过滤，"
                    f"这些key不会出现在Expand线索中"
                )

            # 返回没有扩展出新实体的 parent key IDs
            no_expansion_key_ids = [parent_id for parent_id in parent_key_ids
                                   if len(parent_to_children.get(parent_id, set())) == 0]
            return no_expansion_key_ids

        except Exception as e:
            self.logger.error(f"Key扩展诊断失败: {e}", exc_info=True)
            return []

    async def _step1_keys_to_events(self, key_ids: List[str]) -> List[str]:
        """
        步骤1: 根据keys查找到所有关联的events
        用sql找到所有关联事项，得到新的[Event-key-related-2]
        """
        if not key_ids:
            return []

        self.logger.info(f"步骤1: 根据keys查找关联events")
        self.logger.info(f"  输入keys数量: {len(key_ids)}")

        async with self.session_factory() as session:
            # 查询包含这些key的所有event
            query = (
                select(EventEntity.event_id)
                .where(EventEntity.entity_id.in_(key_ids))
                .distinct()
            )

            result = await session.execute(query)
            event_ids = [row[0] for row in result.fetchall()]

        # 调试：输出关联的event_ids
        self.logger.info(f"步骤1完成:")
        self.logger.info(f"  • 输入keys数量: {len(key_ids)}")
        self.logger.info(f"  • 发现关联events数量: {len(event_ids)}")
        events_preview = event_ids[:3] if len(event_ids) > 3 else event_ids
        events_suffix = "..." if len(event_ids) > 3 else ""
        self.logger.info(f"  • 关联events: {events_preview}{events_suffix}")

        return event_ids

    async def _step2_calculate_event_query_similarity(
        self, config: SearchConfig, event_ids: List[str]
    ) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
        """
        步骤2: 计算原始query和给定events的相似度
        得到相似性向量(event-query-2)

        注意：这里计算的是给定events与query的相似度，而不是重新搜索相似events
        """
        if not event_ids:
            return [], {}

        self.logger.info(f"步骤2: 计算原始query与 {len(event_ids)} 个给定events的相似度")

        try:
            # 检查是否已有缓存的query_embedding
            if config.has_query_embedding and config.query_embedding:
                query_embedding = config.query_embedding
                self.logger.debug(f"📦 使用缓存的query向量，长度: {len(query_embedding)}")
            else:
                # 生成原始query的向量
                query_embedding = await self.processor.generate_embedding(config.query)
                self.logger.debug(f"  Query向量生成成功，长度: {len(query_embedding)}")

                # 缓存query_embedding到config
                config.query_embedding = query_embedding
                config.has_query_embedding = True
                self.logger.debug("📦 Query向量已缓存到config中")
        except Exception as e:
            raise AIError(f"查询向量生成失败: {e}") from e

        self.logger.debug(f"  Query向量长度: {len(query_embedding)}")

        # 存储计算结果
        event_query_related = []
        event_similarities = {}

        # 统计信息
        successful_calculations = 0
        failed_calculations = 0
        below_threshold_count = 0

        # 分批处理events，避免一次性查询过多
        batch_size = 50  # ✅ 优化：提高批量大小，减少查询次数
        for i in range(0, len(event_ids), batch_size):
            batch_event_ids = event_ids[i:i + batch_size]

            self.logger.debug(f"  处理批次 {i//batch_size + 1}: {len(batch_event_ids)} 个events")

            # 获取这批事件的详细信息（包含向量）
            try:
                batch_events = await self.event_repo.get_events_by_ids(batch_event_ids)

                # 为查找方便，创建event_id到event数据的映射
                event_map = {}
                for event in batch_events:
                    if isinstance(event, dict) and "event_id" in event:
                        event_id = event["event_id"]
                        event_map[event_id] = event
                    else:
                        # 记录格式错误的事件数据
                        self.logger.warning(f"事件数据格式错误或缺少event_id字段: {type(event)}")
                        if isinstance(event, dict):
                            self.logger.debug(f"事件字段: {list(event.keys())}")

            except Exception as e:
                self.logger.warning(f"获取批次事件信息失败: {e}")
                event_map = {}

            # ✅ 优化：收集所有有效的向量，准备批量计算
            valid_event_data = []
            for event_id in batch_event_ids:
                try:
                    # 获取事件详细信息
                    event_info = event_map.get(event_id, {})

                    # 确保event_info是字典类型
                    if not isinstance(event_info, dict):
                        event_info = {}

                    # ✅ 优化：直接从批量查询结果中获取向量，不再调用 get_event_vector
                    content_vector = event_info.get("content_vector")
                    title_vector = event_info.get("title_vector")

                    # 优先使用 content_vector，其次使用 title_vector
                    event_vector = content_vector or title_vector
                    vector_type = "content_vector" if content_vector else "title_vector"

                    if event_vector is None:
                        self.logger.debug(f"    Event {event_id[:8]}: 无向量数据")
                        failed_calculations += 1
                        continue

                    # 收集有效的事件数据
                    valid_event_data.append({
                        'event_id': event_id,
                        'vector': event_vector,
                        'title': event_info.get("title", ""),
                        'summary': event_info.get("summary", ""),
                        'match_type': vector_type
                    })

                except Exception as e:
                    self.logger.warning(f"    ❌ Event {event_id[:8]}: 处理失败: {e}")
                    failed_calculations += 1
                    continue

            # ✅ 优化：批量计算所有向量的余弦相似度
            if valid_event_data:
                vectors = [item['vector'] for item in valid_event_data]
                similarities = await self._batch_cosine_similarity(query_embedding, vectors)

                # 处理批量计算结果
                for item, similarity in zip(valid_event_data, similarities):
                    event_data = {
                        "event_id": item['event_id'],
                        "title": item['title'],
                        "summary": item['summary'],
                        "similarity": float(similarity),
                        "match_type": item['match_type'],
                    }

                    event_query_related.append(event_data)
                    event_similarities[item['event_id']] = float(similarity)
                    successful_calculations += 1

                    # 调试信息
                    if similarity >= config.expand.event_similarity_threshold:
                        self.logger.debug(f"    ✅ Event {item['event_id'][:8]}: 相似度={similarity:.4f} (超过阈值)")
                    else:
                        below_threshold_count += 1
                        self.logger.debug(f"    ⚠️  Event {item['event_id'][:8]}: 相似度={similarity:.4f} (低于阈值)")

        # 输出统计信息
        self.logger.info(f"步骤2相似度计算统计:")
        self.logger.info(f"  • 总事件数: {len(event_ids)}")
        self.logger.info(f"  • 成功计算: {successful_calculations}")
        self.logger.info(f"  • 计算失败: {failed_calculations}")
        self.logger.info(f"  • 低于阈值: {below_threshold_count}")

        if event_similarities:
            self.logger.info(f"  • 相似度范围: {min(event_similarities.values()):.4f} - {max(event_similarities.values()):.4f}")

        # 过滤相似度阈值
        before_threshold = len(event_query_related)
        event_query_related = [
            event for event in event_query_related
            if event["similarity"] >= config.expand.event_similarity_threshold
        ]
        event_similarities = {
            event_id: similarity
            for event_id, similarity in event_similarities.items()
            if similarity >= config.expand.event_similarity_threshold
        }

        self.logger.info(f"步骤2完成: 找到 {len(event_query_related)} 个相似事件 (阈值过滤前: {before_threshold})")

        return event_query_related, event_similarities

    async def _step3_calculate_event_key_weights(
        self,
        event_ids: List[str],
        key_ids: List[str],
        key_weights: Dict[str, float],
    ) -> Dict[str, float]:
        """
        步骤3: 计算Event-key-related-2权重向量
        根据每个event包含key的情况，将对应key的权重相加

        优化：使用批量查询 + 内存分组，避免循环查询数据库
        """
        if not event_ids or not key_ids:
            return {}

        event_key_weights = {}

        try:
            async with self.session_factory() as session:
                # 🔥 优化：一次性批量查询所有event-key关系
                query = (
                    select(EventEntity.event_id, EventEntity.entity_id)
                    .where(EventEntity.event_id.in_(event_ids))
                    .where(EventEntity.entity_id.in_(key_ids))
                )
                result = await session.execute(query)
                all_relations = result.fetchall()

                # 🔥 优化：在内存中按event_id分组
                event_to_keys = {}
                for event_id, entity_id in all_relations:
                    if event_id not in event_to_keys:
                        event_to_keys[event_id] = []
                    event_to_keys[event_id].append(entity_id)

                # 计算每个event的权重
                for event_id in event_ids:
                    event_keys = event_to_keys.get(event_id, [])

                    # 计算权重：将对应key的权重相加
                    total_weight = sum(key_weights.get(key_id, 0.0) for key_id in event_keys)
                    event_key_weights[event_id] = total_weight

        except Exception as e:
            self.logger.error(f"步骤3计算event-key权重失败: {e}", exc_info=True)
            raise

        return event_key_weights

    async def _step4_calculate_event_key_query_weights(
        self,
        event_key_weights: Dict[str, float],
        event_query_weights: Dict[str, float],
    ) -> Dict[str, float]:
        """
        步骤4: 计算event-key-query权重向量
        将（event-key-2）*(event-query-2)，得到新的（event-jump-2）
        """
        event_key_query_weights = {}

        # 调试：输出权重计算的详细信息
        self.logger.debug(f"步骤4调试信息:")
        self.logger.debug(f"  event_key_weights数量: {len(event_key_weights)}")
        self.logger.debug(f"  event_query_weights数量: {len(event_query_weights)}")

        # 统计匹配情况
        matched_events = 0
        unmatched_events = 0
        zero_weight_events = 0

        for event_id in event_key_weights:
            key_weight = event_key_weights[event_id]
            query_weight = event_query_weights.get(event_id, 0.0)

            # 复合权重 = key权重 * query相似度权重
            combined_weight = key_weight * query_weight
            event_key_query_weights[event_id] = combined_weight

            # 调试信息
            if query_weight > 0:
                matched_events += 1
                self.logger.debug(f"  ✅ Event {event_id[:8]}: key_weight={key_weight:.4f}, query_weight={query_weight:.4f}, combined={combined_weight:.4f}")
            else:
                unmatched_events += 1
                if combined_weight == 0:
                    zero_weight_events += 1
                self.logger.debug(f"  ❌ Event {event_id[:8]}: key_weight={key_weight:.4f}, query_weight={query_weight:.4f}, combined={combined_weight:.4f}")

        # 输出统计信息
        self.logger.info(f"步骤4权重计算统计:")
        self.logger.info(f"  • 总事件数: {len(event_key_weights)}")
        self.logger.info(f"  • 匹配成功的事件: {matched_events}")
        self.logger.info(f"  • 未匹配的事件: {unmatched_events}")
        self.logger.info(f"  • 权重为0的事件: {zero_weight_events}")

        # 如果权重为0的事件过多，发出警告
        if zero_weight_events > len(event_key_weights) * 0.8:
            self.logger.warning(f"⚠️ 步骤4中有 {zero_weight_events}/{len(event_key_weights)} 个事件权重为0，可能影响搜索效果")

        # 权重归一化和容错处理
        if event_key_query_weights:
            # 过滤掉权重为0的事件
            non_zero_weights = {k: v for k, v in event_key_query_weights.items() if v > 0}

            if non_zero_weights:
                # 对权重进行归一化，避免数值过小
                max_weight = max(non_zero_weights.values())
                if max_weight > 0:
                    normalized_weights = {
                        k: v / max_weight for k, v in non_zero_weights.items()
                    }

                    self.logger.debug(f"权重归一化:")
                    self.logger.debug(f"  归一化前权重范围: {min(non_zero_weights.values()):.6f} - {max(non_zero_weights.values()):.6f}")
                    self.logger.debug(f"  归一化后权重范围: {min(normalized_weights.values()):.6f} - 1.000000")

                    return normalized_weights
                else:
                    self.logger.warning("⚠️ 所有事件权重都为0，使用默认权重")
                    # 给所有事件分配相同的小权重
                    fallback_weight = 0.1
                    return {k: fallback_weight for k in event_key_weights.keys()}
            else:
                self.logger.warning("⚠️ 没有非零权重事件，使用默认权重")
                fallback_weight = 0.1
                return {k: fallback_weight for k in event_key_weights.keys()}

        return event_key_query_weights

    async def _step5_calculate_key_event_weights(
        self,
        event_ids: List[str],
        key_ids: List[str],
        event_weights: Dict[str, float],
    ) -> Tuple[Dict[str, float], Dict[str, List[Tuple[str, str, float]]]]:
        """
        步骤5: 反向计算key权重向量
        根据event权重反向得出event里所有的key的重要性
        修正：从events中提取所有keys，不仅限于已知keys

        优化：合并两次数据库查询为一次，减少网络往返

        新增：追踪扩展关系，记录每个child entity是通过哪些(parent_id, event_id, weight)扩展而来

        Returns:
            Tuple[key_event_weights, key_expansion_trace]
            - key_event_weights: {entity_id: total_weight}
            - key_expansion_trace: {child_entity_id: [(parent_entity_id, event_id, event_weight), ...]}
        """
        if not event_ids:
            return {}, {}

        key_event_weights = {}
        key_expansion_trace = {}  # 新增：追踪扩展关系

        # 调试：输出权重计算信息
        self.logger.info(f"步骤5调试信息:")
        self.logger.info(f"  输入event_ids数量: {len(event_ids)}")
        self.logger.info(f"  输入key_ids数量: {len(key_ids)}")
        self.logger.info(f"  输入event_weights数量: {len(event_weights)}")

        try:
            async with self.session_factory() as session:
                # ✅ 优化：一次查询获取所有 entity-event 关系
                entity_event_query = (
                    select(EventEntity.entity_id, EventEntity.event_id)
                    .where(EventEntity.event_id.in_(event_ids))
                )
                result = await session.execute(entity_event_query)
                all_relations = result.fetchall()

                # 在内存中处理：提取所有 entity_ids 并分组
                all_entity_ids = set()
                entity_to_events = {}
                event_to_entities = {}  # 新增：反向映射

                for entity_id, event_id in all_relations:
                    all_entity_ids.add(entity_id)
                    if entity_id not in entity_to_events:
                        entity_to_events[entity_id] = []
                    entity_to_events[entity_id].append(event_id)

                    # 构建反向映射：event -> entities
                    if event_id not in event_to_entities:
                        event_to_entities[event_id] = []
                    event_to_entities[event_id].append(entity_id)

                # 转换为 list 保持与原实现类型一致
                all_entity_ids = list(all_entity_ids)

                self.logger.info(f"  从events中发现的总entities: {len(all_entity_ids)}")

                # 区分已知keys和新发现的keys
                known_keys = set(key_ids)
                new_keys = [eid for eid in all_entity_ids if eid not in known_keys]

                self.logger.info(f"  已知keys: {len(known_keys)}")
                self.logger.info(f"  新发现keys: {len(new_keys)}")

                # 计算所有entities的权重 + 追踪扩展关系
                for entity_id in all_entity_ids:
                    entity_events = entity_to_events.get(entity_id, [])

                    # 计算权重：将包含该entity的所有event权重相加
                    total_weight = sum(
                        event_weights.get(event_id, 0.0) for event_id in entity_events
                    )
                    key_event_weights[entity_id] = total_weight

                    # 🆕 追踪扩展关系：记录child entity是通过哪些(parent, event)扩展而来
                    is_new = entity_id not in known_keys
                    if is_new:
                        expansion_paths = []
                        for event_id in entity_events:
                            event_weight = event_weights.get(event_id, 0.0)
                            if event_weight > 0:
                                # 找出该event中包含的parent entities（来自known_keys）
                                event_entities = event_to_entities.get(event_id, [])
                                parent_entities = [eid for eid in event_entities if eid in known_keys]

                                # 为每个parent记录一条扩展路径
                                for parent_id in parent_entities:
                                    expansion_paths.append((parent_id, event_id, event_weight))

                        if expansion_paths:
                            key_expansion_trace[entity_id] = expansion_paths

                    # 调试信息
                    marker = "🆕" if is_new else "🔄"
                    if total_weight > 0:
                        expansion_info = ""
                        if is_new and entity_id in key_expansion_trace:
                            num_paths = len(key_expansion_trace[entity_id])
                            num_parents = len(set(p[0] for p in key_expansion_trace[entity_id]))
                            expansion_info = f", 扩展路径={num_paths}条(来自{num_parents}个parent)"
                        self.logger.debug(f"  {marker} Entity {entity_id[:8]} ({'新' if is_new else '已知'}): 关联{len(entity_events)}个events, 总权重={total_weight:.4f}{expansion_info}")
                    else:
                        self.logger.debug(f"  {marker} Entity {entity_id[:8]} ({'新' if is_new else '已知'}): 关联{len(entity_events)}个events, 总权重={total_weight:.4f}")

        except Exception as e:
            self.logger.error(f"步骤5计算key-event权重失败: {e}", exc_info=True)
            raise

        # 输出统计信息
        non_zero_keys = sum(1 for weight in key_event_weights.values() if weight > 0)
        self.logger.info(f"步骤5权重计算统计:")
        self.logger.info(f"  • 总entities数: {len(key_event_weights)}")
        self.logger.info(f"  • 权重>0的entities数: {non_zero_keys}")
        self.logger.info(f"  • 追踪到扩展关系的新entities数: {len(key_expansion_trace)}")

        if key_event_weights:
            weight_values = list(key_event_weights.values())
            self.logger.info(f"  • 权重范围: {min(weight_values):.4f} - {max(weight_values):.4f}")

            # 显示权重最高的几个entities
            sorted_entities = sorted(key_event_weights.items(), key=lambda x: x[1], reverse=True)[:5]
            top_entities_str = ", ".join([f"{eid[:8]}:{w:.3f}" for eid, w in sorted_entities])
            self.logger.info(f"  • Top5 entities: {top_entities_str}")

        return key_event_weights, key_expansion_trace

    async def _aggregate_key_weights(self, weight_evolution: Dict[int, Dict[str, float]]) -> Dict[str, float]:
        """
        聚合多跳的key权重
        采用加权平均的方式，越后面的跳跃权重越高
        """
        if not weight_evolution:
            return {}

        self.logger.debug(f"权重聚合调试信息:")
        self.logger.debug(f"  跳跃次数: {len(weight_evolution)}")
        for jump, weights in weight_evolution.items():
            self.logger.debug(f"  第{jump}跳: {len(weights)}个keys, 权重范围: {min(weights.values()) if weights else 0:.4f} - {max(weights.values()) if weights else 0:.4f}")

        aggregated_weights = {}
        total_jumps = len(weight_evolution)

        # 对每个key，计算其在所有跳跃中的加权平均权重
        all_key_ids = set()
        for jump_weights in weight_evolution.values():
            all_key_ids.update(jump_weights.keys())

        self.logger.debug(f"  总共涉及的keys: {len(all_key_ids)}")

        for key_id in all_key_ids:
            weighted_sum = 0.0
            weight_sum = 0.0
            jump_contributions = []

            for jump, jump_weights in weight_evolution.items():
                if key_id in jump_weights:
                    # 越后面的跳跃权重越高
                    jump_importance = jump / total_jumps
                    weighted_sum += jump_weights[key_id] * jump_importance
                    weight_sum += jump_importance
                    jump_contributions.append(f"跳{jump}:{jump_weights[key_id]:.3f}")

            if weight_sum > 0:
                aggregated_weights[key_id] = weighted_sum / weight_sum
                self.logger.debug(f"  Key {key_id[:8]}: 聚合权重={aggregated_weights[key_id]:.4f}, 贡献={', '.join(jump_contributions)}")

        # 输出聚合结果统计
        self.logger.info(f"权重聚合结果:")
        self.logger.info(f"  • 聚合后key数量: {len(aggregated_weights)}")
        if aggregated_weights:
            self.logger.info(f"  • 聚合权重范围: {min(aggregated_weights.values()):.4f} - {max(aggregated_weights.values()):.4f}")

        return aggregated_weights

    async def _extract_final_keys(
        self,
        key_weights: Dict[str, float],
        config: SearchConfig,
        recall_keys: List[Dict[str, Any]],
        weight_evolution: Dict[int, Dict[str, float]],
        all_discovered_keys: set,
        key_parent_map: Dict[str, Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        提取最终的keys，记录最早发现的步骤
        基于all_discovered_keys返回完整的去重keylist，不再使用阈值或数量限制
        """
        if not all_discovered_keys:
            return []

        # 构建召回阶段的key ID集合
        recall_key_ids = {key["key_id"] for key in recall_keys}

        # 计算每个key的最早发现步骤
        key_discovery_steps = {}
        for key_id in all_discovered_keys:
            if key_id in recall_key_ids:
                # 在Recall中发现，步骤记为1
                key_discovery_steps[key_id] = 1
            else:
                # 在Expand中发现，找到最早的跳跃步骤
                earliest_jump = None
                for jump in sorted(weight_evolution.keys()):
                    if key_id in weight_evolution[jump]:
                        earliest_jump = jump
                        break

                if earliest_jump is not None:
                    # Expand的跳跃步骤转换为全局步骤编号
                    # Recall是步骤1，Expand第1跳是步骤2，以此类推
                    key_discovery_steps[key_id] = earliest_jump + 1
                else:
                    # 默认设为Expand第1跳
                    key_discovery_steps[key_id] = 2

        # 对all_discovered_keys中的keys按权重排序（如果有权重信息的话）
        sorted_keys = []
        for key_id in all_discovered_keys:
            weight = key_weights.get(key_id, 0.0)
            sorted_keys.append((key_id, weight))

        # 按权重排序
        sorted_keys.sort(key=lambda x: x[1], reverse=True)

        # 选择所有发现的keys，不再进行阈值或数量筛选
        selected_keys = sorted_keys

        # 获取key的详细信息
        key_final = []
        if selected_keys:
            key_ids = [key_id for key_id, _ in selected_keys]

            try:
                async with self.session_factory() as session:
                    # 查询所有 selected keys 的实体信息
                    query = select(Entity).where(Entity.id.in_(key_ids))
                    result = await session.execute(query)
                    entities = {entity.id: entity for entity in result.scalars().all()}

                    # 收集所有需要查询的 parent entity IDs
                    parent_ids = set()
                    for key_id in key_ids:
                        if key_id in key_parent_map:
                            parent_ids.add(key_parent_map[key_id]["parent_id"])

                    # 批量查询所有 parent entities
                    parent_entities = {}
                    if parent_ids:
                        parent_query = select(Entity).where(Entity.id.in_(list(parent_ids)))
                        parent_result = await session.execute(parent_query)
                        parent_entities = {entity.id: entity for entity in parent_result.scalars().all()}

                for key_id, weight in selected_keys:
                    entity = entities.get(key_id)
                    if entity:
                        key_info = {
                            "key_id": key_id,
                            "name": entity.name,
                            "type": entity.type,
                            "weight": weight,
                            "description": entity.description,
                            "steps": [key_discovery_steps[key_id]],
                            # 记录最早发现的步骤
                            "hop": 0  # 默认为0（Recall阶段），后面会根据实际情况更新
                        }

                        # 如果这个key是在Expand中通过parent扩展发现的，添加parent_entity信息
                        if key_id in key_parent_map:
                            parent_info = key_parent_map[key_id]
                            parent_id = parent_info["parent_id"]
                            parent_entity = parent_entities.get(parent_id)

                            if parent_entity:
                                # 计算parent的hop（比child的hop小1）
                                parent_hop = parent_info["hop"] - 1 if parent_info["hop"] > 0 else 0

                                key_info["parent_entity"] = {
                                    "id": parent_id,
                                    "name": parent_entity.name,
                                    "type": parent_entity.type,
                                    "hop": parent_hop  # 🎨 添加parent的hop
                                }
                                key_info["hop"] = parent_info["hop"]
                                self.logger.debug(
                                    f"Key {entity.name} (step{key_discovery_steps[key_id]}) "
                                    f"由parent {parent_entity.name} 扩展发现 (hop={parent_info['hop']})"
                                )
                            else:
                                # 🔍 Parent entity未找到，记录警告并尝试使用简化信息
                                self.logger.warning(
                                    f"⚠️ Key '{entity.name}' 在key_parent_map中，但parent_id={parent_id[:8]}... "
                                    f"未在数据库中找到。尝试使用简化parent信息。"
                                )
                                # 计算parent的hop
                                parent_hop = parent_info["hop"] - 1 if parent_info["hop"] > 0 else 0

                                # 提供简化的parent信息（至少包含ID）
                                key_info["parent_entity"] = {
                                    "id": parent_id,
                                    "name": f"Unknown-{parent_id[:8]}",
                                    "type": "unknown",
                                    "hop": parent_hop  # 🎨 添加parent的hop
                                }
                                key_info["hop"] = parent_info["hop"]
                        else:
                            # 🔍 检查是否应该有parent但key_parent_map中缺失
                            if key_discovery_steps[key_id] >= 2:
                                self.logger.warning(
                                    f"⚠️ Key '{entity.name}' (step{key_discovery_steps[key_id]}) "
                                    f"不在key_parent_map中！可能是step判断错误或parent记录缺失。"
                                )

                        key_final.append(key_info)

            except Exception as e:
                self.logger.error(f"提取最终keys失败: {e}", exc_info=True)
                raise

        return key_final
    async def _build_expand_clues(
        self,
        config: SearchConfig,
        key_final: List[Dict[str, Any]],
        key_parent_map: Dict[str, Dict[str, Any]],
        tracker: Tracker  # 🆕 接收 tracker 实例，避免创建多个实例
    ) -> List[Dict[str, Any]]:
        """
        构建Expand阶段的线索（entity → event → entity）

        🆕 修改：不再使用单条entity→entity线索，改为拆分成两条：
        1. parent_entity → event
        2. event → child_entity

        这样确保中间节点（event）不会被省略，前端可以构建完整知识图谱

        Args:
            config: 搜索配置
            key_final: 最终的key列表
            key_parent_map: parent关系映射
            tracker: 统一的 Tracker 实例（确保同一跳内 event 节点去重）

        Returns:
            Expand阶段的线索列表（兼容性保留，实际线索已追加到config.all_clues）
        """
        # 🆕 使用传入的 tracker 实例，而不是创建新的
        # tracker = Tracker(config)  # ❌ 删除这行

        clues = []

        # 🆕 批量查询所有需要的event信息（避免N+1查询）
        event_ids_needed = set()
        for key in key_final:
            steps = key.get("steps", [0])[0]
            if steps >= 2 and key["key_id"] in key_parent_map:
                parent_info = key_parent_map[key["key_id"]]
                if "event_id" in parent_info:
                    event_ids_needed.add(parent_info["event_id"])

        # 批量查询events
        event_map = {}
        if event_ids_needed:
            try:
                async with self.session_factory() as session:
                    query = select(SourceEvent).where(SourceEvent.id.in_(list(event_ids_needed)))
                    result = await session.execute(query)
                    events = result.scalars().all()
                    event_map = {event.id: event for event in events}
                    self.logger.info(f"📦 批量查询了 {len(event_map)} 个event用于构建线索")
            except Exception as e:
                self.logger.warning(f"批量查询events失败: {e}，将使用简化的event节点")

        # 🔍 诊断日志：统计线索构建情况
        total_keys = len(key_final)
        expand_keys = 0
        keys_with_parent_entity = 0
        keys_without_parent_entity = []

        # 🆕 统计 parent entities 的情况
        key_final_ids = set(k["key_id"] for k in key_final)
        parent_ids_in_clues = set()
        parent_ids_not_in_key_final = set()

        # 🔍 统计从 Recall 传入的 keys（作为 Expand 的起点）
        recall_keys = [k for k in key_final if k.get("steps", [0])[0] == 1]
        recall_key_ids = set(k["key_id"] for k in recall_keys)

        self.logger.info(
            f"🔍 [Expand诊断] Recall传入的keys: {len(recall_keys)}个, "
            f"Expand扩展的keys: {len([k for k in key_final if k.get('steps', [0])[0] >= 2])}个"
        )

        # 只为在Expand中发现的keys（steps包含2或更大）构建expand线索
        for key in key_final:
            steps = key.get("steps", [0])[0]

            # 统计Expand发现的keys
            if steps >= 2:
                expand_keys += 1

                # 检查parent_entity字段
                if "parent_entity" in key:
                    keys_with_parent_entity += 1
                else:
                    keys_without_parent_entity.append({
                        "key_id": key["key_id"],
                        "name": key["name"],
                        "step": steps
                    })

            # 🆕 拆分成两条线索：parent_entity → event, event → child_entity
            if "parent_entity" in key and steps >= 2:
                parent_entity = key["parent_entity"]

                # 构建parent实体字典（完整信息用于标准节点）
                parent_entity_dict = {
                    "id": parent_entity["id"],
                    "key_id": parent_entity["id"],
                    "name": parent_entity["name"],
                    "type": parent_entity["type"],
                    "description": parent_entity.get("description", ""),
                    "hop": parent_entity.get("hop", 0)  # 🎨 传递hop字段
                }

                # 构建child实体字典（完整信息用于标准节点）
                child_entity_dict = {
                    "id": key["key_id"],
                    "key_id": key["key_id"],
                    "name": key["name"],
                    "type": key["type"],
                    "description": key.get("description", ""),
                    "hop": key.get("hop", 0)  # 🎨 传递hop字段
                }

                # 获取event信息（如果有的话）
                if key["key_id"] in key_parent_map:
                    parent_info = key_parent_map[key["key_id"]]
                    event_id = parent_info.get("event_id")
                    event_weight = parent_info.get("event_weight", 1.0)
                    hop = parent_info.get("hop", 1)

                    if event_id:
                        # 构建标准节点
                        parent_node = Tracker.build_entity_node(parent_entity_dict)
                        child_node = Tracker.build_entity_node(child_entity_dict)

                        event_obj = event_map.get(event_id)
                        if event_obj:
                            # 🆕 使用 tracker 实例方法，传递 stage 和 hop
                            event_node = tracker.get_or_create_event_node(event_obj, stage="expand", hop=hop)
                        else:
                            # Fallback：创建简化的event dict
                            self.logger.warning(f"Event {event_id[:8]} 未在批量查询中找到")
                            event_fallback_dict = {
                                "id": event_id,
                                "title": f"Event-{event_id[:8]}",
                                "content": "",
                                "category": "",
                                "summary": ""
                            }
                            # 需要将dict转为SourceEvent对象模拟
                            # 由于我们没有SourceEvent对象，使用特殊处理
                            event_node = {
                                "id": event_id,
                                "type": "event",
                                "category": "",
                                "content": f"Event-{event_id[:8]}",
                                "description": ""
                            }

                        # 获取实体相似度
                        parent_similarity = parent_entity_dict.get("similarity", 0.0)
                        child_similarity = child_entity_dict.get("similarity", 0.0)

                        # 🆕 第一条线索：parent_entity → event（to节点是事件，不存储weight）
                        metadata1 = {"hop": hop, "method": "cooccurrence"}

                        tracker.add_clue(
                            stage="expand",
                            from_node=parent_node,
                            to_node=event_node,
                            confidence=parent_similarity,  # 统一使用similarity
                            metadata=metadata1
                        )

                        # 🆕 第二条线索：event → child_entity（to节点是实体，需要weight）
                        child_entity_weight = key.get("weight")
                        metadata2 = {"hop": hop, "method": "cooccurrence"}
                        # 只有to节点是实体时才存储weight
                        if child_entity_weight is not None:
                            metadata2["weight"] = child_entity_weight

                        tracker.add_clue(
                            stage="expand",
                            from_node=event_node,
                            to_node=child_node,
                            confidence=child_similarity,  # 统一使用similarity
                            metadata=metadata2
                        )

                        # 🔍 记录parent_id（用于统计）
                        parent_ids_in_clues.add(parent_entity["id"])

                        self.logger.debug(
                            f"  ✅ 拆分线索: {parent_node['content'][:10]} → "
                            f"{event_node['content'][:10]} → {child_node['content'][:10]} "
                            f"(hop={hop})"
                        )
                    else:
                        # 没有event_id，直接创建entity→entity线索（fallback）
                        parent_node = Tracker.build_entity_node(parent_entity_dict)
                        child_node = Tracker.build_entity_node(child_entity_dict)

                        # 获取实体相似度
                        parent_similarity = parent_entity_dict.get("similarity", 0.0)
                        child_similarity = child_entity_dict.get("similarity", 0.0)
                        # 使用平均相似度作为confidence
                        avg_similarity = (parent_similarity + child_similarity) / 2.0

                        # 获取子实体权重信息（如果有）
                        child_entity_weight = key.get("weight")
                        metadata = {"hop": hop, "method": "cooccurrence"}
                        # 只有to节点是实体时才存储weight
                        if child_entity_weight is not None:
                            metadata["weight"] = child_entity_weight

                        tracker.add_clue(
                            stage="expand",
                            from_node=parent_node,
                            to_node=child_node,
                            confidence=avg_similarity,  # 统一使用similarity
                            metadata=metadata
                        )
                        self.logger.warning(
                            f"  ⚠️  缺少event_id，使用直接entity→entity线索: "
                            f"{parent_node['content'][:10]} → {child_node['content'][:10]}"
                        )
                else:
                    # 没有在key_parent_map中找到（理论上不应该发生）
                    self.logger.warning(
                        f"  ⚠️  key {key['key_id'][:8]} 不在key_parent_map中，跳过"
                    )

        # 🔍 诊断日志：输出线索构建统计
        self.logger.info(
            f"🔍 [Expand诊断] 线索构建统计: "
            f"总keys={total_keys}, "
            f"Expand keys={expand_keys}, "
            f"有parent_entity={keys_with_parent_entity}"
        )

        # 🔍 统计哪些 Recall 的 key 出现在了 Expand 线索中
        recall_keys_in_expand = recall_key_ids & parent_ids_in_clues
        recall_keys_not_in_expand = recall_key_ids - parent_ids_in_clues

        self.logger.info(
            f"🔍 [Expand诊断] Recall keys在Expand线索中的情况: "
            f"出现={len(recall_keys_in_expand)}个, "
            f"未出现={len(recall_keys_not_in_expand)}个"
        )

        if recall_keys_not_in_expand:
            # 显示未出现的 Recall keys
            missing_recall_keys = [k for k in recall_keys if k["key_id"] in recall_keys_not_in_expand]
            self.logger.warning(
                f"⚠️ [Expand诊断] {len(recall_keys_not_in_expand)}个Recall key未出现在Expand线索中："
            )
            for k in missing_recall_keys[:5]:  # 显示前5个
                self.logger.warning(
                    f"  • [{k.get('type', 'unknown')}] {k.get('name', 'unknown')} "
                    f"(key_id={k['key_id'][:8]}..., weight={k.get('weight', 0):.3f})"
                )

        # 如果有缺失parent_entity的Expand keys，发出警告
        if keys_without_parent_entity:
            missing_keys_info = [f"{k['name']}(step{k['step']})" for k in keys_without_parent_entity[:3]]
            self.logger.warning(
                f"⚠️ [Expand诊断] {len(keys_without_parent_entity)}个Expand key缺失parent_entity: "
                f"{missing_keys_info}"
            )

        # 返回空列表（兼容性保留，实际线索已通过tracker追加到config.all_clues）
        self.logger.info(
            f"✨ Expand线索已通过tracker追加到config.all_clues "
            f"(每个扩展拆分为2条: entity→event, event→entity)"
        )
        return []
