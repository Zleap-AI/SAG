"""
Elasticsearch 索引初始化脚本

创建所有 ES 索引并验证
"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sag.core.storage.documents import REGISTERED_DOCUMENTS
from sag.core.storage.elasticsearch import ElasticsearchClient
from sag.utils import get_logger

logger = get_logger("scripts.init_es_indices")


# 输出辅助函数
def print_header(text: str) -> None:
    """打印标题"""
    print("\n" + "=" * 70)
    print(f"  {text}")
    print("=" * 70)


def print_success(text: str) -> None:
    """打印成功信息"""
    print(f"  ✓ {text}")


def print_info(text: str) -> None:
    """打印普通信息"""
    print(f"  • {text}")


def print_warning(text: str) -> None:
    """打印警告信息"""
    print(f"  ⚠️  {text}")


def print_error(text: str) -> None:
    """打印错误信息"""
    print(f"  ✗ {text}")


async def create_indices(es_client: ElasticsearchClient) -> dict[str, str]:
    """
    创建所有 ES 索引

    如果索引已存在，则跳过创建（幂等性）

    Returns:
        dict: 索引名 -> 状态 ("created", "skipped", "failed")
    """
    print_header("创建索引")
    logger.info("开始创建索引...")

    results = {}

    for document_cls in REGISTERED_DOCUMENTS:
        try:
            # 从 Document 类获取索引配置
            index_name = document_cls.Index.name
            mapping = document_cls._doc_type.mapping.to_dict()
            settings = getattr(document_cls.Index, "settings", {})
        except AttributeError as e:
            print_error(f"Document 类 {document_cls.__name__} 配置获取失败: {e}")
            logger.error(f"Document 类 {document_cls.__name__} 缺少必要属性: {e}")
            results[document_cls.__name__] = "failed"
            continue

        # 检查索引是否已存在
        exists = await es_client.index_exists(index_name)

        if exists:
            print_info(f"{index_name}: 已存在，跳过创建")
            logger.info(f"  - {index_name}: 已存在，跳过创建")
            results[index_name] = "skipped"
            continue

        # 创建索引
        try:
            print_info(f"{index_name}: 开始创建...")
            logger.info(f"  - {index_name}: 不存在，开始创建")
            await es_client.create_index(
                index=index_name,
                mappings=mapping,
                settings=settings
            )
            print_success(f"{index_name}: 创建成功")
            logger.info(f"  ✓ 索引创建成功: {index_name}")
            results[index_name] = "created"
        except Exception as e:
            print_error(f"{index_name}: 创建失败 - {e}")
            logger.error(f"  ✗ 索引创建失败: {index_name} - {e}")
            results[index_name] = "failed"

    return results


async def verify_indices(es_client: ElasticsearchClient) -> bool:
    """
    验证所有索引是否创建成功

    Returns:
        bool: 是否所有索引都验证通过
    """
    print_header("验证索引")
    logger.info("开始验证索引...")

    all_success = True

    for document_cls in REGISTERED_DOCUMENTS:
        try:
            index_name = document_cls.Index.name
        except AttributeError as e:
            print_error(f"Document 类 {document_cls.__name__} 索引名称获取失败: {e}")
            logger.error(f"Document 类 {document_cls.__name__} 缺少 Index.name: {e}")
            all_success = False
            continue

        exists = await es_client.index_exists(index_name)

        if exists:
            print_success(f"{index_name}: 验证通过")
            logger.info(f"✓ {index_name}: 存在")
        else:
            print_error(f"{index_name}: 验证失败，索引不存在")
            logger.error(f"✗ {index_name}: 不存在")
            all_success = False

    return all_success


async def main() -> None:
    """
    主函数
    """
    es_client = None

    try:
        print_header("SAG Elasticsearch 索引初始化")
        logger.info("=" * 60)
        logger.info("SAG Elasticsearch 索引初始化")
        logger.info("=" * 60)

        # 1. 创建 ES 客户端
        print_info("正在连接 Elasticsearch...")
        es_client = ElasticsearchClient()

        # 2. 检查连接
        if not await es_client.check_connection():
            print_error("Elasticsearch 连接失败，请检查配置")
            raise Exception("ES连接失败，请检查配置")

        print_success("Elasticsearch 连接成功")

        # 3. 创建索引
        create_results = await create_indices(es_client)

        # 4. 验证索引
        verify_success = await verify_indices(es_client)

        # 5. 总结
        print_header("操作总结")

        created_count = sum(1 for status in create_results.values() if status == "created")
        skipped_count = sum(1 for status in create_results.values() if status == "skipped")
        failed_count = sum(1 for status in create_results.values() if status == "failed")

        if created_count > 0:
            print_success(f"新创建索引: {created_count} 个")
        if skipped_count > 0:
            print_info(f"跳过索引: {skipped_count} 个（已存在）")
        if failed_count > 0:
            print_error(f"失败索引: {failed_count} 个")

        if verify_success and failed_count == 0:
            print_success("所有索引初始化成功！")
            logger.info("=" * 60)
            logger.info("✓ Elasticsearch 索引初始化成功！")
            logger.info("=" * 60)
        else:
            print_error("部分索引初始化失败，请查看详细信息")
            raise Exception("索引初始化未完全成功")

        print("=" * 70 + "\n")

    except Exception as e:
        print_error(f"索引初始化失败: {e}")
        logger.error(f"Elasticsearch 索引初始化失败: {e}", exc_info=True)
        sys.exit(1)

    finally:
        # 关闭连接
        if es_client:
            await es_client.close()


if __name__ == "__main__":
    asyncio.run(main())
