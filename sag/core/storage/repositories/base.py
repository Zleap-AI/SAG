"""
Elasticsearch Repository 基类
"""

from abc import ABC
from typing import Any, Dict, List, Optional

from elasticsearch import AsyncElasticsearch


class BaseRepository(ABC):
    """Repository 基类"""

    def __init__(self, es_client: AsyncElasticsearch):
        """
        初始化 Repository

        Args:
            es_client: Elasticsearch 异步客户端
        """
        self.es_client = es_client

    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any], routing: Optional[str] = None
    ) -> str:
        """
        索引单个文档

        Args:
            index: 索引名称
            doc_id: 文档ID
            document: 文档内容
            routing: 路由键（可选）

        Returns:
            文档ID
        """
        response = await self.es_client.index(
            index=index, id=doc_id, document=document, routing=routing
        )
        return response["_id"]

    async def get_document(self, index: str, doc_id: str) -> Optional[Dict[str, Any]]:
        """
        获取单个文档

        Args:
            index: 索引名称
            doc_id: 文档ID

        Returns:
            文档内容，不存在返回None
        """
        try:
            response = await self.es_client.get(index=index, id=doc_id)
            return response["_source"]
        except Exception:
            return None

    async def delete_document(self, index: str, doc_id: str) -> bool:
        """
        删除单个文档

        Args:
            index: 索引名称
            doc_id: 文档ID

        Returns:
            是否成功删除
        """
        try:
            await self.es_client.delete(index=index, id=doc_id)
            return True
        except Exception:
            return False

    async def bulk_index(
        self, index: str, documents: List[Dict[str, Any]], routing: Optional[str] = None
    ) -> Dict[str, int]:
        """
        批量索引文档

        Args:
            index: 索引名称
            documents: 文档列表，每个文档需包含 _id 字段
            routing: 路由键（可选）

        Returns:
            统计信息：{"success": 10, "failed": 0}
        """
        from elasticsearch.helpers import async_bulk

        actions = [
            {
                "_index": index,
                "_id": doc.get("_id") or doc.get("id"),
                "_source": {k: v for k, v in doc.items() if k not in ["_id", "id"]},
                **({"_routing": routing} if routing else {}),
            }
            for doc in documents
        ]

        success, failed = await async_bulk(self.es_client, actions, raise_on_error=False)

        return {"success": success, "failed": len(failed) if isinstance(failed, list) else 0}
