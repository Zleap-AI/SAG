"""依赖注入

提供通用的依赖项
"""

from typing import AsyncGenerator

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from sag.core.storage.mysql import get_mysql_client
from sag.exceptions import DatabaseError


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库会话
    
    处理 FastAPI 的 HTTPException，不将其视为数据库错误
    """
    client = get_mysql_client()
    session = client.session_factory()
    
    try:
        yield session
        await session.commit()
    except HTTPException:
        # HTTPException 是业务逻辑异常，不需要回滚
        await session.commit()
        raise
    except Exception as e:
        # 其他异常才是真正的数据库错误
        await session.rollback()
        raise DatabaseError(f"数据库操作失败: {e}") from e
    finally:
        await session.close()

