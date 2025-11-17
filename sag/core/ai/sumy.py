"""
Sumy 文本摘要模块

结合 Sumy 库的 TextRank 算法和大模型，生成高质量的文本摘要：
1. 使用 TextRank (基于 PageRank) 提取关键句子
2. 使用大模型对提取的句子进行总结和润色
"""

import re
import time
from typing import Dict, List, Optional
from sumy.nlp.tokenizers import Tokenizer
from sumy.parsers.plaintext import PlaintextParser
from sumy.summarizers.text_rank import TextRankSummarizer
from sumy.summarizers.luhn import LuhnSummarizer

from sag.core.ai.base import BaseLLMClient
from sag.core.ai.models import LLMMessage, LLMRole
from sag.core.prompt import get_prompt_manager
from sag.exceptions import AIError
from sag.utils import get_logger
from sag.utils.text import TokenEstimator
import nltk



logger = get_logger("core.ai.sumy")

# 全局标志：确保 nltk 资源只初始化一次
_nltk_initialized = False

def init_nltk_tokenizers():
    """
    初始化 nltk 分词器资源

    使用全局标志确保只在模块首次导入时执行一次检查，
    避免每次实例化 SumySummarizer 时重复检查资源。
    """
    global _nltk_initialized

    # 如果已经初始化过，直接返回
    if _nltk_initialized:
        logger.debug("nltk分词器已初始化，跳过检查")
        return

    logger.info("初始化nltk分词器环境")
    # 需要检查和下载的资源（注意：nltk资源的实际路径是"tokenizers/资源名"）
    required_resources = [
        "tokenizers/punkt",    # punkt分词模型的实际路径
        "tokenizers/punkt_tab" # punkt_tab的实际路径
    ]
    # 记录缺失的资源
    missing_resources = []

    for resource in required_resources:
        try:
            # 检查资源是否已存在
            nltk.data.find(resource)
            logger.info(f"nltk资源 '{resource}' 已存在，跳过下载")
        except LookupError:
            # 资源不存在，加入待下载列表（提取资源名，如"punkt"）
            resource_name = resource.split("/")[-1]
            missing_resources.append(resource_name)
            logger.info(f"nltk资源 '{resource}' 不存在，将进行下载")

    # 仅下载缺失的资源
    if missing_resources:
        logger.info(f"开始下载缺失的nltk资源：{missing_resources}")
        nltk.download(missing_resources)
        logger.info("nltk分词器环境缺失资源下载完成")
    else:
        logger.info("所有nltk分词器资源已存在，无需下载")

    # 标记为已初始化
    _nltk_initialized = True
    logger.info("nltk分词器环境初始化完成")

# 模块导入时自动初始化 nltk 资源（只执行一次）
init_nltk_tokenizers()

