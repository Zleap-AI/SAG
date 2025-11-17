"""
句子切分器

按标点符号将文本切分为句子（用于从 SourceChunk 生成 ArticleSection）
"""

import re
from typing import List


class SentenceSplitter:
    """句子切分器"""

    # 标点符号正则：句号、问号、感叹号、分号（中英文）
    PUNCTUATION_PATTERN = re.compile(r'[!?;。！？；]+')

    def __init__(self, min_sentence_length: int = 5):
        """
        初始化切分器

        Args:
            min_sentence_length: 最小句子长度（过滤过短的句子）
        """
        self.min_sentence_length = min_sentence_length

    def split_by_punctuation(self, text: str) -> List[str]:
        """
        按标点符号切分文本为句子

        Args:
            text: 要切分的文本

        Returns:
            句子列表

        Example:
            >>> splitter = SentenceSplitter()
            >>> text = "这是第一句。这是第二句！这是第三句？"
            >>> sentences = splitter.split_by_punctuation(text)
            >>> print(sentences)
            ['这是第一句', '这是第二句', '这是第三句']
        """
        # 使用标点符号分割
        # 注意：标点符号本身也会被分割出来，所以需要处理
        parts = self.PUNCTUATION_PATTERN.split(text)

        # 过滤空字符串和过短的句子
        sentences = []
        for part in parts:
            sentence = part.strip()
            if sentence and len(sentence) >= self.min_sentence_length:
                sentences.append(sentence)

        return sentences

    def split_with_punctuation(self, text: str) -> List[tuple[str, str]]:
        """
        按标点符号切分文本，同时保留标点符号

        Args:
            text: 要切分的文本

        Returns:
            (句子, 标点符号) 元组列表

        Example:
            >>> splitter = SentenceSplitter()
            >>> text = "这是第一句。这是第二句！这是第三句？"
            >>> sentences = splitter.split_with_punctuation(text)
            >>> print(sentences)
            [('这是第一句', '。'), ('这是第二句', '！'), ('这是第三句', '？')]
        """
        # 找到所有标点符号的位置
        matches = list(self.PUNCTUATION_PATTERN.finditer(text))

        if not matches:
            # 没有找到标点符号
            if len(text.strip()) >= self.min_sentence_length:
                return [(text.strip(), "")]
            return []

        sentences = []
        start = 0

        for match in matches:
            # 提取句子（从上一个标点符号后到当前标点符号）
            sentence = text[start:match.start()].strip()
            punctuation = match.group()

            if sentence and len(sentence) >= self.min_sentence_length:
                sentences.append((sentence, punctuation))

            # 更新起始位置为当前标点符号之后
            start = match.end()

        # 处理最后一个标点符号之后的文本
        if start < len(text):
            last_sentence = text[start:].strip()
            if last_sentence and len(last_sentence) >= self.min_sentence_length:
                sentences.append((last_sentence, ""))

        return sentences
