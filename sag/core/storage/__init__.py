"""
存储模块

提供MySQL, Elasticsearch, Redis客户端及Repository访问层
"""

from sag.core.storage.documents import (
    SourceChunkDocument,
    EntityVectorDocument,
    EventVectorDocument,
    REGISTERED_DOCUMENTS,
)
from sag.core.storage.elasticsearch import (
    ESConfig,
    ElasticsearchClient,
    close_es_client,
    get_es_client,
)
from sag.core.storage.mysql import (
    MySQLClient,
    close_mysql_client,
    create_mysql_client,
    get_mysql_client,
)
from sag.core.storage.redis import (
    RedisClient,
    close_redis_client,
    get_redis_client,
)
from sag.core.storage.repositories import (
    BaseRepository,
    EntityVectorRepository,
    EventVectorRepository,
    SourceChunkRepository,
)

__all__ = [
    # MySQL
    "MySQLClient",
    "create_mysql_client",
    "get_mysql_client",
    "close_mysql_client",
    # Elasticsearch
    "ESConfig",
    "ElasticsearchClient",
    "get_es_client",
    "close_es_client",
    # Redis
    "RedisClient",
    "get_redis_client",
    "close_redis_client",
    # ES Documents
    "EntityVectorDocument",
    "EventVectorDocument",
    "SourceChunkDocument",
    "REGISTERED_DOCUMENTS",
    # ES Repositories
    "BaseRepository",
    "EntityVectorRepository",
    "EventVectorRepository",
    "SourceChunkRepository",
  ]
