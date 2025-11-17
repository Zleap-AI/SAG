"""
工具函数模块
"""

from sag.utils.logger import get_logger, logger, setup_logging
from sag.utils.text import (
    clean_whitespace,
    compute_text_hash,
    count_chinese_characters,
    estimate_tokens,
    extract_markdown_headings,
    normalize_entity_name,
    normalize_text,
    split_text_by_paragraphs,
    truncate_text,
)
from sag.utils.time import (
    calculate_time_decay,
    format_datetime,
    get_time_ago,
    get_utc_now,
    parse_iso_datetime,
)
from sag.utils.text import TokenEstimator

__all__ = [
    # Logger
    "setup_logging",
    "get_logger",
    "logger",
    # Text
    "normalize_text",
    "normalize_entity_name",
    "extract_markdown_headings",
    "compute_text_hash",
    "truncate_text",
    "split_text_by_paragraphs",
    "estimate_tokens",
    "clean_whitespace",
    "count_chinese_characters",
    # Time
    "get_utc_now",
    "parse_iso_datetime",
    "format_datetime",
    "get_time_ago",
    "calculate_time_decay",
    # Token
    "TokenEstimator",
]
