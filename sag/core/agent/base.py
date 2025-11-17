"""
BaseAgent - 基于 JSON 系统提示词的智能 Agent

完全围绕 agent.json 构建，提供灵活的数据管理和执行能力
"""

from copy import deepcopy
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional
from zoneinfo import ZoneInfo

from sag.core.agent.builder import Builder
from sag.core.ai.base import BaseLLMClient
from sag.core.ai.models import LLMMessage, LLMRole
from sag.core.prompt import get_prompt_manager
from sag.utils import get_logger

logger = get_logger("agent.base")


class BaseAgent:
    """基础 Agent - 基本的Agent机制"""

    def __init__(
        self,
        timezone: Optional[str] = None,
        language: Optional[str] = None,
        database: Optional[List[Dict]] = None,
        memory: Optional[List[Dict]] = None,
        todo: Optional[List[Dict]] = None,
        output: Optional[Dict[str, Any]] = None,
        model_config: Optional[Dict] = None,
        scenario: str = 'chat',
    ):
        """
        初始化 Agent

        Args:
            timezone: 时区（可选，默认使用 agent.json 中的配置）
            language: 语言（可选，默认使用 agent.json 中的配置）
            database: 初始数据库分区列表（可选）
            memory: 初始记忆分区列表（可选）
            todo: 初始待办任务列表（可选）
            output: 输出配置覆盖（可选）
            model_config: LLM配置字典（可选）
                - 如果传入：使用该配置
                - 如果不传：自动从配置管理器获取场景配置
            scenario: LLM场景标识（默认 'chat'）
                - 'extract': 事项提取
                - 'chat': 对话交互
                - 'summary': 总结
                - 'general': 通用

        Example:
            # 最简单：使用所有默认值
            agent = BaseAgent()

            # 覆盖时区和语言
            agent = BaseAgent(timezone="America/New_York", language="en-US")

            # 带初始数据
            agent = BaseAgent(
                database=[{"type": "reports", "description": "...", "list": [...]}],
                output={"stream": True, "think": False}
            )
            
            # 自定义LLM配置
            agent = BaseAgent(
                model_config={'model': 'gpt-4', 'temperature': 0.7}
            )
        """
        # 1. 加载基本配置（固定使用 agent.json）
        self.prompt_manager = get_prompt_manager()
        self.agent_config = self.prompt_manager.load_json_config("agent")

        # 2. 提取默认值并允许覆盖
        role = self.agent_config["config"]["role"]
        self.timezone = timezone or role.get("timezone", "Asia/Shanghai")
        self.language = language or role.get("language", "zh-CN")

        # 3. 初始化数据（允许直接注入）
        self.database: List[Dict[str, Any]] = database or []
        self.memory: List[Dict[str, Any]] = memory or []
        self.todo: List[Dict[str, Any]] = todo or []

        # 4. 输出配置（允许覆盖）
        self.output_config = deepcopy(self.agent_config["config"]["output"])
        if output:
            self.output_config.update(output)

        # 5. LLM 配置（延迟初始化客户端）
        self.model_config = model_config
        self.scenario = scenario
        self._llm_client = None

        # 6. Prompt 构建器
        self.builder = Builder(self.agent_config)

        logger.info(
            "初始化 BaseAgent",
            extra={
                "timezone": self.timezone,
                "language": self.language,
                "database_partitions": len(self.database),
                "memory_partitions": len(self.memory),
                "todo_count": len(self.todo),
                "config_version": self.agent_config.get("version"),
            }
        )
    
    async def _get_llm_client(self) -> BaseLLMClient:
        """获取LLM客户端（懒加载）"""
        if self._llm_client is None:
            from sag.core.ai.factory import create_llm_client
            
            self._llm_client = await create_llm_client(
                scenario=self.scenario,
                model_config=self.model_config
            )
        
        return self._llm_client

    # ============ 统一执行入口 ============

    async def run(
        self,
        query: str,
        think: Optional[bool] = None,
        output_format: Optional[str] = None,
        schema: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        执行查询（非流式）

        Args:
            query: 用户查询
            think: 是否展示思考（None 则使用默认配置）
            output_format: 输出格式（None 则使用默认配置）
            schema: JSON Schema（可选）
            **kwargs: 其他 LLM 参数

        Returns:
            查询结果 Dict

        Example:
            # 使用默认配置
            result = await agent.run("总结财报")
            
            # Schema 输出
            result = await agent.run("提取指标", schema={...})
        """
        logger.info(f"执行查询: {query[:50]}...")

        # 构建系统提示词
        system_prompt = self._build_system_prompt(
            output_overrides={
                k: v for k, v in {
                    "stream": False,  # run() 总是非流式
                    "think": think,
                    "format": output_format,
                    "schema": schema if schema else None,
                }.items() if v is not None
            }
        )

        # 构建消息
        messages = [
            LLMMessage(
                role=LLMRole.SYSTEM,
                content=self.builder.to_json_string(system_prompt),
            ),
            LLMMessage(
                role=LLMRole.USER,
                content=query,
            ),
        ]

        # 执行
        if schema:
            return await self._execute_with_schema(messages, schema, **kwargs)
        else:
            return await self._execute_normal(messages, **kwargs)

    async def run_stream(
        self,
        query: str,
        think: Optional[bool] = None,
        output_format: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncIterator[Dict[str, str]]:
        """
        执行查询（流式）

        Args:
            query: 用户查询
            think: 是否展示思考（None 则使用默认配置）
            output_format: 输出格式（None 则使用默认配置）
            **kwargs: 其他 LLM 参数

        Yields:
            流式响应块

        Example:
            # 流式输出
            async for chunk in agent.run_stream("详细分析"):
                print(chunk["content"], end="")
        """
        logger.info(f"执行查询（流式）: {query[:50]}...")

        # 构建系统提示词
        system_prompt = self._build_system_prompt(
            output_overrides={
                k: v for k, v in {
                    "stream": True,  # run_stream() 总是流式
                    "think": think,
                    "format": output_format,
                }.items() if v is not None
            }
        )

        # 构建消息
        messages = [
            LLMMessage(
                role=LLMRole.SYSTEM,
                content=self.builder.to_json_string(system_prompt),
            ),
            LLMMessage(
                role=LLMRole.USER,
                content=query,
            ),
        ]

        # 确定是否展示思考
        use_think = think if think is not None else self.output_config.get("think", True)

        # 流式执行
        async for chunk in self._execute_stream(messages, use_think, **kwargs):
            yield chunk

    async def _execute_normal(self, messages: List[LLMMessage], **kwargs: Any) -> Dict[str, Any]:
        """普通执行（非流式、无 schema）"""
        llm_client = await self._get_llm_client()
        response = await llm_client.chat(messages=messages, **kwargs)
        return {
            "content": response.content,
            "model": response.model,
            "usage": response.usage.model_dump(),
            "finish_reason": response.finish_reason,
        }

    async def _execute_stream(
        self,
        messages: List[LLMMessage],
        include_reasoning: bool,
        **kwargs: Any,
    ) -> AsyncIterator[Dict[str, str]]:
        """流式执行"""
        llm_client = await self._get_llm_client()
        async for content, reasoning in llm_client.chat_stream(
            messages=messages,
            include_reasoning=include_reasoning,
            **kwargs,
        ):
            yield {
                "content": content,
                "reasoning": reasoning if include_reasoning else None,
            }

    async def _execute_with_schema(
        self,
        messages: List[LLMMessage],
        schema: Dict[str, Any],
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """带 Schema 执行（结构化输出）"""
        llm_client = await self._get_llm_client()
        result = await llm_client.chat_with_schema(
            messages=messages,
            response_schema=schema,
            **kwargs,
        )
        return result

    def _build_system_prompt(
        self,
        output_overrides: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """构建系统提示词（总是注入当前时间）"""
        # 总是注入当前时间
        current_time = self._get_current_time()

        return self.builder.build(
            database=self.database,
            memory=self.memory,
            todo=self.todo,
            timezone=self.timezone,
            current_time=current_time,
            language=self.language,
            output_overrides=output_overrides,
        )

    def _get_current_time(self) -> str:
        """获取当前时间（带时区）"""
        try:
            tz = ZoneInfo(self.timezone)
            return datetime.now(tz).isoformat()
        except Exception as e:
            logger.warning(f"获取时区时间失败: {e}")
            return datetime.utcnow().isoformat() + "Z"

    # ============ 数据管理方法 ============

    def add_database(
        self,
        data_type: str,
        items: List[Dict[str, Any]],
        description: Optional[str] = None,
    ) -> None:
        """
        添加数据库分区

        Args:
            data_type: 分区类型
            items: 数据列表
            description: 分区描述（如果已存在且提供新描述，则更新）

        Example:
            agent.add_database(
                data_type="financial_reports",
                items=[{"id": "doc1", "summary": "...", "content": "..."}],
                description="财务报告专区"
            )
        """
        # 查找现有分区
        partition = self._find_partition(self.database, data_type)

        if partition:
            # 已存在：追加数据
            partition["list"].extend(items)
            # 更新描述（如果提供）
            if description:
                partition["description"] = description
            logger.debug(f"追加数据到分区: {data_type}，新增 {len(items)} 条")
        else:
            # 不存在：创建新分区
            self.database.append({
                "type": data_type,
                "description": description or f"{data_type}类型的数据",
                "list": items,
            })
            logger.debug(f"创建新分区: {data_type}，包含 {len(items)} 条数据")

    def add_memory(
        self,
        data_type: str,
        items: List[Dict[str, Any]],
        description: Optional[str] = None,
    ) -> None:
        """
        添加记忆分区

        Args:
            data_type: 记忆类型（如：user_preferences, conversation_history）
            items: 记忆列表
            description: 分区描述
        """
        partition = self._find_partition(self.memory, data_type)

        if partition:
            partition["list"].extend(items)
            if description:
                partition["description"] = description
            logger.debug(f"追加记忆到分区: {data_type}，新增 {len(items)} 条")
        else:
            self.memory.append({
                "type": data_type,
                "description": description or f"{data_type}类型的记忆",
                "list": items,
            })
            logger.debug(f"创建新记忆分区: {data_type}，包含 {len(items)} 条")

    def add_todo(
        self,
        task_id: str,
        description: str,
        status: str = "pending",
        priority: int = 5,
        **kwargs: Any,
    ) -> None:
        """
        添加待办任务

        Args:
            task_id: 任务ID
            description: 任务描述
            status: 状态（pending/in_progress/completed）
            priority: 优先级（1-10）
            **kwargs: 其他字段
        """
        task = {
            "id": task_id,
            "description": description,
            "status": status,
            "priority": priority,
            **kwargs
        }
        self.todo.append(task)
        logger.debug(f"添加任务: {task_id} - {description}")

    def update_todo_status(self, task_id: str, status: str) -> bool:
        """
        更新任务状态

        Args:
            task_id: 任务ID
            status: 新状态

        Returns:
            是否更新成功
        """
        for task in self.todo:
            if task.get("id") == task_id:
                task["status"] = status
                logger.debug(f"更新任务状态: {task_id} -> {status}")
                return True
        logger.warning(f"任务不存在: {task_id}")
        return False

    def clear_database(self, data_type: Optional[str] = None) -> None:
        """
        清空数据库（可指定类型）

        Args:
            data_type: 分区类型（None 则清空所有）
        """
        if data_type:
            self.database = [p for p in self.database if p["type"] != data_type]
            logger.debug(f"清空数据库分区: {data_type}")
        else:
            self.database.clear()
            logger.debug("清空所有数据库")

    def clear_memory(self, data_type: Optional[str] = None) -> None:
        """
        清空记忆（可指定类型）

        Args:
            data_type: 记忆类型（None 则清空所有）
        """
        if data_type:
            self.memory = [p for p in self.memory if p["type"] != data_type]
            logger.debug(f"清空记忆分区: {data_type}")
        else:
            self.memory.clear()
            logger.debug("清空所有记忆")

    def clear_todo(self, status: Optional[str] = None) -> None:
        """
        清空待办（可指定状态）

        Args:
            status: 任务状态（None 则清空所有）
        """
        if status:
            self.todo = [t for t in self.todo if t.get("status") != status]
            logger.debug(f"清空 {status} 状态的任务")
        else:
            self.todo.clear()
            logger.debug("清空所有待办")

    def get_database_summary(self) -> Dict[str, Any]:
        """
        获取数据库摘要

        Returns:
            数据库摘要信息
        """
        return {
            "total_partitions": len(self.database),
            "partitions": [
                {
                    "type": p["type"],
                    "description": p.get("description"),
                    "count": len(p.get("list", [])),
                }
                for p in self.database
            ],
        }

    def get_memory_summary(self) -> Dict[str, Any]:
        """
        获取记忆摘要

        Returns:
            记忆摘要信息
        """
        return {
            "total_partitions": len(self.memory),
            "partitions": [
                {
                    "type": p["type"],
                    "description": p.get("description"),
                    "count": len(p.get("list", [])),
                }
                for p in self.memory
            ],
        }

    def get_todo_summary(self) -> Dict[str, Any]:
        """
        获取待办摘要

        Returns:
            待办摘要信息
        """
        status_count = {}
        for task in self.todo:
            status = task.get("status", "unknown")
            status_count[status] = status_count.get(status, 0) + 1

        return {
            "total_tasks": len(self.todo),
            "by_status": status_count,
        }

    def _find_partition(
        self,
        partitions: List[Dict],
        data_type: str
    ) -> Optional[Dict]:
        """
        查找分区

        Args:
            partitions: 分区列表
            data_type: 分区类型

        Returns:
            找到的分区，不存在则返回 None
        """
        for partition in partitions:
            if partition.get("type") == data_type:
                return partition
        return None
