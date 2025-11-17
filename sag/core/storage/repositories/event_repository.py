"""
事件向量 Repository

提供事件向量的业务查询方法
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
from elasticsearch_dsl import Q, Search

from sag.core.storage.repositories.base import BaseRepository


class EventVectorRepository(BaseRepository):
    """事件向量 Repository"""

    INDEX_NAME = "event_vectors"

    async def index_event(
        self,
        event_id: str,
        source_config_id: str,
        source_type: str,
        source_id: str,
        title: str,
        summary: str,
        content: str,
        title_vector: List[float],
        content_vector: List[float],
        **kwargs,
    ) -> str:
        """
        索引单个事件

        Args:
            event_id: 事件ID
            source_config_id: 信息源ID
            source_type: 来源类型（ARTICLE/CHAT）
            source_id: 来源ID
            title: 标题
            summary: 摘要
            content: 内容
            title_vector: 标题向量
            content_vector: 内容向量
            **kwargs: 其他字段（category, tags, entity_ids, start_time, end_time等）

        Returns:
            文档ID
        """
        document = {
            "event_id": event_id,
            "source_config_id": source_config_id,
            "source_type": source_type,
            "source_id": source_id,
            "title": title,
            "summary": summary,
            "content": content,
            "title_vector": title_vector,
            "content_vector": content_vector,
            **kwargs,
        }

        # 使用 source_config_id 作为路由键，确保同一信息源的数据在同一分片
        return await self.index_document(
            self.INDEX_NAME, event_id, document, routing=source_config_id
        )

    async def search_by_text(
        self, query: str, source_config_id: Optional[str] = None, size: int = 10
    ) -> List[Dict[str, Any]]:
        """
        全文检索

        Args:
            query: 查询文本
            source_config_id: 信息源ID（可选）
            size: 返回数量

        Returns:
            事件列表
        """
        s = Search(using=self.es_client, index=self.INDEX_NAME)

        # 多字段查询
        s = s.query(
            "multi_match",
            query=query,
            fields=["title^3", "summary^2", "content"],  # title权重最高
        )

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
        return response

    async def get_event_vector(
        self, event_id: str, vector_type: str = "content_vector"
    ) -> Optional[List[float]]:
        """
        获取特定事件的向量

        Args:
            event_id: 事件ID
            vector_type: 向量类型, "title_vector" 或 "content_vector"

        Returns:
            事件向量, 如果不存在则返回None
        """
        # 构建ES查询
        query_body = {
            "query": {
                "term": {
                    "event_id": event_id
                }
            },
            "size": 1,
            "_source": [vector_type, "event_id", "title", "summary"]
        }

        try:
            response = await self.es_client.search(
                index=self.INDEX_NAME,
                query=query_body["query"],
                size=query_body.get("size", 1),
                return_full_response=True,
                **{"_source": query_body.get("_source", [])}
            )

            hits = response.get("hits", [])

            if hits:
                # Handle different response formats
                if isinstance(hits[0], dict):
                    # Standard format: hit is a dict with _source field
                    event_data = hits[0].get(
                        "source", hits[0].get("_source", {}))
                else:
                    # Non-standard format: hit might be an object with attributes
                    event_data = getattr(
                        hits[0], "source", getattr(hits[0], "_source", {}))

                return event_data.get(vector_type)
            else:
                return None

        except Exception as e:
            # 如果查询失败，返回None而不是抛出异常
            return None

    async def get_events_by_ids(
        self, event_ids: List[str]
    ) -> List[Dict[str, Any]]:
        """
        根据事件ID列表获取事件详细信息

        Args:
            event_ids: 事件ID列表

        Returns:
            事件详细信息列表
        """
        if not event_ids:
            return []

        # 构建ES查询
        query_body = {
            "query": {
                "terms": {
                    "event_id": event_ids
                }
            },
            "size": len(event_ids)
        }

        try:
            response = await self.es_client.search(
                index=self.INDEX_NAME,
                query=query_body["query"],
                size=query_body.get("size", 10)
            )

            # 确保返回正确的数据格式
            events = []

            # 处理两种返回格式：
            # 1. dict 格式（完整响应）：{"hits": {"hits": [{"_source": {...}, "_id": "..."}]}}
            # 2. list 格式（文档列表）：[{event_id: "...", ...}, ...]

            if isinstance(response, list):
                # 格式2：直接是文档列表（ES client 的默认返回格式）
                for event_data in response:
                    if isinstance(event_data, dict) and "event_id" in event_data:
                        events.append(event_data)
                    else:
                        print(f"警告: 事件数据缺少event_id字段: {event_data}")

            elif isinstance(response, dict) and "hits" in response:
                # 格式1：完整响应格式
                hits = response["hits"].get("hits", [])
                for hit in hits:
                    if isinstance(hit, dict):
                        # 优先使用_source字段
                        if "_source" in hit:
                            event_data = hit["_source"]
                        elif "source" in hit:
                            event_data = hit["source"]
                        else:
                            continue

                        # 确保event_id字段存在
                        if "event_id" not in event_data and "_id" in hit:
                            event_data["event_id"] = hit["_id"]

                        # 验证必要字段
                        if isinstance(event_data, dict) and "event_id" in event_data:
                            events.append(event_data)
                        else:
                            print(f"警告: 事件数据缺少event_id字段: {event_data}")
            else:
                print(f"警告: Elasticsearch响应格式异常: {type(response)}")

            return events

        except Exception as e:
            print(f"查询事件失败: {e}")
            return []

    async def search_similar_by_title(
        self,
        query_vector: List[float],
        k: int = 10,
        source_config_id: Optional[str] = None,
        source_config_ids: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        通过标题向量搜索相似事件

        Args:
            query_vector: 查询向量
            k: 返回数量
            source_config_id: 信息源ID（单个，向后兼容）
            source_config_ids: 信息源ID列表（支持多源搜索）
            category: 分类（可选）

        Returns:
            相似事件列表
        """
        return await self._vector_search(
            "title_vector", query_vector, k, source_config_id, source_config_ids, category
        )

    async def search_similar_by_content(
        self,
        query_vector: List[float],
        k: int = 10,
        source_config_id: Optional[str] = None,
        source_config_ids: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        通过内容向量搜索相似事件

        Args:
            query_vector: 查询向量
            k: 返回数量
            source_config_id: 信息源ID（单个，向后兼容）
            source_config_ids: 信息源ID列表（支持多源搜索）
            category: 分类（可选）

        Returns:
            相似事件列表
        """
        return await self._vector_search(
            "content_vector", query_vector, k, source_config_id, source_config_ids, category
        )

    def _is_valid_vector(self, vector: List[float]) -> bool:
        """
        验证向量是否有效（不包含NaN或Inf值）

        Args:
            vector: 待验证的向量

        Returns:
            bool: 向量是否有效
        """
        if not vector:
            return False

        # 转换为numpy数组进行检查
        try:
            np_array = np.array(vector, dtype=np.float32)
            # 检查是否包含NaN或Inf
            return not (np.isnan(np_array).any() or np.isinf(np_array).any())
        except (ValueError, TypeError):
            # 如果转换失败，说明向量包含无效值
            return False

    async def _vector_search(
        self,
        vector_field: str,
        query_vector: List[float],
        k: int,
        source_config_id: Optional[str] = None,
        source_config_ids: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        向量检索（内部方法）

        Args:
            vector_field: 向量字段名
            query_vector: 查询向量
            k: 返回数量
            source_config_id: 信息源ID（单个，向后兼容）
            source_config_ids: 信息源ID列表（支持多源搜索）
            category: 分类

        Returns:
            相似事件列表
        """
        # 验证查询向量是否有效
        if not self._is_valid_vector(query_vector):
            raise ValueError("查询向量包含无效值（NaN或Inf）")

        # 参数兼容处理：优先使用 source_config_ids，如果没有则使用 source_config_id
        if not source_config_ids and source_config_id:
            source_config_ids = [source_config_id]

        knn_query = {
            "field": vector_field,
            "query_vector": query_vector,
            "k": k,
            "num_candidates": k * 10,
        }

        # 添加过滤条件
        filters = []
        if source_config_ids:
            # 单源使用 term 查询，多源使用 terms 查询
            if len(source_config_ids) == 1:
                filters.append(
                    Q("term", source_config_id=source_config_ids[0]))
            else:
                filters.append(Q("terms", source_config_id=source_config_ids))
        if category:
            filters.append(Q("term", category=category))

        # 构建filter
        filter_query = None
        if filters:
            filter_query = Q("bool", must=filters).to_dict()

        # 使用 source_config_id 作为路由键优化查询性能
        # 仅在单源时使用 routing，多源时禁用以支持跨分片查询
        routing = source_config_ids[0] if source_config_ids and len(
            source_config_ids) == 1 else None

        # 使用vector_search方法
        return await self.es_client.vector_search(
            index=self.INDEX_NAME,
            field=vector_field,
            vector=query_vector,
            size=k,
            filter_query=filter_query,
            routing=routing,
        )

    async def search_by_time_range(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        source_config_id: Optional[str] = None,
        size: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        按时间范围搜索

        Args:
            start_time: 开始时间（可选）
            end_time: 结束时间（可选）
            source_config_id: 信息源ID（可选）
            size: 返回数量

        Returns:
            事件列表
        """
        s = Search(using=self.es_client, index=self.INDEX_NAME)

        # 时间范围过滤
        time_filters = []
        if start_time:
            time_filters.append(Q("range", start_time={"gte": start_time}))
        if end_time:
            time_filters.append(Q("range", end_time={"lte": end_time}))

        if time_filters:
            s = s.filter("bool", must=time_filters)

        if source_config_id:
            s = s.filter("term", source_config_id=source_config_id)

        s = s.sort("-created_time")  # 按创建时间倒序
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
        return response

    async def search_by_entities(
        self, entity_ids: List[str], source_config_id: Optional[str] = None, size: int = 100
    ) -> List[Dict[str, Any]]:
        """
        按关联实体搜索事件

        Args:
            entity_ids: 实体ID列表
            source_config_id: 信息源ID（可选）
            size: 返回数量

        Returns:
            事件列表
        """
        s = Search(using=self.es_client, index=self.INDEX_NAME)

        # entity_ids 是数组字段，使用 terms 查询
        s = s.filter("terms", entity_ids=entity_ids)

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
        return response
