"""
文档处理器

负责生成元数据、计算向量
"""

import json
import re
from typing import Dict, List, Optional

from sag.core.ai import LLMMessage, LLMRole
from sag.core.ai.base import BaseLLMClient
from sag.core.prompt import get_prompt_manager
from sag.exceptions import AIError, LoadError
from sag.models.article import Article, ArticleSection
from sag.utils import estimate_tokens, get_logger
from sag.core.ai.sumy import get_sumy_summarizer
logger = get_logger("modules.load.processor")


class DocumentProcessor:
    """文档处理器"""

    def __init__(
        self,
        llm_client: Optional[BaseLLMClient] = None,
        embedding_model_name: Optional[str] = None,
    ) -> None:
        """
        初始化文档处理器

        Args:
            llm_client: LLM客户端（如果不提供，使用默认）
            embedding_model_name: 向量模型名称（如果不提供，从配置读取）
        """
        from sag.core.config import get_settings

        settings = get_settings()

        # LLM 客户端：懒加载（第一次使用时创建）
        self._llm_client = llm_client
        # 优先使用传入的模型名，否则从配置读取
        self.embedding_model_name = embedding_model_name or settings.embedding_model_name
        self.prompt_manager = get_prompt_manager()
        # 使用全局 SumySummarizer 单例（避免重复创建实例）
        self.sumy_summary = get_sumy_summarizer()
        logger.info(
            "文档处理器初始化完成",
            extra={
                "embedding_model_name": self.embedding_model_name,
            },
        )

    async def generate_metadata(
        self,
        content: str,
        background: str = "",
        max_tokens: int = 2000,
    ) -> Dict[str, any]:
        """
        生成文档元数据（标题、摘要、分类、标签）

        Args:
            content: 文档内容
            background: 背景信息
            max_tokens: 最大token数（截断内容）

        Returns:
            元数据字典 {"title": str, "summary": str, "category": str, "tags": List[str]}

        Raises:
            LoadError: 元数据生成失败

        Example:
            >>> processor = DocumentProcessor()
            >>> metadata = await processor.generate_metadata(content)
            >>> print(metadata["title"])
        """
        try:
            # 截断内容（避免超出token限制）
            # truncated_content = self._truncate_content(content, max_tokens=1000000)

            # generate_summary_with_ratio 现在直接返回解析后的字典
            metadata = await self.sumy_summary.generate_summary_with_ratio(
                content,
                background="文章摘要"
            )

            # metadata 已经是解析后的字典，包含 title, summary, category, tags

            logger.info(
                "元数据生成成功",
                extra={
                    "title": metadata.get("title", "")[:50],
                    "category": metadata.get("category", ""),
                    "tags_count": len(metadata.get("tags", [])),
                },
            )

            return metadata

        except Exception as e:
            raise LoadError(f"元数据生成失败: {e}") from e

    async def generate_embedding(self, text: str) -> List[float]:
        """
        生成文本向量

        Args:
            text: 文本内容

        Returns:
            向量列表

        Raises:
            AIError: 向量生成失败

        Example:
            >>> processor = DocumentProcessor()
            >>> embedding = await processor.generate_embedding("文本内容")
            >>> len(embedding)  # 1536 (OpenAI Qwen/Qwen3-Embedding-0.6B)
        """
        try:
            import time
            # ✅ 使用 factory 的异步版本，支持配置管理（数据库/环境变量）
            from sag.core.ai.factory import get_embedding_client

            total_start = time.perf_counter()

            # 截断文本（避免超出token限制）
            truncate_start = time.perf_counter()
            truncated_text = self._truncate_content(text, max_tokens=8000)
            truncate_time = time.perf_counter() - truncate_start

            logger.debug(f"生成向量，文本长度: {len(text)}字符")

            # 使用 factory 的全局单例（支持数据库配置管理）
            api_start = time.perf_counter()
            embedding_client = await get_embedding_client(scenario='general')
            embedding = await embedding_client.generate(truncated_text)
            api_time = time.perf_counter() - api_start

            total_time = time.perf_counter() - total_start

            logger.info(
                f"向量生成耗时统计 - "
                f"总耗时: {total_time:.3f}s, "
                f"文本截断: {truncate_time:.3f}s ({truncate_time/total_time*100:.1f}%), "
                f"API调用: {api_time:.3f}s ({api_time/total_time*100:.1f}%), "
                f"向量维度: {len(embedding)}"
            )

            return embedding

        except Exception as e:
            raise AIError(f"向量生成失败: {e}") from e

    async def process_article(
        self,
        article: Article,
        sections: List[ArticleSection],
        background: str = "",
    ) -> Article:
        """
        处理文章（生成元数据和向量）

        Args:
            article: 文章对象（部分填充）
            sections: 章节列表
            background: 背景信息

        Returns:
            处理后的文章对象（包含元数据和向量）

        Example:
            >>> processor = DocumentProcessor()
            >>> article = await processor.process_article(article, sections)
        """
        try:
            # 1. 生成文章元数据
            logger.info(f"开始处理文章: {article.title}")

            # 准备内容（前2000字）
            full_content = "\n\n".join([s.content for s in sections])

            metadata = await self.generate_metadata(full_content, background)

            # 更新文章元数据
            article.title = metadata.get("title", article.title or "Untitled")
            article.summary = metadata.get("summary")
            article.category = metadata.get("category")
            article.tags = metadata.get("tags", [])

            logger.info(
                f"文章处理完成: {article.title}",
                extra={
                    "sections": len(sections),
                    "summary_length": len(article.summary) if article.summary else 0,
                    "tags_count": len(article.tags) if article.tags else 0,
                },
            )

            return article

        except Exception as e:
            raise LoadError(f"文章处理失败: {e}") from e

    def _truncate_content(self, content: str, max_tokens: int) -> str:
        """
        截断内容以适应token限制

        Args:
            content: 原始内容
            max_tokens: 最大token数

        Returns:
            截断后的内容
        """
        estimated_tokens = estimate_tokens(content)

        if estimated_tokens <= max_tokens:
            return content

        # 按比例截断
        ratio = max_tokens / estimated_tokens
        target_length = int(len(content) * ratio * 0.9)  # 留10%余量

        truncated = content[:target_length]

        logger.debug(
            f"内容截断: {len(content)}字符 -> {len(truncated)}字符 "
            f"({estimated_tokens} tokens -> ~{max_tokens} tokens)"
        )

        return truncated

