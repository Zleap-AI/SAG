"""
安全重建数据库脚本

只删除项目相关表并重新创建，保护其他表和alembic版本信息

功能：
- 只删除项目定义的ORM表（12个表）
- 保护非项目表（如其他应用的表）
- 保护alembic_version版本管理表
- 重新创建项目表结构
- 插入默认数据

安全性：
- 使用Base.metadata.tables自动识别项目表
- 明确区分项目表和非项目表
- 详细日志显示操作和保护情况
"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import text

from sag.db import get_engine, get_session_factory, init_database
from sag.db.base import Base  # 导入Base获取项目表定义
from sag.utils import get_logger

logger = get_logger("scripts.recreate_database")


def get_project_tables() -> set[str]:
    """获取项目中所有ORM定义的表"""
    # 确保所有模型都被导入
    from sag.db import models
    
    project_tables = set()
    for table_name in Base.metadata.tables.keys():
        # 提取表名（处理可能的schema前缀）
        if '.' in table_name:
            project_tables.add(table_name.split('.')[-1])
        else:
            project_tables.add(table_name)
    
    return project_tables


async def drop_project_tables() -> None:
    """
    只删除项目相关的表，保护其他表和alembic版本信息
    """
    logger.info("开始安全删除项目表...")

    project_tables = get_project_tables()
    logger.info(f"项目定义的表: {sorted(project_tables)}")

    factory = get_session_factory()
    async with factory() as session:
        # 禁用外键检查
        await session.execute(text("SET FOREIGN_KEY_CHECKS = 0"))

        # 获取数据库中所有的表
        result = await session.execute(text("SHOW TABLES"))
        all_tables = {row[0] for row in result.fetchall()}

        logger.info(f"数据库中共有 {len(all_tables)} 个表")

        # 找出需要删除的项目表（存在于数据库中的项目表）
        tables_to_drop = []
        protected_tables = []
        
        for table in all_tables:
            if table in project_tables:
                tables_to_drop.append(table)
                logger.info(f"  准备删除项目表: {table}")
            else:
                protected_tables.append(table)
                if 'alembic' in table.lower():
                    logger.info(f"  保护版本管理表: {table}")
                else:
                    logger.warning(f"  保护非项目表: {table}")

        if not tables_to_drop:
            logger.info("没有找到需要删除的项目表")
        else:
            # 删除项目表
            for table in tables_to_drop:
                logger.info(f"  删除项目表: {table}")
                await session.execute(text(f"DROP TABLE IF EXISTS `{table}`"))

        # 启用外键检查
        await session.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

        await session.commit()

        logger.info(f"✓ 表删除完成！")
        logger.info(f"  - 删除了 {len(tables_to_drop)} 个项目表")
        logger.info(f"  - 保护了 {len(protected_tables)} 个其他表")


async def main() -> None:
    """
    主函数
    """
    try:
        logger.info("=" * 60)
        logger.info("SAG 数据库重建")
        logger.info("=" * 60)

        # 1. 安全删除项目表
        await drop_project_tables()

        # 2. 初始化数据库（创建所有表）
        await init_database()

        # 3. 插入默认实体类型（由init_database调用）
        from scripts.init_database import insert_default_entity_types, verify_database

        await insert_default_entity_types()

        # 4. 验证数据库
        await verify_database()

        logger.info("=" * 60)
        logger.info("✓ 数据库重建成功！")
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"数据库重建失败: {e}", exc_info=True)
        sys.exit(1)

    finally:
        # 关闭数据库连接
        from sag.db.base import close_database

        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
