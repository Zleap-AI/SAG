"""
SAG 引擎模块

提供统一的任务引擎接口
"""

from sag.engine.config import (
    ModelConfig,
    OutputConfig,
    TaskConfig,
)
from sag.engine.core import SAGEngine
from sag.engine.enums import LogLevel, OutputMode, TaskStage, TaskStatus
from sag.engine.models import StageResult, TaskLog, TaskResult
from sag.modules.extract.config import ExtractBaseConfig
from sag.modules.load.config import (
    DocumentLoadConfig,
    LoadBaseConfig,
    ConversationLoadConfig,
)
from sag.modules.search.config import SearchBaseConfig

__all__ = [
    # 核心引擎
    "SAGEngine",
    # 配置类
    "ModelConfig",
    "LoadBaseConfig",
    "DocumentLoadConfig",
    "ConversationLoadConfig",
    "ExtractBaseConfig",
    "SearchBaseConfig",
    "OutputConfig",
    "TaskConfig",
    # 枚举
    "TaskStatus",
    "TaskStage",
    "LogLevel",
    "OutputMode",
    # 模型
    "TaskResult",
    "TaskLog",
    "StageResult",
]
