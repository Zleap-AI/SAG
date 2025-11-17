"""
搜索器 - 统一入口

只保留SAG引擎，实现三阶段搜索：recall → expand → rerank
"""

import time
from typing import Dict, List, Any, Optional

from sag.core.ai.base import BaseLLMClient
from sag.core.prompt.manager import PromptManager
from sag.db import SourceEvent
from sag.exceptions import SearchError
from sag.modules.search.config import SearchConfig, RerankStrategy, ReturnType
from sag.modules.search.recall import RecallSearcher, RecallResult
from sag.modules.search.expand import ExpandSearcher, ExpandResult
from sag.modules.search.ranking.pagerank import RerankPageRankSearcher as EventPageRankSearcher
from sag.modules.search.ranking.pagerank_section import RerankPageRankSearcher as SectionPageRankSearcher
from sag.modules.search.ranking.rrf import RerankRRFSearcher
from sag.utils import get_logger

logger = get_logger("search.searcher")


class SAGSearcher:
    """
    SAG搜索器（Structured + Attributes + Graph）

    三阶段搜索流程：
    1. Recall - 实体召回（从query召回相关实体）
    2. Expand - 实体扩展（通过多跳关系发现更多实体）
    3. Rerank - 重排序（基于实体检索和排序事项或段落）

    返回结果（根据 return_type 配置）：

    EVENT模式（返回事项，默认）:
    {
        "events": List[SourceEvent],  # 事项列表
        "clues": List[Dict],           # 线索列表（支持前端图谱）
        "stats": Dict,                 # 统计信息
        "query": Dict                  # 查询信息
    }

    PARAGRAPH模式（返回段落）:
    {
        "sections": List[Dict],        # 段落列表
        "clues": List[Dict],           # 线索列表（支持前端图谱）
        "stats": Dict,                 # 统计信息
        "query": Dict                  # 查询信息
    }
    """
    
    def __init__(
        self,
        prompt_manager: PromptManager,
        model_config: Optional[Dict] = None,
    ):
        """
        初始化搜索器
        
        Args:
            prompt_manager: 提示词管理器
            model_config: LLM配置字典（可选）
                - 如果传入：使用该配置
                - 如果不传：自动从配置管理器获取 'search' 场景配置
        """
        self.prompt_manager = prompt_manager
        self.model_config = model_config
        self._llm_client = None  # 延迟初始化
        self.logger = get_logger("search.sag")
        
        self.logger.info("SAG搜索器初始化完成")
    
    async def _get_llm_client(self) -> BaseLLMClient:
        """获取LLM客户端（懒加载）"""
        if self._llm_client is None:
            from sag.core.ai.factory import create_llm_client
            
            self._llm_client = await create_llm_client(
                scenario='search',
                model_config=self.model_config
            )
        
        # 初始化三阶段搜索器
        self.recall_searcher = RecallSearcher(llm_client=self._llm_client, prompt_manager=self.prompt_manager)
        self.expand_searcher = ExpandSearcher(
                self._llm_client,
                self.prompt_manager,
            self.recall_searcher
        )

        # 初始化重排策略 - 事项级
        self.rerank_event_pagerank = EventPageRankSearcher(self._llm_client)
        self.rerank_rrf = RerankRRFSearcher(llm_client=self._llm_client)

        # 初始化重排策略 - 段落级
        self.rerank_section_pagerank = SectionPageRankSearcher(self._llm_client)
        
        return self._llm_client
    
    async def search(self, config: SearchConfig) -> Dict[str, Any]:
        """
        执行搜索

        Args:
            config: 搜索配置

        Returns:
            根据 config.return_type 返回不同格式：

            EVENT模式（返回事项）:
            {
                "events": List[SourceEvent],  # 事项列表
                "clues": List[Dict],           # 完整线索链
                "stats": Dict,                 # 统计信息
                "query": Dict                  # 查询信息
            }

            PARAGRAPH模式（返回段落）:
            {
                "sections": List[Dict],        # 段落列表
                "clues": List[Dict],           # 完整线索链
                "stats": Dict,                 # 统计信息
                "query": Dict                  # 查询信息
            }
        """
        try:
            # 确保LLM客户端已初始化
            await self._get_llm_client()

            total_start = time.perf_counter()

            # 🆕 打印完整的配置参数，方便验证前端传参
            self.logger.info("=" * 100)
            self.logger.info("📋 SAG搜索配置参数详情:")
            self.logger.info("=" * 100)

            # 基础参数
            self.logger.info("🔹 基础参数:")
            self.logger.info(f"  query: '{config.query}'")
            self.logger.info(f"  original_query: '{config.original_query}'")
            self.logger.info(f"  source_config_id: {config.source_config_id}")
            self.logger.info(f"  source_config_ids: {config.source_config_ids}")
            self.logger.info(f"  enable_query_rewrite: {config.enable_query_rewrite}")
            self.logger.info(f"  return_type: {config.return_type}")

            # Recall 配置
            self.logger.info("")
            self.logger.info("🔹 Recall (实体召回) 配置:")
            self.logger.info(f"  use_fast_mode: {config.recall.use_fast_mode}")
            self.logger.info(f"  vector_top_k: {config.recall.vector_top_k}")
            self.logger.info(f"  vector_candidates: {config.recall.vector_candidates}")
            self.logger.info(f"  entity_similarity_threshold: {config.recall.entity_similarity_threshold}")
            self.logger.info(f"  event_similarity_threshold: {config.recall.event_similarity_threshold}")
            self.logger.info(f"  max_entities: {config.recall.max_entities}")
            self.logger.info(f"  max_events: {config.recall.max_events}")
            self.logger.info(f"  entity_weight_threshold: {config.recall.entity_weight_threshold}")
            self.logger.info(f"  final_entity_count: {config.recall.final_entity_count}")

            # Expand 配置
            self.logger.info("")
            self.logger.info("🔹 Expand (实体扩展) 配置:")
            self.logger.info(f"  enabled: {config.expand.enabled}")
            self.logger.info(f"  max_hops: {config.expand.max_hops}")
            self.logger.info(f"  entities_per_hop: {config.expand.entities_per_hop}")
            self.logger.info(f"  weight_change_threshold: {config.expand.weight_change_threshold}")
            self.logger.info(f"  event_similarity_threshold: {config.expand.event_similarity_threshold}")
            self.logger.info(f"  min_events_per_hop: {config.expand.min_events_per_hop}")
            self.logger.info(f"  max_events_per_hop: {config.expand.max_events_per_hop}")

            # Rerank 配置
            self.logger.info("")
            self.logger.info("🔹 Rerank (事项重排) 配置:")
            self.logger.info(f"  strategy: {config.rerank.strategy}")
            self.logger.info(f"  score_threshold: {config.rerank.score_threshold}")
            self.logger.info(f"  max_results: {config.rerank.max_results}")
            self.logger.info(f"  max_key_recall_results: {config.rerank.max_key_recall_results}")
            self.logger.info(f"  max_query_recall_results: {config.rerank.max_query_recall_results}")
            self.logger.info(f"  pagerank_damping_factor: {config.rerank.pagerank_damping_factor}")
            self.logger.info(f"  pagerank_max_iterations: {config.rerank.pagerank_max_iterations}")
            self.logger.info(f"  rrf_k: {config.rerank.rrf_k}")
            self.logger.info("=" * 100)

            self.logger.info(
                f"🔍 开始搜索：query='{config.query}', source_config_id={config.source_config_id}"
            )

            # Recall: 实体召回
            recall_start = time.perf_counter()
            recall_result = await self._recall(config)
            recall_time = time.perf_counter() - recall_start
            
            # Expand: 实体扩展（可选）
            expand_start = time.perf_counter()
            if config.expand.enabled:
                expand_result = await self._expand(config, recall_result)
            else:
                expand_result = recall_result  # 跳过扩展
            expand_time = time.perf_counter() - expand_start
            
            # Rerank: 事项重排
            rerank_start = time.perf_counter()
            rerank_result = await self._rerank(config, expand_result)
            rerank_time = time.perf_counter() - rerank_start
            
            total_time = time.perf_counter() - total_start
            
            # 输出耗时统计
            self._log_timing(recall_time, expand_time, rerank_time, total_time)
            
            # 构建最终响应
            response = self._build_response(
                config=config,
                recall_result=recall_result,
                expand_result=expand_result,
                rerank_result=rerank_result,
            )

            # 根据 return_type 输出不同的日志
            if config.return_type == ReturnType.PARAGRAPH:
                self.logger.info(
                    f"✅ 搜索完成：返回 {len(response.get('sections', []))} 个段落，"
                    f"{len(response['clues'])} 条线索"
                )
            else:
                self.logger.info(
                    f"✅ 搜索完成：返回 {len(response.get('events', []))} 个事项，"
                    f"{len(response['clues'])} 条线索"
                )

            return response

        except Exception as e:
            self.logger.error(f"❌ 搜索失败: {e}", exc_info=True)
            raise SearchError(f"搜索失败: {e}") from e
    
    async def _recall(self, config: SearchConfig) -> RecallResult:
        """
        Recall: 实体召回
        
        从query召回相关实体
        """
        self.logger.info("📍 Recall: 实体召回")
        result = await self.recall_searcher.search(config)
        self.logger.info(f"✓ Recall完成：召回 {len(result.key_final)} 个实体")
        return result
    
    async def _expand(
        self, 
        config: SearchConfig, 
        recall_result: RecallResult
    ) -> ExpandResult:
        """
        Expand: 实体扩展
        
        基于召回的实体，通过多跳关系发现更多相关实体
        """
        self.logger.info("📍 Expand: 实体扩展")
        result = await self.expand_searcher.search(config, recall_result)
        self.logger.info(
            f"✓ Expand完成：扩展到 {len(result.key_final)} 个实体，"
            f"跳跃 {result.total_jumps} 次"
        )
        return result
    
    async def _rerank(
        self,
        config: SearchConfig,
        expand_result: ExpandResult
    ) -> Dict[str, Any]:
        """
        Rerank: 事项/段落重排

        根据 return_type 选择返回事项或段落：
        - EVENT: 返回事项列表（使用 pagerank.py）
        - PARAGRAPH: 返回段落列表（使用 pagerank_section.py）
        """
        strategy = config.rerank.strategy
        return_type = config.return_type

        self.logger.info(
            f"📍 Rerank: {'事项' if return_type == ReturnType.EVENT else '段落'}重排"
            f"（策略={strategy}, 返回类型={return_type}）"
        )

        # 根据 return_type 选择不同的 Rerank 实现
        if return_type == ReturnType.PARAGRAPH:
            # 段落级 PageRank（只支持 PAGERANK 策略）
            if strategy != RerankStrategy.PAGERANK:
                self.logger.warning(
                    f"段落返回模式仅支持 PAGERANK 策略，当前策略 {strategy} 将被忽略"
                )
            reranker = self.rerank_section_pagerank
        else:
            # 事项级重排（支持 PAGERANK 和 RRF）
            if strategy == RerankStrategy.PAGERANK:
                reranker = self.rerank_event_pagerank
            elif strategy == RerankStrategy.RRF:
                reranker = self.rerank_rrf
            else:
                raise SearchError(f"不支持的重排策略: {strategy}")

        # 执行重排
        result = await reranker.search(
            key_final=expand_result.key_final,
            config=config
        )

        # 日志输出
        if return_type == ReturnType.PARAGRAPH:
            self.logger.info(
                f"✓ Rerank完成：返回 {len(result.get('sections', []))} 个段落"
            )
        else:
            self.logger.info(
                f"✓ Rerank完成：返回 {len(result.get('events', []))} 个事项"
            )

        return result
    
    def _log_timing(
        self, 
        recall_time: float, 
        expand_time: float, 
        rerank_time: float, 
        total_time: float
    ):
        """输出耗时统计"""
        self.logger.info("=" * 80)
        self.logger.info("⏱️  搜索耗时统计：")
        self.logger.info("-" * 80)
        self.logger.info(
            f"  Recall (实体召回): {recall_time:.3f}秒 "
            f"({recall_time/total_time*100:.1f}%)"
        )
        self.logger.info(
            f"  Expand (实体扩展): {expand_time:.3f}秒 "
            f"({expand_time/total_time*100:.1f}%)"
        )
        self.logger.info(
            f"  Rerank (事项重排): {rerank_time:.3f}秒 "
            f"({rerank_time/total_time*100:.1f}%)"
        )
        self.logger.info("-" * 80)
        self.logger.info(f"  总耗时: {total_time:.3f}秒")
        self.logger.info("=" * 80)
    
    def _build_response(
        self,
        config: SearchConfig,
        recall_result: RecallResult,
        expand_result,  # Union[RecallResult, ExpandResult]
        rerank_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        构建最终响应

        整合三阶段的线索和结果，支持事项和段落两种返回格式
        """
        from collections import Counter

        # 🆕 使用统一的all_clues字段（新版追踪器）
        all_clues = getattr(config, 'all_clues', [])

        # Fallback: 兼容旧版（如果all_clues为空，尝试从旧字段收集）
        if not all_clues:
            all_clues.extend(getattr(config, 'recall_clues', []))
            all_clues.extend(getattr(config, 'expansion_clues', []))
            all_clues.extend(getattr(config, 'rerank_clues', []))

        # 构建统计信息
        recall_entities = recall_result.key_final
        expand_entities = [
            k for k in expand_result.key_final
            if k.get("steps", [0])[0] >= 2
        ]

        recall_by_type = Counter(
            e.get("type") for e in recall_entities if e.get("type")
        )

        # 判断 expand 是否被执行
        from .expand import ExpandResult
        expand_was_executed = isinstance(expand_result, ExpandResult)

        # 根据 return_type 提取结果
        return_type = config.return_type

        if return_type == ReturnType.PARAGRAPH:
            # 段落模式
            sections = rerank_result.get("sections", [])
            result_key = "sections"
            result_count = len(sections)
            self.logger.info(
                f"✨ 响应构建完成（段落模式）: "
                f"sections={result_count}, "
                f"clues={len(all_clues)}"
            )
        else:
            # 事项模式（默认）
            events = rerank_result.get("events", [])
            result_key = "events"
            result_count = len(events)
            self.logger.info(
                f"✨ 响应构建完成（事项模式）: "
                f"events={result_count}, "
                f"clues={len(all_clues)}"
            )

        stats = {
            "recall": {
                "entities_count": len(recall_entities),
                "by_type": dict(recall_by_type),
            },
            "expand": {
                "entities_count": len(expand_entities) if expand_was_executed else 0,
                "total_entities": len(expand_result.key_final),
                "hops": expand_result.total_jumps if expand_was_executed else 0,
                "converged": expand_result.convergence_reached if expand_was_executed else False,
            },
            "rerank": {
                f"{result_key}_count": result_count,  # events_count 或 sections_count
                "strategy": str(config.rerank.strategy),
                "return_type": str(return_type),  # 新增：记录返回类型
            }
        }

        # 构建查询信息
        query_info = {
            "original": config.original_query or config.query,
            "current": config.query,
            "rewritten": (
                config.original_query != config.query
                if config.original_query else False
            )
        }

        # 构建响应（根据 return_type 使用不同的键）
        response = {
            result_key: rerank_result.get(result_key, []),  # "events" 或 "sections"
            "clues": all_clues,
            "stats": stats,
            "query": query_info,
        }

        return response


# 向后兼容别名
EventSearcher = SAGSearcher

__all__ = [
    "SAGSearcher",
    "EventSearcher",
]
