"""
来源片段 Repository

提供来源片段 (SourceChunk) 的业务查询方法
"""

from typing import Any, Dict, List, Optional

from elasticsearch_dsl import Q, Search

from sag.core.storage.repositories.base import BaseRepository


class SourceChunkRepository(BaseRepository):
    """来源片段 Repository"""

    INDEX_NAME = "source_chunks"

    async def index_chunk(
        self,
        chunk_id: str,
        source_id: str,
        source_config_id: str,
        rank: int,
        heading: Optional[str],
        content: str,
        heading_vector: Optional[List[float]],
        content_vector: List[float],
        references: Optional[List[str]] = None,
        **kwargs,
    ) -> str:
        """
        索引单个来源片段

        Args:
            chunk_id: 片段ID (SourceChunk.id)
            source_id: 来源ID (Article.id 或 Conversation.id)
            source_config_id: 信息源ID
            rank: 排序
            heading: 标题
            content: 内容
            heading_vector: 标题向量
            content_vector: 内容向量
            references: 关联的 ArticleSection ID 列表
            **kwargs: 其他字段（chunk_type, content_length等）

        Returns:
            文档ID
        """
        document = {
            "chunk_id": chunk_id,
            "source_id": source_id,
            "source_config_id": source_config_id,
            "rank": rank,
            "heading": heading,
            "content": content,
            "heading_vector": heading_vector,
            "content_vector": content_vector,
            "references": references or [],
            **kwargs,
        }

        # 使用 source_config_id 作为路由键，确保同一信息源的数据在同一分片
        return await self.index_document(
            self.INDEX_NAME, chunk_id, document, routing=source_config_id
        )

    async def get_by_source(
        self, source_id: str, sort_by_rank: bool = True
    ) -> List[Dict[str, Any]]:
        """
        获取来源的所有片段

        Args:
            source_id: 来源ID (Article.id 或 Conversation.id)
            sort_by_rank: 是否按rank排序

        Returns:
            片段列表
        """
        s = Search(using=self.es_client, index=self.INDEX_NAME)

        s = s.filter("term", source_id=source_id)

        if sort_by_rank:
            s = s.sort("rank")

        s = s[:100]  # 最多返回100个片段

        # 转换为字典并执行
        search_dict = s.to_dict()
        response = await self.es_client.search(
            index=self.INDEX_NAME,
            query=search_dict.get("query", {}),
            size=search_dict.get("size", 10)
        )
        return [hit["_source"] for hit in response["hits"]["hits"]]

    async def search_similar_by_content(
        self,
        query_vector: List[float],
        k: int = 10,
        source_config_id: Optional[str] = None,
        source_config_ids: Optional[List[str]] = None,
        chunk_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        通过内容向量搜索相似片段

        Args:
            query_vector: 查询向量
            k: 返回数量
            source_config_id: 信息源ID（单个，向后兼容）
            source_config_ids: 信息源ID列表（支持多源搜索）
            chunk_type: 片段类型（可选）

        Returns:
            相似片段列表
        """
        knn_query = {
            "field": "content_vector",
            "query_vector": query_vector,
            "k": k,
            "num_candidates": k * 10,
        }

        # 添加过滤条件
        filter_query = None
        filters = []

        # 处理信息源过滤（支持单个或多个）
        if source_config_ids:
            # 优先使用 source_config_ids（复数）
            filters.append(Q("terms", source_config_id=source_config_ids))
        elif source_config_id:
            # 向后兼容 source_config_id（单数）
            filters.append(Q("term", source_config_id=source_config_id))

        if chunk_type:
            filters.append(Q("term", chunk_type=chunk_type))

        if filters:
            filter_query = Q("bool", must=filters).to_dict()

        # 使用 source_config_id 作为路由键优化查询性能（仅单源时）
        routing = source_config_id if source_config_id else None

        # 使用vector_search方法
        return await self.es_client.vector_search(
            index=self.INDEX_NAME,
            field="content_vector",
            vector=query_vector,
            size=k,
            filter_query=filter_query,
            routing=routing,
        )

    async def search_by_text(
        self,
        query: str,
        source_config_id: Optional[str] = None,
        source_config_ids: Optional[List[str]] = None,
        size: int = 10
    ) -> List[Dict[str, Any]]:
        """
        全文检索片段

        Args:
            query: 查询文本
            source_config_id: 信息源ID（单个，向后兼容）
            source_config_ids: 信息源ID列表（支持多源搜索）
            size: 返回数量

        Returns:
            片段列表
        """
        s = Search(using=self.es_client, index=self.INDEX_NAME)

        # 多字段查询
        s = s.query(
            "multi_match",
            query=query,
            fields=["heading^2", "content"],  # heading权重更高
        )

        # 处理信息源过滤（支持单个或多个）
        if source_config_ids:
            # 优先使用 source_config_ids（复数）
            s = s.filter("terms", source_config_id=source_config_ids)
        elif source_config_id:
            # 向后兼容 source_config_id（单数）
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

    async def get_chunks_by_ids(
        self,
        chunk_ids: List[str],
        include_vectors: bool = True
    ) -> List[Dict[str, Any]]:
        """
        根据片段ID列表批量获取片段详细信息（包含向量）

        Args:
            chunk_ids: 片段ID列表
            include_vectors: 是否包含向量字段（默认True）

        Returns:
            片段详细信息列表（包含 content_vector 和 heading_vector）
        """
        if not chunk_ids:
            return []

        # 构建ES查询
        query_body = {
            "query": {
                "terms": {
                    "chunk_id": chunk_ids
                }
            },
            "size": len(chunk_ids)
        }

        # 如果不需要向量，排除向量字段
        if not include_vectors:
            query_body["_source"] = {
                "excludes": ["heading_vector", "content_vector"]
            }

        try:
            response = await self.es_client.search(
                index=self.INDEX_NAME,
                query=query_body["query"],
                size=query_body.get("size", 10),
                _source=query_body.get("_source")
            )

            results = []

            if isinstance(response, list):
                # 格式2：直接是文档列表
                for chunk_data in response:
                    if isinstance(chunk_data, dict) and "chunk_id" in chunk_data:
                        results.append(chunk_data)

            elif isinstance(response, dict) and "hits" in response:
                # 格式1：完整响应格式
                hits = response["hits"].get("hits", [])
                for hit in hits:
                    if isinstance(hit, dict):
                        if "_source" in hit:
                            result = hit["_source"]
                            if "chunk_id" not in result:
                                result["chunk_id"] = hit.get("_id")
                            results.append(result)

            return results

        except Exception as e:
            import logging
            import traceback
            logger = logging.getLogger(__name__)
            logger.error(f"批量获取来源片段失败: {e}")
            logger.error(f"详细错误: {traceback.format_exc()}")
            return []
