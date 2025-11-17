"""
日志工具模块

提供结构化日志功能
"""

import logging
import sys
from typing import Any, Dict, Optional

from sag.core.config import get_settings


def setup_logging(
    level: Optional[str] = None,
    format_type: Optional[str] = None,
) -> logging.Logger:
    """
    配置日志系统

    Args:
        level: 日志级别（DEBUG, INFO, WARNING, ERROR, CRITICAL）
        format_type: 日志格式（json, text）

    Returns:
        根日志器
    """
    settings = get_settings()
    log_level = level or settings.log_level
    log_format = format_type or settings.log_format

    # 创建根日志器
    logger = logging.getLogger("sag")
    logger.setLevel(getattr(logging, log_level))

    # 清除已有处理器
    logger.handlers.clear()

    # 创建控制台处理器
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(getattr(logging, log_level))

    # 设置格式
    if log_format == "json":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


class JsonFormatter(logging.Formatter):
    """JSON格式化器"""

    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录为JSON"""
        import json
        from datetime import datetime

        log_data: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # 添加额外字段
        if hasattr(record, "extra"):
            log_data.update(record.extra)

        # 添加异常信息
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data, ensure_ascii=False)


def get_logger(name: str) -> logging.Logger:
    """
    获取日志器

    Args:
        name: 日志器名称

    Returns:
        日志器实例
    """
    return logging.getLogger(f"sag.{name}")


# 默认日志器
logger = get_logger("main")
