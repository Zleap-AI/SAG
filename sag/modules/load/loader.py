"""
文档加载器

负责加载文档、调用解析器和处理器、保存到数据库
"""

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sag.db import (
    Article,
    ArticleSection,
    ChatConversation,
    ChatMessage,
    SourceChunk,
    SourceConfig,
    get_session_factory,
)
from sag.exceptions import LoadError
from sag.models.article import Article as ArticleModel
from sag.modules.load.config import ConversationLoadConfig, DocumentLoadConfig, LoadResult
from sag.modules.load.parser import MarkdownParser
from sag.modules.load.processor import DocumentProcessor
from sag.utils import get_logger

logger = get_logger("modules.load.loader")


class BaseLoader(ABC):
    """加载器基类"""

    def __init__(
        self,
        processor: Optional[DocumentProcessor] = None,
    ) -> None:
        """
        初始化加载器

        Args:
            processor: 文档处理器（如果不提供，使用默认）
        """
        self.processor = processor or DocumentProcessor()
        self.session_factory = get_session_factory()
        logger.info(f"{self.__class__.__name__} 初始化完成")

    @abstractmethod
    async def load(self, config) -> LoadResult:
        """
        加载数据（主入口方法）

        Args:
            config: 加载配置对象

        Returns:
            LoadResult（包含source_id和chunk_ids）
        """
        pass

    @abstractmethod
    async def _save_to_database(self, *args, **kwargs) -> tuple[str, List[str]]:
        """
        保存到数据库

        Returns:
            (source_id, chunk_ids)
        """
        pass

    async def _generate_metadata(self, content: str, background: str = ""):
        """
        生成元数据（委托给 processor）

        Args:
            content: 内容文本
            background: 背景信息

        Returns:
            元数据字典
        """
        return await self.processor.generate_metadata(content, background)

    async def _generate_embedding(self, text: str):
        """
        生成向量（委托给 processor）

        Args:
            text: 文本内容

        Returns:
            向量数组
        """
        return await self.processor.generate_embedding(text)

    async def _index_source_chunks_to_es(
        self, source_id: str, source_type: str
    ) -> None:
        """
        索引 SourceChunk 到 Elasticsearch（通用方法）

        Args:
            source_id: 源ID (UUID)
            source_type: 源类型 ("ARTICLE" 或 "CHAT")
        """
        try:
            from sag.core.storage import SourceChunkRepository, ElasticsearchClient

            # 创建 ES 客户端
            es_client_wrapper = ElasticsearchClient()
            repo = SourceChunkRepository(es_client_wrapper.client)

            async with self.session_factory() as session:
                # 获取所有 SourceChunk
                stmt = (
                    select(SourceChunk)
                    .where(
                        SourceChunk.source_id == source_id,
                        SourceChunk.source_type == source_type,
                    )
                    .order_by(SourceChunk.rank)
                )
                result = await session.execute(stmt)
                chunks = result.scalars().all()

                if not chunks:
                    logger.warning(
                        f"源没有 SourceChunk: {source_id} (type={source_type})"
                    )
                    return

                # 为每个 SourceChunk 生成向量并索引
                indexed_count = 0
                for chunk in chunks:
                    try:
                        # 生成标题向量（如果有标题）
                        heading_vector = None
                        if chunk.heading:
                            heading_vector = await self._generate_embedding(
                                chunk.heading
                            )

                        # 生成内容向量
                        content_vector = await self._generate_embedding(
                            f"{chunk.heading}\n\n{chunk.content[:1024]}"
                        )

                        # 索引到 ES
                        await repo.index_chunk(
                            chunk_id=chunk.id,
                            source_id=chunk.source_id,
                            source_config_id=chunk.source_config_id,
                            rank=chunk.rank,
                            heading=chunk.heading,
                            content=chunk.content,
                            heading_vector=heading_vector,
                            content_vector=content_vector,
                            references=chunk.references,
                            chunk_type="TEXT",
                            content_length=chunk.chunk_length,
                        )
                        indexed_count += 1

                    except Exception as e:
                        logger.error(f"SourceChunk 索引失败 {chunk.id}: {e}")

                logger.info(
                    f"SourceChunk 索引完成: {source_id} (type={source_type})",
                    extra={
                        "indexed_chunks": indexed_count,
                        "total_chunks": len(chunks),
                    },
                )

            # 关闭 ES 客户端
            await es_client_wrapper.client.close()

        except Exception as e:
            logger.error(f"索引失败: {source_id}: {e}", exc_info=True)


