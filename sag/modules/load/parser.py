"""
文档解析器

支持多格式文档的智能切片和结构化解析
基于智能切片器，提供Token级精确控制
支持 PDF、Word、PowerPoint、Excel、HTML、图片等格式自动转换
"""

import re
from pathlib import Path
from typing import List, Dict, Optional

from sag.exceptions import LoadError
from sag.models.article import ArticleSection
from sag.utils import count_chinese_characters, get_logger, TokenEstimator

logger = get_logger("modules.load.parser")


class MarkdownParser:
    """文档解析器（支持多格式，智能切片版）"""

    def __init__(
        self,
        max_tokens: int = 1000,
        model_type: str = "generic",
        delimiter: str = "\n!?;。；！？",
        enable_converter: bool = True,
        min_content_length: int = 100,
        merge_short_sections: bool = True
    ) -> None:
        """
        初始化解析器

        Args:
            max_tokens: 每个chunk的最大token数量
            model_type: 用于token估算的模型类型
            delimiter: 分隔符字符串
            enable_converter: 是否启用文档格式转换（默认True）
            min_content_length: 最小内容长度（字符数），低于此长度的片段会尝试合并
            merge_short_sections: 是否启用短片段合并
        """
        self.max_tokens = max_tokens
        self.token_estimator = TokenEstimator(model_type)
        self.delimiter = delimiter
        self.min_content_length = min_content_length
        self.merge_short_sections = merge_short_sections

        # 初始化文档转换器
        self.converter: Optional['DocumentConverter'] = None
        if enable_converter:
            try:
                from sag.modules.load.converter import DocumentConverter
                self.converter = DocumentConverter()
            except ImportError as e:
                logger.warning(f"文档转换器加载失败，仅支持 Markdown: {e}")
                self.converter = None

        # 标题正则表达式
        self.heading_pattern = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)

        logger.info(
            f"文档解析器初始化完成",
            extra={
                "max_tokens": max_tokens,
                "model_type": model_type,
                "converter_enabled": self.converter is not None,
                "min_content_length": min_content_length,
                "merge_short_sections": merge_short_sections
            }
        )

    def parse_file(self, file_path: Path) -> tuple[str, List[ArticleSection]]:
        """
        解析文档文件（支持多格式）

        Args:
            file_path: 文档文件路径（支持 .md, .pdf, .docx, .pptx, .xlsx 等）

        Returns:
            (完整内容, 章节列表)

        Raises:
            LoadError: 文件读取或转换失败

        Example:
            >>> parser = MarkdownParser()
            >>> content, sections = parser.parse_file(Path("doc.pdf"))
        """
        try:
            logger.info(f"开始解析文件: {file_path.name} ({file_path.suffix})")

            if not file_path.exists():
                raise LoadError(f"文件不存在: {file_path}")

            file_suffix = file_path.suffix.lower()
            is_markdown = file_suffix in {'.md', '.markdown'}

            # 1. 转换为 Markdown（如果需要）
            if not is_markdown:
                # 非 Markdown 文件，必须使用转换器
                if not self.converter:
                    raise LoadError(
                        f"不支持的文件格式: {file_path.suffix}。"
                        f"仅支持 Markdown 格式，或安装 markitdown 以支持更多格式。"
                    )
                if not self.converter.is_supported(file_path):
                    raise LoadError(
                        f"不支持的文件格式: {file_path.suffix}。"
                        f"支持的格式: {', '.join(self.converter.SUPPORTED_EXTENSIONS)}"
                    )
                content = self.converter.convert_to_markdown(file_path)
            else:
                # 直接读取 Markdown 文件
                content = file_path.read_text(encoding="utf-8")

            # 2. 解析 Markdown 内容
            sections = self.parse_content(content)

            logger.info(
                f"文件解析完成: {file_path.name}",
                extra={
                    "sections": len(sections),
                    "total_tokens": sum(s.extra_data.get("tokens", 0) for s in sections)
                }
            )

            return content, sections

        except Exception as e:
            logger.error(f"文件解析失败: {file_path}: {e}", exc_info=True)
            raise LoadError(f"文件解析失败: {e}") from e

    def parse_content(self, content: str) -> List[ArticleSection]:
        """
        解析Markdown内容为章节列表（智能切片）

        Args:
            content: Markdown文本

        Returns:
            章节列表

        Example:
            >>> parser = MarkdownParser()
            >>> sections = parser.parse_content("# Title\\n\\nContent")
        """
        logger.debug(f"开始解析内容，长度: {len(content)}字符")

        # 1. 提取智能章节
        sections = self._extract_sections(content)
        logger.debug(f"提取到 {len(sections)} 个章节")

        # 2. 处理每个章节，生成chunks
        chunks = []
        chunk_index = 0

        for section in sections:
            section_chunks = self._process_section(section, chunk_index)
            chunks.extend(section_chunks)
            chunk_index += len(section_chunks)

        # 3. 合并短片段（如果启用）
        if self.merge_short_sections:
            chunks = self._merge_short_chunks(chunks)
            logger.info(f"合并短片段后，共生成 {len(chunks)} 个章节")
        
        logger.info(f"解析完成，共生成 {len(chunks)} 个章节")
        return chunks

    def _merge_short_chunks(self, chunks: List[ArticleSection]) -> List[ArticleSection]:
        """
        合并相邻的短片段
        
        Args:
            chunks: 原始片段列表
            
        Returns:
            合并后的片段列表
        """
        if not chunks:
            return chunks
            
        merged_chunks = []
        i = 0
        
        while i < len(chunks):
            current_chunk = chunks[i]
            current_content = current_chunk.content
            current_heading = current_chunk.heading
            
            # 检查当前片段是否需要合并
            if len(current_content) < self.min_content_length and i < len(chunks) - 1:
                # 尝试与下一个片段合并
                next_chunk = chunks[i + 1]
                
                # 合并内容
                merged_content = current_content
                if current_content.strip() and next_chunk.content.strip():
                    merged_content += '\n' + next_chunk.content
                else:
                    merged_content += next_chunk.content
                
                # 合并标题（如果不同）
                merged_heading = current_heading
                if current_heading != next_chunk.heading:
                    if current_heading and next_chunk.heading:
                        merged_heading = current_heading + ' | ' + next_chunk.heading
                    elif next_chunk.heading:
                        merged_heading = next_chunk.heading
                
                # 检查合并后的token数量
                merged_tokens = self.token_estimator.estimate_tokens(merged_content)
                if merged_tokens <= self.max_tokens:  # 遵守相同的token限制
                    # 创建合并后的片段
                    merged_section = self._create_section(
                        merged_content, 
                        merged_heading, 
                        len(merged_chunks),  # 新的rank
                        current_chunk.extra_data.get('level', 1) if current_chunk.extra_data else 1,
                        None
                    )
                    merged_chunks.append(merged_section)
                    i += 2  # 跳过下一个片段，因为已经合并了
                    continue
            
            # 不需要合并，直接添加当前片段
            # 更新rank以反映新位置
            current_chunk.rank = len(merged_chunks)
            merged_chunks.append(current_chunk)
            i += 1
        
        if len(merged_chunks) < len(chunks):
            logger.info(f"合并了 {len(chunks) - len(merged_chunks)} 个短片段，从 {len(chunks)} 个合并为 {len(merged_chunks)} 个")
        
        return merged_chunks

    def extract_title(self, content: str) -> str:
        """
        从Markdown内容中提取标题（第一个一级标题）

        Args:
            content: Markdown文本

        Returns:
            标题，如果没有则返回"Untitled"

        Example:
            >>> parser = MarkdownParser()
            >>> title = parser.extract_title("# My Title\\n\\nContent")
            >>> print(title)  # "My Title"
        """
        match = self.heading_pattern.search(content)
        if match:
            return match.group(2).strip()
        return "Untitled"

    def _extract_sections(self, content: str) -> List[Dict]:
        """
        提取智能章节结构

        Args:
            content: 文档内容

        Returns:
            章节列表，每个章节包含标题和内容
        """
        lines = content.split('\n')

        # 预分析每个标题后面是否有实质内容
        heading_has_content = {}

        for i, line in enumerate(lines):
            heading_match = self.heading_pattern.match(line)
            if heading_match:
                # 从当前标题往后查找，直到下一个标题或文件结束
                j = i + 1
                has_content = False
                while j < len(lines):
                    if self.heading_pattern.match(lines[j]):
                        break
                    if lines[j].strip():  # 有非空内容
                        has_content = True
                        break
                    j += 1
                heading_has_content[i] = has_content

        # 根据预处理结果进行分组
        sections = []
        current_section = {"headings": [], "content_lines": []}

        for i, line in enumerate(lines):
            heading_match = self.heading_pattern.match(line)
            if heading_match:
                level = len(heading_match.group(1))
                title = heading_match.group(2).strip()

                # 判断是否需要开始新section
                will_have_content = heading_has_content.get(i, False)
                current_has_content = any(
                    line.strip() and not self.heading_pattern.match(line)
                    for line in current_section["content_lines"]
                )

                should_start_new = will_have_content or current_has_content or not current_section["headings"]

                if should_start_new:
                    # 保存当前section
                    if current_section["headings"] or any(line.strip() for line in current_section["content_lines"]):
                        sections.append(current_section)

                    # 开始新section
                    current_section = {
                        "headings": [(level, title, line)],
                        "content_lines": []
                    }
                else:
                    # 合并到同一section
                    current_section["headings"].append((level, title, line))
            else:
                current_section["content_lines"].append(line)

        # 添加最后一个section
        if current_section["headings"] or any(line.strip() for line in current_section["content_lines"]):
            sections.append(current_section)

        return sections

    def _process_section(self, section: Dict, start_chunk_index: int) -> List[ArticleSection]:
        """
        处理单个章节，生成chunks

        Args:
            section: 章节数据
            start_chunk_index: 起始chunk索引

        Returns:
            ArticleSection列表
        """
        headings = section["headings"]
        content_lines = section["content_lines"]

        if not headings:
            # 没有标题的内容块
            content = '\n'.join(content_lines).strip()
            if content:
                return self._create_chunks_from_text(
                    content, None, start_chunk_index, 1
                )
            return []

        # 获取主标题
        min_level = min(h[0] for h in headings)
        main_heading = next(h[2] for h in headings if h[0] == min_level)

        # 重建标题部分
        heading_content = '\n'.join(h[2] for h in headings)

        # 处理内容
        content_text = '\n'.join(content_lines).strip()

        if not content_text:
            # 只有标题，无内容
            section_content = heading_content
            return self._create_chunks_from_text(
                section_content, main_heading, start_chunk_index, min_level
            )

        # 标题 + 内容
        full_content = heading_content + '\n' + content_text
        return self._create_chunks_from_text(
            full_content, main_heading, start_chunk_index, min_level
        )

    def _create_chunks_from_text(
        self,
        text: str,
        heading: str,
        start_index: int,
        level: int
    ) -> List[ArticleSection]:
        """
        从文本创建chunks

        Args:
            text: 文本内容
            heading: 关联标题
            start_index: 起始索引
            level: 层级

        Returns:
            ArticleSection列表
        """
        # 估算token数量
        token_count = self.token_estimator.estimate_tokens(text)

        if token_count <= self.max_tokens:
            # 不需要切分
            chunk = self._create_section(
                text, heading, start_index, level, token_count
            )
            return [chunk]

        # 需要切分
        text_chunks = self._split_text_by_tokens(text)

        chunks = []
        for i, chunk_text in enumerate(text_chunks):
            chunk_tokens = self.token_estimator.estimate_tokens(chunk_text)
            chunk = self._create_section(
                chunk_text, heading, start_index + i, level, chunk_tokens
            )
            chunks.append(chunk)

        return chunks

    def _create_section(
        self,
        content: str,
        heading: str,
        order: int,
        level: int,
        token_count: int = None
    ) -> ArticleSection:
        """
        创建章节对象

        Args:
            content: 内容
            heading: 标题
            order: 排序
            level: 层级
            token_count: token数量（可选）

        Returns:
            ArticleSection对象
        """
        # 计算统计信息
        char_count = len(content)
        chinese_count = count_chinese_characters(content)
        word_count = chinese_count + max(0, len(content.split()) - chinese_count)

        return ArticleSection(
            article_id="",  # 将在DocumentLoader中填充
            rank=order,
            heading=heading or "",
            content=content,
            extra_data={
                "level": level,
                "char_count": char_count,
                "word_count": word_count,
            }
        )

    def _split_text_by_tokens(self, text: str) -> List[str]:
        """
        按token数量切分文本

        Args:
            text: 文本内容

        Returns:
            切分后的文本列表
        """
        # 1. 按分隔符分割文本为句子
        sentences = self._split_by_delimiters(text)

        # 2. 合并句子到合适的chunk大小
        chunks = self._merge_sentences_to_chunks(sentences)

        return chunks

    def _split_by_delimiters(self, text: str) -> List[str]:
        """
        使用分隔符分割文本为句子

        Args:
            text: 文本内容

        Returns:
            句子列表
        """
        # 构建正则表达式模式
        escaped_chars = []
        for char in self.delimiter:
            if char in r'\.^$*+?{}[]|()':
                escaped_chars.append('\\' + char)
            else:
                escaped_chars.append(char)

        pattern = '[' + ''.join(escaped_chars) + ']'

        # 分割文本，保留分隔符
        parts = re.split(f'({pattern})', text)

        # 重新组合，将分隔符附加到前面的句子
        sentences = []
        current_sentence = ""

        for part in parts:
            if not part:
                continue
            if re.match(f'^{pattern}$', part):
                # 这是分隔符，附加到当前句子
                current_sentence += part
                if current_sentence.strip():
                    sentences.append(current_sentence)
                    current_sentence = ""
            else:
                # 这是内容
                current_sentence += part

        # 处理最后一个句子
        if current_sentence.strip():
            sentences.append(current_sentence)

        return sentences

    def _merge_sentences_to_chunks(self, sentences: List[str]) -> List[str]:
        """
        将句子合并为适当大小的分块

        Args:
            sentences: 句子列表

        Returns:
            分块列表
        """
        chunks = []
        current_chunk = ""
        current_token_count = 0

        for sentence in sentences:
            sentence_tokens = self.token_estimator.estimate_tokens(sentence)

            # 如果单个句子就超过了最大token数，需要进一步切分
            if sentence_tokens > self.max_tokens:
                # 先保存当前分块（如果不为空）
                if current_chunk.strip():
                    chunks.append(current_chunk.strip())
                    current_chunk = ""
                    current_token_count = 0

                # 切分过长的句子
                split_chunks = self._split_long_sentence(sentence)
                chunks.extend(split_chunks)
                continue

            # 检查是否可以合并到当前分块
            if current_token_count + sentence_tokens <= self.max_tokens:
                current_chunk += sentence
                current_token_count += sentence_tokens
            else:
                # 保存当前分块，开始新分块
                if current_chunk.strip():
                    chunks.append(current_chunk.strip())
                current_chunk = sentence
                current_token_count = sentence_tokens

        # 保存最后一个分块
        if current_chunk.strip():
            chunks.append(current_chunk.strip())

        return chunks

    def _split_long_sentence(self, sentence: str) -> List[str]:
        """
        切分过长的句子

        Args:
            sentence: 过长的句子

        Returns:
            切分后的文本列表
        """
        # 使用次级分隔符切分
        secondary_delimiters = "，,、 \t"
        parts = []
        current_part = ""

        for char in sentence:
            current_part += char
            if char in secondary_delimiters:
                if current_part.strip():
                    parts.append(current_part)
                    current_part = ""

        if current_part.strip():
            parts.append(current_part)

        # 如果没有找到次级分隔符，就按token数强行切分
        if len(parts) <= 1:
            return self._force_split_by_tokens(sentence)

        # 合并parts到合适的大小
        chunks = []
        current_chunk = ""
        current_token_count = 0

        for part in parts:
            part_tokens = self.token_estimator.estimate_tokens(part)

            if part_tokens > self.max_tokens:
                # 这一部分仍然太长，需要强行切分
                if current_chunk.strip():
                    chunks.append(current_chunk.strip())
                    current_chunk = ""
                    current_token_count = 0

                force_chunks = self._force_split_by_tokens(part)
                chunks.extend(force_chunks)
                continue

            if current_token_count + part_tokens <= self.max_tokens:
                current_chunk += part
                current_token_count += part_tokens
            else:
                if current_chunk.strip():
                    chunks.append(current_chunk.strip())
                current_chunk = part
                current_token_count = part_tokens

        if current_chunk.strip():
            chunks.append(current_chunk.strip())

        return chunks

    def _force_split_by_tokens(self, text: str) -> List[str]:
        """
        按token数强行切分文本

        Args:
            text: 文本内容

        Returns:
            切分后的文本列表
        """
        chunks = []
        current_pos = 0

        while current_pos < len(text):
            remaining_text = text[current_pos:]

            # 二分查找最佳切分点
            left, right = 1, len(remaining_text)
            best_pos = 1

            while left <= right:
                mid = (left + right) // 2
                chunk = remaining_text[:mid]
                tokens = self.token_estimator.estimate_tokens(chunk)

                if tokens <= self.max_tokens:
                    best_pos = mid
                    left = mid + 1
                else:
                    right = mid - 1

            chunk = remaining_text[:best_pos]
            if chunk.strip():
                chunks.append(chunk.strip())

            current_pos += best_pos

        return chunks


