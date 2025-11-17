"""
MySQL 存储客户端

使用SQLAlchemy 2.0异步API
"""

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy import text

from sag.core.config import get_settings
from sag.exceptions import DatabaseError
from sag.utils import get_logger

logger = get_logger("storage.mysql")


class MySQLClient:
    """MySQL异步客户端"""

    def __init__(
        self,
        database_url: Optional[str] = None,
        pool_size: Optional[int] = None,
        max_overflow: Optional[int] = None,
        pool_recycle: Optional[int] = None,
        echo: bool = False,
    ) -> None:
        """
        初始化MySQL客户端

        Args:
            database_url: 数据库连接URL
            pool_size: 连接池大小
            max_overflow: 连接池最大溢出
            pool_recycle: 连接回收时间（秒）
            echo: 是否打印SQL语句
        """
        settings = get_settings()

        self.database_url = database_url or settings.mysql_url
        self.pool_size = pool_size or settings.db_pool_size
        self.max_overflow = max_overflow or settings.db_max_overflow
        self.pool_recycle = pool_recycle or settings.db_pool_recycle

        # 创建异步引擎
        self.engine: AsyncEngine = create_async_engine(
            self.database_url,
            pool_size=self.pool_size,
            max_overflow=self.max_overflow,
            pool_recycle=self.pool_recycle,
            pool_pre_ping=True,  # 连接前测试
            echo=echo,
        )

        # 创建会话工厂
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )

        logger.info(
            "MySQL客户端初始化完成",
            extra={
                "pool_size": self.pool_size,
                "max_overflow": self.max_overflow,
            },
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """
        获取数据库会话（上下文管理器）

        Yields:
            AsyncSession实例

        Example:
            >>> async with mysql_client.session() as session:
            ...     result = await session.execute(select(User))
            ...     users = result.scalars().all()
        """
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.error(f"数据库操作失败: {e}", exc_info=True)
                raise DatabaseError(f"数据库操作失败: {e}") from e

    async def close(self) -> None:
        """关闭数据库连接池"""
        await self.engine.dispose()
        logger.info("MySQL连接池已关闭")

    async def ping(self) -> bool:
        """
        测试数据库连接

        Returns:
            连接成功返回True，否则返回False
        """
        try:
            async with self.session() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception as e:
            logger.error(f"数据库连接测试失败: {e}")
            return False


def create_mysql_client(
    database_url: Optional[str] = None,
    **kwargs: Any,
) -> MySQLClient:
    """
    创建MySQL客户端实例

    Args:
        database_url: 数据库连接URL
        **kwargs: 其他参数

    Returns:
        MySQLClient实例
    """
    return MySQLClient(database_url=database_url, **kwargs)


# 全局客户端实例（单例）
_mysql_client: Optional[MySQLClient] = None


def get_mysql_client() -> MySQLClient:
    """
    获取MySQL客户端单例

    Returns:
        MySQLClient实例
    """
    global _mysql_client
    if _mysql_client is None:
        _mysql_client = create_mysql_client()
    return _mysql_client


async def close_mysql_client() -> None:
    """关闭全局MySQL客户端"""
    global _mysql_client
    if _mysql_client is not None:
        await _mysql_client.close()
        _mysql_client = None
