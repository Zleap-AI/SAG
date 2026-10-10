"""zleap-sag 适配层 —— 管理运行资源与业务查询结果。

查询结果使用业务 DTO；文档处理沿用引擎公开请求与回执，由 Job 保存调用绑定。
"""

from sag_api.sag.dto import (
    ChunkInfo,
    EntityInfo,
    GraphAssociationInfo,
    GraphEventInfo,
    RetrievedSection,
    SearchOutcome,
    SourceGraphInfo,
)
from sag_api.sag.engine_manager import EngineManager

__all__ = [
    "ChunkInfo",
    "EngineManager",
    "EntityInfo",
    "GraphAssociationInfo",
    "GraphEventInfo",
    "RetrievedSection",
    "SearchOutcome",
    "SourceGraphInfo",
]
