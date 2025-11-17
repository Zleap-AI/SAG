"""
SummarizerAgent - 继承自 BaseAgent

专门用于对文档事项进行总结和分析
"""

from typing import Any, AsyncIterator, Dict, List, Optional, Union

from sag.core.agent.base import BaseAgent
from sag.utils import get_logger

logger = get_logger("agent.summarizer")


class SummarizerAgent(BaseAgent):
    """
    总结 Agent - 专注于文档事项的总结和分析
    
    特点：
    - 自动将 events 转为带序号的文档事项
    - 默认流式输出
    - 自动添加待办任务（回答时引用事项序号）
    """

    def __init__(
        self,
        events: Optional[List[Dict]] = None,
        **kwargs: Any,
    ):
        """
        初始化 SummarizerAgent

        Args:
            events: 文档事项列表（可选）
            **kwargs: 其他参数传递给 BaseAgent

        Example:
            # 最简单
            agent = SummarizerAgent()

            # 带初始事项
            agent = SummarizerAgent(
                events=[{"id": "1", "summary": "...", "content": "..."}]
            )

            # 覆盖配置
            agent = SummarizerAgent(timezone="America/New_York")
        """
        # 设置默认输出配置（流式输出）
        if "output" not in kwargs:
            kwargs["output"] = {"stream": True}
        elif isinstance(kwargs["output"], dict) and "stream" not in kwargs["output"]:
            kwargs["output"]["stream"] = True

        # 调用父类初始化
        super().__init__(**kwargs)

        # 加载初始事项
        if events:
            self.load_events(events)

        logger.info("初始化 SummarizerAgent", extra={"events_count": len(events) if events else 0})

    async def run(
        self,
        query: str,
        events: Optional[List[Dict]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[Dict[str, str]]:
        """
        执行总结（流式输出，默认模式）

        Args:
            query: 用户查询
            events: 事项列表（可选，自动加载并添加序号）
            **kwargs: 其他参数

        Yields:
            流式响应块

        Example:
            # 流式输出（默认）
            async for chunk in agent.run("总结Q3财报", events=events):
                print(chunk["content"], end="")
        """
        # 加载事项到数据库
        if events:
            self.load_events(events)

        # 执行查询（调用父类的 run_stream，默认流式）
        async for chunk in super().run_stream(query=query, **kwargs):
            yield chunk

    async def run_normal(
        self,
        query: str,
        events: Optional[List[Dict]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        执行总结（非流式）

        Args:
            query: 用户查询
            events: 事项列表（可选）
            **kwargs: 其他参数

        Returns:
            查询结果

        Example:
            result = await agent.run_normal("快速总结", events=events)
        """
        # 加载事项到数据库
        if events:
            self.load_events(events)

        # 执行查询（调用父类的 run，非流式）
        return await super().run(query=query, **kwargs)

    def load_events(self, events: List[Dict]) -> None:
        """
        加载事项到数据库（添加序号）

        Args:
            events: 事项列表

        Example:
            events = [
                {"id": "doc-001", "summary": "Q3财报", "content": "..."}
            ]
            agent.load_events(events)
        """
        # 清空现有的文档事项
        self.clear_database(data_type="文档事项")

        # 为每个事项添加序号
        items_with_order = []
        for idx, event in enumerate(events, start=1):
            item = event.copy()
            item["order"] = idx  # 添加序号
            items_with_order.append(item)

        # 添加到数据库
        self.add_database(
            data_type="文档事项",
            items=items_with_order,
            description="从文档中提取的事项"
        )

        # 自动添加待办任务
        self._add_summary_todo(len(events))

        logger.info(f"加载文档事项: {len(events)} 条（已添加序号）")

    def _add_summary_todo(self, event_count: int) -> None:
        """
        添加总结任务到待办清单

        Args:
            event_count: 事项数量
        """
        # 清空现有待办
        self.clear_todo()

        # 添加总结任务
        self.add_todo(
            task_id="find-events",
            description=f"查找数据，根据 {event_count} 条文档事项输出回答，从所有事项中查找出跟问题相关的项跟准确序号",
            status="pending",
            priority=10
        )
        self.add_todo(
            task_id="summarize-events",
            description=f"将选好的事项整合成回答，回答中需要引用事项序号（如：[#1]、[#2]）以标明信息来源, 如果引用多个则是 [#1][#2]，确保引用正确 ",
            status="pending",
            priority=10
        )
        self.add_todo(
            task_id="integrate-answer",
            description=f"根据内容合理排布分段，合并数据整合成一个完整且连贯的回答，不要死板，合理的分段跟聚合，更加拟人化的输出，输出纯文本， 不应该出现类似**这样的markdown标记符",
            status="pending",
            priority=10
        )
        self.add_todo(
            task_id="format-answer",
            description=f"检查内容跟格式，修正错误内容跟纠正为最终输出",
            status="pending",
            priority=10
        )

        logger.debug(f"添加总结任务: {event_count} 条事项")