class DocumentLoader(BaseLoader):
    """文档加载器"""

    def __init__(
        self,
        parser: Optional[MarkdownParser] = None,
        processor: Optional[DocumentProcessor] = None,
        max_tokens: Optional[int] = None,
        min_content_length: int = 100,
        merge_short_sections: bool = True,
    ) -> None:
        """
        初始化文档加载器

        Args:
            parser: 文档解析器（如果不提供，使用默认）
            processor: 文档处理器（如果不提供，使用默认）
            max_tokens: 最大token数（用于创建默认parser）
            min_content_length: 最小内容长度（用于创建默认parser）
            merge_short_sections: 是否启用短片段合并（用于创建默认parser）
        """
        # 调用父类初始化
        super().__init__(processor=processor)

        # 创建 parser（如果未提供）
        if parser is not None:
            self.parser = parser
        else:
            parser_params = {}
            if max_tokens is not None:
                parser_params["max_tokens"] = max_tokens
            parser_params["min_content_length"] = min_content_length
            parser_params["merge_short_sections"] = merge_short_sections
            self.parser = MarkdownParser(**parser_params)

    async def load(self, config: DocumentLoadConfig) -> LoadResult:
        """
        加载文档（主入口方法）

        Args:
            config: DocumentLoadConfig配置对象

        Returns:
            LoadResult（包含article_id和chunk_ids）

        Example:
            >>> config = DocumentLoadConfig(
            ...     source_config_id="source-uuid",
            ...     path="doc.md",
            ...     background="技术文档"
            ... )
            >>> result = await loader.load(config)
            >>> # result.source_id, result.chunk_ids
        """
        # 从数据库加载
        if config.article_id and config.load_from_database:
            return await self._load_from_database(
                article_id=config.article_id,
                source_config_id=config.source_config_id,
                background=config.background or "",
                auto_vector=config.auto_vector,
            )

        # 从文件加载
        if not config.path:
            raise LoadError("必须提供 path 或 article_id")

        path = config.path if isinstance(config.path, Path) else Path(config.path)

        if not path.is_file():
            raise LoadError(f"不是文件: {path}")

        return await self.load_file(
            file_path=path,
            source_config_id=config.source_config_id,
            background=config.background or "",
            auto_vector=config.auto_vector,
            max_tokens=config.max_tokens,
            article_id=config.article_id,
            min_content_length=config.min_content_length,
            merge_short_sections=config.merge_short_sections,
        )

    async def load_file(
        self,
        file_path: Path,
        source_config_id: str,
        background: str = "",
        auto_vector: bool = True,
        max_tokens: Optional[int] = None,
        article_id: Optional[str] = None,
        min_content_length: Optional[int] = None,
        merge_short_sections: Optional[bool] = None,
    ) -> LoadResult:
        """
        加载文档文件

        Args:
            file_path: 文件路径
            source_config_id: 信息源ID
            background: 背景信息
            auto_vector: 是否自动索引到Elasticsearch
            max_tokens: 每个片段的最大token数
            article_id: 文章ID（可选，更新已存在的文章）
            min_content_length: 最小内容长度
            merge_short_sections: 是否合并短片段

        Returns:
            LoadResult（包含article_id和chunk_ids）

        Raises:
            LoadError: 加载失败
        """
        try:
            logger.info(f"开始加载文档: {file_path}")

            # 1. 检查文件
            if not file_path.exists():
                raise LoadError(f"文件不存在: {file_path}")

            if not file_path.is_file():
                raise LoadError(f"不是文件: {file_path}")

            # 2. 解析文档（根据配置参数）
            parser_params = {}
            if max_tokens is not None and max_tokens != 8000:
                parser_params["max_tokens"] = max_tokens
            if min_content_length is not None:
                parser_params["min_content_length"] = min_content_length
            if merge_short_sections is not None:
                parser_params["merge_short_sections"] = merge_short_sections
            
            if parser_params:
                # 创建临时 parser 使用指定参数
                parser = MarkdownParser(**parser_params)
                content, sections = parser.parse_file(file_path)
            else:
                content, sections = self.parser.parse_file(file_path)

            logger.info(f"文档解析完成，共{len(sections)}个章节")

            # 3. 创建Article对象
            article = self._create_article_model(
                file_path=file_path,
                content=content,
                source_config_id=source_config_id,
            )

            # 4. 处理文档（生成元数据和向量）
            article = await self.processor.process_article(
                article,
                sections=sections,
                background=background,
            )

            # 5. 保存到数据库
            article_id, chunk_ids = await self._save_to_database(
                article,
                sections,
                source_config_id,
                article_id=article_id,
            )

            logger.info(
                f"文档加载完成: {article.title}",
                extra={
                    "article_id": article_id,
                    "chunk_count": len(chunk_ids),
                    "file_path": str(file_path),
                },
            )

            # 6. 索引到Elasticsearch（可选）
            if auto_vector:
                await self._index_to_elasticsearch(article_id)

            # 7. 返回LoadResult
            return LoadResult(
                source_id=article_id,
                source_type="ARTICLE",
                chunk_ids=chunk_ids,
                source_config_id=source_config_id,
                title=article.title,
                chunk_count=len(chunk_ids),
                extra={
                    "file_path": str(file_path),
                    "section_count": len(sections)
                }
            )

        except Exception as e:
            logger.error(f"文档加载失败: {file_path}: {e}", exc_info=True)
            raise LoadError(f"文档加载失败: {e}") from e

    async def load_directory(
        self,
        dir_path: Path,
        source_config_id: str,
        pattern: str = "*.*",
        recursive: bool = True,
        background: str = "",
        max_tokens: Optional[int] = None,
        min_content_length: Optional[int] = None,
        merge_short_sections: Optional[bool] = None,
    ) -> list[str]:
        """
        批量加载目录中的文档（支持多格式）

        Args:
            dir_path: 目录路径
            source_config_id: 信息源ID (UUID)
            pattern: 文件匹配模式（默认所有文件）
            recursive: 是否递归搜索子目录
            background: 背景信息
            max_tokens: 每个片段的最大token数（如果不提供，使用默认parser的配置）

        Returns:
            文章ID列表 (UUIDs)

        Example:
            >>> loader = DocumentLoader()
            >>> article_ids = await loader.load_directory(
            ...     Path("docs/"),
            ...     source_config_id="xxx-xxx-xxx",
            ...     pattern="*.*",
            ...     recursive=True,
            ...     max_tokens=800
            ... )
        """
        if not dir_path.exists():
            raise LoadError(f"目录不存在: {dir_path}")

        if not dir_path.is_dir():
            raise LoadError(f"不是目录: {dir_path}")

        # 查找文件
        if recursive:
            files = list(dir_path.rglob(pattern))
        else:
            files = list(dir_path.glob(pattern))

        # 过滤出支持的文件格式
        try:
            from sag.modules.load.converter import DocumentConverter
            converter = DocumentConverter()
            supported_files = [f for f in files if f.is_file() and converter.is_supported(f)]
            logger.info(
                f"找到 {len(supported_files)}/{len(files)} 个支持的文件待加载",
                extra={"total": len(files), "supported": len(supported_files)}
            )
            files = supported_files
        except ImportError:
            # 如果转换器不可用，只处理 .md 文件
            files = [f for f in files if f.is_file() and f.suffix.lower() in {'.md', '.markdown'}]
            logger.info(f"找到{len(files)}个 Markdown 文件待加载")

        # 批量加载
        article_ids = []
        for file_path in files:
            try:
                article_id = await self.load_file(
                    file_path=file_path,
                    source_config_id=source_config_id,
                    background=background,
                    max_tokens=max_tokens,
                    min_content_length=min_content_length,
                    merge_short_sections=merge_short_sections,
                )
                article_ids.append(article_id)
            except Exception as e:
                logger.error(f"文件加载失败: {file_path}: {e}")

        logger.info(f"批量加载完成，成功{len(article_ids)}/{len(files)}个文件")
        return article_ids

    def _create_article_model(
        self,
        file_path: Path,
        content: str,
        source_config_id: str,
    ) -> ArticleModel:
        """
        创建Article Pydantic模型

        Args:
            file_path: 文件路径
            content: 文档内容
            source_config_id: 信息源ID

        Returns:
            ArticleModel对象
        """
        # 提取标题
        title = self.parser.extract_title(content)

        return ArticleModel(
            source_config_id=source_config_id,
            title=title,
            content=content,
        )

    async def _save_to_database(
        self,
        article_model: ArticleModel,
        sections: list,
        source_config_id: str,
        article_id: Optional[str] = None,
    ) -> tuple[str, List[str]]:
        """
        保存文章、SourceChunk和ArticleSection到数据库

        Args:
            article_model: Article Pydantic模型
            sections: 章节列表（来自parser，这些作为SourceChunk）
            source_config_id: 信息源ID
            article_id: 可选的文章ID（如果提供，则更新现有文章）

        Returns:
            (article_id, chunk_ids)
        """
        import uuid
        from sqlalchemy import delete
        from sag.db import SourceChunk
        from sag.modules.load.sentence_splitter import SentenceSplitter

        chunk_ids = []  # 收集chunk_ids

        async with self.session_factory() as session:
            # 检查信息源是否存在
            source = await session.get(SourceConfig, source_config_id)
            if not source:
                raise LoadError(f"信息源不存在: {source_config_id}")

            # 初始化句子切分器
            sentence_splitter = SentenceSplitter()

            # 如果提供了 article_id，尝试获取并更新
            if article_id:
                article = await session.get(Article, article_id)
                if article:
                    # 更新现有 Article
                    article.title = article_model.title
                    article.summary = article_model.summary
                    article.content = article_model.content
                    article.category = article_model.category
                    article.tags = article_model.tags if article_model.tags else None
                    article.error = None

                    # 删除旧的 SourceChunk 和 ArticleSection
                    stmt_chunk = delete(SourceChunk).where(
                        SourceChunk.source_id == article_id,
                        SourceChunk.source_type == "ARTICLE"
                    )
                    await session.execute(stmt_chunk)

                    stmt_section = delete(ArticleSection).where(
                        ArticleSection.article_id == article_id)
                    await session.execute(stmt_section)
                else:
                    # 不存在，用提供的 ID 创建
                    article = Article(
                        id=article_id,
                        source_config_id=source_config_id,
                        title=article_model.title,
                        summary=article_model.summary,
                        content=article_model.content,
                        category=article_model.category,
                        tags=article_model.tags if article_model.tags else None,
                    )
                    session.add(article)
            else:
                # 生成新 ID
                article_id = str(uuid.uuid4())
                article = Article(
                    id=article_id,
                    source_config_id=source_config_id,
                    title=article_model.title,
                    summary=article_model.summary,
                    content=article_model.content,
                    category=article_model.category,
                    tags=article_model.tags if article_model.tags else None,
                    status="COMPLETED",
                )
                session.add(article)

            await session.flush()  # 确保 article.id 可用

            # 使用计数器保持 rank 连续递增
            section_rank_counter = 0

            # 遍历所有 SourceChunk（来自 parser 的切片）
            for chunk_model in sections:
                # 1. 创建 SourceChunk
                chunk_id = str(uuid.uuid4())
                chunk_ids.append(chunk_id)  # 记录chunk_id
                chunk_length = len(chunk_model.content)

                source_chunk = SourceChunk(
                    id=chunk_id,
                    source_type="ARTICLE",
                    source_id=article.id,
                    source_config_id=source_config_id,
                    article_id=article.id,
                    conversation_id=None,
                    heading=chunk_model.heading,
                    content=chunk_model.content,
                    rank=chunk_model.rank,
                    chunk_length=chunk_length,
                )
                session.add(source_chunk)

                # 2. 将 SourceChunk 内容按标点符号切分为句子
                sentences = sentence_splitter.split_by_punctuation(chunk_model.content)

                # 3. 创建对应的 ArticleSection（句子级别），并记录 references
                section_ids = []
                for sentence in sentences:
                    section_id = str(uuid.uuid4())
                    section_ids.append(section_id)

                    section = ArticleSection(
                        id=section_id,
                        article_id=article.id,
                        rank=section_rank_counter,  # 连续递增
                        heading=chunk_model.heading,  # 继承源片段的 heading
                        content=sentence,
                        extra_data=None,
                    )
                    session.add(section)
                    section_rank_counter += 1

                # 4. 更新 SourceChunk 的 references 字段
                source_chunk.references = section_ids

            await session.commit()

            logger.info(
                f"文章保存成功",
                extra={
                    "article_id": article.id,
                    "chunk_count": len(chunk_ids),
                    "total_sentences": section_rank_counter,
                },
            )

            return article.id, chunk_ids

    async def _index_to_elasticsearch(self, article_id: str) -> None:
        """
        索引文章 SourceChunk 到 Elasticsearch

        Args:
            article_id: 文章ID (UUID)
        """
        # 调用父类的通用索引方法
        await self._index_source_chunks_to_es(article_id, "ARTICLE")

    async def _load_from_database(
        self,
        article_id: str,
        source_config_id: str,
        background: str = "",
        auto_vector: bool = True,
    ) -> LoadResult:
        """
        从数据库加载并处理已存在的文章

        Args:
            article_id: 文章ID
            source_config_id: 信息源ID
            background: 背景信息
            auto_vector: 是否索引到Elasticsearch

        Returns:
            LoadResult（包含article_id和chunk_ids）

        Raises:
            LoadError: 文章不存在或没有SourceChunk
        """
        logger.info(f"从数据库加载文章: {article_id}")

        async with self.session_factory() as session:
            # 1. 从数据库加载文章和 SourceChunk
            result = await session.execute(
                select(Article)
                .where(Article.id == article_id)
            )
            article_orm = result.scalar_one_or_none()

            if not article_orm:
                raise LoadError(f"文章不存在: {article_id}")

            # 加载 SourceChunk
            stmt_chunks = select(SourceChunk).where(
                SourceChunk.source_id == article_id,
                SourceChunk.source_type == "ARTICLE"
            ).order_by(SourceChunk.rank)
            result_chunks = await session.execute(stmt_chunks)
            source_chunks = result_chunks.scalars().all()

            if not source_chunks:
                raise LoadError(f"文章没有SourceChunk: {article_id}")

            # 2. 转换为 Processor 需要的模型（使用 SourceChunk 作为 sections）
            article_model = ArticleModel(
                id=article_orm.id,
                source_config_id=article_orm.source_config_id,
                title=article_orm.title or "",
                content=article_orm.content,
                summary=article_orm.summary,
                category=article_orm.category,
                tags=article_orm.tags or [],
            )

            # 将 SourceChunk 转换为 ArticleSection 格式（复用共享字段）
            sections_model = [
                ArticleSection(
                    id=chunk.id,
                    article_id=chunk.source_id,
                    rank=chunk.rank,
                    heading=chunk.heading,
                    content=chunk.content,
                    extra_data=chunk.extra_data,
                )
                for chunk in source_chunks
            ]

            # 3. 重新生成总结（使用 SumySummarizer）
            full_content = "\n\n".join([s.content for s in sections_model])
            metadata = await self.processor.sumy_summary.generate_summary_with_ratio(
                full_content,
                background="文章摘要"
            )
            article_model.summary = metadata.get("summary")

            logger.info(
                f"文章总结已重新生成: {article_id}",
                extra={
                    "article_id": article_id,
                    "source_chunks": len(sections_model),
                },
            )

        # 4. 直接从已加载的 SourceChunk 中获取 chunk_ids（不重新处理和保存）
        chunk_ids = [chunk.id for chunk in source_chunks]

        logger.info(
            f"文章 chunk_ids 已提取: {article_id}",
            extra={"article_id": article_id, "chunk_count": len(chunk_ids)},
        )

        # 5. 更新数据库的 summary 字段（只更新 summary，不重建 ArticleSection）
        async with self.session_factory() as session:
            result = await session.execute(
                select(Article).where(Article.id == article_id)
            )
            article_orm = result.scalar_one()
            article_orm.summary = article_model.summary
            await session.commit()
            logger.info(
                f"文章总结已更新到数据库: {article_id}",
                extra={"article_id": article_id},
            )

        # 6. 索引到Elasticsearch（如果需要）
        if auto_vector:
            await self._index_to_elasticsearch(article_id)

        # 7. 返回LoadResult
        return LoadResult(
            source_id=article_id,
            source_type="ARTICLE",
            chunk_ids=chunk_ids,
            source_config_id=source_config_id,
            title=article_model.title,
            chunk_count=len(chunk_ids),
            extra={"from_database": True}
        )


