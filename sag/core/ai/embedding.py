"""
Embedding生成服务

提供统一的文本向量化能力，所有模块共享
"""

from typing import List, Optional

from sag.core.config import get_settings
from sag.exceptions import AIError
from sag.utils import get_logger

logger = get_logger("ai.embedding")


class EmbeddingClient:
    """
    Embedding客户端
    
    统一的文本向量化服务，支持：
    - OpenAI Embedding API
    - 自定义Embedding服务
    - 本地Embedding模型（未来扩展）
    """
    
    def __init__(
        self, 
        model: Optional[str] = None, 
        base_url: Optional[str] = None,
        api_key: Optional[str] = None
    ):
        """
        初始化Embedding客户端
        
        Args:
            model: 模型名称（默认从配置读取）
            base_url: API地址（默认从配置读取）
            api_key: API密钥（默认从配置读取）
        """
        from openai import AsyncOpenAI
        
        settings = get_settings()
        
        self.model = model or settings.embedding_model_name
        self.base_url = base_url or settings.embedding_base_url or settings.llm_base_url
        # ✅ 优先使用传入的 api_key，然后才是环境变量
        self.api_key = api_key or settings.embedding_api_key or settings.llm_api_key
        
        # 初始化OpenAI客户端
        client_kwargs = {"api_key": self.api_key}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        
        self.client = AsyncOpenAI(**client_kwargs)
        
        logger.info(
            f"Embedding客户端初始化完成",
            extra={
                "model": self.model,
                "base_url": self.base_url or "default",
            },
        )
    
    async def generate(self, text: str) -> List[float]:
        """
        生成文本的embedding向量
        
        Args:
            text: 文本内容
            
        Returns:
            embedding向量
            
        Raises:
            AIError: 生成失败
        """
        try:
            response = await self.client.embeddings.create(
                input=text,
                model=self.model,
            )
            
            embedding = response.data[0].embedding
            
            logger.debug(
                f"生成embedding成功",
                extra={
                    "text_length": len(text),
                    "vector_dim": len(embedding),
                },
            )
            
            return embedding
            
        except Exception as e:
            logger.error(f"生成embedding失败: {e}", exc_info=True)
            raise AIError(f"生成embedding失败: {e}") from e
    
    async def batch_generate(self, texts: List[str]) -> List[List[float]]:
        """
        批量生成embedding向量
        
        Args:
            texts: 文本列表
            
        Returns:
            embedding向量列表
            
        Raises:
            AIError: 生成失败
        """
        try:
            response = await self.client.embeddings.create(
                input=texts,
                model=self.model,
            )
            
            embeddings = [item.embedding for item in response.data]
            
            logger.debug(
                f"批量生成embedding成功",
                extra={
                    "batch_size": len(texts),
                    "vector_dim": len(embeddings[0]) if embeddings else 0,
                },
            )
            
            return embeddings
            
        except Exception as e:
            logger.error(f"批量生成embedding失败: {e}", exc_info=True)
            raise AIError(f"批量生成embedding失败: {e}") from e


# 全局单例
_embedding_client: Optional[EmbeddingClient] = None


def get_embedding_client() -> EmbeddingClient:
    """
    获取全局Embedding客户端（单例）- 同步版本
    
    ⚠️ 警告：此函数仅用于纯同步环境（如测试脚本）
    在异步环境中请使用 factory.get_embedding_client() 异步版本
    
    此函数不使用配置管理，仅从环境变量读取配置
    
    推荐使用：
    - factory.get_embedding_client(scenario='general') - 异步版本，支持配置管理
    
    Returns:
        EmbeddingClient实例
    """
    global _embedding_client
    if _embedding_client is None:
        # 简单创建，从环境变量读取配置（不使用 factory 的配置管理）
        _embedding_client = EmbeddingClient()
    return _embedding_client


def reset_embedding_client() -> None:
    """重置全局Embedding客户端"""
    global _embedding_client
    _embedding_client = None


async def generate_embedding(text: str) -> List[float]:
    """
    生成embedding的便捷函数
    
    Args:
        text: 文本内容
        
    Returns:
        embedding向量
    """
    client = get_embedding_client()
    return await client.generate(text)


async def batch_generate_embedding(texts: List[str]) -> List[List[float]]:
    """
    批量生成embedding的便捷函数
    
    Args:
        texts: 文本列表
        
    Returns:
        embedding向量列表
    """
    client = get_embedding_client()
    return await client.batch_generate(texts)