class ConversationParser:
    """会话解析器 - 格式化会话消息"""

    def __init__(self, max_tokens: int = 8000) -> None:
        """
        初始化会话解析器

        Args:
            max_tokens: 每个 chunk 的最大 token 数量
        """
        self.max_tokens = max_tokens
        self.token_estimator = TokenEstimator()
        logger.info(
            f"会话解析器初始化完成",
            extra={"max_tokens": max_tokens}
        )

    def format_messages(self, messages: list, include_role: bool = False) -> str:
        """
        格式化消息列表为文本

        Args:
            messages: ChatMessage 列表
            include_role: 是否包含发送者角色信息

        Returns:
            格式化的文本内容

        Format:
            发送者名称(时间戳):
            内容

        Example:
            >>> parser = ConversationParser()
            >>> formatted = parser.format_messages(messages)
            张三(2025-01-01 10:30:15):
            今天天气真好

            李四(2025-01-01 10:31:20):
            确实不错
        """
        formatted_messages = []

        for msg in messages:
            sender_name = msg.sender_name or "Unknown"
            timestamp_str = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")

            if include_role:
                formatted_content = (
                    f"{sender_name}[{msg.sender_role.value}]({timestamp_str}):\n"
                    f"{msg.content}\n"
                )
            else:
                formatted_content = (
                    f"{sender_name}({timestamp_str}):\n"
                    f"{msg.content}\n"
                )

            formatted_messages.append(formatted_content)

        return "\n".join(formatted_messages)

    def format_single_message(self, message, include_role: bool = False) -> str:
        """
        格式化单条消息

        Args:
            message: ChatMessage 对象
            include_role: 是否包含发送者角色信息

        Returns:
            格式化的文本内容
        """
        sender_name = message.sender_name or "Unknown"
        timestamp_str = message.timestamp.strftime("%Y-%m-%d %H:%M:%S")

        if include_role:
            return (
                f"{sender_name}[{message.sender_role.value}]({timestamp_str}):\n"
                f"{message.content}\n"
            )
        else:
            return (
                f"{sender_name}({timestamp_str}):\n"
                f"{message.content}\n"
            )

    def estimate_tokens(self, text: str) -> int:
        """
        估算文本的 token 数量

        Args:
            text: 文本内容

        Returns:
            token 数量
        """
        return self.token_estimator.estimate_tokens(text)

    def split_messages_by_tokens(
        self,
        messages: list,
        max_tokens: Optional[int] = None,
    ) -> list[list]:
        """
        按 token 数量切分消息列表

        Args:
            messages: ChatMessage 列表
            max_tokens: 最大 token 数（如果不提供，使用初始化时的值）

        Returns:
            消息批次列表，每个批次是一个消息列表

        Example:
            >>> parser = ConversationParser(max_tokens=1000)
            >>> batches = parser.split_messages_by_tokens(messages)
            >>> for batch in batches:
            ...     content = parser.format_messages(batch)
            ...     print(f"Batch: {len(batch)} messages, content length: {len(content)}")
        """
        max_tokens = max_tokens or self.max_tokens
        batches = []
        current_batch = []
        current_tokens = 0

        for msg in messages:
            # 格式化消息以估算 token
            formatted_msg = self.format_single_message(msg)
            msg_tokens = self.token_estimator.estimate_tokens(formatted_msg)

            # 如果单条消息本身就超过限制，需要拆分
            if msg_tokens > max_tokens:
                logger.warning(
                    f"单条消息超过 max_tokens 限制，将拆分消息: {msg.sender_name}, "
                    f"原始 tokens: {msg_tokens}, 限制: {max_tokens}"
                )
                # 拆分超长消息
                split_msgs = self._split_long_message(msg, max_tokens)

                # 将拆分后的消息添加到批次
                for split_msg in split_msgs:
                    split_formatted = self.format_single_message(split_msg)
                    split_tokens = self.token_estimator.estimate_tokens(split_formatted)

                    # 检查是否会超过当前批次限制
                    if current_tokens + split_tokens > max_tokens and current_batch:
                        batches.append(current_batch)
                        current_batch = []
                        current_tokens = 0

                    current_batch.append(split_msg)
                    current_tokens += split_tokens

                continue

            # 如果添加这条消息会超过限制（普通情况）
            if current_tokens + msg_tokens > max_tokens and current_batch:
                # 保存当前批次
                batches.append(current_batch)

                # 重置
                current_batch = []
                current_tokens = 0

            # 添加当前消息
            current_batch.append(msg)
            current_tokens += msg_tokens

        # 保存最后一个批次
        if current_batch:
            batches.append(current_batch)

        logger.info(
            f"消息切分完成",
            extra={
                "total_messages": len(messages),
                "batches": len(batches),
                "max_tokens": max_tokens,
            }
        )

        return batches

    def _split_long_message(self, msg, max_tokens: int):
        """
        将单条超长消息拆分为多条

        Args:
            msg: ChatMessage 对象
            max_tokens: 每个片段的最大 token 数量

        Returns:
            拆分后的 ChatMessage 列表
        """
        # 格式化消息头以计算 token
        sender_name = msg.sender_name or "Unknown"
        timestamp_str = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")

        # 获取发送者角色（兼容字符串和枚举）
        sender_role = (
            msg.sender_role.value
            if hasattr(msg.sender_role, 'value')
            else str(msg.sender_role)
        )

        # 构建消息头
        header = f"{sender_name}[{sender_role}]({timestamp_str}):\n"
        header_tokens = self.token_estimator.estimate_tokens(header)

        # 内容可用的最大 token 数
        content_max_tokens = max_tokens - header_tokens

        if content_max_tokens <= 0:
            logger.warning(
                f"消息头本身超过 max_tokens 限制，将使用最小值: {max_tokens}, header_tokens: {header_tokens}"
            )
            content_max_tokens = max_tokens // 2  # 如果头信息就占满了，使用一半

        # 获取消息内容
        content = msg.content or ""

        # 估算内容 token 数
        content_tokens = self.token_estimator.estimate_tokens(content)

        # 如果内容本身很短，直接返回原消息
        if content_tokens <= content_max_tokens:
            return [msg]

        # 拆分内容
        content_chunks = self._split_text_by_tokens(
            content, content_max_tokens
        )

        # 创建拆分后的消息
        split_messages = []
        for i, chunk in enumerate(content_chunks):
            # 为后续片段添加 [Continued] 标记
            if i > 0:
                chunk_content = f"[Continued] {chunk}"
            else:
                chunk_content = chunk

            # 创建新消息对象（保持其他字段不变）
            split_msg = type(msg)(
                id=msg.id,
                conversation_id=msg.conversation_id,
                timestamp=msg.timestamp,
                content=chunk_content,
                sender_id=msg.sender_id,
                sender_name=msg.sender_name,
                sender_avatar=msg.sender_avatar,
                sender_title=msg.sender_title,
                type=msg.type,
                sender_role=msg.sender_role,
            )

            split_messages.append(split_msg)

            logger.debug(
                f"消息拆分片段 {i+1}/{len(content_chunks)}",
                extra={
                    "chunk_length": len(chunk),
                    "chunk_tokens": self.token_estimator.estimate_tokens(
                        header + chunk
                    ),
                }
            )

        logger.info(
            f"消息拆分完成",
            extra={
                "original_tokens": header_tokens + content_tokens,
                "split_into": len(split_messages),
                "max_tokens_per_chunk": max_tokens,
            }
        )

        return split_messages

    def _split_text_by_tokens(
        self, text: str, max_tokens: int
    ) -> List[str]:
        """
        按 token 数量切分文本（简化版）

        Args:
            text: 文本内容
            max_tokens: 每个片段的最大 token 数量

        Returns:
            切分后的文本片段列表
        """
        # 估算总 token 数
        total_tokens = self.token_estimator.estimate_tokens(text)

        if total_tokens <= max_tokens:
            return [text]

        # 使用二分查找进行切分
        chunks = []
        current_pos = 0

        while current_pos < len(text):
            remaining_text = text[current_pos:]
            remaining_tokens = self.token_estimator.estimate_tokens(
                remaining_text
            )

            # 如果剩余部分在限制内，直接添加
            if remaining_tokens <= max_tokens:
                chunks.append(remaining_text)
                break

            # 二分查找最佳切分点
            left, right = 1, len(remaining_text)
            best_pos = 1

            while left <= right:
                mid = (left + right) // 2
                chunk = remaining_text[:mid]
                tokens = self.token_estimator.estimate_tokens(chunk)

                if tokens <= max_tokens:
                    best_pos = mid
                    left = mid + 1
                else:
                    right = mid - 1

            # 添加找到的片段
            chunk = remaining_text[:best_pos]
            if chunk.strip():
                chunks.append(chunk)

            current_pos += best_pos

        return chunks