class ConversationLoader(BaseLoader):
    """会话加载器"""

    def __init__(
        self,
        processor: Optional[DocumentProcessor] = None,
        max_tokens: int = 8000,
    ) -> None:
        """
        初始化会话加载器

        Args:
            processor: 文档处理器（如果不提供，使用默认）
            max_tokens: 每个 SourceChunk 的最大 token 数
        """
        super().__init__(processor=processor)
        self.max_tokens = max_tokens

    async def load(self, config: ConversationLoadConfig) -> LoadResult:
        """
        加载会话（主入口方法）

        Args:
            config: ConversationLoadConfig 配置对象

        Returns:
            LoadResult（包含conversation_id和chunk_ids）

        Raises:
            LoadError: 加载失败

        Example:
            >>> config = ConversationLoadConfig(
            ...     source_config_id="source-uuid",
            ...     conversation_id="conv-uuid",
            ...     start_time="2025-01-01 10:00:00",
            ...     end_time="2025-01-01 14:00:00",
            ...     interval_minutes=60
            ... )
            >>> result = await loader.load(config)
            >>> # result.source_id, result.chunk_ids
        """
        try:
            logger.info(f"开始加载会话: {config.conversation_id}")

            # 1. 从数据库加载会话和消息
            async with self.session_factory() as session:
                # 加载会话
                conversation = await session.get(
                    ChatConversation, config.conversation_id
                )
                if not conversation:
                    raise LoadError(f"会话不存在: {config.conversation_id}")

                # 加载消息（按时间范围过滤）
                stmt = select(ChatMessage).where(
                    ChatMessage.conversation_id == config.conversation_id
                )

                # 添加时间过滤
                if config.start_time:
                    start_dt = datetime.fromisoformat(config.start_time)
                    stmt = stmt.where(ChatMessage.timestamp >= start_dt)

                if config.end_time:
                    end_dt = datetime.fromisoformat(config.end_time)
                    stmt = stmt.where(ChatMessage.timestamp <= end_dt)

                stmt = stmt.order_by(ChatMessage.timestamp)
                result = await session.execute(stmt)
                messages = result.scalars().all()

                if not messages:
                    raise LoadError(f"会话没有消息: {config.conversation_id}")

                logger.info(
                    f"加载了 {len(messages)} 条消息",
                    extra={
                        "conversation_id": config.conversation_id,
                        "message_count": len(messages),
                    },
                )

            # 2. 创建时间窗口分块
            chunks = await self._create_time_window_chunks(
                messages=messages,
                interval_minutes=config.interval_minutes,
                max_tokens=config.max_tokens,
                start_time=(
                    datetime.fromisoformat(config.start_time)
                    if config.start_time
                    else messages[0].timestamp
                ),
                end_time=(
                    datetime.fromisoformat(config.end_time)
                    if config.end_time
                    else messages[-1].timestamp
                ),
            )

            logger.info(
                f"创建了 {len(chunks)} 个时间窗口分块",
                extra={
                    "conversation_id": config.conversation_id,
                    "chunk_count": len(chunks),
                },
            )

            # 3. 保存到数据库
            conversation_id, chunk_ids = await self._save_to_database(
                conversation=conversation,
                chunks=chunks,
                source_config_id=config.source_config_id,
            )

            # 4. 索引到 Elasticsearch（可选）
            if config.auto_vector:
                await self._index_source_chunks_to_es(
                    conversation_id, "CHAT"
                )

            logger.info(
                f"会话加载完成: {conversation_id}",
                extra={"conversation_id": conversation_id, "chunk_count": len(chunk_ids)},
            )

            # 5. 返回LoadResult
            return LoadResult(
                source_id=conversation_id,
                source_type="CHAT",
                chunk_ids=chunk_ids,
                source_config_id=config.source_config_id,
                title=conversation.title,
                chunk_count=len(chunk_ids),
                extra={
                    "message_count": len(messages),
                    "time_range": f"{config.start_time} - {config.end_time}"
                }
            )

        except Exception as e:
            logger.error(
                f"会话加载失败: {config.conversation_id}: {e}", exc_info=True
            )
            raise LoadError(f"会话加载失败: {e}") from e

    async def _create_time_window_chunks(
        self,
        messages: list,
        interval_minutes: int,
        max_tokens: int,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict]:
        """
        创建时间窗口分块

        Args:
            messages: 消息列表
            interval_minutes: 时间间隔（分钟）
            max_tokens: 最大 token 数
            start_time: 开始时间
            end_time: 结束时间

        Returns:
            分块列表，每个分块包含：
            {
                "heading": "时间窗口描述",
                "content": "格式化的消息内容",
                "rank": 排序号,
                "references": [message_id 列表]
            }
        """
        from datetime import timedelta
        from sag.utils import TokenEstimator

        token_estimator = TokenEstimator()
        chunks = []
        rank = 0

        # 创建时间窗口
        current_time = start_time
        while current_time < end_time:
            window_end = min(
                current_time + timedelta(minutes=interval_minutes), end_time
            )

            # 过滤该时间窗口内的消息
            window_messages = [
                msg
                for msg in messages
                if current_time <= msg.timestamp < window_end
            ]

            if window_messages:
                # 格式化消息内容：发送者名称(时间戳):\n内容\n
                formatted_messages = []
                message_ids = []

                for msg in window_messages:
                    sender_name = msg.sender_name or "Unknown"
                    timestamp_str = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                    formatted_content = (
                        f"{sender_name}({timestamp_str}):\n{msg.content}\n"
                    )
                    formatted_messages.append(formatted_content)
                    message_ids.append(msg.id)

                # 合并所有消息
                combined_content = "\n".join(formatted_messages)
                token_count = token_estimator.estimate_tokens(combined_content)

                # 检查 token 溢出
                if token_count > max_tokens:
                    # 按消息数量切分
                    sub_chunks = self._split_messages_by_tokens(
                        window_messages,
                        max_tokens,
                        current_time,
                        window_end,
                        rank,
                        token_estimator,
                    )
                    chunks.extend(sub_chunks)
                    rank += len(sub_chunks)
                else:
                    # 创建单个 chunk
                    heading = f"{current_time.strftime('%Y-%m-%d %H:%M')}-{window_end.strftime('%H:%M')} 对话"
                    chunks.append(
                        {
                            "heading": heading,
                            "content": combined_content,
                            "rank": rank,
                            "references": message_ids,
                        }
                    )
                    rank += 1

            # 移动到下一个时间窗口
            current_time = window_end

        return chunks

    def _split_messages_by_tokens(
        self,
        messages: list,
        max_tokens: int,
        window_start: datetime,
        window_end: datetime,
        start_rank: int,
        token_estimator,
    ) -> list[dict]:
        """
        按消息数量切分（处理 token 溢出）

        Args:
            messages: 消息列表
            max_tokens: 最大 token 数
            window_start: 窗口开始时间
            window_end: 窗口结束时间
            start_rank: 起始排序号
            token_estimator: Token 估算器

        Returns:
            子分块列表
        """
        sub_chunks = []
        current_messages = []
        current_message_ids = []
        current_content = ""
        current_tokens = 0
        sub_rank = start_rank

        for msg in messages:
            sender_name = msg.sender_name or "Unknown"
            timestamp_str = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            formatted_content = (
                f"{sender_name}({timestamp_str}):\n{msg.content}\n\n"
            )

            msg_tokens = token_estimator.estimate_tokens(formatted_content)

            # 如果添加这条消息会超过限制
            if current_tokens + msg_tokens > max_tokens and current_messages:
                # 保存当前批次
                first_msg_time = current_messages[0].timestamp
                last_msg_time = current_messages[-1].timestamp
                heading = f"{first_msg_time.strftime('%Y-%m-%d %H:%M')}-{last_msg_time.strftime('%H:%M')} 对话 (第{sub_rank - start_rank + 1}段)"

                sub_chunks.append(
                    {
                        "heading": heading,
                        "content": current_content.strip(),
                        "rank": sub_rank,
                        "references": current_message_ids.copy(),
                    }
                )
                sub_rank += 1

                # 重置
                current_messages = []
                current_message_ids = []
                current_content = ""
                current_tokens = 0

            # 添加当前消息
            current_messages.append(msg)
            current_message_ids.append(msg.id)
            current_content += formatted_content
            current_tokens += msg_tokens

        # 保存最后一个批次
        if current_messages:
            first_msg_time = current_messages[0].timestamp
            last_msg_time = current_messages[-1].timestamp
            heading = f"{first_msg_time.strftime('%Y-%m-%d %H:%M')}-{last_msg_time.strftime('%H:%M')} 对话"
            if len(sub_chunks) > 0:
                heading += f" (第{sub_rank - start_rank + 1}段)"

            sub_chunks.append(
                {
                    "heading": heading,
                    "content": current_content.strip(),
                    "rank": sub_rank,
                    "references": current_message_ids,
                }
            )

        return sub_chunks

    async def _save_to_database(
        self,
        conversation: ChatConversation,
        chunks: list[dict],
        source_config_id: str,
    ) -> tuple[str, List[str]]:
        """
        保存会话 SourceChunk 到数据库

        Args:
            conversation: ChatConversation ORM 对象
            chunks: 分块列表
            source_config_id: 信息源 ID

        Returns:
            (conversation_id, chunk_ids)
        """
        import uuid
        from sqlalchemy import delete

        chunk_ids = []  # 收集chunk_ids

        async with self.session_factory() as session:
            # 检查信息源是否存在
            source = await session.get(SourceConfig, source_config_id)
            if not source:
                raise LoadError(f"信息源不存在: {source_config_id}")

            conversation_id = conversation.id

            # 删除旧的 SourceChunk
            stmt_chunk = delete(SourceChunk).where(
                SourceChunk.source_id == conversation_id,
                SourceChunk.source_type == "CHAT",
            )
            await session.execute(stmt_chunk)

            # 创建新的 SourceChunk
            for chunk_data in chunks:
                chunk_id = str(uuid.uuid4())
                chunk_ids.append(chunk_id)  # 记录chunk_id
                chunk_length = len(chunk_data["content"])

                source_chunk = SourceChunk(
                    id=chunk_id,
                    source_type="CHAT",
                    source_id=conversation_id,
                    source_config_id=source_config_id,
                    article_id=None,
                    conversation_id=conversation_id,
                    heading=chunk_data["heading"],
                    content=chunk_data["content"],
                    rank=chunk_data["rank"],
                    chunk_length=chunk_length,
                    references=chunk_data["references"],  # ChatMessage IDs（直接list）
                )
                session.add(source_chunk)

            await session.commit()

            logger.info(
                f"会话 SourceChunk 保存成功",
                extra={
                    "conversation_id": conversation_id,
                    "chunk_count": len(chunk_ids),
                },
            )

            return conversation_id, chunk_ids
