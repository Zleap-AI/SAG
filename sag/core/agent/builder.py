"""
Builder - Agent 系统提示词构建器

负责构建完整的 JSON 系统提示词
"""

import json
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sag.utils import get_logger

logger = get_logger("agent.builder")


class Builder:
    """Agent 提示词构建器"""

    def __init__(self, base_config: Dict[str, Any]):
        """
        初始化构建器

        Args:
            base_config: 从 agent.json 加载的基础配置
        """
        self.base_config = deepcopy(base_config)
        logger.debug("初始化 Builder")

    def build(
        self,
        database: Optional[List[Dict]] = None,
        memory: Optional[List[Dict]] = None,
        todo: Optional[List[Dict]] = None,
        timezone: Optional[str] = None,
        current_time: Optional[str] = None,
        language: Optional[str] = None,
        output_overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        构建完整的系统提示词

        Args:
            database: 数据库分区列表
            memory: 记忆分区列表
            todo: 待办任务列表
            timezone: 时区
            current_time: 当前时间
            language: 语言
            output_overrides: 输出配置覆盖

        Returns:
            完整的系统提示词 JSON
        """
        prompt = deepcopy(self.base_config["config"])

        # 更新 role 配置
        if timezone:
            prompt["role"]["timezone"] = timezone
        if current_time:
            prompt["role"]["current_time"] = current_time
        if language:
            prompt["role"]["language"] = language

        # 填充数据
        prompt["database"] = database or []
        prompt["memory"] = memory or []
        prompt["todo"] = todo or []

        # 覆盖输出配置
        if output_overrides:
            prompt["output"].update(output_overrides)

        logger.debug(
            "构建系统提示词",
            extra={
                "database_partitions": len(prompt["database"]),
                "memory_partitions": len(prompt["memory"]),
                "todo_count": len(prompt["todo"]),
            }
        )

        return prompt

    def to_json_string(self, prompt: Dict[str, Any]) -> str:
        """
        将提示词转换为 JSON 字符串（用于发送给 LLM）

        Args:
            prompt: 系统提示词字典

        Returns:
            格式化的 JSON 字符串
        """
        return json.dumps(prompt, ensure_ascii=False, indent=2)

    @staticmethod
    def get_current_time(timezone: str = "Asia/Shanghai") -> str:
        """
        获取当前时间（带时区）

        Args:
            timezone: 时区标识符

        Returns:
            ISO 8601 格式的时间字符串
        """
        try:
            tz = ZoneInfo(timezone)
            return datetime.now(tz).isoformat()
        except Exception as e:
            logger.warning(f"获取时区时间失败: {e}")
            return datetime.utcnow().isoformat() + "Z"

