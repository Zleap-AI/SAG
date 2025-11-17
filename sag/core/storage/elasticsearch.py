"""
Elasticsearch 存储客户端

支持向量检索和全文检索
"""

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

from elasticsearch import AsyncElasticsearch
from elasticsearch.exceptions import NotFoundError

from sag.core.config import get_settings
from sag.exceptions import StorageError
from sag.utils import get_logger

logger = get_logger("storage.elasticsearch")


@dataclass
class ESConfig:
    """ES配置类"""

    hosts: Union[str, List[str]]
    username: Optional[str] = None
    password: Optional[str] = None
    scheme: str = "http"
    timeout: int = 30
    max_connections: int = 10
    max_retries: int = 3
    verify_certs: bool = False

    @classmethod
    def from_env(cls) -> "ESConfig":
        """从环境变量创建配置"""
        hosts = os.getenv("ES_HOSTS", os.getenv("ES_HOST", "localhost:9200"))

        # 处理多主机配置
        if isinstance(hosts, str) and "," in hosts:
            hosts = [host.strip() for host in hosts.split(",")]

        return cls(
            hosts=hosts,
            username=os.getenv("ES_USERNAME", "elastic"),
            password=os.getenv("ELASTIC_PASSWORD"),
            scheme=os.getenv("ES_SCHEME", "http"),
            timeout=int(os.getenv("ES_TIMEOUT", "30")),
            max_connections=int(os.getenv("ES_MAX_CONNECTIONS", "10")),
            max_retries=int(os.getenv("ES_MAX_RETRIES", "3")),
            verify_certs=os.getenv("ES_VERIFY_CERTS", "false").lower() == "true",
        )


