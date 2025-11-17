"""
实体召回模块（Recall）

实现8步骤的复合搜索算法：
1. query找key：LLM抽取query的结构化属性，通过向量相似度找到关联实体
2. key找event：通过[key-query-related]用sql找到所有关联事项
3. query再找event：通过向量相似度在找到query关联事项
4. 过滤Event：[Event-query-related]和[Event-key-query-related]取交集
5. 计算event-key权重向量：根据每个event包含key的情况计算权重
6. 计算event-key-query权重向量：将(event-key)*(e1)得到新的权重向量
7. 反向计算key权重向量：根据event权重反向计算key重要性
8. 提取重要的key：通过阈值或top-n方式提取重要key
"""

from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
import numpy as np

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
from sag.modules.search.tracker import Tracker  # 🆕 统一使用Tracker
from sag.utils import get_logger

logger = get_logger("search.recall")


@dataclass
class RecallResult:
    """实体召回结果"""
    # 查询追踪信息
    original_query: str  # 原始查询文本（用于调试和追踪）

    # 最终结果
    # [{"key": str, "weight": float, "steps": List[int]}, ...]
    key_final: List[Dict[str, Any]]

    # 中间结果（用于调试）
    key_query_related: List[Dict[str, Any]]  # 步骤1结果
    event_key_query_related: List[str]       # 步骤2结果
    event_query_related: List[Dict[str, Any]]  # 步骤3结果
    event_related: List[str]                 # 步骤4结果
    key_related: List[str]                   # 步骤4结果
    event_key_weights: Dict[str, float]      # 步骤5结果
    event_key_query_weights: Dict[str, float]  # 步骤6结果
    key_event_weights: Dict[str, float]      # 步骤7结果


