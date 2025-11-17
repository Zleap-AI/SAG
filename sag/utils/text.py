"""
文本处理工具模块
"""

import hashlib
import re
import unicodedata
from typing import List, Optional


def normalize_text(text: str) -> str:
    """
    标准化文本

    Args:
        text: 原始文本

    Returns:
        标准化后的文本
    """
    # 去除多余空白
    text = re.sub(r"\s+", " ", text)
    # 去除首尾空白
    text = text.strip()
    return text


def normalize_text_for_embedding(text: str) -> str:
    """
    为向量嵌入（Embedding）规范化文本

    采用业界最佳实践，适用于语义搜索、相似度计算等场景。
    该方法保留语义信息（标点、数字、多语言字符），只做必要的清洗。

    处理步骤：
    1. Unicode 标准化（NFKC）- 统一字符表示形式
    2. 小写转换 - 消除大小写差异
    3. 空白字符清理 - 统一空白符并去除多余空白

    Args:
        text: 原始文本

    Returns:
        规范化后的文本

    Examples:
        >>> normalize_text_for_embedding("Hello WORLD!")
        "hello world!"

        >>> normalize_text_for_embedding("  Multiple   spaces  ")
        "multiple spaces"

        >>> normalize_text_for_embedding("OpenAI发布sophnet/Qwen3-30B-A3B-Thinking-2507模型")
        "openai发布sophnet/Qwen3-30B-A3B-Thinking-2507模型"

        >>> normalize_text_for_embedding("½ cup → 1/2 cup")  # Unicode标准化
        "1/2 cup -> 1/2 cup"

    Notes:
        - 保留标点符号：有助于区分语义（如 "don't" vs "dont"）
        - 保留数字：在技术文档中很重要（如 "Python 3.11"）
        - 保留多语言字符：支持中英文混合等场景
        - NFKC 标准化：统一全角/半角、上标/下标等变体
    """
    if not text:
        return ""

    # 1. Unicode 标准化（NFKC）
    # 将全角字符转半角、统一兼容字符（如 ① → 1）
    text = unicodedata.normalize("NFKC", text)

    # 2. 转小写（支持多语言）
    text = text.lower()

    # 3. 清理空白字符
    # 替换所有空白字符（空格、制表符、换行等）为单个空格
    text = re.sub(r"\s+", " ", text)

    # 4. 去除首尾空白
    text = text.strip()

    return text


def normalize_entity_name(name: str) -> str:
    """
    标准化实体名称

    Args:
        name: 原始实体名称

    Returns:
        标准化后的实体名称
    """
    # 转小写
    normalized = name.lower()
    # 去除标点符号（保留中文）
    normalized = re.sub(r"[^\w\s\u4e00-\u9fff]", "", normalized)
    # 去除多余空白
    normalized = re.sub(r"\s+", " ", normalized)
    # 去除首尾空白
    normalized = normalized.strip()
    return normalized


def extract_markdown_headings(content: str) -> List[str]:
    """
    提取Markdown标题

    Args:
        content: Markdown内容

    Returns:
        标题列表
    """
    pattern = r"^(#{1,6})\s+(.+)$"
    headings = []

    for line in content.split("\n"):
        match = re.match(pattern, line.strip())
        if match:
            level = len(match.group(1))
            title = match.group(2)
            headings.append(f"{'#' * level} {title}")

    return headings


def compute_text_hash(text: str) -> str:
    """
    计算文本哈希值

    Args:
        text: 文本内容

    Returns:
        MD5哈希值（十六进制字符串）
    """
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def truncate_text(
    text: str,
    max_length: int = 100,
    suffix: str = "...",
) -> str:
    """
    截断文本

    Args:
        text: 原始文本
        max_length: 最大长度
        suffix: 后缀

    Returns:
        截断后的文本
    """
    if len(text) <= max_length:
        return text

    return text[: max_length - len(suffix)] + suffix


def split_text_by_paragraphs(text: str) -> List[str]:
    """
    按段落分割文本

    Args:
        text: 原始文本

    Returns:
        段落列表
    """
    paragraphs = text.split("\n\n")
    return [p.strip() for p in paragraphs if p.strip()]


def count_chinese_characters(text: str) -> int:
    """
    统计中文字符数量

    Args:
        text: 文本内容

    Returns:
        中文字符数量
    """
    return len([c for c in text if "\u4e00" <= c <= "\u9fff"])


def estimate_tokens(text: str, method: str = "simple") -> int:
    """
    估算文本token数量

    Args:
        text: 文本内容
        method: 估算方法（simple | tiktoken）

    Returns:
        估算的token数量
    """
    if method == "tiktoken":
        try:
            import tiktoken

            encoding = tiktoken.get_encoding("cl100k_base")
            return len(encoding.encode(text))
        except ImportError:
            pass

    # 简单估算：中文1.5字符/token, 英文4字符/token
    chinese_count = count_chinese_characters(text)
    english_count = len(text) - chinese_count

    return int(chinese_count / 1.5 + english_count / 4)


def clean_whitespace(text: str) -> str:
    """
    清理文本中的空白字符

    Args:
        text: 原始文本

    Returns:
        清理后的文本
    """
    # 替换多个空格为单个空格
    text = re.sub(r" +", " ", text)
    # 替换多个换行为两个换行
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 去除行首行尾空格
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(lines)


class TokenEstimator:
    """通用Token估算器"""

    def __init__(self, model_type: str = "generic"):
        """
        初始化Token估算器

        Args:
            model_type: 模型类型 ("gpt", "claude", "llama", "generic")
        """
        self.model_type = model_type

    def estimate_tokens(self, text: str) -> int:
        """
        估算文本的token数量

        Args:
            text: 文本内容

        Returns:
            估算的token数量
        """
        if not text:
            return 0

        # 统计中文字符数
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))

        # 统计英文单词数
        english_words = len(re.findall(r"\b[a-zA-Z]+\b", text))

        # 根据模型类型调整估算策略
        if self.model_type == "gpt":
            # GPT模型：中文约1.5字符=1token，英文约4字符=1token
            chinese_tokens = int(chinese_chars * 0.7)
            english_tokens = english_words
        elif self.model_type == "claude":
            # Claude模型：与GPT类似但略有不同
            chinese_tokens = int(chinese_chars * 0.65)
            english_tokens = int(english_words * 1.1)
        elif self.model_type == "llama":
            # LLaMA模型：更倾向于字符级
            chinese_tokens = int(chinese_chars * 0.8)
            english_tokens = int(english_words * 1.3)
        else:
            # 通用估算：保守策略
            chinese_tokens = int(chinese_chars * 0.8)
            english_tokens = english_words

        # 考虑标点符号和特殊字符
        special_chars = len(re.findall(r"[^\w\s\u4e00-\u9fff]", text))
        special_tokens = int(special_chars * 0.5)

        total_tokens = chinese_tokens + english_tokens + special_tokens

        # 至少返回1（如果文本非空）
        return max(1, total_tokens)
