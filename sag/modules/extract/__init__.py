"""
Extract模块 - 事项提取

从文章片段中提取结构化事项和实体
"""

from sag.modules.extract.config import ExtractConfig
from sag.modules.extract.extractor import EventExtractor
from sag.modules.extract.processor import EventProcessor

__all__ = [
    "EventExtractor",
    "EventProcessor",
    "ExtractConfig",
]