class RecallSearcher:
    """实体召回搜索器 - 实现8步骤复合搜索算法"""

    def __init__(
        self,
        llm_client: BaseLLMClient,
        prompt_manager: PromptManager,
    ):
        """
        初始化实体召回搜索器

        Args:
            llm_client: LLM客户端
            prompt_manager: 提示词管理器
        """
        self.llm_client = llm_client
        self.prompt_manager = prompt_manager
        self.session_factory = get_session_factory()
        self.logger = get_logger("search.recall")

        # 初始化Elasticsearch仓库
        self.es_client = get_es_client()
        self.entity_repo = EntityVectorRepository(self.es_client)
        self.event_repo = EventVectorRepository(self.es_client)

        # 初始化文档处理器用于生成向量
        self.processor = DocumentProcessor(llm_client=llm_client)

        self.logger.info(
            "实体召回搜索器初始化完成",
            extra={
                "embedding_model_name": self.processor.embedding_model_name,
            },
        )

    async def search(self, config: SearchConfig) -> RecallResult:
        """
        执行8步骤搜索算法

        Args:
            config: 搜索配置

        Returns:
            实体召回结果
        """
        try:
            # 保存原始query用于结果追踪（必须在step1之前）
            original_query = config.query

            # 🆕 创建线索构建器
            tracker = Tracker(config)

            self.logger.info(
                f"开始实体召回：source_config_ids={config.get_source_config_ids()}, query={config.query}"
            )

            # === 步骤1: query找key（语义扩展） ===
            key_query_related, k1_weights = await self._step1_query_to_keys(config)
            self.logger.info(f"步骤1完成：找到 {len(key_query_related)} 个相关key")

            # 🆕 记录线索：query → entity（使用标准节点构建）
            for entity in key_query_related:
                # 获取实体权重信息（如果有）
                entity_weight = entity.get("weight")
                metadata = {
                    "method": "vector_search",
                    "step": "step1",
                    # 🆕 添加来源属性
                    "source_attribute": entity.get("source_attribute")
                }
                # 只有to节点是实体时才存储weight
                if entity_weight is not None:
                    metadata["weight"] = entity_weight

                tracker.add_clue(
                    stage="recall",
                    from_node=Tracker.build_query_node(config),
                    to_node=Tracker.build_entity_node(entity),
                    confidence=entity.get("similarity", 0.0),  # 统一使用similarity
                    metadata=metadata
                )

            # 🔍 显示召回实体的详细信息
            if key_query_related:
                self.logger.info(f"📋 步骤1召回实体详情 (共{len(key_query_related)}个):")
                for idx, entity in enumerate(key_query_related, 1):
                    self.logger.info(
                        f"  {idx}. 实体ID: {entity.get('entity_id')}, "
                        f"名称: '{entity.get('name')}', "
                        f"类型: {entity.get('type')}, "
                        f"相似度: {entity.get('similarity', 0.0):.4f}, "
                        f"来源属性: '{entity.get('source_attribute')}'"
                    )

            # 🔍 Step1诊断日志
            if key_query_related:
                top3 = sorted(key_query_related, key=lambda x: x.get(
                    "similarity", 0), reverse=True)[:3]
                top3_info = [(e['name'], e.get('similarity', 0)) for e in top3]
                self.logger.info(
                    f"🔍 [Step1诊断] 召回实体数={len(key_query_related)}, "
                    f"Top3: {top3_info}, "
                    f"线索数={len([c for c in config.all_clues if c.get('step') == 'step1'])}"
                )
            else:
                self.logger.warning("⚠️ [Step1诊断] 未召回任何实体，后续步骤可能无结果")

            # 存储query召回的所有key到config中
            config.query_recalled_keys = key_query_related
            self.logger.debug(
                f"已将 {len(key_query_related)} 个query召回的key存储到config.query_recalled_keys")

            # === 步骤2: key找event（精准匹配） ===
            event_key_query_related = await self._step2_keys_to_events(config, key_query_related)
            self.logger.info(
                f"步骤2完成：找到 {len(event_key_query_related)} 个key相关event")

            # === 步骤3: query再找event（语义匹配） ===
            event_query_related, e1_weights = await self._step3_query_to_events(config)
            self.logger.info(
                f"步骤3完成：找到 {len(event_query_related)} 个query相关event")

            # 🆕 记录线索：query → event（需要查询完整的event对象）
            if event_query_related:
                async with self.session_factory() as session:
                    event_ids_step3 = [e["event_id"]
                                       for e in event_query_related]
                    events_query_step3 = select(SourceEvent).where(
                        SourceEvent.id.in_(event_ids_step3))
                    events_result_step3 = await session.execute(events_query_step3)
                    events_step3 = {
                        event.id: event for event in events_result_step3.scalars().all()}

                    for event_dict in event_query_related:
                        event_obj = events_step3.get(event_dict["event_id"])
                        if event_obj:
                            tracker.add_clue(
                                stage="recall",
                                from_node=Tracker.build_query_node(config),
                                to_node=tracker.get_or_create_event_node(event_obj, "recall"),
                                confidence=event_dict.get("similarity", 0.0),
                                display_level="intermediate",  # 🆕 中间结果
                                metadata={"method": "vector_search", "step": "step3"}
                            )

            # === 步骤4: 过滤Event（精准筛选） ===
            event_related, key_related = await self._step4_filter_events(
                event_key_query_related, event_query_related, key_query_related
            )
            self.logger.info(
                f"步骤4完成：过滤后 {len(event_related)} 个event, {len(key_related)} 个key")

            # 🔍 显示key过滤情况
            original_key_count = len(key_query_related)
            retained_keys_count = len(key_related)
            lost_keys_count = original_key_count - retained_keys_count

            # 避免除零错误
            if original_key_count > 0:
                retention_rate = (retained_keys_count / original_key_count * 100)
                self.logger.info(
                    f"🔍 [Step4] Key过滤结果: "
                    f"步骤1召回={original_key_count}个 → "
                    f"步骤4过滤后={retained_keys_count}个 "
                        f"(保留率={retention_rate:.1f}%, "
                    f"过滤掉{lost_keys_count}个)"
                )
            else:
                self.logger.info(
                    f"🔍 [Step4] Key过滤结果: "
                    f"步骤1召回={original_key_count}个 → "
                    f"步骤4过滤后={retained_keys_count}个"
                )

            if lost_keys_count > 0:
                self.logger.info(
                    f"📌 [Step4] 过滤掉的{lost_keys_count}个key是因为：它们关联的events不在交集中 "
                    f"(即这些key的events与query相似度不够高)"
                )

            # 🔍 显示步骤4保留的key详情
            if key_related:
                # 从步骤1的结果中过滤出保留的key信息
                key_related_set = set(key_related)
                retained_key_infos = [
                    k for k in key_query_related if k["entity_id"] in key_related_set
                ]

                self.logger.info(f"📋 步骤4过滤后保留的key详情 (共{len(retained_key_infos)}个):")
                for idx, key_info in enumerate(retained_key_infos, 1):
                    self.logger.info(
                        f"  {idx}. 实体ID: {key_info['entity_id']}, "
                        f"名称: '{key_info['name']}', "
                        f"类型: {key_info['type']}, "
                        f"原始相似度: {key_info.get('similarity', 0.0):.4f}, "
                        f"来源属性: '{key_info.get('source_attribute', 'N/A')}'"
                    )
            else:
                self.logger.warning("⚠️ 步骤4后没有保留任何key，后续步骤将无结果")

            # 🔍 Step4诊断日志
            query_event_ids = {event["event_id"]
                               for event in event_query_related}
            key_event_ids = set(event_key_query_related)
            self.logger.info(
                f"🔍 [Step4诊断] Event交集过滤: "
                f"query召回={len(query_event_ids)}, "
                f"key召回={len(key_event_ids)}, "
                f"交集={len(event_related)}, "
                f"交集率={len(event_related) / max(len(query_event_ids), 1):.1%}"
            )
            self.logger.info(
                f"🔍 [Step4诊断] Key过滤: "
                f"输入={len(key_query_related)} (步骤1召回), "
                f"输出={len(key_related)} (events在交集中的key)"
            )

            # === 步骤5: 计算event-key权重向量 ===
            event_key_weights = await self._step5_calculate_event_key_weights(
                event_related, key_related, k1_weights
            )
            self.logger.info(
                f"步骤5完成：计算了 {len(event_key_weights)} 个event的key权重")

            # === 步骤6: 计算event-key-query权重向量 ===
            event_key_query_weights = await self._step6_calculate_event_key_query_weights(
                event_key_weights, e1_weights
            )
            self.logger.info(
                f"步骤6完成：计算了 {len(event_key_query_weights)} 个event的复合权重")

            # === 步骤7: 反向计算key权重向量 ===
            key_event_weights = await self._step7_calculate_key_event_weights(
                event_related, key_related, event_key_query_weights
            )
            self.logger.info(f"步骤7完成：计算了 {len(key_event_weights)} 个key的反向权重")

            # 🔍 Step7诊断日志
            if key_event_weights:
                weights = list(key_event_weights.values())
                self.logger.info(
                    f"🔍 [Step7诊断] Key权重分布: "
                    f"总数={len(weights)}, "
                    f"最大={max(weights):.4f}, "
                    f"最小={min(weights):.4f}, "
                    f"平均={sum(weights)/len(weights):.4f}"
                )
            else:
                self.logger.warning("⚠️ [Step7诊断] 未计算出任何key权重，Step8将无结果")

            # === 步骤8: 提取重要的key ===
            key_final = await self._step8_extract_important_keys(
                key_event_weights, config
            )
            self.logger.info(f"步骤8完成：提取了 {len(key_final)} 个重要key")

            # 🔍 分析最终key的过滤情况
            if key_final:
                self.logger.info(
                    f"🔍 [Step8] 最终结果: "
                    f"步骤1召回={len(key_query_related)}个 → "
                    f"步骤4过滤后={len(key_related)}个 → "
                    f"步骤8提取={len(key_final)}个"
                )

            # 🔍 Step8诊断日志（最关键！）
            input_keys = len(key_event_weights)
            output_keys = len(key_final)
            recall_rate = output_keys / max(input_keys, 1)

            self.logger.info(
                f"🔍 [Step8诊断] 最终过滤: "
                f"输入={input_keys}, 输出={output_keys}, 召回率={recall_rate:.1%}"
            )
            self.logger.info(
                f"🔍 [Step8诊断] 配置参数: "
                f"top_n_keys={config.recall.final_entity_count}, "
                f"final_key_threshold={config.recall.entity_weight_threshold}"
            )

            # === 🆕 步骤8完成后：生成最终线索 (display_level="final") ===
            # 为最终保留的entity生成 query → entity 线索
            # 前端精简模式：只显示这些 final 线索
            # 前端可以根据 final 线索反推完整路径（query → extracted_entity → entity）
            if key_final:
                self.logger.info(f"🎯 [Step8] 生成 {len(key_final)} 条最终线索 (display_level=final)")

                for key in key_final:
                    # 从 key_query_related 中找到原始entity信息
                    original_entity = next(
                        (e for e in key_query_related if e["entity_id"] == key["key_id"]),
                        None
                    )

                    if original_entity:
                        # 获取实体权重信息（如果有）
                        entity_weight = key.get("weight")
                        metadata = {
                            "method": "final_result",
                            "step": "step8",
                            "steps": key.get("steps", [1]),
                            "source_attribute": original_entity.get("source_attribute")
                        }
                        # 只有to节点是实体时才存储weight
                        if entity_weight is not None:
                            metadata["weight"] = entity_weight

                        tracker.add_clue(
                            stage="recall",
                            from_node=Tracker.build_query_node(config),
                            to_node=Tracker.build_entity_node(original_entity),
                            confidence=original_entity.get("similarity", 0.0),  # 统一使用similarity
                            relation="语义相似",
                            display_level="final",  # 🆕 标记为最终结果
                            metadata=metadata
                        )
                    else:
                        self.logger.warning(
                            f"⚠️ [Step8] 无法为 key_id={key['key_id']} 生成最终线索: "
                            f"在 key_query_related 中找不到原始信息"
                        )

                self.logger.info(
                    f"✅ [Step8] 最终线索生成完成，前端可根据这些 final 线索反推完整推理路径"
                )
            else:
                self.logger.warning(
                    f"⚠️ [Step8] 没有生成任何最终线索！key_final 为空。"
                    f"这可能导致前端精简模式图谱为空。"
                    f"建议检查配置参数：top_n_keys={config.recall.final_entity_count}, "
                    f"final_key_threshold={config.recall.entity_weight_threshold}"
                )


            # 如果召回率过低，发出警告并显示被过滤掉的实体
            if recall_rate < 0.3 and input_keys > 0:
                self.logger.warning(
                    f"⚠️ [Step8诊断] 召回率过低 ({recall_rate:.1%})！"
                    f"可能需要调整参数：增大top_n_keys或降低final_key_threshold"
                )

                # 显示被过滤掉的实体（权重最高的5个）
                if input_keys - output_keys > 0:
                    sorted_weights = sorted(
                        key_event_weights.items(), key=lambda x: x[1], reverse=True)
                    filtered_out = sorted_weights[output_keys:output_keys + 5]

                    if filtered_out:
                        filtered_info = [(kid, w) for kid, w in filtered_out]
                        self.logger.warning(
                            f"⚠️ [Step8诊断] 被过滤掉的高权重实体（前5个）: "
                            f"{filtered_info}"
                        )

            # === 构建Recall阶段线索 ===
            # 使用config.query_recalled_keys（已在step8中过滤并更新为key_final格式）
            recall_clues = await self._build_recall_clues(
                config, config.query_recalled_keys)
            config.recall_clues = recall_clues
            self.logger.info(
                f"✨ 构建了 {len(recall_clues)} 条Recall线索 (query → entity), "
                f"这些是步骤1直接召回且在最终结果中的实体"
            )

            result = RecallResult(
                original_query=original_query,
                key_final=key_final,
                key_query_related=key_query_related,
                event_key_query_related=event_key_query_related,
                event_query_related=event_query_related,
                event_related=event_related,
                key_related=key_related,
                event_key_weights=event_key_weights,
                event_key_query_weights=event_key_query_weights,
                key_event_weights=key_event_weights,
            )

            self.logger.info(
                f"实体召回完成：返回 {len(key_final)} 个重要key",
                extra={
                    "source_config_ids": config.source_config_ids,
                    "query": config.query,
                    "final_keys_count": len(key_final),
                },
            )

            return result

        except Exception as e:
            self.logger.error(f"实体召回失败: {e}", exc_info=True)
            raise

    # === 步骤实现方法 ===

    async def _step1_query_to_keys(
        self, config: SearchConfig
    ) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
        """
        步骤1: query找key（语义扩展）
        LLM抽取query的结构化属性，通过向量相似度找到关联实体

        如果启用了query重写，会直接修改config.query为重写后的query，
        这样后续的模块都会自动使用重写后的query

        Returns:
            Tuple[List[Dict[str, Any]], Dict[str, float]]:
                (key_query_related, k1_weights)
        """
        # TODO: 完善LLM属性抽取实现
        # 当前实现：
        # 1. 使用简单规则从query中提取属性（占位符）
        # 2. 将属性转换为向量（占位符实现）
        # 3. 使用向量搜索找到相似实体

        self.logger.info(
            f"步骤1开始: query='{config.query}', "
            f"key_similarity_threshold={config.recall.entity_similarity_threshold}, "
            f"max_keys={config.recall.max_entities}, "
            f"source_config_ids={config.get_source_config_ids()}, "
            f"use_fast_mode={config.recall.use_fast_mode}"
        )

        # 快速模式：直接用query的embedding召回key，跳过LLM属性抽取和query重写
        if config.recall.use_fast_mode:
            self.logger.info("🚀 使用快速模式：跳过LLM属性抽取，直接使用query embedding召回key")

            # 快速模式下也需要设置origin_query（未重写）
            config.original_query = config.query

            try:
                # 生成原始query的embedding
                self.logger.debug(f"开始为query '{config.query}' 生成向量...")
                query_embedding = await self.processor.generate_embedding(config.query)
                self.logger.info(f"✅ Query向量生成成功，维度: {len(query_embedding)}")

                # 缓存query_embedding到config，避免重复生成
                config.query_embedding = query_embedding
                config.has_query_embedding = True
                self.logger.debug("📦 Query向量已缓存到config中")

                # 直接搜索entity（不限制entity_type）
                self.logger.debug(
                    f"开始向量搜索: k={config.recall.vector_top_k}, source_config_ids={config.get_source_config_ids()}")
                similar_entities = await self.entity_repo.search_similar(
                    query_vector=query_embedding,
                    k=config.recall.vector_top_k,
                    source_config_ids=config.get_source_config_ids(),  # 使用多源支持
                    entity_type=None,  # 不限制类型
                    include_type_threshold=True,
                )

                self.logger.info(f"📊 快速模式搜索到 {len(similar_entities)} 个候选实体")

                # 过滤阈值
                key_query_related = []
                k1_weights = {}
                passed_count = 0

                for entity in similar_entities:
                    similarity = float(entity.get("_score", 0.0))
                    type_threshold = entity.get("type_threshold", 0.800)
                    final_threshold = max(
                        config.recall.entity_similarity_threshold, type_threshold)

                    if similarity >= final_threshold:
                        key_query_related.append({
                            "entity_id": entity["entity_id"],
                            "name": entity["name"],
                            "type": entity["type"],
                            "similarity": similarity,
                            "source_attribute": config.query,  # 直接使用原始query
                            "type_threshold": type_threshold,
                            "final_threshold": final_threshold,
                        })
                        k1_weights[entity["entity_id"]] = similarity
                        passed_count += 1

                self.logger.info(
                    f"📈 快速模式阈值过滤结果: "
                    f"通过 {passed_count}/{len(similar_entities)}"
                )

                # 去重并限制数量
                seen_entities = set()
                unique_keys = []
                for key_info in key_query_related:
                    entity_id = key_info["entity_id"]
                    if entity_id not in seen_entities:
                        seen_entities.add(entity_id)
                        unique_keys.append(key_info)

                key_query_related = unique_keys[:config.recall.max_entities]

                self.logger.info(
                    f"📋 快速模式完成: 最终返回 {len(key_query_related)} 个key"
                )

                if len(key_query_related) > 0:
                    top_entities = sorted(
                        key_query_related, key=lambda x: x["similarity"], reverse=True)[:3]
                    top_info = [
                        f"'{e['name']}'({e['type']}, {e['similarity']:.3f})"
                        for e in top_entities
                    ]
                    self.logger.info(f"🏆 Top 3 相似实体: {', '.join(top_info)}")

                return key_query_related, k1_weights

            except Exception as e:
                self.logger.error(f"❌ 快速模式失败: {e}")
                import traceback
                self.logger.debug(f"详细错误信息: {traceback.format_exc()}")
                raise

        # 🆕 创建线索构建器（统一方式）
        tracker = Tracker(config)

        # 1. 从query中抽取结构化属性，可选地进行query重写
        # 向后兼容性检查：确保配置项存在
        enable_rewrite = getattr(config, 'enable_query_rewrite', True)

        # 保存原始query
        original_query = config.query

        query_attributes, rewritten_query = await self._extract_attributes_from_query(
            config.query,
            enable_rewrite=enable_rewrite
        )

        # 如果启用了重写功能
        if enable_rewrite:
            if rewritten_query:
                # 保存原始query到origin_query
                config.original_query = original_query
                # 将重写后的query保存到query
                config.query = rewritten_query
                self.logger.info(
                    f"🔄 Query重写: origin='{original_query}' → query='{rewritten_query}'")

                # 🆕 记录 prepare 阶段线索：query重写
                tracker.add_clue(
                    stage="prepare",
                    from_node=Tracker.build_query_node(
                        config, use_origin=True),  # 原始query
                    to_node=Tracker.build_query_node(
                        config, use_origin=False),   # 重写后query
                    confidence=1.0,
                    relation="重写用户请求",
                    metadata={"method": "llm_rewrite"}
                )
            else:
                # 没有重写结果，origin_query和query都使用原始query
                config.original_query = original_query
                self.logger.debug(f"📝 Query未重写，保持原样: '{config.query}'")
        else:
            # 未启用重写功能，origin_query和query都使用原始query
            config.original_query = original_query
            self.logger.debug(f"📝 Query重写功能未启用，使用原始query: '{config.query}'")

        self.logger.info(
            f"抽取到 {len(query_attributes)} 个属性: {[attr['name'] for attr in query_attributes]}")

        # 🆕 记录 prepare 阶段线索：属性提取
        if query_attributes:
            query_node = Tracker.build_query_node(config)
            for attr in query_attributes:
                entity_node = Tracker.build_extracted_entity_node(attr)
                tracker.add_clue(
                    stage="prepare",
                    from_node=query_node,
                    to_node=entity_node,
                    confidence=await self._importance_to_confidence(
                        attr.get("importance", "medium")),
                    relation="请求的属性提取",
                    metadata={
                        "method": "llm_extraction",
                        "attribute_type": attr.get("type"),
                        "importance": attr.get("importance", "medium")
                    }
                )

        # 详细记录抽取的属性 + 收集属性信息，为后面批量embedding做准备
        attribute_names = []
        if query_attributes:
            self.logger.info("详细属性信息:")
            for i, attr in enumerate(query_attributes, 1):
                self.logger.info(
                    f"  {i}. 名称: '{attr['name']}', 类型: {attr['type']}, 重要性: {attr.get('importance', 'N/A')}")
                # 提取所有属性名称
                attribute_names.append(attr["name"])

        else:
            self.logger.warning("⚠️ 未抽取到任何属性，这可能导致无法找到Keys")

        key_query_related = []
        k1_weights = {}
        total_searched = 0

        # 2. 批量生成所有属性的 embedding 向量（优化：减少API调用次数）
        if query_attributes:
            import time
            self.logger.info(
                f"🚀 开始批量生成 {len(query_attributes)} 个属性的embedding向量...")
            batch_embedding_start = time.perf_counter()

            # 批量生成 embedding
            from sag.core.ai.embedding import batch_generate_embedding
            attribute_vectors = await batch_generate_embedding(attribute_names)

            batch_embedding_time = time.perf_counter() - batch_embedding_start
            self.logger.info(
                f"✅ 批量生成完成，共 {len(attribute_vectors)} 个向量，"
                f"耗时: {batch_embedding_time:.3f}秒，"
                f"平均每个: {batch_embedding_time/len(attribute_vectors):.3f}秒"
            )

            # 将向量附加到属性信息中
            for attr, vector in zip(query_attributes, attribute_vectors):
                attr["vector"] = vector
        else:
            self.logger.warning("⚠️ 没有属性需要生成向量")

        # 3. 对每个属性进行向量搜索
        for i, attribute_info in enumerate(query_attributes, 1):
            self.logger.info(
                f"🔍 正在搜索属性 {i}/{len(query_attributes)}: '{attribute_info['name']}' (类型: {attribute_info['type']})")

            try:
                # 使用预先生成的向量
                query_embedding = attribute_info["vector"]
                self.logger.debug(f"使用预生成的向量，维度: {len(query_embedding)}")

                # 使用向量搜索找相似实体，包含实体类型阈值信息
                self.logger.debug(
                    f"开始向量搜索: k={config.recall.vector_top_k}, source_config_ids={config.get_source_config_ids()}, entity_type={attribute_info['type']}")
                similar_entities = await self.entity_repo.search_similar(
                    query_vector=query_embedding,
                    k=config.recall.vector_top_k,
                    source_config_ids=config.get_source_config_ids(),  # 使用多源支持
                    entity_type=attribute_info["type"],
                    include_type_threshold=True,
                )

                total_searched += len(similar_entities)
                self.logger.info(
                    f"📊 属性 '{attribute_info['name']}' 搜索到 {len(similar_entities)} 个候选实体")

                # 如果没有搜索到任何实体，记录详细信息
                if len(similar_entities) == 0:
                    self.logger.warning(
                        f"⚠️ 属性 '{attribute_info['name']}' 未找到任何候选实体")
                    self.logger.warning(
                        f"   可能原因: 1) source_config_ids '{config.source_config_ids}' 无数据 2) entity_type '{attribute_info['type']}' 无数据 3) ES索引问题")
                    continue

                # 记录搜索到的原始结果（前3个）
                self.logger.debug("搜索到的候选实体（前3个）:")
                for j, entity in enumerate(similar_entities[:3], 1):
                    similarity = float(entity.get("_score", 0.0))
                    type_threshold = entity.get("type_threshold", 0.800)
                    self.logger.debug(
                        f"  {j}. '{entity['name']}' [{entity['type']}] - similarity: {similarity:.3f}, type_threshold: {type_threshold:.3f}")

                passed_count = 0
                failed_count = 0
                # 过滤相似度阈值并记录权重
                for entity in similar_entities:
                    similarity = float(entity.get("_score", 0.0))
                    type_threshold = entity.get("type_threshold", 0.800)

                    # 使用配置阈值和类型阈值中的最大值
                    final_threshold = max(
                        config.recall.entity_similarity_threshold, type_threshold)

                    if similarity >= final_threshold:
                        key_query_related.append({
                            "entity_id": entity["entity_id"],
                            "name": entity["name"],
                            "type": entity["type"],
                            "similarity": similarity,
                            "source_attribute": attribute_info["name"],
                            "type_threshold": type_threshold,
                            "final_threshold": final_threshold,
                        })
                        k1_weights[entity["entity_id"]] = similarity
                        passed_count += 1

                        # 🆕 记录线索：extracted_entity → real_entity
                        extracted_node = Tracker.build_extracted_entity_node(
                            attribute_info)
                        real_entity_dict = {
                            "entity_id": entity["entity_id"],
                            "name": entity["name"],
                            "type": entity["type"],
                            "description": entity.get("description", "")
                        }
                        # 获取实体权重信息（如果有）
                        entity_weight = real_entity_dict.get("weight")
                        metadata = {
                            "method": "vector_search",
                            "step": "step1",
                            "source_attribute": attribute_info["name"],
                            "attribute_type": attribute_info.get("type"),
                            "type_threshold": type_threshold,
                            "final_threshold": final_threshold,
                        }
                        # 只有to节点是实体时才存储weight
                        if entity_weight is not None:
                            metadata["weight"] = entity_weight

                        tracker.add_clue(
                            stage="recall",
                            from_node=extracted_node,
                            to_node=Tracker.build_entity_node(
                                real_entity_dict),
                            confidence=similarity,  # 统一使用similarity
                            relation="向量召回",
                            display_level="intermediate",  # 🆕 中间结果
                            metadata=metadata
                        )

                        self.logger.debug(
                            f"✅ 实体 '{entity['name']}' 通过阈值检查: "
                            f"similarity={similarity:.3f} >= final_threshold={final_threshold:.3f} "
                            f"(type_threshold={type_threshold:.3f}, config_threshold={config.recall.entity_similarity_threshold:.3f})"
                        )
                    else:
                        failed_count += 1
                        self.logger.debug(
                            f"❌ 实体 '{entity['name']}' 未通过阈值检查: "
                            f"similarity={similarity:.3f} < final_threshold={final_threshold:.3f} "
                            f"(type_threshold={type_threshold:.3f}, config_threshold={config.recall.entity_similarity_threshold:.3f})"
                        )

                self.logger.info(
                    f"📈 属性 '{attribute_info['name']}' 阈值过滤结果: "
                    f"通过 {passed_count}/{len(similar_entities)}, 失败 {failed_count}/{len(similar_entities)}"
                )

            except AIError as e:
                self.logger.error(
                    f"❌ 属性 '{attribute_info['name']}' 向量生成失败: {e}")
                self.logger.error(
                    f"   可能原因: 1) Embedding API问题 2) 网络连接问题 3) API密钥问题")
                # 向量生成失败时跳过该属性，继续处理其他属性
                continue
            except Exception as e:
                self.logger.error(f"❌ 搜索实体失败: {attribute_info['name']} - {e}")
                import traceback
                self.logger.debug(f"详细错误信息: {traceback.format_exc()}")
                continue

        # 3. 去重（基于entity_id）并限制数量
        seen_entities = set()
        unique_keys = []
        for key_info in key_query_related:
            entity_id = key_info["entity_id"]
            if entity_id not in seen_entities:
                seen_entities.add(entity_id)
                unique_keys.append(key_info)

        before_limit = len(unique_keys)
        key_query_related = unique_keys[: config.recall.max_entities]

        # 汇总日志
        self.logger.info(
            f"📋 步骤1完成: 总搜索={total_searched}, "
            f"通过阈值={before_limit}, "
            f"去重后={len(key_query_related)}, "
            f"限制max_keys={config.recall.max_entities}"
        )

        if len(key_query_related) > 0:
            # 显示最高相似度的几个实体
            top_entities = sorted(
                key_query_related, key=lambda x: x["similarity"], reverse=True)[:3]
            top_info = [
                f"'{e['name']}'({e['type']}, {e['similarity']:.3f})"
                for e in top_entities
            ]
            self.logger.info(f"🏆 Top 3 相似实体: {', '.join(top_info)}")
        else:
            self.logger.error("❌ 步骤1最终结果: 未找到任何Keys！")
            self.logger.error("   可能的解决方案:")
            self.logger.error("   1. 降低 key_similarity_threshold (当前: {:.1f})".format(
                config.recall.entity_similarity_threshold))
            self.logger.error("   2. 检查 source_config_ids '{}' 是否有实体数据".format(
                config.source_config_ids))
            self.logger.error(
                "   3. 检查 Elasticsearch 索引 'entity_vectors' 是否有数据")
            self.logger.error("   4. 检查实体类型相似度阈值设置是否过高")

        return key_query_related, k1_weights

    async def _extract_attributes_from_query(self, query: str, enable_rewrite: bool = True) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """
        从query中抽取结构化属性，可选择性地进行query重写

        Args:
            query: 原始查询文本
            enable_rewrite: 是否启用query重写功能

        Returns:
            Tuple[List[Dict[str, Any]], Optional[str]]: (属性列表, 重写后的query)
        """
        self.logger.debug(
            f"开始从query中抽取属性: {query}, enable_rewrite={enable_rewrite}")

        try:
            if enable_rewrite:
                # 使用带重写功能的提示词模板
                prompt = self.prompt_manager.render(
                    "extract_attributes_with_rewrite",
                    query=query
                )

                # 调用LLM进行属性抽取和查询重写
                messages = [
                    LLMMessage(role=LLMRole.USER, content=prompt)
                ]

                # 构建支持重写的JSON Schema
                schema = self._build_attribute_extraction_with_rewrite_schema()

                response = await self.llm_client.chat_with_schema(
                    messages, response_schema=schema, temperature=0.2, max_tokens=2000
                )

                # 解析LLM响应，提取结构化属性和重写后的查询
                attributes, rewritten_query = await self._parse_attribute_extraction_with_rewrite_response(
                    response)

                if not attributes:
                    self.logger.debug("LLM未提取到属性，使用回退方案")
                    return await self._fallback_attribute_extraction(query), None

                self.logger.debug(
                    f"LLM抽取到 {len(attributes)} 个属性: {[attr['name'] for attr in attributes]}")

                # 记录重写信息
                if rewritten_query and rewritten_query != query:
                    self.logger.info(
                        f"📝 Query重写: '{query}' → '{rewritten_query}'")
                elif rewritten_query:
                    self.logger.debug(f"📝 Query保持不变: '{query}' (质量分数未达到阈值)")

                return attributes, rewritten_query
            else:
                # 使用原有的属性抽取逻辑
                prompt = self.prompt_manager.render(
                    "extract_attributes",
                    query=query
                )

                messages = [
                    LLMMessage(role=LLMRole.USER, content=prompt)
                ]

                schema = await self._build_attribute_extraction_schema()

                response = await self.llm_client.chat_with_schema(
                    messages, response_schema=schema, temperature=0.2, max_tokens=2000
                )

                attributes = await self._parse_attribute_extraction_response(
                    response)

                if not attributes:
                    self.logger.debug("LLM未提取到属性，使用回退方案")
                    return await self._fallback_attribute_extraction(query), None

                self.logger.debug(
                    f"LLM抽取到 {len(attributes)} 个属性: {[attr['name'] for attr in attributes]}")
                return attributes, None

        except Exception as e:
            self.logger.warning(f"LLM属性抽取失败，使用回退方案: {e}")
            # 回退到简单的规则匹配
            return await self._fallback_attribute_extraction(query), None

    async def _build_attribute_extraction_schema(self) -> Dict[str, Any]:
        """
        构建属性提取的JSON Schema，匹配现有提示词模板的输出格式
        """
        return {
            "type": "object",
            "properties": {
                "attributes": {
                    "type": "array",
                    "description": "提取的属性列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "属性名称"},
                            "type": {"type": "string", "description": "属性类型（person/location/time/topic/action/organization/product等）"},
                            "context": {"type": "string", "description": "在查询中的上下文"},
                            "importance": {"type": "string", "description": "重要性（high/medium/low）"}
                        },
                        "required": ["name", "type", "importance"]
                    }
                }
            },
            "required": ["attributes"],
        }

    async def _build_attribute_extraction_with_rewrite_schema(self) -> Dict[str, Any]:
        """
        构建支持query重写的属性提取JSON Schema
        """
        return {
            "type": "object",
            "properties": {
                "attributes": {
                    "type": "array",
                    "description": "提取的属性列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "属性名称"},
                            "type": {"type": "string", "description": "属性类型（person/location/time/topic/action/organization/product等）"},
                            "context": {"type": "string", "description": "在查询中的上下文"},
                            "importance": {"type": "string", "description": "重要性（high/medium/low）"}
                        },
                        "required": ["name", "type", "importance"]
                    }
                },
                "rewritten_query": {
                    "type": "string",
                    "description": "重写后的查询文本"
                }
            },
            "required": ["attributes", "rewritten_query"],
        }

    async def _parse_attribute_extraction_response(self, response: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        解析属性提取响应，匹配现有提示词模板的输出格式
        """
        attributes = []
        attributes_data = response.get("attributes", [])

        if not isinstance(attributes_data, list):
            return attributes

        for attr_item in attributes_data:
            if not isinstance(attr_item, dict):
                continue

            name = attr_item.get("name", "").strip()
            attr_type = attr_item.get("type", "").strip()
            context = attr_item.get("context", "").strip()
            importance = attr_item.get("importance", "medium").strip()

            if name and attr_type:  # 确保名称和类型都不为空
                # 验证重要性字段
                if importance not in ["high", "medium", "low"]:
                    importance = "medium"

                attributes.append({
                    "name": name,
                    "type": attr_type,
                    "context": context,
                    "importance": importance,
                    "confidence": await self._importance_to_confidence(importance)
                })

        return attributes

    async def _parse_attribute_extraction_with_rewrite_response(
        self, response: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """
        解析支持query重写的属性提取响应

        Args:
            response: LLM响应

        Returns:
            Tuple[List[Dict[str, Any]], Optional[str]]: (属性列表, 重写后的query或None)
        """
        # 解析属性部分
        attributes = await self._parse_attribute_extraction_response(response)

        # 解析重写部分
        rewritten_query = response.get("rewritten_query", "").strip()

        if rewritten_query:
            self.logger.debug(f"获取到重写后的query: '{rewritten_query}'")
        else:
            self.logger.debug("未获取到重写后的query，将使用原query")

        return attributes, rewritten_query if rewritten_query else None

    async def _importance_to_confidence(self, importance: str) -> float:
        """
        将重要性转换为置信度
        """
        importance_confidence_map = {
            "high": 0.9,
            "medium": 0.7,
            "low": 0.5
        }
        return importance_confidence_map.get(importance, 0.7)

    async def _parse_llm_attributes_response(self, response: str) -> List[Dict[str, Any]]:
        """
        解析LLM返回的属性信息（保留原有方法作为兼容）
        """
        import json
        import re

        try:
            # 尝试直接解析JSON
            if response.strip().startswith('[') or response.strip().startswith('{'):
                return json.loads(response)
        except json.JSONDecodeError:
            pass

        # 如果JSON解析失败，尝试从文本中提取
        attributes = []

        # 简单的正则匹配提取
        patterns = [
            r'名称[：:]\s*([^\n,，]+)\s*类型[：:]\s*([^\n,，]+)',
            r'属性[：:]\s*([^\n,，]+)\s*\(([^)]+)\)',
            r'([^\n,，]+)\s*-\s*([^\n,，]+)',
        ]

        for pattern in patterns:
            matches = re.findall(pattern, response)
            for name, attr_type in matches:
                # 标准化属性类型
                attr_type = attr_type.strip().lower()
                if any(word in attr_type for word in ['人', 'person', '人物', '专家']):
                    standardized_type = 'person'
                elif any(word in attr_type for word in ['组织', 'org', '企业', '公司', '机构']):
                    standardized_type = 'organization'
                elif any(word in attr_type for word in ['地', 'location', '地点', '地方']):
                    standardized_type = 'location'
                elif any(word in attr_type for word in ['时', 'time', '时间', '日期']):
                    standardized_type = 'time'
                else:
                    standardized_type = 'topic'

                attributes.append({
                    "name": name.strip(),
                    "type": standardized_type
                })

        return attributes[:10]  # 限制最多10个属性

    async def _fallback_attribute_extraction(self, query: str) -> List[Dict[str, Any]]:
        """
        回退的属性抽取方案（基于规则）
        """
        attributes = []

        # 基于一些简单的规则提取属性
        if any(word in query.lower() for word in ["ai", "artificial intelligence", "人工智能"]):
            attributes.append({"name": "AI", "type": "topic"})
        if any(word in query.lower() for word in ["tech", "technology", "技术", "科技"]):
            attributes.append({"name": "科技", "type": "topic"})
        if any(word in query.lower() for word in ["innovation", "创新"]):
            attributes.append({"name": "创新", "type": "topic"})
        if any(word in query.lower() for word in ["medical", "health", "医疗", "健康"]):
            attributes.append({"name": "医疗", "type": "topic"})
        if any(word in query.lower() for word in ["company", "企业", "公司"]):
            attributes.append({"name": "企业", "type": "organization"})
        if any(word in query.lower() for word in ["person", "people", "人物", "专家"]):
            attributes.append({"name": "人物", "type": "person"})

        # 如果没有提取到属性，使用默认
        if not attributes:
            attributes = [
                {"name": "AI", "type": "topic"},
                {"name": "科技", "type": "topic"},
            ]

        return attributes

    async def _step2_keys_to_events(
        self, config: SearchConfig, key_query_related: List[Dict[str, Any]]
    ) -> List[str]:
        """
        步骤2: key找event（精准匹配）
        通过[key-query-related]用sql找到所有关联事项

        同时记录线索：entity → event
        """
        if not key_query_related:
            return []

        key_entity_ids = [key["entity_id"] for key in key_query_related]

        # 🆕 构建 entity_id → source_attribute 映射
        entity_source_map = {
            key["entity_id"]: key.get("source_attribute")
            for key in key_query_related
        }

        # 🆕 创建线索构建器记录线索
        tracker = Tracker(config)

        async with self.session_factory() as session:
            # 查询entity-event关系（返回完整映射，用于记录线索）
            query = (
                select(EventEntity.entity_id, EventEntity.event_id)
                .where(EventEntity.entity_id.in_(key_entity_ids))
            )

            result = await session.execute(query)
            entity_event_pairs = result.fetchall()

            # 🆕 记录线索：entity → event（使用标准节点，查询event对象获取完整信息）
            # 先批量查询event对象
            event_ids_for_query = list(
                set(event_id for _, event_id in entity_event_pairs))
            events_query = select(SourceEvent).where(
                SourceEvent.id.in_(event_ids_for_query))
            events_result = await session.execute(events_query)
            events = {event.id: event for event in events_result.scalars().all()}

            # 同时查询entity对象
            entities_query = select(Entity).where(
                Entity.id.in_(key_entity_ids))
            entities_result = await session.execute(entities_query)
            entities = {
                entity.id: entity for entity in entities_result.scalars().all()}

            # 记录每个entity→event的线索
            for entity_id, event_id in entity_event_pairs:
                entity_obj = entities.get(entity_id)
                event_obj = events.get(event_id)

                # 构建entity和event节点
                if entity_obj:
                    entity_dict = {
                        "id": entity_obj.id,
                        "entity_id": entity_obj.id,  # 兼容字段
                        "name": entity_obj.name,
                        "type": entity_obj.type,
                        "description": entity_obj.description or "",
                        # 🆕 添加来源属性
                        "source_attribute": entity_source_map.get(entity_id)
                    }
                else:
                    # Fallback
                    entity_dict = {
                        "id": entity_id,
                        "entity_id": entity_id,
                        # 🆕 添加来源属性
                        "source_attribute": entity_source_map.get(entity_id)
                    }

                if event_obj:
                    # 从实体字典中获取相似度作为confidence（如果可用）
                    entity_similarity = entity_dict.get("similarity", 1.0)
                    metadata = {
                        "method": "database_lookup",
                        "step": "step2",
                        # 🆕 添加到metadata
                        "source_attribute": entity_dict.get("source_attribute")
                    }
                    # to节点是事件，不存储weight

                    tracker.add_clue(
                        stage="recall",
                        from_node=Tracker.build_entity_node(entity_dict),
                        to_node=tracker.get_or_create_event_node(event_obj, "recall"),
                        confidence=entity_similarity,  # 使用实体的相似度
                        display_level="intermediate",  # 🆕 中间结果
                        metadata=metadata
                    )

            # 返回去重的event_ids
            event_ids = list(
                set(event_id for _, event_id in entity_event_pairs))

        return event_ids

    async def _step3_query_to_events(
        self, config: SearchConfig
    ) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
        """
        步骤3: query再找event（语义匹配）
        通过向量相似度在找到query关联事项
        """
        self.logger.debug(f"步骤3: 通过语义匹配搜索相关事件 - {config.query}")

        try:
            # 检查是否已有缓存的query_embedding
            if config.has_query_embedding and config.query_embedding:
                query_embedding = config.query_embedding
                self.logger.debug(f"📦 使用缓存的query向量，维度: {len(query_embedding)}")
            else:
                # 使用真实Embedding API生成查询向量
                query_embedding = await self.processor.generate_embedding(config.query)
                self.logger.debug(f"查询向量生成成功，维度: {len(query_embedding)}")

                # 缓存query_embedding到config
                config.query_embedding = query_embedding
                config.has_query_embedding = True
                self.logger.debug("📦 Query向量已缓存到config中")
        except Exception as e:
            raise AIError(f"查询向量生成失败: {e}") from e

        content_similar_events = []

        try:
            # 通过内容向量搜索
            content_similar_events = await self.event_repo.search_similar_by_content(
                query_vector=query_embedding,
                k=config.recall.vector_top_k,
                source_config_ids=config.get_source_config_ids(),  # 使用多源支持
            )
        except Exception as e:
            self.logger.warning(f"内容向量搜索失败: {e}")
            return [], {}

        # 直接使用内容搜索结果
        event_query_related = []
        for event in content_similar_events:
            event_query_related.append({
                "event_id": event["event_id"],
                "title": event["title"],
                "summary": event.get("summary", ""),
                "similarity": float(event.get("_score", 0.0)),
                "match_type": "content",
            })

        # 过滤相似度阈值
        before_threshold = len(event_query_related)
        event_query_related = [
            event for event in event_query_related
            if event["similarity"] >= config.recall.event_similarity_threshold
        ]

        # 限制数量
        event_query_related = event_query_related[: config.recall.max_events]

        # 构建权重向量
        e1_weights = {event["event_id"]: event["similarity"]
                      for event in event_query_related}

        self.logger.debug(
            f"步骤3完成(仅内容搜索): 找到 {len(event_query_related)} 个相关事件 "
            f"(阈值过滤前: {before_threshold})"
        )

        return event_query_related, e1_weights

    async def _step4_filter_events(
        self,
        event_key_query_related: List[str],
        event_query_related: List[Dict[str, Any]],
        key_query_related: List[Dict[str, Any]],
    ) -> Tuple[List[str], List[str]]:
        """
        步骤4: 过滤Event（精准筛选）
        [Event-query-related]和[Event-key-query-related]取交集
        然后只保留步骤1召回的key中，那些events在交集中的key
        """
        # 提取event_query_related中的event_id
        query_event_ids = {event["event_id"] for event in event_query_related}
        key_event_ids = set(event_key_query_related)

        # 取交集
        event_related = list(query_event_ids.intersection(key_event_ids))
        event_related_set = set(event_related)

        self.logger.debug(
            f"📊 [Step4内部] Events交集: "
            f"key找到的events={len(key_event_ids)}, "
            f"query找到的events={len(query_event_ids)}, "
            f"交集={len(event_related)}"
        )

        # 只保留步骤1召回的key中，那些关联的events在交集中的key
        key_related = []
        if event_related:
            async with self.session_factory() as session:
                for key_info in key_query_related:
                    key_id = key_info["entity_id"]

                    # 查询这个key关联的所有events
                    query = (
                        select(EventEntity.event_id)
                        .where(EventEntity.entity_id == key_id)
                    )
                    result = await session.execute(query)
                    key_events = {row[0] for row in result.fetchall()}

                    # 检查这个key的events是否与交集有交集
                    if key_events.intersection(event_related_set):
                        key_related.append(key_id)

                self.logger.debug(
                    f"📊 [Step4内部] 从步骤1召回的{len(key_query_related)}个key中，"
                    f"保留了{len(key_related)}个（它们的events在交集中）"
                )

        return event_related, key_related

    async def _step5_calculate_event_key_weights(
        self,
        event_related: List[str],
        key_related: List[str],
        k1_weights: Dict[str, float],
    ) -> Dict[str, float]:
        """
        步骤5: 计算event-key权重向量
        根据每个event包含key的情况，将对应key的权重相加
        """
        if not event_related or not key_related:
            return {}

        event_key_weights = {}

        try:
            async with self.session_factory() as session:
                for event_id in event_related:
                    # 查询该event包含的所有key
                    query = (
                        select(EventEntity.entity_id)
                        .where(EventEntity.event_id == event_id)
                        .where(EventEntity.entity_id.in_(key_related))
                    )
                    result = await session.execute(query)
                    event_keys = [row[0] for row in result.fetchall()]

                    # 计算权重：W_event-key(ej) = Σ(k1)i (k_i ∈ e_j)
                    total_weight = sum(k1_weights.get(key_id, 0.0)
                                       for key_id in event_keys)
                    event_key_weights[event_id] = total_weight
        except Exception as e:
            self.logger.error(f"步骤5计算event-key权重失败: {e}", exc_info=True)
            raise

        return event_key_weights

    async def _step6_calculate_event_key_query_weights(
        self,
        event_key_weights: Dict[str, float],
        e1_weights: Dict[str, float],
    ) -> Dict[str, float]:
        """
        步骤6: 计算event-key-query权重向量
        将(event-key)*(e1)，得到新的（e2）向量
        """
        event_key_query_weights = {}

        for event_id in event_key_weights:
            key_weight = event_key_weights[event_id]
            query_weight = e1_weights.get(event_id, 0.0)

            # W_e2(ej) = W_event-key(ej) × (e1)j
            event_key_query_weights[event_id] = key_weight * query_weight

        return event_key_query_weights

    async def _step7_calculate_key_event_weights(
        self,
        event_related: List[str],
        key_related: List[str],
        event_key_query_weights: Dict[str, float],
    ) -> Dict[str, float]:
        """
        步骤7: 反向计算key权重向量
        根据每个event的权重反向计算key的重要性
        """
        if not event_related or not key_related:
            return {}

        key_event_weights = {}

        async with self.session_factory() as session:
            for key_id in key_related:
                # 查询包含该key的所有event
                query = (
                    select(EventEntity.event_id)
                    .where(EventEntity.entity_id == key_id)
                    .where(EventEntity.event_id.in_(event_related))
                )
                result = await session.execute(query)
                key_events = [row[0] for row in result.fetchall()]

                # 计算权重：W_key-event(ki) = Σ W_e2(ej) (e_j contains k_i)
                total_weight = sum(
                    event_key_query_weights.get(event_id, 0.0) for event_id in key_events
                )
                key_event_weights[key_id] = total_weight

        return key_event_weights

    async def _step8_extract_important_keys(
        self,
        key_event_weights: Dict[str, float],
        config: SearchConfig,
    ) -> List[Dict[str, Any]]:
        """
        步骤8: 提取重要的key
        设置相似度阈值或提取top-n重要的key
        """
        # 获取key的详细信息
        key_final = []

        if not key_event_weights:
            return key_final

        # 按权重排序
        sorted_keys = sorted(key_event_weights.items(),
                             key=lambda x: x[1], reverse=True)

        # 应用阈值或top-n筛选
        if config.recall.final_entity_count:
            # Top-N模式
            selected_keys = sorted_keys[: config.recall.final_entity_count]
        else:
            # 阈值模式
            selected_keys = [
                (key_id, weight) for key_id, weight in sorted_keys
                if weight >= config.recall.entity_weight_threshold
            ]

        # 获取key的详细信息
        if selected_keys:
            key_ids = [key_id for key_id, _ in selected_keys]

            try:
                async with self.session_factory() as session:
                    query = select(Entity).where(Entity.id.in_(key_ids))
                    result = await session.execute(query)
                    entities = {
                        entity.id: entity for entity in result.scalars().all()}

                for key_id, weight in selected_keys:
                    entity = entities.get(key_id)
                    if entity:
                        key_final.append({
                            "key_id": key_id,
                            "name": entity.name,
                            "type": entity.type,
                            "weight": weight,
                            "steps": [1],  # 第一阶段，所有值都为1
                        })
            except Exception as e:
                self.logger.error(f"步骤8提取重要keys失败: {e}", exc_info=True)
                raise

        # 筛选出最终被使用的query召回的key
        if key_final and config.query_recalled_keys:
            # 构建key_final的key_id到key对象的映射
            key_final_map = {key["key_id"]: key for key in key_final}

            # 记录原始数量
            original_count = len(config.query_recalled_keys)

            # 筛选出在key_final中的query召回的key，并使用key_final中的key对象
            used_query_keys = []
            for query_key in config.query_recalled_keys:
                entity_id = query_key["entity_id"]
                if entity_id in key_final_map:
                    # 使用key_final中的key对象（包含weight和steps等信息）
                    used_query_keys.append(key_final_map[entity_id])

            # 更新config.query_recalled_keys，只保留最终被使用的key（来自key_final）
            config.query_recalled_keys = used_query_keys

            self.logger.info(
                f"步骤8: query召回的key中总共{original_count}个 "
                f"有{len(used_query_keys)}个被保留在key_final中（使用key_final中的key对象）"
            )

            if used_query_keys:
                # 显示被保留的query召回的key
                used_key_names = [key["name"] for key in used_query_keys[:5]]
                self.logger.debug(
                    f"被保留的query召回key（前5个）: {', '.join(used_key_names)}")

        return key_final

    async def _generate_vector_unified(
        self,
        text: str,
        context: str = "unknown",
        use_cache: bool = True
    ) -> List[float]:
        """
        统一的向量生成方法

        Args:
            text: 需要生成向量的文本
            context: 上下文描述，用于日志记录
            use_cache: 是否使用缓存（预留扩展）

        Returns:
            生成的向量数组

        Raises:
            AIError: 向量生成失败
        """
        if not text or not text.strip():
            raise AIError(f"向量生成失败：输入文本为空 (context: {context})")

        try:
            self.logger.debug(
                f"开始生成向量 - context: {context}, "
                f"文本长度: {len(text)}字符, "
                f"文本预览: {text[:50]}{'...' if len(text) > 50 else ''}"
            )

            # 使用统一的处理器生成向量
            vector = await self.processor.generate_embedding(text)

            # 验证向量有效性
            if not vector or len(vector) == 0:
                raise AIError(f"向量生成失败：返回空向量 (context: {context})")

            # 验证向量维度是否合理
            if len(vector) < 100 or len(vector) > 10000:
                self.logger.warning(
                    f"向量维度异常: {len(vector)} (context: {context}), "
                    f"通常应在100-10000范围内"
                )

            # 验证向量是否包含无效值
            if not await self._is_valid_vector(vector):
                raise AIError(f"向量生成失败：向量包含无效值 (context: {context})")

            self.logger.debug(
                f"向量生成成功 - context: {context}, "
                f"维度: {len(vector)}"
            )

            return vector

        except AIError:
            # AI错误直接重新抛出
            raise
        except Exception as e:
            error_msg = f"向量生成失败 - context: {context}, error: {e}"
            self.logger.error(error_msg, exc_info=True)
            raise AIError(error_msg) from e

    async def _is_valid_vector(self, vector: List[float]) -> bool:
        """
        验证向量是否有效

        Args:
            vector: 向量数组

        Returns:
            是否有效
        """
        if not vector:
            return False

        try:
            import math
            return all(
                not math.isnan(x) and not math.isinf(x)
                for x in vector
            )
        except (TypeError, ValueError):
            return False

    async def _build_recall_clues(
        self,
        config: SearchConfig,
        key_query_related: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        构建Recall阶段的线索（query → entity）

        使用统一的Tracker构建，确保数据结构一致性

        Args:
            config: 搜索配置
            key_query_related: query召回的实体列表

        Returns:
            Recall阶段的线索列表
        """
        from sag.modules.search.tracker import Tracker

        clues = []

        # query → entity线索
        for entity in key_query_related:
            # 统一使用similarity作为confidence
            confidence = entity.get("similarity", 0.0)

            # 获取实体权重信息（如果有）
            entity_weight = entity.get("weight")
            metadata = {
                "similarity": entity.get("similarity", 0.0),
                "method": entity.get("method", "vector_search"),
                "source_attribute": entity.get("source_attribute")  # 🆕 添加来源属性
            }
            # 只有to节点是实体时才存储weight
            if entity_weight is not None:
                metadata["weight"] = entity_weight

            # 使用统一构建器创建线索
            clue = Tracker.build_recall_clue(
                config=config,
                entity=entity,
                confidence=confidence,
                metadata=metadata
            )
            clues.append(clue)

            # 将to节点（entity节点）存入缓存，供expand阶段使用
            to_node = clue.get("to")
            if to_node and to_node.get("id"):
                config.entity_node_cache[to_node["id"]] = to_node

        return clues
