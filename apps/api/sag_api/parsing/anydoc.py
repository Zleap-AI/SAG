"""本地 AnyDoc 文档转换适配层。

`firecrawl-anydoc` 是 anydoc Rust 库的 Python 绑定（导入名 `anydoc`），把
docx/pptx/xlsx/epub/pdf 等格式转成 GitHub-Flavored Markdown。这里只做三件事：

- 延迟导入，缺失依赖时给出可读的配置错误，而不是让 ImportError 逃逸；
- 把 anydoc 的转换异常映射到 SAG 领域异常，并标明是否可重试；
- CSV 先复用 :mod:`sag_api.parsing.text` 的编码识别规范成 UTF-8，
  再按 CSV 转换（AnyDoc 的 CSV 回退编码是 Windows-1252，直接喂 GBK
  字节会得到静默乱码）。

本模块保持同步：调用方负责放进线程执行。转换本身不做 OCR —— 调用始终显式
`ocr="reject"`，即使存在 `FIRECRAWL_API_KEY` 等环境变量也不会走 Firecrawl
托管服务。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from sag_api.core.error_taxonomy import ErrorStage
from sag_api.core.errors import (
    ApiError,
    ConfigurationError,
    ValidationError,
)
from sag_api.parsing.text import TextDecodingError, read_text_file

#: 本地适配规则版本。CSV 编码规范化规则、格式路由和异常映射的语义变更都要提升它，
#: 否则旧缓存会被当成本次结果继续复用。
ADAPTER_VERSION = "v2"
#: 转换提供者标识；同时用于缓存签名与解析状态。
PROVIDER = "anydoc"
_ANYDOC_DISTRIBUTION = "firecrawl-anydoc"
# 依赖固定版本，导入名仍为 anydoc；签名带上版本，升级包即失效旧缓存。
_ANYDOC_PACKAGE_VERSION = "0.2.4"

#: 交由 AnyDoc 直接按文件路径转换的扩展名。
# Excel 继续由 MarkItDown 转换：当前引擎的记录组对齐依赖其 Markdown 形态。
PATH_SUFFIXES = frozenset({".docx", ".pptx", ".epub", ".pdf"})
#: 需要先规范化编码再由 AnyDoc 转换的扩展名。
TEXT_SUFFIXES = frozenset({".csv"})

_CONVERT_ERROR_NAMES = (
    "UnsupportedError",
    "NeedsOcrError",
    "MalformedError",
    "EncryptedError",
    "ResourceLimitError",
    "MissingPartError",
    "HostedError",
)


class AnyDocError(ApiError):
    """AnyDoc 未预期的转换或协议错误，不允许解析器回退。"""


class AnyDocUnsupportedError(AnyDocError):
    """AnyDoc 不支持文件，允许尝试一次 MarkItDown 回退。"""


def signature() -> str:
    """AnyDoc 独立缓存签名：包版本 + 本地适配规则版本。"""
    return f"{PROVIDER}-{_ANYDOC_PACKAGE_VERSION}-{ADAPTER_VERSION}"


def handles_path(path: str) -> bool:
    """该路径是否由 AnyDoc 转换（含需要先做编码规范化的文本类）。"""
    return Path(path).suffix.lower() in (PATH_SUFFIXES | TEXT_SUFFIXES)


def requires_text_normalization(path: str) -> bool:
    """是否需要先复用 SAG 的解码逻辑规范成 UTF-8 再交给 AnyDoc。"""
    return Path(path).suffix.lower() in TEXT_SUFFIXES


def convert(path: str) -> str:
    """把文件转成 Markdown；失败时抛出 :class:`AnyDocError` 等 SAG 领域异常。"""
    if requires_text_normalization(path):
        return _convert_text_sync(path)
    return _convert_path_sync(path)


def _convert_path_sync(path: str) -> str:
    anydoc = _load_anydoc()
    markdown = _call_with_classified_errors(
        anydoc, lambda: anydoc.to_markdown(path, ocr="reject")
    )
    return _require_markdown(markdown)


def _convert_text_sync(path: str) -> str:
    """CSV：先用 SAG 的编码识别得到可靠 Unicode，再按 UTF-8 字节交给 AnyDoc。

    不修改上传原文件，也不对内容做整段 strip —— 否则会改变 AnyDoc 的
    表头/字段切分。解码不可靠或存在替换字符时直接失败，避免把乱码写进缓存。
    """
    try:
        decoded = read_text_file(path)
    except TextDecodingError as exc:
        raise ValidationError(f"CSV 文本编码识别失败：{exc}") from exc
    if decoded.replacement_count:
        raise ValidationError(
            f"CSV 文本编码识别不可靠（{decoded.encoding} 解码出现 "
            f"{decoded.replacement_count} 个替换字符），请用 UTF-8 重新导出后再上传"
        )
    anydoc = _load_anydoc()
    data = decoded.text.encode("utf-8")
    markdown = _call_with_classified_errors(
        anydoc, lambda: anydoc.to_markdown_bytes(data, "csv", ocr="reject")
    )
    return _require_markdown(markdown)


def _load_anydoc() -> Any:
    """延迟导入；缺失或原生扩展加载失败按配置错误报告，不触发回退。"""
    try:
        import anydoc  # noqa: PLC0415 - 延迟导入：未启用 AnyDoc 的部署不需要它
    except ImportError as exc:
        raise ConfigurationError(
            f"AnyDoc 未安装，请安装 {_ANYDOC_DISTRIBUTION}；"
            f"或在设置中改用其他解析器（原因：{exc}）"
        ) from exc
    except OSError as exc:
        raise ConfigurationError(
            f"AnyDoc 原生扩展加载失败（{exc}）；请确认当前平台有对应的预编译 wheel"
        ) from exc
    return anydoc


def _call_with_classified_errors(anydoc: Any, call: Callable[[], Any]) -> Any:
    """执行一次 AnyDoc 转换；只在这里翻译 anydoc 自己抛出的转换异常。"""
    try:
        return call()
    except _convert_error_types(anydoc) as exc:
        raise classify_convert_error(anydoc, exc) from exc


def _convert_error_types(anydoc: Any) -> tuple[type[BaseException], ...]:
    base = getattr(anydoc, "ConvertError", None)
    if isinstance(base, type) and issubclass(base, BaseException):
        return (base,)
    # 极端情况下（绑定缺失 ConvertError）退回显式列出各子类。
    candidates: list[type[BaseException]] = []
    for name in _CONVERT_ERROR_NAMES:
        candidate = getattr(anydoc, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            candidates.append(candidate)
    return tuple(candidates)


def classify_convert_error(anydoc: Any, error: BaseException) -> ApiError:
    """AnyDoc 转换异常 → SAG 领域异常。

    除 `UnsupportedError` 外一律不可重试、也不触发解析器回退：它们要么是文件
    本身不可用，要么需要用户改配置。
    """
    name = type(error).__name__
    message = _error_message(error)
    if _is_instance(anydoc, error, "UnsupportedError"):
        # 只有这一类允许调用方尝试一次 MarkItDown 回退。
        return AnyDocUnsupportedError(
            f"AnyDoc 不支持该文件：{message}",
            stage=ErrorStage.PARSE,
            retryable=False,
        )
    if _is_instance(anydoc, error, "NeedsOcrError"):
        detail = _page_detail(getattr(error, "pages", None), getattr(error, "page_count", None))
        return ValidationError(
            f"AnyDoc 本地转换不做 OCR：{detail}需要 OCR，"
            f"请改用已配置的 MinerU 解析器后重新处理该文档",
            stage=ErrorStage.PARSE,
            retryable=False,
        )
    if _is_instance(anydoc, error, "EncryptedError"):
        return ValidationError(
            f"AnyDoc 无法解析加密或受密码保护的文件：{message}",
            stage=ErrorStage.PARSE,
            retryable=False,
        )
    if _is_instance(anydoc, error, "ResourceLimitError"):
        limit = getattr(error, "limit", None)
        suffix = f"（触发限制：{limit}）" if limit else ""
        return ValidationError(
            f"AnyDoc 转换超出安全限制{suffix}：{message}",
            stage=ErrorStage.PARSE,
            retryable=False,
        )
    if _is_instance(anydoc, error, "MalformedError"):
        part = getattr(error, "part", None)
        suffix = f"（问题部分：{part}）" if part else ""
        return ValidationError(
            f"AnyDoc 无法从文件中解析出有效内容{suffix}：{message}",
            stage=ErrorStage.PARSE,
            retryable=False,
        )
    if _is_instance(anydoc, error, "MissingPartError"):
        part = getattr(error, "part", None)
        suffix = f"（缺失部分：{part}）" if part else ""
        return ValidationError(
            f"AnyDoc 认为文件缺少必要部分{suffix}：{message}",
            stage=ErrorStage.PARSE,
            retryable=False,
        )
    if _is_instance(anydoc, error, "HostedError"):
        # 本适配层不会请求托管服务；出现即说明调用参数被改坏。
        return ConfigurationError(
            f"AnyDoc 意外进入托管 OCR 分支：{message}", stage=ErrorStage.PARSE
        )
    return AnyDocError(
        f"AnyDoc 转换失败（{name}）：{message}", stage=ErrorStage.PARSE, retryable=False
    )


def _is_instance(anydoc: Any, error: BaseException, name: str) -> bool:
    candidate = getattr(anydoc, name, None)
    return isinstance(candidate, type) and isinstance(error, candidate)


def _page_detail(pages: object, page_count: object) -> str:
    if isinstance(pages, (list, tuple)) and pages:
        listed = ", ".join(str(page) for page in pages[:20])
        more = "…" if len(pages) > 20 else ""
        total = f"（共 {page_count} 页）" if isinstance(page_count, int) else ""
        return f"第 {listed}{more} 页{total}"
    return "部分页面"


def _error_message(error: BaseException) -> str:
    message = str(error).strip() or type(error).__name__
    return message[:300]


def _require_markdown(markdown: object) -> str:
    if not isinstance(markdown, str):
        raise AnyDocError("AnyDoc 返回了未知结果格式", stage=ErrorStage.PARSE)
    text = markdown.strip()
    if not text or text.casefold() in {"none", "null", "undefined"}:
        raise ValidationError(
            "AnyDoc 未从文件中解析出有效文本",
            stage=ErrorStage.PARSE,
            retryable=False,
        )
    return text + "\n"


__all__ = [
    "ADAPTER_VERSION",
    "AnyDocError",
    "AnyDocUnsupportedError",
    "PATH_SUFFIXES",
    "PROVIDER",
    "TEXT_SUFFIXES",
    "classify_convert_error",
    "convert",
    "handles_path",
    "requires_text_normalization",
    "signature",
]
