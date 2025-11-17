"""
Elasticsearch Document 模型

使用 elasticsearch-dsl 定义索引映射
"""

from sag.core.storage.documents.source_chunk import SourceChunkDocument
from sag.core.storage.documents.entity_vector import EntityVectorDocument
from sag.core.storage.documents.event_vector import EventVectorDocument

__all__ = [
    "EntityVectorDocument",
    "EventVectorDocument",
    "SourceChunkDocument",
    "REGISTERED_DOCUMENTS",
]

# 索引注册表：所有需要在 ES 中创建的 Document 类
# 添加新索引时，只需在此列表中添加对应的 Document 类即可
REGISTERED_DOCUMENTS = [
    EntityVectorDocument,
    EventVectorDocument,
    SourceChunkDocument,
]
