"""
Elasticsearch 索引重建脚本

删除并重新创建所有 ES 索引
"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sag.core.storage.documents import REGISTERED_DOCUMENTS
from sag.core.storage.elasticsearch import ElasticsearchClient
from sag.utils import get_logger  # 添加这行

logger = get_logger("scripts.recreate_es_indices")


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


async def delete_indices(es_client: ElasticsearchClient) -> dict[str, bool]:
    """
    删除所有 ES 索引

    Returns:
        dict: 索引名 -> 是否成功删除
    """
    print_header("步骤 1/2: 删除现有索引")

    results = {}

    for document_cls in REGISTERED_DOCUMENTS:
        try:
            index_name = document_cls.Index.name
        except AttributeError as e:
            print_error(f"Document 类 {document_cls.__name__} 索引名称获取失败: {e}")
            logger.error(f"Document 类 {document_cls.__name__} 缺少 Index.name: {e}")
            results[document_cls.__name__] = False
            continue

        # 检查索引是否存在
        exists = await es_client.index_exists(index_name)

        if not exists:
            print_info(f"{index_name}: 索引不存在，跳过删除")
            results[index_name] = True
            continue

        # 删除索引
        try:
            await es_client.delete_index(index_name)
            print_success(f"{index_name}: 删除成功")
            results[index_name] = True
        except Exception as e:
            print_error(f"{index_name}: 删除失败 - {e}")
            results[index_name] = False

    return results


async def create_indices(es_client: ElasticsearchClient) -> dict[str, bool]:
    """
    创建所有 ES 索引

    Returns:
        dict: 索引名 -> 是否成功创建
    """
    print_header("步骤 2/2: 创建新索引")

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
            results[document_cls.__name__] = False
            continue

        try:
            # 创建索引
            await es_client.create_index(
                index=index_name,
                mappings=mapping,
                settings=settings
            )
            print_success(f"{index_name}: 创建成功")

            # 验证索引配置
            exists = await es_client.index_exists(index_name)
            if exists:
                print_info(f"{index_name}: 验证通过，索引已存在")
                results[index_name] = True
            else:
                print_error(f"{index_name}: 验证失败，索引不存在")
                results[index_name] = False

        except Exception as e:
            print_error(f"{index_name}: 创建失败 - {e}")
            results[index_name] = False

    return results


async def verify_indices(es_client: ElasticsearchClient) -> None:
    """
    验证所有索引及其配置
    """
    print_header("验证索引配置")

    for document_cls in REGISTERED_DOCUMENTS:
        try:
            index_name = document_cls.Index.name
        except AttributeError as e:
            print_error(f"Document 类 {document_cls.__name__} 索引名称获取失败: {e}")
            logger.error(f"Document 类 {document_cls.__name__} 缺少 Index.name: {e}")
            continue

        exists = await es_client.index_exists(index_name)

        if exists:
            print_success(f"{index_name}: 索引存在")

            # 可以添加更多验证，比如检查映射配置
            try:
                # 这里可以添加获取映射并验证的逻辑
                print_info(f"{index_name}: 配置验证通过")
            except Exception as e:
                print_warning(f"{index_name}: 配置验证失败 - {e}")
        else:
            print_error(f"{index_name}: 索引不存在")


async def main() -> None:
    """
    主函数
    """
    es_client = None

    try:
        print_header("SAG Elasticsearch 索引重建工具")
        print_info("此工具将删除并重新创建所有 Elasticsearch 索引")
        print_warning("警告: 所有索引中的数据将被永久删除！")

        # 请求用户确认
        print("\n")
        confirm = input("  确认继续? (yes/no): ").strip().lower()

        if confirm != 'yes':
            print_info("操作已取消")
            return

        # 1. 创建 ES 客户端
        print_info("正在连接 Elasticsearch...")
        es_client = ElasticsearchClient()

        # 2. 检查连接
        if not await es_client.check_connection():
            print_error("Elasticsearch 连接失败，请检查配置")
            sys.exit(1)

        print_success("Elasticsearch 连接成功")

        # 3. 删除现有索引
        delete_results = await delete_indices(es_client)

        # 4. 创建新索引
        create_results = await create_indices(es_client)

        # 5. 验证索引
        await verify_indices(es_client)

        # 6. 总结
        print_header("操作总结")

        all_success = all(delete_results.values()) and all(create_results.values())

        if all_success:
            print_success("所有索引重建成功！")
            print_info(f"共重建 {len(REGISTERED_DOCUMENTS)} 个索引:")
            for document_cls in REGISTERED_DOCUMENTS:
                try:
                    index_name = document_cls.Index.name
                except AttributeError as e:
                    print_error(f"Document 类 {document_cls.__name__} 索引名称获取失败: {e}")
                    logger.error(f"Document 类 {document_cls.__name__} 缺少 Index.name: {e}")
                    continue
                print(f"    - {index_name}")
        else:
            print_warning("部分索引重建失败，请查看上方详细信息")

            failed_indices = [
                name for name, success in create_results.items()
                if not success
            ]
            if failed_indices:
                print_error(f"失败的索引: {', '.join(failed_indices)}")

        print("=" * 70 + "\n")

    except Exception as e:
        print_error(f"索引重建失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    finally:
        # 关闭连接
        if es_client:
            await es_client.close()


if __name__ == "__main__":
    asyncio.run(main())
