"""
搜索模块

提供SAG搜索引擎：recall → expand → rerank

架构：
- SAGSearcher/EventSearcher: 统一搜索入口（推荐使用）
- recall/expand/rerank: 三阶段模块（高级使用）
- ranking: 事项排序策略
"""

from sag.modules.search.config import (
    SearchConfig,
    SearchBaseConfig,
    RecallConfig,
    ExpandConfig,
    RerankConfig,
    RerankStrategy,
)
from sag.modules.search.searcher import (
    SAGSearcher,
    EventSearcher,
)
from sag.modules.search.recall import (
    RecallSearcher,
    RecallResult,
)
from sag.modules.search.expand import (
    ExpandSearcher,
    ExpandResult,
)
from sag.modules.search.tracker import Tracker

__all__ = [
    # 配置
    "SearchConfig",
    "SearchBaseConfig",
    "RecallConfig",
    "ExpandConfig",
    "RerankConfig",
    "RerankStrategy",
    # 搜索器（推荐）
    "SAGSearcher",
    "EventSearcher",
    # 三阶段模块（高级）
    "RecallSearcher",
    "RecallResult",
    "ExpandSearcher",
    "ExpandResult",
    # 工具
    "Tracker",
]