class ElasticsearchClient:
    """Elasticsearch异步客户端"""

    def __init__(
        self,
        hosts: Optional[List[str]] = None,
        config: Optional[ESConfig] = None,
        **kwargs: Any,
    ) -> None:
        """
        初始化Elasticsearch客户端

        Args:
            hosts: ES主机列表（可选，优先级最高，向下兼容）
            config: ES配置对象（可选，使用ESConfig配置）
            **kwargs: 其他参数
        """
        settings = get_settings()

        # 优先级: hosts参数 > config对象 > 配置文件
        if hosts:
            # 向下兼容：优先使用传入的hosts参数
            self.hosts = hosts
            client_config = {
                "hosts": self.hosts,
                **kwargs,
            }
        elif config:
            # 使用ESConfig配置
            raw_hosts = config.hosts if isinstance(config.hosts, list) else [config.hosts]

            # 将hosts转换为完整的URL格式（包含scheme）
            self.hosts = []
            for host in raw_hosts:
                if not host.startswith("http://") and not host.startswith("https://"):
                    # 没有scheme，添加scheme
                    self.hosts.append(f"{config.scheme}://{host}")
                else:
                    self.hosts.append(host)

            # 构建客户端配置
            client_config = {
                **kwargs,
            }

            # 如果提供了认证信息，将认证嵌入到URL中
            if config.username and config.password:
                hosts_with_auth = []
                for host in self.hosts:
                    # 解析URL并添加认证信息
                    from urllib.parse import urlparse
                    parsed = urlparse(host)
                    auth_url = f"{parsed.scheme}://{config.username}:{config.password}@{parsed.netloc}{parsed.path}"
                    hosts_with_auth.append(auth_url)
                client_config["hosts"] = hosts_with_auth
            else:
                client_config["hosts"] = self.hosts

            # 添加其他配置
            client_config["request_timeout"] = config.timeout
            client_config["max_retries"] = config.max_retries
            client_config["verify_certs"] = config.verify_certs
        else:
            # 使用配置文件中的es_url
            self.hosts = settings.es_url
            # 构建包含认证信息的完整URL或使用basic_auth
            if settings.es_username and settings.es_password:
                # 从URL解析并添加认证信息
                from urllib.parse import urlparse
                parsed = urlparse(settings.es_url)
                # 重建带认证的URL
                auth_url = f"{parsed.scheme}://{settings.es_username}:{settings.es_password}@{parsed.netloc}{parsed.path}"
                client_config = {
                    "hosts": [auth_url],
                    **kwargs,
                }
            else:
                client_config = {
                    "hosts": [settings.es_url],
                    **kwargs,
                }

        # 创建客户端
        self.client = AsyncElasticsearch(**client_config)

        logger.info("Elasticsearch客户端初始化完成", extra={"hosts": self.hosts})

    async def index_document(
        self,
        index: str,
        document: Dict[str, Any],
        doc_id: Optional[str] = None,
        routing: Optional[str] = None,
    ) -> str:
        """
        索引文档

        Args:
            index: 索引名称
            document: 文档内容
            doc_id: 文档ID（可选）
            routing: 路由键（可选，用于指定分片）

        Returns:
            文档ID

        Raises:
            StorageError: 索引失败
        """
        try:
            response = await self.client.index(
                index=index,
                document=document,
                id=doc_id,
                routing=routing,
            )
            return response["_id"]
        except Exception as e:
            logger.error(f"索引文档失败: {e}", exc_info=True)
            raise StorageError(f"索引文档失败: {e}") from e

    async def get_document(
        self,
        index: str,
        doc_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        获取文档

        Args:
            index: 索引名称
            doc_id: 文档ID

        Returns:
            文档内容，不存在返回None
        """
        try:
            response = await self.client.get(index=index, id=doc_id)
            return response["_source"]
        except NotFoundError:
            return None
        except Exception as e:
            logger.error(f"获取文档失败: {e}", exc_info=True)
            raise StorageError(f"获取文档失败: {e}") from e

    async def delete_document(
        self,
        index: str,
        doc_id: str,
    ) -> bool:
        """
        删除文档

        Args:
            index: 索引名称
            doc_id: 文档ID

        Returns:
            删除成功返回True
        """
        try:
            await self.client.delete(index=index, id=doc_id)
            return True
        except NotFoundError:
            return False
        except Exception as e:
            logger.error(f"删除文档失败: {e}", exc_info=True)
            raise StorageError(f"删除文档失败: {e}") from e

    async def update_document(
        self,
        index: str,
        doc_id: str,
        update_data: Dict[str, Any],
    ) -> bool:
        """
        部分更新文档

        Args:
            index: 索引名称
            doc_id: 文档ID
            update_data: 要更新的数据

        Returns:
            更新成功返回True

        Raises:
            StorageError: 更新失败
        """
        try:
            await self.client.update(
                index=index,
                id=doc_id,
                doc=update_data,
            )
            logger.info(f"文档 {doc_id} 更新成功")
            return True
        except NotFoundError:
            logger.warning(f"文档 {doc_id} 不存在")
            return False
        except Exception as e:
            logger.error(f"更新文档失败: {e}", exc_info=True)
            raise StorageError(f"更新文档失败: {e}") from e

    async def count_documents(
        self,
        index: str,
        query: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        统计文档数量

        Args:
            index: 索引名称
            query: 查询条件，默认匹配所有文档

        Returns:
            文档数量

        Raises:
            StorageError: 统计失败
        """
        try:
            if query is None:
                query = {"match_all": {}}

            response = await self.client.count(index=index, query=query)
            return response["count"]
        except Exception as e:
            logger.error(f"统计文档数量失败: {e}", exc_info=True)
            raise StorageError(f"统计文档数量失败: {e}") from e

    async def get_mapping(self, index: str) -> Dict[str, Any]:
        """
        获取索引映射

        Args:
            index: 索引名称

        Returns:
            索引映射信息

        Raises:
            StorageError: 获取失败
        """
        try:
            response = await self.client.indices.get_mapping(index=index)
            return response.get(index, {}).get("mappings", {})
        except Exception as e:
            logger.error(f"获取索引映射失败: {e}", exc_info=True)
            raise StorageError(f"获取索引映射失败: {e}") from e

    async def search(
        self,
        index: str,
        query: Dict[str, Any],
        size: int = 10,
        from_: int = 0,
        return_full_response: bool = False,
        routing: Optional[str] = None,
        **kwargs: Any,
    ) -> Union[List[Dict[str, Any]], Dict[str, Any]]:
        """
        搜索文档

        Args:
            index: 索引名称
            query: 查询DSL
            size: 返回数量
            from_: 起始位置
            return_full_response: 是否返回完整响应（包含total、max_score等）
            routing: 路由键（可选，用于指定分片）
            **kwargs: 其他参数

        Returns:
            return_full_response=False: 文档列表（向下兼容）
            return_full_response=True: 完整响应字典 {total, max_score, hits}
        """
        try:
            search_params = {
                "index": index,
                "query": query,
                "size": size,
                "from_": from_,
                **kwargs,
            }
            if routing:
                search_params["routing"] = routing

            response = await self.client.search(**search_params)

            if return_full_response:
                # 返回完整信息
                hits = response.get("hits", {})
                return {
                    "total": hits.get("total", {}).get("value", 0),
                    "max_score": hits.get("max_score", 0),
                    "hits": [
                        {
                            "id": hit.get("_id"),
                            "score": hit.get("_score"),
                            "source": hit.get("_source"),
                            "index": hit.get("_index"),
                        }
                        for hit in hits.get("hits", [])
                    ],
                }
            else:
                # 向下兼容：只返回文档列表
                return [hit["_source"] for hit in response["hits"]["hits"]]
        except Exception as e:
            logger.error(f"搜索文档失败: {e}", exc_info=True)
            raise StorageError(f"搜索文档失败: {e}") from e

    async def vector_search(
        self,
        index: str,
        field: str,
        vector: List[float],
        size: int = 10,
        filter_query: Optional[Dict[str, Any]] = None,
        routing: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        向量检索

        Args:
            index: 索引名称
            field: 向量字段名
            vector: 查询向量
            size: 返回数量
            filter_query: 过滤条件
            routing: 路由键（可选，用于指定分片）

        Returns:
            相似文档列表（包含_score字段）
        """
        try:
            knn_query: Dict[str, Any] = {
                "field": field,
                "query_vector": vector,
                "k": size,
                "num_candidates": size * 10,
            }

            if filter_query:
                knn_query["filter"] = filter_query

            search_params = {
                "index": index,
                "knn": knn_query,
                "size": size,
            }
            if routing:
                search_params["routing"] = routing

            response = await self.client.search(**search_params)

            return [
                {**hit["_source"], "_score": hit["_score"]}
                for hit in response["hits"]["hits"]
            ]
        except Exception as e:
            logger.error(f"向量检索失败: {e}", exc_info=True)
            raise StorageError(f"向量检索失败: {e}") from e

    async def bulk_index(
        self,
        index: str,
        documents: List[Dict[str, Any]],
        return_details: bool = False,
        routing: Optional[str] = None,
    ) -> Union[int, Dict[str, Any]]:
        """
        批量索引文档

        Args:
            index: 索引名称
            documents: 文档列表
            return_details: 是否返回详细信息（包含错误列表）
            routing: 路由键（可选，用于指定分片）

        Returns:
            return_details=False: 成功索引的文档数量（向下兼容）
            return_details=True: 详细结果字典 {success, total, success_count, error_count, errors}
        """
        from elasticsearch.helpers import async_bulk

        try:
            if not documents:
                if return_details:
                    return {
                        "success": True,
                        "total": 0,
                        "success_count": 0,
                        "error_count": 0,
                        "errors": [],
                    }
                return 0

            actions = [
                {
                    "_index": index,
                    "_source": doc,
                    "_id": doc.get("id"),
                    **({"_routing": routing} if routing else {}),
                }
                for doc in documents
            ]

            success_count, errors = await async_bulk(
                self.client, actions, raise_on_error=False, stats_only=False
            )

            error_count = len(errors) if isinstance(errors, list) else 0
            logger.info(f"批量索引完成: 成功{success_count}, 失败{error_count}")

            if return_details:
                # 返回详细信息
                error_list = []
                if isinstance(errors, list):
                    for error in errors:
                        if isinstance(error, dict):
                            error_list.append(
                                {
                                    "id": error.get("index", {}).get("_id"),
                                    "error": error.get("index", {}).get(
                                        "error", "Unknown error"
                                    ),
                                }
                            )

                return {
                    "success": error_count == 0,
                    "total": len(documents),
                    "success_count": success_count,
                    "error_count": error_count,
                    "errors": error_list,
                }
            else:
                # 向下兼容：只返回成功数量
                return success_count
        except Exception as e:
            logger.error(f"批量索引失败: {e}", exc_info=True)
            if return_details:
                return {
                    "success": False,
                    "total": len(documents),
                    "success_count": 0,
                    "error_count": len(documents),
                    "errors": [{"error": str(e)}],
                }
            raise StorageError(f"批量索引失败: {e}") from e

    async def create_index(
        self,
        index: str,
        mappings: Dict[str, Any],
        settings: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        创建索引

        Args:
            index: 索引名称
            mappings: 映射定义
            settings: 索引设置

        Returns:
            创建成功返回True
        """
        try:
            # Elasticsearch 8.x 不再使用 body 参数
            await self.client.indices.create(
                index=index,
                mappings=mappings,
                settings=settings or {}
            )
            logger.info(f"索引创建成功: {index}")
            return True
        except Exception as e:
            logger.error(f"创建索引失败: {e}", exc_info=True)
            raise StorageError(f"创建索引失败: {e}") from e

    async def delete_index(self, index: str) -> bool:
        """
        删除索引

        Args:
            index: 索引名称

        Returns:
            删除成功返回True
        """
        try:
            await self.client.indices.delete(index=index)
            logger.info(f"索引删除成功: {index}")
            return True
        except NotFoundError:
            return False
        except Exception as e:
            logger.error(f"删除索引失败: {e}", exc_info=True)
            raise StorageError(f"删除索引失败: {e}") from e

    async def index_exists(self, index: str) -> bool:
        """
        检查索引是否存在

        Args:
            index: 索引名称

        Returns:
            存在返回True
        """
        return await self.client.indices.exists(index=index)

    async def close(self) -> None:
        """关闭客户端连接"""
        await self.client.close()
        logger.info("Elasticsearch连接已关闭")

    async def ping(self) -> bool:
        """
        测试连接

        Returns:
            连接成功返回True
        """
        try:
            return await self.client.ping()
        except Exception as e:
            logger.error(f"ES连接测试失败: {e}")
            return False

    async def check_connection(self) -> bool:
        """
        检查ES连接状态并获取版本信息

        Returns:
            连接是否正常
        """
        try:
            info = await self.client.info()
            version = info.get("version", {}).get("number", "unknown")
            logger.info(f"ES连接正常, 版本: {version}")
            return True
        except Exception as e:
            logger.error(f"ES连接检查失败: {e}")
            return False


# 全局客户端实例（单例）
_es_client: Optional[ElasticsearchClient] = None


def get_es_client() -> ElasticsearchClient:
    """
    获取Elasticsearch客户端单例

    Returns:
        ElasticsearchClient实例
    """
    global _es_client
    if _es_client is None:
        _es_client = ElasticsearchClient()
    return _es_client


async def close_es_client() -> None:
    """关闭全局ES客户端"""
    global _es_client
    if _es_client is not None:
        await _es_client.close()
        _es_client = None
