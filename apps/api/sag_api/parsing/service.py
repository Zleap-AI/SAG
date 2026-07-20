"""文档解析路由、缓存与 MarkItDown 本地转换。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from sag_api.core.config import Settings
from sag_api.core.errors import (
    ApiError,
    ServiceUnavailableError,
    UpstreamError,
    ValidationError,
)
from sag_api.parsing.mineru import MinerUClient
from sag_api.parsing.text import TextDecodingError, is_plain_text_path, read_text_file

ParseStateCallback = Callable[[dict[str, Any]], Awaitable[None]]
_PARSE_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


@dataclass(frozen=True, slots=True)
class PreparedDocument:
    path: str
    provider: Literal["original", "markitdown", "mineru"]
    cached: bool = False
    fallback_from: Literal["mineru"] | None = None
    fallback_error: str | None = None


async def prepare_document(
    path: str,
    settings: Settings,
    *,
    state: dict[str, Any] | None = None,
    on_state: ParseStateCallback | None = None,
) -> PreparedDocument:
    """返回可直接交给 zleap-sag 的 Markdown 路径，保留原始上传文件。"""
    suffix = os.path.splitext(path)[1].lower()
    if suffix in {".md", ".markdown"}:
        return PreparedDocument(path=path, provider="original")

    use_mineru = suffix == ".pdf" and settings.effective_document_parser == "mineru"
    provider: Literal["markitdown", "mineru"] = "mineru" if use_mineru else "markitdown"
    signature = _signature(provider, settings)
    cache_path = f"{path}.parsed.{signature}.md"
    if _is_cached(cache_path):
        return PreparedDocument(path=cache_path, provider=provider, cached=True)
    cached_fallback = _cached_fallback_document(path, provider, signature, settings)
    if cached_fallback:
        return cached_fallback

    # 同一进程内同一文档只做一次转换，避免并发“重新处理”重复创建付费任务。
    async with _lock_for(cache_path):
        if _is_cached(cache_path):
            return PreparedDocument(path=cache_path, provider=provider, cached=True)
        cached_fallback = _cached_fallback_document(path, provider, signature, settings)
        if cached_fallback:
            return cached_fallback
        return await _prepare_and_cache(
            path,
            cache_path,
            provider,
            signature,
            settings,
            state=state,
            on_state=on_state,
        )


async def _prepare_and_cache(
    path: str,
    cache_path: str,
    provider: Literal["markitdown", "mineru"],
    signature: str,
    settings: Settings,
    *,
    state: dict[str, Any] | None,
    on_state: ParseStateCallback | None,
) -> PreparedDocument:
    parser_state = _compatible_state(state, provider, signature, settings)
    current_state = dict(parser_state)

    async def track_state(next_state: dict[str, Any]) -> None:
        nonlocal current_state
        current_state = dict(next_state)
        if on_state:
            await on_state(current_state)

    if on_state:
        await track_state(parser_state)

    if provider == "mineru":
        fallback_signature = _signature("markitdown", settings)
        fallback_cache_path = f"{path}.parsed.{fallback_signature}.md"
        fallback_marker_path = _fallback_marker_path(path, signature, settings)
        fallback = _compatible_fallback(
            parser_state, fallback_signature, fallback_cache_path
        )
        if fallback and parser_state.get("status") == "fallback_done" and _is_cached(
            fallback_cache_path
        ):
            await asyncio.to_thread(_write_fallback_marker, fallback_marker_path)
            return PreparedDocument(
                path=fallback_cache_path,
                provider="markitdown",
                cached=True,
                fallback_from="mineru",
                fallback_error=_state_string(fallback, "mineru_error"),
            )
        if fallback and parser_state.get("status") in {
            "fallback_running",
            "fallback_done",
        }:
            return await _prepare_markitdown_fallback(
                path,
                parser_state,
                fallback_cache_path,
                fallback_signature,
                fallback_marker_path,
                mineru_message=_state_string(fallback, "mineru_error")
                or "MinerU 解析失败",
                mineru_error_code=_state_string(fallback, "mineru_error_code")
                or UpstreamError.code,
                on_state=track_state,
            )
        try:
            markdown = await MinerUClient(settings).parse(
                path, state=parser_state, on_state=track_state
            )
        except ApiError as mineru_error:
            return await _prepare_markitdown_fallback(
                path,
                current_state,
                fallback_cache_path,
                fallback_signature,
                fallback_marker_path,
                mineru_message=_exception_message(mineru_error),
                mineru_error_code=mineru_error.code,
                on_state=track_state,
            )
    else:
        markdown = (
            await asyncio.to_thread(_convert_plain_text, path)
            if is_plain_text_path(path)
            else await _convert_with_markitdown(path)
        )

    await asyncio.to_thread(_write_markdown, cache_path, markdown)
    if on_state:
        await on_state(
            {
                **current_state,
                "provider": provider,
                "signature": signature,
                "status": "done",
                "cache_path": cache_path,
            }
        )
    return PreparedDocument(path=cache_path, provider=provider)


async def _prepare_markitdown_fallback(
    path: str,
    parser_state: dict[str, Any],
    cache_path: str,
    signature: str,
    marker_path: str,
    *,
    mineru_message: str,
    mineru_error_code: str,
    on_state: ParseStateCallback,
) -> PreparedDocument:
    fallback_state = {
        "provider": "markitdown",
        "signature": signature,
        "status": "running",
        # 只用于诊断；恢复时始终从原文件路径重新推导并校验缓存路径。
        "cache_path": cache_path,
        "mineru_error": mineru_message,
        "mineru_error_code": mineru_error_code,
    }
    running_state = {
        **parser_state,
        "status": "fallback_running",
        "fallback": fallback_state,
    }
    await on_state(running_state)

    fallback_cached = False
    try:
        async with _lock_for(cache_path):
            fallback_cached = _is_cached(cache_path)
            if not fallback_cached:
                markdown = await _convert_with_markitdown(path)
                await asyncio.to_thread(_write_markdown, cache_path, markdown)
            await asyncio.to_thread(_write_fallback_marker, marker_path)
    except Exception as fallback_error:  # noqa: BLE001 - 本地转换/写盘错误合并上游原因
        fallback_message = _exception_message(fallback_error)
        await on_state(
            {
                **running_state,
                "status": "fallback_failed",
                "fallback": {
                    **fallback_state,
                    "status": "failed",
                    "markitdown_error": fallback_message,
                },
            }
        )
        message = (
            f"MinerU 解析失败：{mineru_message}；"
            f"MarkItDown 回退失败：{fallback_message}"
        )
        if mineru_error_code == ServiceUnavailableError.code:
            raise ServiceUnavailableError(message) from fallback_error
        if mineru_error_code == UpstreamError.code:
            raise UpstreamError(message) from fallback_error
        raise ValidationError(message) from fallback_error

    await on_state(
        {
            **running_state,
            "status": "fallback_done",
            "fallback": {
                **fallback_state,
                "status": "done",
                "cached": fallback_cached,
            },
        }
    )
    return PreparedDocument(
        path=cache_path,
        provider="markitdown",
        cached=fallback_cached,
        fallback_from="mineru",
        fallback_error=mineru_message,
    )


def _compatible_fallback(
    state: dict[str, Any], signature: str, cache_path: str
) -> dict[str, Any] | None:
    fallback = state.get("fallback")
    if not isinstance(fallback, dict):
        return None
    if (
        fallback.get("provider") != "markitdown"
        or fallback.get("signature") != signature
        or fallback.get("cache_path") != cache_path
    ):
        return None
    return fallback


def _state_string(state: dict[str, Any], key: str) -> str | None:
    value = state.get(key)
    return value if isinstance(value, str) and value else None


def _cached_fallback_document(
    path: str,
    provider: Literal["markitdown", "mineru"],
    signature: str,
    settings: Settings,
) -> PreparedDocument | None:
    if provider != "mineru":
        return None
    cache_path = f"{path}.parsed.{_signature('markitdown', settings)}.md"
    marker_path = _fallback_marker_path(path, signature, settings)
    if not (_is_cached(marker_path) and _is_cached(cache_path)):
        return None
    return PreparedDocument(
        path=cache_path,
        provider="markitdown",
        cached=True,
        fallback_from="mineru",
        fallback_error="MinerU 曾解析失败，已复用 MarkItDown 回退缓存",
    )


def _fallback_marker_path(path: str, signature: str, settings: Settings) -> str:
    identity = "\0".join(
        (
            signature,
            str(settings.mineru_base_url or ""),
            _mineru_key_fingerprint(settings),
        )
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return f"{path}.parsed.{signature}.fallback-{digest}.marker"


def _write_fallback_marker(path: str) -> None:
    _write_markdown(path, "markitdown\n")


def _is_cached(path: str) -> bool:
    try:
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            return False
        if not path.lower().endswith(".md"):
            return True
        with open(path, encoding="utf-8") as cached:
            return _is_meaningful_markdown(cached.read(4096))
    except (OSError, UnicodeError):
        return False


def _exception_message(error: Exception) -> str:
    message = getattr(error, "message", None) or str(error) or error.__class__.__name__
    return str(message).strip()[:500]


def _lock_for(path: str) -> asyncio.Lock:
    lock = _PARSE_LOCKS.get(path)
    if lock is None:
        lock = asyncio.Lock()
        _PARSE_LOCKS[path] = lock
    return lock


def parsed_sidecar_paths(path: str) -> list[str]:
    """列出一个原文件旁的解析缓存，供删除文档时一并清理。"""
    directory = os.path.dirname(path) or "."
    prefix = os.path.basename(path) + ".parsed."
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return [os.path.join(directory, name) for name in names if name.startswith(prefix)]


def _signature(provider: str, settings: Settings) -> str:
    if provider == "mineru":
        return f"mineru-{settings.mineru_version}-{settings.mineru_parse_method}"
    return "markitdown"


def _compatible_state(
    state: dict[str, Any] | None,
    provider: str,
    signature: str,
    settings: Settings,
) -> dict[str, Any]:
    expected = {
        "provider": provider,
        "signature": signature,
        "base_url": settings.mineru_base_url if provider == "mineru" else None,
        "key_fingerprint": _mineru_key_fingerprint(settings)
        if provider == "mineru"
        else "",
    }
    current = dict(state or {})
    if any(current.get(key) != value for key, value in expected.items()):
        return expected
    return current


def _mineru_key_fingerprint(settings: Settings) -> str:
    if not settings.mineru_api_key:
        return ""
    return hashlib.sha256(settings.mineru_api_key.encode()).hexdigest()[:12]


async def _convert_with_markitdown(path: str) -> str:
    try:
        markdown = await asyncio.to_thread(_markitdown_sync, path)
    except (ImportError, ModuleNotFoundError) as exc:
        raise UpstreamError("MarkItDown 未安装，无法解析该文件") from exc
    except Exception as exc:  # noqa: BLE001 - 第三方转换器错误统一映射
        raise ValidationError(f"MarkItDown 解析失败：{exc}") from exc
    markdown = markdown.strip()
    if not _is_meaningful_markdown(markdown):
        raise ValidationError("MarkItDown 未从文件中解析出有效文本")
    return markdown + "\n"


def _convert_plain_text(path: str) -> str:
    try:
        decoded = read_text_file(path)
    except TextDecodingError as exc:
        raise ValidationError(f"文本编码识别失败：{exc}") from exc
    text = decoded.text.strip()
    if not _is_meaningful_markdown(text):
        raise ValidationError("文本文件中没有可解析的有效内容")
    return text + "\n"


def _is_meaningful_markdown(markdown: str) -> bool:
    normalized = markdown.strip().casefold()
    return bool(normalized) and normalized not in {
        "none",
        "null",
        "undefined",
        "nan",
        "{}",
        "[]",
    }


def _markitdown_sync(path: str) -> str:
    from markitdown import MarkItDown

    result = MarkItDown().convert(path)
    markdown = getattr(result, "markdown", None)
    if markdown is None:  # 兼容 0.0.x / 早期 0.1.x 返回对象
        markdown = getattr(result, "text_content", None)
    if not isinstance(markdown, str):
        raise TypeError("MarkItDown 返回了未知结果格式")
    return markdown


def _write_markdown(path: str, markdown: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".parsed-", suffix=".md", dir=os.path.dirname(path) or "."
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(markdown)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise
