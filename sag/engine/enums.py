"""
引擎枚举类

定义任务状态、阶段、日志级别等枚举
"""

from enum import Enum


class TaskStatus(str, Enum):
    """任务状态"""

    PENDING = "pending"
    LOADING = "loading"
    EXTRACTING = "extracting"
    SEARCHING = "searching"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStage(str, Enum):
    """任务阶段"""

    INIT = "init"
    LOAD = "load"
    EXTRACT = "extract"
    SEARCH = "search"
    OUTPUT = "output"


class LogLevel(str, Enum):
    """日志级别"""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class OutputMode(str, Enum):
    """输出模式"""

    ID_ONLY = "id_only"  # 只输出ID
    FULL = "full"  # 输出完整内容

