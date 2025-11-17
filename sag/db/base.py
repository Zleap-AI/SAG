"""
数据库基础模块

提供SQLAlchemy Base类和数据库初始化工具
"""

from typing import AsyncIterator, Optional

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from sag.core.config import get_settings
from sag.utils import get_logger

logger = get_logger("db.base")

# 命名约定（用于自动生成约束名称）
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """SQLAlchemy基类"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# 全局引擎和会话工厂
_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def get_engine() -> AsyncEngine:
    """
    获取数据库引擎（单例）

    Returns:
        AsyncEngine实例
    """
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.mysql_url,
            echo=settings.log_level == "DEBUG",
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            pool_recycle=3600,  # 1小时回收连接
            connect_args={"init_command": "SET time_zone='+00:00'"}  # UTC时区
        )
        logger.info(
            "数据库引擎创建完成（UTC时区）",
            extra={"host": settings.mysql_host, "database": settings.mysql_database},
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """
    获取会话工厂（单例）

    Returns:
        async_sessionmaker实例
    """
    global _session_factory
    if _session_factory is None:
        engine = get_engine()
        _session_factory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )
        logger.info("会话工厂创建完成")
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """
    获取数据库会话（用于依赖注入）

    Yields:
        AsyncSession实例

    Example:
        >>> async with get_session() as session:
        ...     result = await session.execute(select(User))
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_database() -> None:
    """
    初始化数据库（创建所有表）
    
    自动根据所有已注册的模型创建表结构
    
    Warning:
        这会删除并重新创建所有表，仅用于开发环境

    Example:
        >>> await init_database()
    """
    # 确保所有模型都已导入注册到 Base.metadata
    from sag.db import models  # noqa: F401
    
    engine = get_engine()

    logger.info("开始创建数据库表...")
    logger.info(f"检测到 {len(Base.metadata.tables)} 个表定义")

    async with engine.begin() as conn:
        # 删除所有表（开发环境）
        await conn.run_sync(Base.metadata.drop_all)
        logger.info("已删除所有旧表")

        # 创建所有表
        await conn.run_sync(Base.metadata.create_all)
        logger.info(f"已创建 {len(Base.metadata.tables)} 个新表")

    logger.info("数据库初始化完成")


async def close_database() -> None:
    """
    关闭数据库连接

    Example:
        >>> await close_database()
    """
    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("数据库连接已关闭")
