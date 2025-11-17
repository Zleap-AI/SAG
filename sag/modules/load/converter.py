"""
文档格式转换器

使用 MarkItDown 将各种文档格式转换为 Markdown
"""

from pathlib import Path
from typing import Optional

from markitdown import MarkItDown

from sag.exceptions import LoadError
from sag.utils import get_logger

logger = get_logger("modules.load.converter")


class DocumentConverter:
    """文档格式转换器"""

    # 支持的文件格式
    SUPPORTED_EXTENSIONS = {
        # 原生 Markdown
        '.md', '.markdown',
        # Office 文档
        '.docx', '.doc', '.pptx', '.ppt', '.xlsx', '.xls',
        # PDF
        '.pdf',
        # 网页
        '.html', '.htm',
        # 图片（带 OCR）
        '.png', '.jpg', '.jpeg',
        # 其他
        '.txt', '.csv', '.json', '.xml'
    }

    def __init__(self):
        """初始化转换器"""
        self.converter = MarkItDown()
        logger.info(f"文档转换器初始化完成，支持格式: {', '.join(self.SUPPORTED_EXTENSIONS)}")

    def is_supported(self, file_path: Path) -> bool:
        """
        检查文件格式是否支持

        Args:
            file_path: 文件路径

        Returns:
            bool: 是否支持该格式
        """
        return file_path.suffix.lower() in self.SUPPORTED_EXTENSIONS

    def convert_to_markdown(self, file_path: Path) -> str:
        """
        将文档转换为 Markdown

        Args:
            file_path: 文件路径

        Returns:
            str: 转换后的 Markdown 内容

        Raises:
            LoadError: 转换失败
        """
        if not file_path.exists():
            raise LoadError(f"文件不存在: {file_path}")

        if not self.is_supported(file_path):
            raise LoadError(
                f"不支持的文件格式: {file_path.suffix}。"
                f"支持的格式: {', '.join(self.SUPPORTED_EXTENSIONS)}"
            )

        try:
            logger.info(f"开始转换文档: {file_path.name} ({file_path.suffix})")

            # 如果已经是 Markdown，直接读取
            if file_path.suffix.lower() in {'.md', '.markdown'}:
                return file_path.read_text(encoding='utf-8')

            # 使用 MarkItDown 转换
            result = self.converter.convert(str(file_path))
            markdown_content = result.text_content

            if not markdown_content or not markdown_content.strip():
                raise LoadError(f"转换结果为空: {file_path}")

            logger.info(
                f"文档转换成功: {file_path.name}",
                extra={
                    "file_type": file_path.suffix,
                    "content_length": len(markdown_content)
                }
            )

            return markdown_content

        except Exception as e:
            logger.error(f"文档转换失败: {file_path}: {e}", exc_info=True)
            raise LoadError(f"文档转换失败: {e}") from e

