"""
重置数据库和Alembic迁移版本

智能重置流程：
1. 删除项目模型关联的表 + alembic_version 表
2. 检查是否存在迁移文件
3. 如果有迁移文件：直接执行迁移
4. 如果没有迁移文件：生成初始迁移
5. 执行迁移并插入默认数据
"""
import asyncio
import sys
import subprocess
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import text
from sag.db import get_session_factory
from sag.db.base import Base
from sag.utils import get_logger

logger = get_logger("reset_database")


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


async def drop_project_and_version_tables():
    """删除项目表和alembic_version表，保护其他表"""
    logger.info("开始删除项目表和版本表...")

    project_tables = get_project_tables()
    logger.info(f"项目定义的表 ({len(project_tables)}个): {sorted(project_tables)}")

    factory = get_session_factory()
    async with factory() as session:
        # 禁用外键检查
        await session.execute(text("SET FOREIGN_KEY_CHECKS = 0"))

        # 获取数据库中所有的表
        result = await session.execute(text("SHOW TABLES"))
        all_tables = {row[0] for row in result.fetchall()}

        logger.info(f"数据库中共有 {len(all_tables)} 个表")

        # 找出需要删除的表
        tables_to_drop = []
        protected_tables = []
        
        for table in all_tables:
            # 删除项目表
            if table in project_tables:
                tables_to_drop.append(table)
                logger.info(f"  准备删除项目表: {table}")
            # 删除alembic_version表
            elif table == 'alembic_version':
                tables_to_drop.append(table)
                logger.info(f"  准备删除版本表: {table}")
            # 保护其他表
            else:
                protected_tables.append(table)
                logger.warning(f"  保护非项目表: {table}")

        if not tables_to_drop:
            logger.info("没有找到需要删除的表")
        else:
            # 删除表
            for table in tables_to_drop:
                logger.info(f"  删除表: {table}")
                await session.execute(text(f"DROP TABLE IF EXISTS `{table}`"))

        # 启用外键检查
        await session.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        await session.commit()

        logger.info(f"✓ 表删除完成！")
        logger.info(f"  - 删除了 {len(tables_to_drop)} 个表")
        logger.info(f"  - 保护了 {len(protected_tables)} 个其他表")


def check_migration_files() -> bool:
    """检查是否存在迁移文件"""
    versions_dir = project_root / "migrations" / "versions"
    if not versions_dir.exists():
        return False
    
    # 检查是否有 .py 文件（排除 __init__.py 和 __pycache__）
    migration_files = [
        f for f in versions_dir.glob("*.py") 
        if f.name != "__init__.py"
    ]
    
    return len(migration_files) > 0


def generate_initial_migration():
    """生成初始迁移"""
    logger.info("生成初始迁移...")
    try:
        result = subprocess.run(
            ["alembic", "revision", "--autogenerate", "-m", "initial migration"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True
        )
        logger.info("✓ 初始迁移已生成")
        if result.stdout:
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    logger.info(f"  {line}")
    except subprocess.CalledProcessError as e:
        logger.error(f"生成迁移失败: {e.stderr}")
        raise


def run_alembic_upgrade():
    """执行 Alembic 迁移"""
    logger.info("执行数据库迁移...")
    try:
        result = subprocess.run(
            ["alembic", "upgrade", "head"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True
        )
        logger.info("✓ 数据库迁移完成")
        if result.stdout:
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    logger.info(f"  {line}")
    except subprocess.CalledProcessError as e:
        logger.error(f"迁移失败: {e.stderr}")
        raise


async def main():
    try:
        logger.info("=" * 60)
        logger.info("重置数据库和Alembic版本")
        logger.info("=" * 60)
        
        # 1. 删除项目表和版本表
        logger.info("\n[1/4] 删除项目表和版本表...")
        await drop_project_and_version_tables()
        
        # 2. 检查是否存在迁移文件
        logger.info("\n[2/4] 检查迁移文件...")
        has_migrations = check_migration_files()
        
        if has_migrations:
            logger.info("✓ 检测到现有迁移文件，将直接执行迁移")
        else:
            logger.info("⚠️  未检测到迁移文件，将生成初始迁移")
            generate_initial_migration()
        
        # 3. 执行迁移
        logger.info("\n[3/4] 执行数据库迁移...")
        run_alembic_upgrade()
        
        # 4. 插入默认数据
        logger.info("\n[4/4] 插入默认数据...")
        from scripts.init_database import insert_default_entity_types
        await insert_default_entity_types()
        
        logger.info("=" * 60)
        logger.info("✓ 重置完成！数据库已就绪")
        logger.info("\n验证命令:")
        logger.info("  alembic current")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"重置失败: {e}", exc_info=True)
        sys.exit(1)
    finally:
        from sag.db.base import close_database
        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