class SumySummarizer:
    """集成 Sumy 和大模型的摘要生成器"""

    def __init__(
        self,
        model_config: Optional[Dict] = None,
    ):
        """
        初始化摘要生成器

        Args:
            model_config: LLM配置字典（可选）
                - 如果传入：使用该配置
                - 如果不传：自动从配置管理器获取 'summary' 场景配置
        """
        self.model_config = model_config
        self._llm_client = None  # 延迟初始化
        self.prompt_manager = get_prompt_manager()
        self.token_estimator = TokenEstimator(model_type="generic")

        logger.info("Sumy摘要生成器初始化完成")
    
    async def _get_llm_client(self) -> 'BaseLLMClient':
        """获取LLM客户端（懒加载）"""
        if self._llm_client is None:
            from sag.core.ai.factory import create_llm_client
            
            self._llm_client = await create_llm_client(
                scenario='summary',
                model_config=self.model_config
            )
        
        return self._llm_client

    def detect_language(self, text: str) -> str:
        """
        自动检测文本主要语言

        Args:
            text: 输入文本

        Returns:
            语言代码（"chinese" 或 "english"）
        """
        # 统计中文字符数量（包括中日韩统一表意文字）
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))

        # 统计英文字符数量（只统计字母）
        english_chars = len(re.findall(r'[a-zA-Z]', text))

        # 如果中文字符占比超过30%，认为是中文文本
        total_chars = chinese_chars + english_chars
        if total_chars == 0:
            # 如果都没有，默认使用英文
            return "english"

        chinese_ratio = chinese_chars / total_chars
        detected_lang = "chinese" if chinese_ratio > 0.3 else "english"

        logger.debug(
            f"语言检测完成",
            extra={
                "chinese_chars": chinese_chars,
                "english_chars": english_chars,
                "chinese_ratio": f"{chinese_ratio:.2%}",
                "detected_language": detected_lang
            }
        )

        return detected_lang

    def count_sentences(self, text: str, language: Optional[str] = None) -> int:
        """
        统计文本句子数

        Args:
            text: 输入文本
            language: 文本语言（chinese/english），None 表示自动检测

        Returns:
            句子数量
        """
        if language is None:
            language = self.detect_language(text)

        parser = PlaintextParser.from_string(text, Tokenizer(language))
        return len(list(parser.document.sentences))

    def extract_key_sentences(
        self,
        text: str,
        sentence_count: int = 5,
        language: Optional[str] = None
    ) -> List[str]:
        """
        智能提取关键句子（自动选择算法）

        根据文本句子数量自动选择最优算法：
        - ≤1000句子：使用 TextRank 算法（精确度高）
        - >1000句子：使用 Luhn 算法（速度快）

        Args:
            text: 输入文本
            sentence_count: 要提取的句子数量
            language: 文本语言（chinese/english），None 表示自动检测

        Returns:
            关键句子列表

        Raises:
            AIError: 提取失败
        """
        if language is None:
            language = self.detect_language(text)

        try:
            parser = PlaintextParser.from_string(text, Tokenizer(language))

            # 统计总句子数
            total_sentences = len(list(parser.document.sentences))

            # 根据句子数量选择算法
            if total_sentences <= 1000:
                # 小规模文本：使用 TextRank 算法（精确度高）
                logger.info(f"文本句子数 {total_sentences} ≤ 1000，使用 TextRank 算法")
                summarizer = TextRankSummarizer()
                summary_sentences = summarizer(parser.document, sentence_count)
                algorithm_name = "TextRank"
            else:
                # 大规模文本：使用 Luhn 算法（速度快）
                logger.info(f"文本句子数 {total_sentences} > 1000，使用 Luhn 算法")
                summarizer = LuhnSummarizer()
                summary_sentences = summarizer(parser.document, sentences_count=sentence_count)
                algorithm_name = "Luhn"

            key_sentences = [str(sentence) for sentence in summary_sentences]

            # 关键：用 enumerate 获取编号（start=1 确保从1开始，而非默认的0）
            # for idx, sentence in enumerate(summary_sentences, start=1):
            #     # 格式化输出 "编号. 句子内容"
            #     logger.debug(f"{idx}: {sentence}")

            logger.debug(
                f"{algorithm_name} 提取完成",
                extra={
                    "algorithm": algorithm_name,
                    "language": language,
                    "total_sentences": total_sentences,
                    "extracted_sentences": len(key_sentences)
                }
            )

            return key_sentences

        except Exception as e:
            raise AIError(f"关键句提取失败: {e}") from e

    def _calculate_optimal_sentence_count(
        self,
        text: str,
        sentence_count: Optional[int] = None,
        language: Optional[str] = None,
        compression_ratio: float = 0.3
    ) -> tuple[int, bool]:
        """
        根据文本长度计算最优提取句子数

        Args:
            text: 输入文本
            sentence_count: 用户指定的句子数（可选）
            language: 文本语言（可选）
            compression_ratio: 压缩率（0-1），默认0.3表示保留30%的句子

        Returns:
            (最优句子数, 是否跳过Sumy提取直接使用原文)
        """
        token_count = self.token_estimator.estimate_tokens(text)

        # 如果用户明确指定了句子数，优先使用用户指定的值
        if sentence_count is not None:
            return sentence_count, False, 300

        # 根据token数自动计算
        if token_count <= 5000:
            # 短文本：直接使用原文
            logger.info(f"文本长度 {token_count} tokens，跳过Sumy提取，直接使用原文")
            return 0, True, 300

        # 需要统计总句子数来计算压缩率
        if language is None:
            language = self.detect_language(text)

        total_sentences = self.count_sentences(text, language)
        calculated_sentences = max(1, int(total_sentences * compression_ratio))

        # 根据文本长度应用最大句子数限制
        if token_count <= 50000:
            # 中等文本：提取最多150个句子
            optimal_count = min(150, calculated_sentences)
            logger.info(f"文本长度 {token_count} tokens，句子数 {total_sentences}，{compression_ratio*100:.0f}%压缩={calculated_sentences}，限制最多150，实际提取 {optimal_count}")
            return optimal_count, False, 350
        elif token_count <= 400000:
            # 长文本：提取最多350个句子
            optimal_count = min(350, calculated_sentences)
            logger.info(f"文本长度 {token_count} tokens，句子数 {total_sentences}，{compression_ratio*100:.0f}%压缩={calculated_sentences}，限制最多350，实际提取 {optimal_count}")
            return optimal_count, False, 450
        else:
            # 超长文本：提取最多500个句子
            optimal_count = min(500, calculated_sentences)
            logger.info(f"文本长度 {token_count} tokens，句子数 {total_sentences}，{compression_ratio*100:.0f}%压缩={calculated_sentences}，限制最多500，实际提取 {optimal_count}")
            return optimal_count, False, 550

    async def generate_summary(
        self,
        text: str,
        sentence_count: Optional[int] = None,
        background: str = "文档",
        language: Optional[str] = None,
        compression_ratio: float = 0.3
    ) -> dict:
        """
        生成文本摘要（智能两阶段：Sumy提取 + LLM总结）

        根据文本token数自动选择处理策略：
        - ≤10000 tokens：直接使用原文进行摘要
        - >10000 tokens：按总句子数的压缩率提取关键句子，但受最大句子数限制
          - 10000~50000 tokens：最多150个句子
          - 50000~400000 tokens：最多350个句子
          - >400000 tokens：最多500个句子

        Args:
            text: 输入文本
            sentence_count: 提取的句子数量（可选，None表示自动计算）
            background: 背景信息
            language: 文本语言（chinese/english），None 表示自动检测
            compression_ratio: 压缩率（0-1），默认0.3表示保留30%的句子

        Returns:
            包含以下字段的字典：
            - title (str): 文档标题
            - summary (str): 文档摘要
            - category (str): 文档分类（技术文档、业务文档、个人笔记、其他）
            - tags (List[str]): 关键词标签列表

        Raises:
            AIError: 摘要生成失败（包括 JSON 解析失败、Schema 验证失败等）

        Example:
            >>> summarizer = SumySummarizer()
            >>> # 自动检测语言和句子数（使用默认30%压缩率）
            >>> result = await summarizer.generate_summary(
            ...     text="长文本...",
            ...     background="AI发展报告"
            ... )
            >>> print(result["title"])  # 访问标题
            >>> print(result["summary"])  # 访问摘要
            >>> # 手动指定句子数（覆盖自动计算）
            >>> result = await summarizer.generate_summary(
            ...     text="长文本...",
            ...     sentence_count=10,
            ...     background="AI发展报告"
            ... )
            >>> # 自定义压缩率
            >>> result = await summarizer.generate_summary(
            ...     text="长文本...",
            ...     compression_ratio=0.2,  # 20%压缩
            ...     background="AI发展报告"
            ... )

        Note:
            使用 chat_with_schema() 自动获得：
            - JSON 解析（自动处理 markdown 代码块）
            - Schema 验证（验证必需字段）
            - 失败重试（最多3次，指数退避）
        """
        # 记录总开始时间
        total_start_time = time.time()
        sumy_time = 0.0
        llm_time = 0.0

        if language is None:
            language = self.detect_language(text)

        try:
            # 计算最优句子数和是否跳过提取（传递language和compression_ratio）
            optimal_count, skip_extraction,token_limit = self._calculate_optimal_sentence_count(
                text, sentence_count, language, compression_ratio
            )

            token_count = self.token_estimator.estimate_tokens(text)
            logger.info(
                f"开始生成摘要: {background}",
                extra={
                    "language": language,
                    "token_count": token_count,
                    "compression_ratio": compression_ratio if sentence_count is None else "N/A (manual)",
                    "skip_extraction": skip_extraction,
                    "sentence_count": optimal_count if not skip_extraction else "N/A"
                }
            )

            if skip_extraction:
                # 短文本：直接使用原文
                content_for_summary = text
                logger.debug(f"使用原文直接生成摘要（{token_count} tokens）")
            else:
                # 阶段1: 使用智能算法提取关键句子
                sumy_start_time = time.time()

                key_sentences = self.extract_key_sentences(text, optimal_count, language)

                if not key_sentences:
                    raise AIError("未能提取到关键句子")

                sumy_time = time.time() - sumy_start_time
                logger.debug(f"提取了 {len(key_sentences)} 个关键句子，耗时 {sumy_time:.2f} 秒")

                # 将关键句子组合成文本
                content_for_summary = "\n".join(f"{i+1}. {s}" for i, s in enumerate(key_sentences))

            # 阶段2: 使用大模型进行总结润色
            llm_start_time = time.time()

            prompt = self.prompt_manager.render(
                "article_metadata_with_sumy",
                token_limit=token_limit,
                background=background,
                content=content_for_summary
            )

            messages = [
                LLMMessage(role=LLMRole.USER, content=prompt)
            ]

            # 定义期望的 JSON Schema（与 prompt 中的格式对应）
            response_schema = {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "文档标题"
                    },
                    "summary": {
                        "type": "string",
                        "description": "文档摘要"
                    },
                    "category": {
                        "type": "string",
                        "description": "文档分类（技术文档、业务文档、个人笔记、其他）"
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "关键词标签列表"
                    }
                },
                "required": ["title", "summary", "category", "tags"]
            }

            # 使用 chat_with_schema 自动获得：
            # 1. JSON 解析（自动处理 markdown 代码块）
            # 2. Schema 验证（验证必需字段）
            # 3. 失败重试（最多3次，指数退避）
            # 注意：所有参数从配置读取，不硬编码
            llm_client = await self._get_llm_client()
            result = await llm_client.chat_with_schema(
                messages,
                response_schema=response_schema,
                temperature=0.3,  # 摘要场景使用低温度保证稳定性（未来可从数据库配置）
                # max_tokens 和 timeout 从环境变量读取
            )

            # result 已经是解析和验证后的字典
            summary = result.get("summary", "").strip()
            llm_time = time.time() - llm_start_time

            # 计算总耗时
            total_time = time.time() - total_start_time

            logger.info(
                f"摘要生成成功: {background}",
                extra={
                    "original_token_count": token_count,
                    "summary_length": len(summary),
                    "extraction_used": not skip_extraction,
                    "time_sumy": f"{sumy_time:.2f}s" if not skip_extraction else "N/A",
                    "time_llm": f"{llm_time:.2f}s",
                    "time_total": f"{total_time:.2f}s"
                }
            )

            # 输出时间统计
            print("\n" + "=" * 70)
            print("时间消耗统计:")
            print("-" * 70)
            if not skip_extraction:
                print(f"  Sumy 提取句子耗时: {sumy_time:.2f} 秒")
            else:
                print(f"  Sumy 提取句子耗时: 跳过（使用原文）")
            print(f"  LLM 总结摘要耗时:  {llm_time:.2f} 秒")
            print(f"  总耗时:           {total_time:.2f} 秒")
            print("=" * 70)

            # 返回完整的结果字典（包含 title, summary, category, tags）
            # 不再返回 LLMResponse 对象，而是直接返回解析后的字典
            return result

        except Exception as e:
            logger.error(f"摘要生成失败: {e}", exc_info=True)
            raise AIError(f"摘要生成失败: {e}") from e

    async def generate_summary_with_ratio(
        self,
        text: str,
        compression_ratio: float = 0.3,
        background: str = "文档",
        language: Optional[str] = None
    ) -> dict:
        """
        根据压缩率生成摘要（便捷方法）

        这是 generate_summary 的便捷包装方法，直接使用压缩率参数。

        Args:
            text: 输入文本
            compression_ratio: 压缩率（0-1），例如 0.2 表示保留 20% 的句子
            background: 背景信息
            language: 文本语言（chinese/english），None 表示自动检测

        Returns:
            包含以下字段的字典：
            - title (str): 文档标题
            - summary (str): 文档摘要
            - category (str): 文档分类
            - tags (List[str]): 关键词标签列表

        Example:
            >>> summarizer = SumySummarizer()
            >>> # 使用20%压缩率
            >>> result = await summarizer.generate_summary_with_ratio(
            ...     text="长文本...",
            ...     compression_ratio=0.2
            ... )
        """
        return await self.generate_summary(
            text=text,
            sentence_count=None,  # 使用自动计算
            background=background,
            language=language,
            compression_ratio=compression_ratio
        )


# ============================================================================
# 全局单例实例（模块级别）
# ============================================================================
# 创建一个全局 SumySummarizer 实例供整个应用复用
# 原因：SumySummarizer 是无状态工具类，不需要每次创建新实例
# 性能优化：避免重复初始化 LLM 客户端、prompt 管理器、token estimator
_global_summarizer = None

def get_sumy_summarizer() -> SumySummarizer:
    """
    获取全局 SumySummarizer 单例实例

    这个函数返回一个全局共享的 SumySummarizer 实例，避免重复创建。
    SumySummarizer 是无状态的工具类，可以安全地在多个地方复用，
    包括并发场景和测试环境。

    Returns:
        SumySummarizer: 全局单例实例

    Example:
        >>> from sag.core.ai.sumy import get_sumy_summarizer
        >>> summarizer = get_sumy_summarizer()
        >>> result = await summarizer.generate_summary(text)
    """
    global _global_summarizer
    if _global_summarizer is None:
        logger.info("创建全局 SumySummarizer 单例实例")
        _global_summarizer = SumySummarizer()
    return _global_summarizer
