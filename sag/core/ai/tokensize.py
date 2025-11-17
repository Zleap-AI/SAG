"""
分词器模块

提供中英文混合分词的单例工厂，避免重复加载 spaCy 模型
"""

import re
import threading
from typing import List

from sag.utils import get_logger

logger = get_logger("ai.tokenizer")


class MixedTokenizer:
    """
    中英文混合分词器（单例模式）

    特点：
    - 中文使用 jieba 分词
    - 英文使用 spaCy 分词，识别并保留实体完整性（如人名）
    - 懒加载：首次调用时才加载 spaCy 模型
    - 单例模式：全局只加载一次 spaCy 模型，避免重复加载耗时
    - 线程安全：使用双重检查锁定模式

    使用示例：
        tokenizer = MixedTokenizer.get_instance()
        tokens = tokenizer.tokenize("我喜欢用Python编程")
    """

    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        """
        私有构造函数，防止外部直接实例化

        注意：不要直接调用此方法，请使用 get_instance() 获取单例
        """
        self._nlp_en = None
        self._spacy_loaded = False
        self._spacy_failed = False
        logger.info("MixedTokenizer 实例已创建（spaCy 模型尚未加载，将在首次使用时懒加载）")

    @classmethod
    def get_instance(cls):
        """
        获取 MixedTokenizer 单例实例

        使用双重检查锁定（Double-Checked Locking）确保线程安全

        Returns:
            MixedTokenizer: 全局唯一的分词器实例
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _load_spacy_model(self):
        """
        懒加载 spaCy 英文模型

        只在首次调用 tokenize() 时加载，避免启动时的性能损耗
        """
        if self._spacy_loaded or self._spacy_failed:
            return

        with self._lock:
            # 双重检查
            if self._spacy_loaded or self._spacy_failed:
                return

            try:
                import spacy

                logger.info("正在加载 spaCy 英文模型（en_core_web_sm）...")
                self._nlp_en = spacy.load("en_core_web_sm")
                self._spacy_loaded = True
                logger.info("✅ spaCy 英文模型加载成功")
            except Exception as e:
                self._spacy_failed = True
                logger.warning(
                    f"⚠️ spaCy 英文模型加载失败，将使用降级分词方案: {e}\n"
                    f"提示：安装 spaCy 模型以获得更好的分词效果：\n"
                    f"  uv add spacy\n"
                    f"  uv pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0.tar.gz"
                )

    def tokenize(self, text: str, fast_mode: bool = False) -> List[str]:
        """
        对文本进行中英文混合分词

        处理流程：
        1. 使用正则表达式分离中文和非中文部分
        2. 中文部分使用 jieba 分词
        3. 英文部分：
           - fast_mode=False: 使用 spaCy 分词（识别实体），若不可用则降级到空格分词
           - fast_mode=True: 直接使用空格分词（更快）
        4. 过滤空白字符

        Args:
            text: 待分词的文本
            fast_mode: 快速模式，跳过 spaCy 分词，只使用 jieba + 空格分词（默认False）

        Returns:
            List[str]: 分词结果列表

        示例：
            >>> tokenizer = MixedTokenizer.get_instance()
            >>> tokenizer.tokenize("我喜欢用Python编程")
            ['我', '喜欢', '用', 'Python', '编程']
            >>> tokenizer.tokenize("Were Scott Derrickson and Ed Wood of the same nationality?")
            ['Were', 'Scott Derrickson', 'and', 'Ed Wood', 'of', 'the', 'same', 'nationality', '?']
            >>> tokenizer.tokenize("我喜欢用Python编程", fast_mode=True)  # 快速模式
            ['我', '喜欢', '用', 'Python', '编程']
        """
        # 懒加载 spaCy 模型（仅在非快速模式下）
        if not fast_mode and not self._spacy_loaded and not self._spacy_failed:
            self._load_spacy_model()

        # 正则匹配非中文字符（英文、数字、符号等）
        pattern = re.compile(r"[^\u4e00-\u9fa5]+")
        matches = pattern.finditer(text)
        segments = []
        last_end = 0

        for match in matches:
            start, end = match.start(), match.end()

            # 处理中文部分
            if start > last_end:
                chinese_part = text[last_end:start]
                try:
                    import jieba
                    segments.extend(jieba.lcut(chinese_part))
                except ImportError:
                    # jieba未安装，使用简单分词
                    segments.extend(chinese_part.split())

            # 处理英文部分
            english_part = text[start:end].strip()
            if english_part:
                if fast_mode:
                    # 快速模式：直接空格分词
                    segments.extend(self._tokenize_english_simple(english_part))
                elif self._spacy_loaded and self._nlp_en is not None:
                    # 使用 spaCy 识别实体并合并
                    segments.extend(self._tokenize_english_with_spacy(english_part))
                else:
                    # 降级方案：简单的空格分词
                    segments.extend(self._tokenize_english_simple(english_part))

            last_end = end

        # 处理剩余中文
        if last_end < len(text):
            try:
                import jieba
                segments.extend(jieba.lcut(text[last_end:]))
            except ImportError:
                # jieba未安装，使用简单分词
                segments.extend(text[last_end:].split())

        # 过滤空白
        return [seg for seg in segments if seg.strip()]

    def _tokenize_english_with_spacy(self, text: str) -> List[str]:
        """
        使用 spaCy 对英文文本分词，识别并保留实体完整性

        Args:
            text: 英文文本

        Returns:
            List[str]: 分词结果
        """
        doc = self._nlp_en(text)
        segments = []
        current_pos = 0

        for ent in doc.ents:
            # 实体前的非实体部分
            if ent.start_char > current_pos:
                non_ent_text = text[current_pos : ent.start_char]
                segments.extend(
                    [token.text for token in self._nlp_en(non_ent_text) if token.text.strip()]
                )
            # 合并实体（如人名"Scott Derrickson"作为整体）
            segments.append(ent.text)
            current_pos = ent.end_char

        # 处理剩余非实体部分
        if current_pos < len(text):
            remaining_text = text[current_pos:]
            segments.extend(
                [token.text for token in self._nlp_en(remaining_text) if token.text.strip()]
            )

        return segments

    def _tokenize_english_simple(self, text: str) -> List[str]:
        """
        简单的英文分词（降级方案）

        Args:
            text: 英文文本

        Returns:
            List[str]: 分词结果
        """
        return [word for word in text.split() if word.strip()]

    @property
    def is_spacy_available(self) -> bool:
        """
        检查 spaCy 是否可用

        Returns:
            bool: True 表示 spaCy 已成功加载，False 表示使用降级方案
        """
        return self._spacy_loaded


# ==================== 便捷函数 ====================


def get_mixed_tokenizer():
    """
    获取全局单例分词器的便捷函数

    Returns:
        MixedTokenizer: 全局唯一的分词器实例

    示例：
        >>> from sag.core.ai.tokensize import get_mixed_tokenizer
        >>> tokenizer = get_mixed_tokenizer()
        >>> tokens = tokenizer.tokenize("我喜欢Python")
    """
    return MixedTokenizer.get_instance()


def tokenize(text: str) -> List[str]:
    """
    快捷分词函数

    Args:
        text: 待分词的文本

    Returns:
        List[str]: 分词结果列表

    示例：
        >>> from sag.core.ai.tokensize import tokenize
        >>> tokens = tokenize("我喜欢Python")
        ['我', '喜欢', 'Python']
    """
    return get_mixed_tokenizer().tokenize(text)
