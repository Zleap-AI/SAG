"""
实体向量 Repository

提供实体向量的业务查询方法
"""

from typing import Any, Dict, List, Optional

from elasticsearch import AsyncElasticsearch
from elasticsearch_dsl import Q, Search

from sag.core.storage.repositories.base import BaseRepository
from sag.db import get_session_factory


class EntityVectorRepository(BaseRepository):
    """实体向量 Repository"""

    INDEX_NAME = "entity_vectors"

    def __init__(self, es_client: AsyncElasticsearch):
        """
        初始化 Repository

        Args:
            es_client: Elasticsearch 异步客户端
        """
        super().__init__(es_client)
        self.session_factory = get_session_factory()

    async def index_entity(
        self,
        entity_id: str,
        source_config_id: str,
        entity_type: str,
        name: str,
        vector: List[float],
        **kwargs,
    ) -> str:
        """
        索引单个实体

        Args:
            entity_id: 实体ID
            source_config_id: 信息源ID
            entity_type: 实体类型
            name: 实体名称
            vector: 向量
            **kwargs: 其他字段（created_time等）

        Returns:
            文档ID
        """
        document = {
            "entity_id": entity_id,
            "source_config_id": source_config_id,
            "type": entity_type,
            "name": name,
            "vector": vector,
            **kwargs,
        }

        # 使用 source_config_id 作为路由键，确保同一信息源的数据在同一分片
        return await self.index_document(
            self.INDEX_NAME, entity_id, document, routing=source_config_id
        )

    async def search_by_name(
        self, name: str, source_config_id: Optional[str] = None, size: int = 10
    ) -> List[Dict[str, Any]]:
        """
        按名称搜索实体

        Args:
            name: 实体名称（模糊匹配）
            source_config_id: 信息源ID（可选）
            size: 返回数量

        Returns:
            实体列表
        """
        s = Search(using=self.es_client, index=self.INDEX_NAME)

        # 构建查询
        s = s.query("match", name=name)

        if source_config_id:
            s = s.filter("term", source_config_id=source_config_id)

        s = s[:size]

        # 转换为字典并执行
        search_dict = s.to_dict()
        # 如果指定了 source_config_id，使用 routing 优化查询性能
        routing = source_config_id if source_config_id else None
        response = await self.es_client.search(
            index=self.INDEX_NAME,
            query=search_dict.get("query", {}),
            size=search_dict.get("size", 10),
            routing=routing
        )
        return [hit["_source"] for hit in response["hits"]["hits"]]

    async def _get_entity_type_thresholds(
        self, source_config_ids: Optional[List[str]] = None
    ) -> Dict[str, float]:
        """
        获取实体类型的相似度阈值

        Args:
            source_config_ids: 信息源ID列表（可选，支持多源）

        Returns:
            实体类型到阈值的映射字典
        """
        from sqlalchemy import select
        from sag.db import EntityType

        thresholds = {}

        async with self.session_factory() as session:
            query = select(EntityType.type, EntityType.similarity_threshold)

            if source_config_ids:
                # 查找信息源特定的实体类型，如果没有则使用系统默认类型
                query = query.where(
                    (EntityType.source_config_id.in_(source_config_ids)) | (EntityType.source_config_id.is_(None))
                )
            else:
                # 只查找系统默认类型
                query = query.where(EntityType.source_config_id.is_(None))

            query = query.where(EntityType.is_active == True)

            result = await session.execute(query)
            for entity_type, threshold in result.fetchall():
                # 如果同一个类型有多个定义，使用更具体的（信息源特定的）
                if entity_type not in thresholds or thresholds[entity_type] is None:
                    thresholds[entity_type] = float(threshold)

        return thresholds

    async def search_similar(
        self,
        query_vector: List[float],
        k: int = 10,
        source_config_id: Optional[str] = None,
        source_config_ids: Optional[List[str]] = None,
        entity_type: Optional[str] = None,
        include_type_threshold: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        向量相似度搜索

        Args:
            query_vector: 查询向量
            k: 返回数量
            source_config_id: 信息源ID（单个，向后兼容）
            source_config_ids: 信息源ID列表（支持多源搜索）
            entity_type: 实体类型（可选）
            include_type_threshold: 是否包含实体类型相似度阈值信息

        Returns:
            相似实体列表，如果 include_type_threshold=True 则包含 type_threshold 字段
        """
        # 参数兼容处理：优先使用 source_config_ids，如果没有则使用 source_config_id
        if not source_config_ids and source_config_id:
            source_config_ids = [source_config_id]

        knn_query = {
            "field": "vector",
            "query_vector": query_vector,
            "k": k,
            "num_candidates": k * 10,
        }

        # 添加过滤条件
        filter_query = None
        filters = []
        if source_config_ids:
            # 单源使用 term 查询，多源使用 terms 查询
            if len(source_config_ids) == 1:
                filters.append(Q("term", source_config_id=source_config_ids[0]))
            else:
                filters.append(Q("terms", source_config_id=source_config_ids))
        if entity_type:
            filters.append(Q("term", type=entity_type))

        if filters:
            filter_query = Q("bool", must=filters).to_dict()

        # 使用 source_config_id 作为路由键优化查询性能
        # 仅在单源时使用 routing，多源时禁用以支持跨分片查询
        routing = source_config_ids[0] if source_config_ids and len(source_config_ids) == 1 else None

        # 使用vector_search方法
        search_results = await self.es_client.vector_search(
            index=self.INDEX_NAME,
            field="vector",
            vector=query_vector,
            size=k,
            filter_query=filter_query,
            routing=routing,
        )

        results = []
        if include_type_threshold:
            # 获取实体类型相似度阈值（多源时传递所有 source_config_ids）
            type_thresholds = await self._get_entity_type_thresholds(source_config_ids)

            for hit in search_results:
                entity_data = hit.copy()
                # 添加实体类型的相似度阈值
                entity_type_name = entity_data.get("type")
                if entity_type_name and entity_type_name in type_thresholds:
                    entity_data["type_threshold"] = type_thresholds[entity_type_name]
                else:
                    # 使用默认阈值
                    entity_data["type_threshold"] = 0.800
                results.append(entity_data)
        else:
            results = search_results

        return results

    async def get_by_source(
        self, source_config_id: str, entity_type: Optional[str] = None, size: int = 100
    ) -> List[Dict[str, Any]]:
        """
        获取信息源的所有实体

        Args:
            source_config_id: 信息源ID
            entity_type: 实体类型（可选）
            size: 返回数量

        Returns:
            实体列表
        """
        s = Search(using=self.es_client, index=self.INDEX_NAME)

        s = s.filter("term", source_config_id=source_config_id)

        if entity_type:
            s = s.filter("term", type=entity_type)

        s = s[:size]

        # 转换为字典并执行
        search_dict = s.to_dict()
        # 使用 routing 优化查询性能
        response = await self.es_client.search(
            index=self.INDEX_NAME,
            query=search_dict.get("query", {}),
            size=search_dict.get("size", 10),
            routing=source_config_id
        )
        return [hit["_source"] for hit in response["hits"]["hits"]]
