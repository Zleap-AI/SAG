"""
ResearcherAgent - 智能对话研究员

核心能力：
- 🧠 认知理解：深度理解问题意图和上下文
- 🔍 主动搜索：基于知识缺口主动搜索
- 💭 推理链路：完整的思考链（CoT）
- 📊 自我评估：每个阶段自我评估质量
- 🔄 迭代优化：不断改进直到满意
- 📝 记忆管理：结构化对话和知识记忆
"""

from typing import Any, AsyncIterator, Dict, List, Optional
from sag.core.agent.base import BaseAgent
from sag.modules.search import SAGSearcher, SearchConfig
from sag.core.ai.models import LLMMessage, LLMRole
from sag.core.prompt import get_prompt_manager
from sag.utils import get_logger

logger = get_logger("agent.researcher")


class ResearcherAgent(BaseAgent):
    """
    研究员 Agent - 具有完整认知能力的对话 Agent

    认知流程：
    1. Understanding  - 理解阶段（问题分析）
    2. Planning      - 规划阶段（制定策略）
    3. Researching   - 研究阶段（主动搜索）
    4. Evaluating    - 评估阶段（知识检查）
    5. Synthesizing  - 综合阶段（整合答案）
    6. Verifying     - 验证阶段（质量把关）
    """

    # 模式定义
    MODE_QUICK = "quick"  # 快速：单轮搜索
    MODE_DEEP = "deep"    # 深度：多轮迭代

    # 认知阶段
    STAGE_UNDERSTANDING = "understanding"
    STAGE_PLANNING = "planning"
    STAGE_RESEARCHING = "researching"
    STAGE_EVALUATING = "evaluating"
    STAGE_SYNTHESIZING = "synthesizing"
    STAGE_VERIFYING = "verifying"

    def __init__(
        self,
        source_config_ids: Optional[List[str]] = None,
        mode: str = MODE_QUICK,
        conversation_history: Optional[List[Dict]] = None,
        **kwargs
    ):
        """
        初始化 ResearcherAgent

        Args:
            source_config_ids: 信息源ID列表
            mode: 对话模式（quick/deep）
            conversation_history: 对话历史（最近10轮）
        """
        # 设置输出配置
        if "output" not in kwargs:
            kwargs["output"] = {
                "stream": True,   # 流式输出
                "think": True,    # 显示思考
                "format": "text",
                "style": "友好、专业、准确。逻辑清晰，重点突出。善用结构化表达。"
            }

        super().__init__(**kwargs)

        # 🔧 重载 agent_config 使用 researcher.json
        try:
            prompt_manager = get_prompt_manager()
            self.agent_config = prompt_manager.load_json_config("researcher")
            # 更新 builder
            from sag.core.agent.builder import Builder
            self.builder = Builder(self.agent_config)
            logger.info("成功加载 researcher.json 配置")
        except Exception as e:
            logger.warning(f"加载 researcher.json 失败，使用默认 agent.json: {e}")

        self.source_config_ids = source_config_ids or []
        self.mode = mode

        # 初始化搜索器（传递 model_config，继承自 BaseAgent）
        self.searcher = SAGSearcher(
            prompt_manager=get_prompt_manager(),
            model_config=self.model_config  # 使用 BaseAgent 的 model_config
        )

        # 认知状态
        self.current_stage = None
        self.understanding: Optional[Dict] = None  # 问题理解结果
        self.search_plan: Optional[Dict] = None    # 搜索计划
        self.knowledge_graph: List[Dict] = []       # 知识图谱
        self.confidence_score: float = 0.0          # 回答置信度

        # 搜索参数（默认值）
        self.search_params = {
            "top_k": 10,
            "threshold": 0.5,  # 相似度阈值
            "result_style": "concise"
        }

        # 加载对话历史到记忆
        if conversation_history:
            self._load_conversation_memory(conversation_history)

        logger.info(
            f"初始化 ResearcherAgent",
            extra={
                "mode": mode,
                "sources": len(self.source_config_ids),
                "has_history": bool(conversation_history)
            }
        )

    # ============ 主入口 ============

    async def chat(
        self,
        query: str,
        source_config_ids: Optional[List[str]] = None,
        **kwargs
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        对话入口（流式输出）

        Args:
            query: 用户问题
            source_config_ids: 信息源列表（可覆盖）
            **kwargs: 参数（会自动分离搜索参数和LLM参数）

        Yields:
            {
                "type": "stage|thinking|content|done|error",
                "stage": "understanding|planning|...",  # 当前阶段
                "content": "...",                       # 内容
                "data": {...}                           # 阶段数据
            }
        """
        # 更新信息源
        if source_config_ids:
            self.source_config_ids = source_config_ids

        # 验证
        if not self.source_config_ids:
            yield {
                "type": "error",
                "content": "请先选择信息源"
            }
            return

        # 🔧 分离搜索参数和 LLM 参数
        # 搜索参数：top_k, threshold, result_style 等
        if "top_k" in kwargs:
            self.search_params["top_k"] = kwargs.pop("top_k")
            logger.info(f"✅ 接收到前端 top_k 参数: {self.search_params['top_k']}")
        if "threshold" in kwargs:
            self.search_params["threshold"] = kwargs.pop("threshold")
            logger.info(
                f"✅ 接收到前端 threshold 参数: {self.search_params['threshold']}")
        if "result_style" in kwargs:
            self.search_params["result_style"] = kwargs.pop("result_style")

        logger.info(f"🔍 当前搜索参数: {self.search_params}")

        # kwargs 剩余的才是 LLM 参数（如 temperature, max_tokens 等）

        # 记录用户问题到记忆
        self._record_user_query(query)

        # 根据模式执行（传递过滤后的 kwargs）
        try:
            if self.mode == self.MODE_QUICK:
                async for chunk in self._execute_quick_mode(query, **kwargs):
                    yield chunk
            else:
                async for chunk in self._execute_deep_mode(query, **kwargs):
                    yield chunk
        except Exception as e:
            logger.error(f"对话执行失败: {e}", exc_info=True)
            yield {
                "type": "error",
                "content": f"执行失败：{str(e)}"
            }

    # ============ 快速模式 ============

    async def _execute_quick_mode(
        self, query: str, **kwargs
    ) -> AsyncIterator[Dict]:
        """
        快速模式 - 简洁高效的搜索问答

        流程：
        1. 搜索阶段 - 搜索相关事项
        2. 分析阶段 - 可选展示推理过程
        3. 回答阶段 - 生成答案
        """
        import time
        start_time = time.time()

        logger.info(f"🚀 快速模式启动: query='{query}'")

        # === 步骤1：快速分析 ===
        # 🟢 执行前立即输出
        yield {
            "type": "thinking_step",
            "stage": "understanding",
            "label": "快速分析",
            "content": "分析问题类型和核心概念",
            "status": "processing"
        }

        understanding = await self._understand_question_quick(query)
        self.understanding = understanding
        self._record_understanding(understanding)
        keywords = understanding.get("keywords", [query])

        # 🟢 执行后立即更新
        yield {
            "type": "update_step",
            "stage": "understanding",
            "content": f"提取关键词：{', '.join(keywords[:5])}",
            "status": "done"
        }

        # === 步骤2：搜索研究 ===
        yield {
            "type": "thinking_step",
            "stage": "researching",
            "label": "搜索研究",
            "content": f"搜索「{keywords[0] if keywords else query}」相关事项",
            "status": "processing"
        }

        search_result = await self._execute_search(query=query, keywords=keywords)
        events = search_result.get("events", [])

        yield {
            "type": "update_step",
            "stage": "researching",
            "content": f"找到 {len(events)} 个相关事项",
            "status": "done"
        }

        # === 步骤3：评估知识 ===
        yield {
            "type": "thinking_step",
            "stage": "evaluating",
            "label": "评估知识",
            "content": "评估搜索结果充分性",
            "status": "processing"
        }

        evaluation = self._evaluate_knowledge(query, events)
        self.confidence_score = evaluation.get("confidence", 0.0)
        self._record_evaluation(evaluation)

        yield {
            "type": "update_step",
            "stage": "evaluating",
            "content": f"{evaluation['assessment']}（置信度 {evaluation['confidence']:.0%}）",
            "status": "done"
        }

        # === 2. 准备数据 ===
        if events:
            self._load_events_to_database(events, "搜索结果")
            
            # 发送引用事项（前20个，与数据库一致）
            references = []
            for idx, event in enumerate(events[:20], 1):
                references.append({
                    "order": idx,
                    "id": event.id if hasattr(event, 'id') else str(idx),
                    "title": event.title if hasattr(event, 'title') else '',
                    "summary": (event.summary if hasattr(event, 'summary') else '')[:150],
                    "article_id": event.article_id if hasattr(event, 'article_id') else None
                })

            yield {
                "type": "references",
                "data": references
            }

        self._setup_quick_todo(len(events), evaluation)

        # === 步骤4：生成答案 ===
        yield {
            "type": "thinking_step",
            "stage": "synthesizing",
            "label": "生成答案",
            "content": "整合所有信息并深度分析",
            "status": "processing"
        }

        show_reasoning = kwargs.get("show_reasoning", True)

        async for chunk in super().run_stream(query=query, **kwargs):
            if chunk.get("reasoning") and show_reasoning:
                yield {
                    "type": "reasoning",
                    "content": chunk["reasoning"]
                }
            if chunk.get("content"):
                yield {
                    "type": "content",
                    "content": chunk["content"]
                }

        # 生成完毕
        yield {
            "type": "update_step",
            "stage": "synthesizing",
            "content": "完成",
            "status": "done"
        }

        # === 完成 ===
        thinking_time = time.time() - start_time

        yield {
            "type": "done",
            "stats": {
                "mode": "quick",
                "events_found": len(events),
                "confidence": evaluation.get("confidence", 0.0),
                "sources": len(self.source_config_ids),
                "thinking_time": round(thinking_time, 2)  # 思考耗时（秒）
            }
        }

    # ============ 深度模式 ============

    async def _execute_deep_mode(
        self, query: str, **kwargs
    ) -> AsyncIterator[Dict]:
        """
        深度模式 - 多轮迭代深入研究

        流程：
        1. 深度理解和规划
        2. 多轮搜索迭代
        3. 深度分析和综合
        """
        import time
        start_time = time.time()

        logger.info(f"🧠 深度模式启动: query='{query}'")

        # === 步骤1：深度理解 ===
        yield {
            "type": "thinking_step",
            "stage": "understanding",
            "label": "深度理解",
            "content": "分析问题类型和核心概念",
            "status": "processing"
        }

        understanding = await self._understand_question_deep(query)
        self.understanding = understanding
        self._record_understanding(understanding)

        yield {
            "type": "update_step",
            "stage": "understanding",
            "content": f"问题类型：{understanding.get('question_type')}，核心概念：{', '.join(understanding.get('concepts', [])[:3])}",
            "status": "done"
        }

        # === 步骤2：制定计划 ===
        yield {
            "type": "thinking_step",
            "stage": "planning",
            "label": "制定计划",
            "content": "规划多轮搜索策略",
            "status": "processing"
        }

        search_plan = await self._create_search_plan(query, understanding)
        self.search_plan = search_plan
        self._record_search_plan(search_plan)

        yield {
            "type": "update_step",
            "stage": "planning",
            "content": f"计划 {search_plan.get('rounds', 1)} 轮搜索",
            "status": "done"
        }

        # === 步骤3：多轮搜索（动态） ===
        all_events = []
        max_rounds = min(search_plan.get("rounds", 3), 5)

        for round_idx in range(max_rounds):
            queries = search_plan.get("queries", [[query]])[round_idx] if round_idx < len(
                search_plan.get("queries", [])) else [query]

            # 🟢 本轮搜索开始
            yield {
                "type": "thinking_step",
                "stage": f"round_{round_idx + 1}",
                "label": f"第 {round_idx + 1} 轮搜索",
                "content": f"查询：{queries if isinstance(queries, list) else [queries]}",
                "status": "processing"
            }

            round_events = await self._execute_multi_query_search(
                queries if isinstance(queries, list) else [queries]
            )

            all_events.extend(round_events)
            all_events = self._deduplicate_events(all_events)

            # 🟢 本轮结果
            yield {
                "type": "update_step",
                "stage": f"round_{round_idx + 1}",
                "content": f"找到 {len(round_events)} 个事项，累计 {len(all_events)} 个",
                "status": "done"
            }

            # 评估
            evaluation = await self._evaluate_knowledge_deep(query, all_events, round_idx + 1)
            self._record_evaluation(evaluation)

            if evaluation["is_sufficient"]:
                # 🟢 知识充分
                yield {
                    "type": "thinking_step",
                    "stage": "decision",
                    "label": "评估决策",
                    "content": f"{evaluation['assessment']}，结束搜索",
                    "status": "done"
                }
                logger.info(f"✅ 知识充分，结束搜索（{round_idx + 1} 轮）")
                break
            else:
                # 🟢 继续搜索
                yield {
                    "type": "thinking_step",
                    "stage": f"decision_{round_idx}",
                    "label": "评估决策",
                    "content": f"知识不足，继续第 {round_idx + 2} 轮搜索",
                    "status": "done"
                }

        # 搜索完成通知
        yield {
            "type": "search_status",
            "status": "done",
            "sources_count": len(self.source_config_ids),
            "events_count": len(all_events),
            "confidence": evaluation.get("confidence", 0.0)
        }

        # === 2. 准备数据 ===
        if all_events:
            self._load_events_to_database(all_events, f"深度研究结果（{round_idx + 1} 轮）")
            
            # 发送引用事项（前20个，与数据库一致）
            references = []
            for idx, event in enumerate(all_events[:20], 1):
                references.append({
                    "order": idx,
                    "id": event.id if hasattr(event, 'id') else str(idx),
                    "title": event.title if hasattr(event, 'title') else '',
                    "summary": (event.summary if hasattr(event, 'summary') else '')[:150],
                    "article_id": event.article_id if hasattr(event, 'article_id') else None
                })

            yield {
                "type": "references",
                "data": references
            }

        self._setup_deep_todo(len(all_events), evaluation)

        # === 步骤N：生成深度答案 ===
        yield {
            "type": "thinking_step",
            "stage": "synthesizing",
            "label": "深度综合",
            "content": "整合所有信息并深度分析",
            "status": "processing"
        }

        show_reasoning = kwargs.get("show_reasoning", True)

        async for chunk in super().run_stream(query=query, **kwargs):
            if chunk.get("reasoning") and show_reasoning:
                yield {
                    "type": "reasoning",
                    "content": chunk["reasoning"]
                }
            if chunk.get("content"):
                yield {
                    "type": "content",
                    "content": chunk["content"]
                }

        # 🟢 深度分析完成
        yield {
            "type": "update_step",
            "stage": "synthesizing",
            "content": "完成",
            "status": "done"
        }

        # === 完成 ===
        thinking_time = time.time() - start_time

        yield {
            "type": "done",
            "stats": {
                "mode": "deep",
                "search_rounds": round_idx + 1,
                "events_found": len(all_events),
                "confidence": evaluation.get("confidence", 0.0),
                "sources": len(self.source_config_ids),
                "thinking_time": round(thinking_time, 2)
            }
        }

    # ============ 认知能力方法 ============

    async def _understand_question_quick(self, query: str) -> Dict:
        """
        快速理解问题

        提取：
        - intent: 用户意图
        - keywords: 关键词列表
        - entity_types: 关键实体类型
        """
        schema = {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "description": "用户意图，一句话概括"},
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "关键词列表（2-5个）"
                },
                "entity_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "关键实体类型"
                },
            },
            "required": ["intent", "keywords"]
        }

        messages = [
            LLMMessage(
                role=LLMRole.SYSTEM,
                content="你是问题分析专家，快速提取问题中的关键信息。"
            ),
            LLMMessage(
                role=LLMRole.USER,
                content=f"分析问题，提取关键词：{query}"
            )
        ]

        llm_client = await self._get_llm_client()
        result = await llm_client.chat_with_schema(
            messages=messages,
            response_schema=schema
        )

        logger.info(
            f"快速理解: intent={result.get('intent')}, keywords={result.get('keywords')}")
        return result

    async def _understand_question_deep(self, query: str) -> Dict:
        """
        深度理解问题

        分析：
        - question_type: 问题类型
        - concepts: 核心概念列表
        - sub_questions: 子问题分解
        - time_range: 时间范围
        - focus_entities: 关注的实体类型
        """
        schema = {
            "type": "object",
            "properties": {
                "question_type": {
                    "type": "string",
                    "enum": ["事实查询", "对比分析", "趋势分析", "原因探究", "方案建议"],
                    "description": "问题类型"
                },
                "concepts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "核心概念（2-5个）"
                },
                "sub_questions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "分解的子问题（2-4个）"
                },
                "time_range": {
                    "type": "string",
                    "description": "时间范围（如：2024年、近期、历史）"
                },
                "focus_entities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "关注的实体类型"
                }
            },
            "required": ["question_type", "concepts"]
        }

        messages = [
            LLMMessage(
                role=LLMRole.SYSTEM,
                content="""你是问题分析专家，进行深度问题理解。

分析维度：
1. 问题类型：判断问题属于哪种类型
2. 核心概念：提取2-5个最核心的概念
3. 子问题：将复杂问题分解为2-4个具体的子问题
4. 时间范围：识别时间限定（如果有）
5. 关注实体：需要重点关注哪些实体类型（人物、地点、时间等）"""
            ),
            LLMMessage(
                role=LLMRole.USER,
                content=f"深度分析以下问题：\n\n{query}"
            )
        ]

        llm_client = await self._get_llm_client()
        result = await llm_client.chat_with_schema(
            messages=messages,
            response_schema=schema
        )

        logger.info(
            f"深度理解: type={result.get('question_type')}, concepts={result.get('concepts')}")
        return result

    async def _create_search_plan(
        self, query: str, understanding: Dict
    ) -> Dict:
        """
        制定搜索计划

        返回：
        {
            "rounds": 3,
            "queries": [
                ["主关键词1", "主关键词2"],  # 第1轮
                ["补充概念1"],               # 第2轮  
                ["关联信息1"]                # 第3轮
            ],
            "strategy": "搜索策略说明"
        }
        """
        schema = {
            "type": "object",
            "properties": {
                "rounds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "计划的搜索轮数"
                },
                "queries": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "description": "每轮的查询列表"
                },
                "strategy": {
                    "type": "string",
                    "description": "搜索策略说明"
                }
            },
            "required": ["rounds", "queries"]
        }

        messages = [
            LLMMessage(
                role=LLMRole.SYSTEM,
                content="""你是搜索策略专家，制定多轮搜索计划。

原则：
1. 第1轮：最核心、最直接的关键词
2. 第2轮：补充概念、同义词、相关术语
3. 第3轮：关联信息、背景知识
4. 每轮1-2个查询，避免过多
5. 从主到次，从直接到关联"""
            ),
            LLMMessage(
                role=LLMRole.USER,
                content=f"""问题：{query}

理解结果：
- 问题类型：{understanding.get('question_type')}
- 核心概念：{understanding.get('concepts')}
- 子问题：{understanding.get('sub_questions')}

请制定详细的搜索计划。"""
            )
        ]

        llm_client = await self._get_llm_client()
        plan = await llm_client.chat_with_schema(
            messages=messages,
            response_schema=schema
        )

        logger.info(
            f"搜索计划: rounds={plan.get('rounds')}, queries={plan.get('queries')}")
        return plan

    async def _execute_search(
        self, query: str, keywords: List[str]
    ) -> Dict:
        """
        执行单次搜索（支持多源）

        Args:
            query: 原始查询
            keywords: 关键词列表
        """
        from sag.modules.search.config import RerankConfig

        search_query = keywords[0] if keywords else query

        logger.info(
            f"🔍 执行搜索: query='{search_query}', sources={self.source_config_ids}")

        # 🔍 调试日志：确认参数传递
        top_k_value = self.search_params.get("top_k", 10)
        threshold_value = self.search_params.get("threshold", 0.5)
        logger.info(
            f"📊 搜索参数: top_k={top_k_value}, threshold={threshold_value}")

        # ✅ 正确构建 SearchConfig，传入 rerank 配置
        result = await self.searcher.search(
            SearchConfig(
                query=search_query,
                source_config_ids=self.source_config_ids,  # ✅ 多源一次调用
                rerank=RerankConfig(
                    max_results=top_k_value,      # 结果数量
                    score_threshold=threshold_value  # 相似度阈值
                )
            )
        )

        # 记录搜索到记忆
        self._record_search(search_query, result)

        return result

    async def _execute_multi_query_search(
        self, queries: List[str]
    ) -> List:
        """
        执行多查询搜索并合并结果

        Args:
            queries: 查询列表

        Returns:
            合并后的事项列表
        """
        all_events = []

        for q in queries:
            result = await self._execute_search(q, [q])
            events = result.get("events", [])
            all_events.extend(events)

            logger.info(f"查询 '{q}' 找到 {len(events)} 个事项")

        return all_events

    def _evaluate_knowledge(
        self, query: str, events: List
    ) -> Dict:
        """
        评估知识充分性（基础评估）

        返回：
        {
            "is_sufficient": bool,
            "confidence": float,
            "assessment": str,
            "gaps": List[str]
        }
        """
        event_count = len(events)

        if event_count == 0:
            return {
                "is_sufficient": False,
                "confidence": 0.0,
                "assessment": "未找到相关信息",
                "gaps": ["完全缺少相关数据"]
            }

        if event_count >= 5:
            return {
                "is_sufficient": True,
                "confidence": 0.85,
                "assessment": "信息充分，可以给出完整回答",
                "gaps": []
            }

        if event_count >= 2:
            return {
                "is_sufficient": True,
                "confidence": 0.65,
                "assessment": "信息基本充分，可以给出初步回答",
                "gaps": ["部分细节可能不完整"]
            }

        return {
            "is_sufficient": True,
            "confidence": 0.4,
            "assessment": "信息有限，仅能给出部分回答",
            "gaps": ["相关信息较少，答案可能不全面"]
        }

    async def _evaluate_knowledge_deep(
        self, query: str, events: List, round_idx: int
    ) -> Dict:
        """
        深度评估知识（考虑迭代轮次）

        策略：
        - 早期轮次：要求较高（至少5个事项）
        - 后期轮次：降低标准（至少3个事项）
        """
        basic_eval = self._evaluate_knowledge(query, events)

        # 深度模式：根据轮次调整判断
        if round_idx >= 2:
            # 已经搜索2轮以上，降低标准
            if len(events) >= 3:
                basic_eval["is_sufficient"] = True
                basic_eval["assessment"] = f"经过 {round_idx} 轮搜索，已收集足够信息"
                basic_eval["confidence"] = max(basic_eval["confidence"], 0.7)

        return basic_eval

    # ============ 记忆管理 ============

    def _load_conversation_memory(self, history: List[Dict]):
        """加载对话历史到记忆（安全序列化）"""
        # 只保留最近10条
        recent_history = history[-10:]

        # 过滤和转换所有字段
        safe_history = []
        for msg in recent_history:
            safe_msg = {
                "role": msg.get("role", ""),
                "content": msg.get("content", ""),
            }

            # 可选字段（只保留基本类型）
            if "sources" in msg and isinstance(msg["sources"], list):
                safe_msg["sources"] = msg["sources"]

            # ✅ timestamp 转换为字符串
            if "timestamp" in msg:
                ts = msg["timestamp"]
                if hasattr(ts, 'isoformat'):
                    safe_msg["timestamp"] = ts.isoformat()
                elif isinstance(ts, str):
                    safe_msg["timestamp"] = ts
                else:
                    safe_msg["timestamp"] = str(ts)

            safe_history.append(safe_msg)

        self.add_memory(
            data_type="对话历史",
            items=safe_history,
            description=f"最近 {len(safe_history)} 条对话记录"
        )

    def _record_user_query(self, query: str):
        """记录用户问题"""
        self.add_memory(
            data_type="当前问题",
            items=[{
                "query": query,
                "timestamp": self._get_current_time(),  # 返回 ISO 字符串，可序列化
                "mode": self.mode,
                "sources": self.source_config_ids
            }],
            description="本次对话的用户问题"
        )

    def _record_understanding(self, understanding: Dict):
        """记录问题理解（只保留基本信息）"""
        # 只保留可序列化的基本信息
        safe_understanding = {
            "intent": understanding.get("intent", ""),
            "keywords": understanding.get("keywords", [])[:5],
            "question_type": understanding.get("question_type", ""),
            "concepts": understanding.get("concepts", [])[:5],
        }

        self.add_memory(
            data_type="问题理解",
            items=[safe_understanding],
            description="对用户问题的认知分析"
        )

    def _record_search_plan(self, plan: Dict):
        """记录搜索计划（只保留基本信息）"""
        # 只保留可序列化的基本信息
        safe_plan = {
            "rounds": plan.get("rounds", 1),
            "queries": plan.get("queries", [])[:5],  # 最多保留5轮
            "strategy": plan.get("strategy", "")
        }

        self.add_memory(
            data_type="搜索计划",
            items=[safe_plan],
            description="制定的多轮搜索策略"
        )

    def _record_search(self, query: str, result: Dict):
        """记录搜索结果（只保留基本信息，避免序列化问题）"""
        self.add_memory(
            data_type="搜索历史",
            items=[{
                "query": query,
                "event_count": len(result.get("events", [])),
                # 不保存复杂的 stats 对象，避免序列化问题
                "timestamp": self._get_current_time()  # ISO 字符串
            }],
            description="搜索执行记录"
        )

    def _record_evaluation(self, evaluation: Dict):
        """记录知识评估"""
        self.add_memory(
            data_type="知识评估",
            items=[evaluation],
            description="对当前知识库的评估结果"
        )

    def _load_events_to_database(self, events: List, description: str):
        """加载事项到数据库（带序号，完全序列化所有字段）"""
        self.clear_database(data_type="搜索事项")

        items = []
        for idx, event in enumerate(events[:20], 1):
            # 基本字段
            item = {
                "order": idx,
                "id": str(event.id) if hasattr(event, 'id') else str(idx),
                "title": str(event.title) if hasattr(event, 'title') else '',
                "summary": str(event.summary) if hasattr(event, 'summary') else '',
                "content": str(event.content) if hasattr(event, 'content') else '',
                "source_id": str(event.source_id) if hasattr(event, 'source_id') else 'unknown',
            }

            # 可选字段
            if hasattr(event, 'category') and event.category:
                item["category"] = str(event.category)

            if hasattr(event, 'rank'):
                item["rank"] = int(event.rank) if isinstance(
                    event.rank, int) else 0

            # ✅ datetime 字段：转换为 ISO 字符串（保留时间信息）
            if hasattr(event, 'start_time') and event.start_time:
                try:
                    item["start_time"] = event.start_time.isoformat()
                except:
                    item["start_time"] = str(event.start_time)

            if hasattr(event, 'end_time') and event.end_time:
                try:
                    item["end_time"] = event.end_time.isoformat()
                except:
                    item["end_time"] = str(event.end_time)

            # created_time 和 updated_time 也转换
            if hasattr(event, 'created_time') and event.created_time:
                try:
                    item["created_time"] = event.created_time.isoformat()
                except:
                    item["created_time"] = str(event.created_time)

            if hasattr(event, 'updated_time') and event.updated_time:
                try:
                    item["updated_time"] = event.updated_time.isoformat()
                except:
                    item["updated_time"] = str(event.updated_time)

            items.append(item)

        self.add_database(
            data_type="搜索事项",
            items=items,
            description=description
        )

    # ============ TODO 任务设置 ============

    def _setup_quick_todo(self, event_count: int, evaluation: Dict):
        """快速模式 TODO"""
        self.clear_todo()

        if event_count == 0:
            self.add_todo(
                task_id="no-data-response",
                description="未找到相关信息。礼貌告知用户，建议：1) 换个表述方式 2) 选择其他信息源 3) 提供更多上下文",
                status="pending",
                priority=10
            )
        else:
            self.add_todo(
                task_id="analyze-relevance",
                description=f"分析 {event_count} 个事项与问题的相关性，筛选最相关的内容",
                status="pending",
                priority=10
            )

            self.add_todo(
                task_id="extract-key-info",
                description="从相关事项中提取关键信息：核心观点、数据、时间、人物等",
                status="pending",
                priority=9
            )

            self.add_todo(
                task_id="synthesize-answer",
                description="基于提取的信息生成简洁回答，逻辑清晰、重点突出。引用事项序号 [#1][#2]",
                status="pending",
                priority=8
            )

            # 如果置信度不高，添加说明任务
            if evaluation.get("confidence", 0) < 0.7:
                self.add_todo(
                    task_id="add-disclaimer",
                    description=f"在回答末尾说明：{evaluation.get('assessment')}，答案可能不完整",
                    status="pending",
                    priority=7
                )

    def _setup_deep_todo(self, event_count: int, evaluation: Dict):
        """深度模式 TODO"""
        self.clear_todo()

        if event_count == 0:
            self.add_todo(
                task_id="no-data-analysis",
                description="深度分析为何没有找到信息：1) 信息源选择 2) 问题表述 3) 知识覆盖范围",
                status="pending",
                priority=10
            )
        else:
            self.add_todo(
                task_id="deep-understanding",
                description=f"深度理解问题本质，结合 {event_count} 个事项进行多维分析",
                status="pending",
                priority=10
            )

            self.add_todo(
                task_id="cross-reference",
                description="交叉验证信息：对比不同事项的观点，识别共识和分歧点",
                status="pending",
                priority=9
            )

            self.add_todo(
                task_id="build-narrative",
                description="构建完整叙事：背景介绍 → 现状分析 → 深入探讨 → 总结结论",
                status="pending",
                priority=8
            )

            self.add_todo(
                task_id="cite-sources",
                description="准确引用来源：每个关键论点都标注事项序号 [#1][#2]",
                status="pending",
                priority=7
            )

            self.add_todo(
                task_id="add-insights",
                description="添加深度洞察：基于事项总结规律、趋势、潜在影响和建议",
                status="pending",
                priority=6
            )

            if evaluation.get("confidence", 0) < 0.8:
                self.add_todo(
                    task_id="acknowledge-limits",
                    description="诚实说明：当前信息的局限性，哪些方面可能需要更多数据",
                    status="pending",
                    priority=5
                )

    # ============ 工具方法 ============

    def _deduplicate_events(self, events: List) -> List:
        """去重事项（基于ID）"""
        seen_ids = set()
        unique = []
        for e in events:
            event_id = e.id if hasattr(e, 'id') else str(e)
            if event_id not in seen_ids:
                seen_ids.add(event_id)
                unique.append(e)
        return unique
