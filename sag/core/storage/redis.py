"""
Redis 缓存客户端

支持字符串、哈希、集合等数据结构
"""

import json
from typing import Any, List, Optional

import redis.asyncio as aioredis

from sag.core.config import get_settings
from sag.exceptions import CacheError
from sag.utils import get_logger

logger = get_logger("storage.redis")


class RedisClient:
    """Redis异步客户端"""

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        password: Optional[str] = None,
        db: Optional[int] = None,
        **kwargs: Any,
    ) -> None:
        """
        初始化Redis客户端

        Args:
            host: Redis主机
            port: Redis端口
            password: Redis密码
            db: 数据库编号
            **kwargs: 其他参数
        """
        settings = get_settings()

        self.host = host or settings.redis_host
        self.port = port or settings.redis_port
        self.password = password or settings.redis_password
        self.db = db or settings.redis_db

        # 构建连接URL
        if self.password:
            url = f"redis://:{self.password}@{self.host}:{self.port}/{self.db}"
        else:
            url = f"redis://{self.host}:{self.port}/{self.db}"

        # 创建客户端
        self.client = aioredis.from_url(
            url,
            encoding="utf-8",
            decode_responses=True,
            **kwargs,
        )

        logger.info(
            "Redis客户端初始化完成",
            extra={"host": self.host, "port": self.port, "db": self.db},
        )

    async def get(self, key: str) -> Optional[Any]:
        """
        获取缓存值

        Args:
            key: 键

        Returns:
            值（JSON反序列化），不存在返回None
        """
        try:
            value = await self.client.get(key)
            if value is None:
                return None
            return json.loads(value)
        except json.JSONDecodeError:
            # 如果不是JSON，直接返回字符串
            return value
        except Exception as e:
            logger.error(f"获取缓存失败: {e}", exc_info=True)
            raise CacheError(f"获取缓存失败: {e}") from e

    async def set(
        self,
        key: str,
        value: Any,
        expire: Optional[int] = None,
    ) -> bool:
        """
        设置缓存值

        Args:
            key: 键
            value: 值（自动JSON序列化）
            expire: 过期时间（秒）

        Returns:
            设置成功返回True
        """
        try:
            # JSON序列化
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)

            await self.client.set(key, value, ex=expire)
            return True
        except Exception as e:
            logger.error(f"设置缓存失败: {e}", exc_info=True)
            raise CacheError(f"设置缓存失败: {e}") from e

    async def delete(self, key: str) -> bool:
        """
        删除缓存

        Args:
            key: 键

        Returns:
            删除成功返回True
        """
        try:
            result = await self.client.delete(key)
            return result > 0
        except Exception as e:
            logger.error(f"删除缓存失败: {e}", exc_info=True)
            raise CacheError(f"删除缓存失败: {e}") from e

    async def exists(self, key: str) -> bool:
        """
        检查键是否存在

        Args:
            key: 键

        Returns:
            存在返回True
        """
        try:
            return bool(await self.client.exists(key))
        except Exception as e:
            logger.error(f"检查缓存存在失败: {e}", exc_info=True)
            return False

    async def expire(self, key: str, seconds: int) -> bool:
        """
        设置过期时间

        Args:
            key: 键
            seconds: 秒数

        Returns:
            设置成功返回True
        """
        try:
            return bool(await self.client.expire(key, seconds))
        except Exception as e:
            logger.error(f"设置过期时间失败: {e}", exc_info=True)
            return False

    async def ttl(self, key: str) -> int:
        """
        获取剩余过期时间

        Args:
            key: 键

        Returns:
            剩余秒数，-1表示永不过期，-2表示不存在
        """
        try:
            return await self.client.ttl(key)
        except Exception as e:
            logger.error(f"获取TTL失败: {e}", exc_info=True)
            return -2

    async def incr(self, key: str, amount: int = 1) -> int:
        """
        递增计数器

        Args:
            key: 键
            amount: 增量

        Returns:
            递增后的值
        """
        try:
            return await self.client.incrby(key, amount)
        except Exception as e:
            logger.error(f"递增计数器失败: {e}", exc_info=True)
            raise CacheError(f"递增计数器失败: {e}") from e

    async def hget(self, name: str, key: str) -> Optional[Any]:
        """
        获取哈希字段值

        Args:
            name: 哈希名称
            key: 字段键

        Returns:
            字段值
        """
        try:
            value = await self.client.hget(name, key)
            if value is None:
                return None
            return json.loads(value)
        except json.JSONDecodeError:
            return value
        except Exception as e:
            logger.error(f"获取哈希字段失败: {e}", exc_info=True)
            raise CacheError(f"获取哈希字段失败: {e}") from e

    async def hset(
        self,
        name: str,
        key: str,
        value: Any,
    ) -> bool:
        """
        设置哈希字段值

        Args:
            name: 哈希名称
            key: 字段键
            value: 字段值

        Returns:
            设置成功返回True
        """
        try:
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            await self.client.hset(name, key, value)
            return True
        except Exception as e:
            logger.error(f"设置哈希字段失败: {e}", exc_info=True)
            raise CacheError(f"设置哈希字段失败: {e}") from e

    async def hdel(self, name: str, *keys: str) -> int:
        """
        删除哈希字段

        Args:
            name: 哈希名称
            *keys: 字段键列表

        Returns:
            删除的字段数量
        """
        try:
            return await self.client.hdel(name, *keys)
        except Exception as e:
            logger.error(f"删除哈希字段失败: {e}", exc_info=True)
            raise CacheError(f"删除哈希字段失败: {e}") from e

    async def hgetall(self, name: str) -> dict:
        """
        获取哈希所有字段

        Args:
            name: 哈希名称

        Returns:
            字段字典
        """
        try:
            return await self.client.hgetall(name)
        except Exception as e:
            logger.error(f"获取哈希所有字段失败: {e}", exc_info=True)
            raise CacheError(f"获取哈希所有字段失败: {e}") from e

    async def sadd(self, name: str, *values: Any) -> int:
        """
        添加集合成员

        Args:
            name: 集合名称
            *values: 成员值列表

        Returns:
            添加的成员数量
        """
        try:
            return await self.client.sadd(name, *values)
        except Exception as e:
            logger.error(f"添加集合成员失败: {e}", exc_info=True)
            raise CacheError(f"添加集合成员失败: {e}") from e

    async def smembers(self, name: str) -> List[str]:
        """
        获取集合所有成员

        Args:
            name: 集合名称

        Returns:
            成员列表
        """
        try:
            members = await self.client.smembers(name)
            return list(members)
        except Exception as e:
            logger.error(f"获取集合成员失败: {e}", exc_info=True)
            raise CacheError(f"获取集合成员失败: {e}") from e

    async def sismember(self, name: str, value: Any) -> bool:
        """
        检查是否是集合成员

        Args:
            name: 集合名称
            value: 成员值

        Returns:
            是成员返回True
        """
        try:
            return bool(await self.client.sismember(name, value))
        except Exception as e:
            logger.error(f"检查集合成员失败: {e}", exc_info=True)
            return False

    async def close(self) -> None:
        """关闭Redis连接"""
        await self.client.close()
        logger.info("Redis连接已关闭")

    async def ping(self) -> bool:
        """
        测试连接

        Returns:
            连接成功返回True
        """
        try:
            return await self.client.ping()
        except Exception as e:
            logger.error(f"Redis连接测试失败: {e}")
            return False


# 全局客户端实例（单例）
_redis_client: Optional[RedisClient] = None


def get_redis_client() -> RedisClient:
    """
    获取Redis客户端单例

    Returns:
        RedisClient实例
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = RedisClient()
    return _redis_client


async def close_redis_client() -> None:
    """关闭全局Redis客户端"""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.close()
        _redis_client = None
