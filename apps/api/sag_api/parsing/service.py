"""文档解析路由、缓存与 MarkItDown 本地转换。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import posixpath
import tempfile
import weakref
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from xml.etree import ElementTree

from sag_api.core.config import Settings
from sag_api.core.errors import (
    ApiError,
    ServiceUnavailableError,
    UpstreamError,
    ValidationError,
)
from sag_api.parsing import anydoc as anydoc_parser
from sag_api.parsing.mineru import MinerUClient, ParsePaused, PauseCallback
from sag_api.parsing.mineru_artifacts import (
    remove_path,
    rewrite_image_links,
    write_assets,
)
from sag_api.parsing.text import TextDecodingError, is_plain_text_path, read_text_file

ParseStateCallback = Callable[[dict[str, Any]], Awaitable[None]]
ParserProvider = Literal["markitdown", "mineru", "anydoc"]
FallbackSource = Literal["mineru", "anydoc"]
_PARSE_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

_DOCX_EXTENSION = ".docx"
_IMAGE_RELATIONSHIP_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
)
_PACKAGE_RELATIONSHIP_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/relationships"
_RELATIONSHIP_TAG = f"{{{_PACKAGE_RELATIONSHIP_NAMESPACE}}}Relationship"
_WORD_RELATIONSHIPS_PREFIX = "word/_rels/"
_WORDPROCESSING_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DRAWING_TAG = f"{{{_WORDPROCESSING_NAMESPACE}}}drawing"
_PICT_TAG = f"{{{_WORDPROCESSING_NAMESPACE}}}pict"
_OFFICE_DOCUMENT_RELATIONSHIP_NAMESPACE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_RELATIONSHIP_EMBED_ATTRIBUTE = f"{{{_OFFICE_DOCUMENT_RELATIONSHIP_NAMESPACE}}}embed"
_RELATIONSHIP_ID_ATTRIBUTE = f"{{{_OFFICE_DOCUMENT_RELATIONSHIP_NAMESPACE}}}id"


@dataclass(frozen=True, slots=True)
class PreparedDocument:
    path: str
    provider: Literal["original", "markitdown", "mineru", "anydoc"]
    cached: bool = False
    fallback_from: FallbackSource | None = None
    fallback_error: str | None = None


async def prepare_document(
    path: str,
    settings: Settings,
    *,
    state: dict[str, Any] | None = None,
    on_state: ParseStateCallback | None = None,
    should_pause: PauseCallback | None = None,
) -> PreparedDocument:
    """返回可直接交给 zleap-sag 的 Markdown 路径，保留原始上传文件。"""
    suffix = os.path.splitext(path)[1].lower()
    if suffix in {".md", ".markdown"}:
        return PreparedDocument(path=path, provider="original")

    use_mineru = suffix == ".pdf" and settings.effective_document_parser == "mineru"
    # AnyDoc 接管适配层支持的格式；Excel 沿用 MarkItDown，保留记录组边界。
    use_anydoc = (
        not use_mineru
        and settings.effective_document_parser == anydoc_parser.PROVIDER
        and anydoc_parser.handles_path(path)
    )
    if use_mineru:
        provider: ParserProvider = "mineru"
    elif use_anydoc:
        provider = "anydoc"
    else:
        provider = "markitdown"
    signature = _signature(provider, settings)
    cache_path = f"{path}.parsed.{signature}.md"
    if _is_cached(cache_path):
        await _emit_cached_state(
            state, provider, signature, cache_path, settings, on_state
        )
        return PreparedDocument(path=cache_path, provider=provider, cached=True)
    cached_fallback = _cached_fallback_document(path, provider, signature, settings)
    if cached_fallback:
        await _emit_cached_fallback_state(
            state, provider, signature, cached_fallback, settings, on_state
        )
        return cached_fallback

    # 同一进程内同一文档只做一次转换，避免并发“重新处理”重复创建付费任务。
    async with _lock_for(cache_path):
        if _is_cached(cache_path):
            await _emit_cached_state(
                state, provider, signature, cache_path, settings, on_state
            )
            return PreparedDocument(path=cache_path, provider=provider, cached=True)
        cached_fallback = _cached_fallback_document(path, provider, signature, settings)
        if cached_fallback:
            await _emit_cached_fallback_state(
                state, provider, signature, cached_fallback, settings, on_state
            )
            return cached_fallback
        return await _prepare_and_cache(
            path,
            cache_path,
            provider,
            signature,
            settings,
            state=state,
            on_state=on_state,
            should_pause=should_pause,
        )


async def _emit_cached_fallback_state(
    state: dict[str, Any] | None,
    provider: ParserProvider,
    signature: str,
    prepared: PreparedDocument,
    settings: Settings,
    on_state: ParseStateCallback | None,
) -> None:
    if not on_state:
        return
    fallback = state.get("fallback") if isinstance(state, dict) else None
    fallback_state = dict(fallback) if isinstance(fallback, dict) else {}
    source = prepared.fallback_from or provider
    error_key = _fallback_error_key(source)
    await on_state(
        {
            **_compatible_state(state, provider, signature, settings),
            "status": "fallback_done",
            "fallback": {
                **fallback_state,
                "provider": "markitdown",
                "status": "done",
                "cached": True,
                error_key: fallback_state.get(error_key) or prepared.fallback_error,
            },
        }
    )


async def _emit_cached_state(
    state: dict[str, Any] | None,
    provider: ParserProvider,
    signature: str,
    cache_path: str,
    settings: Settings,
    on_state: ParseStateCallback | None,
) -> None:
    if on_state:
        await on_state(
            {
                **_compatible_state(state, provider, signature, settings),
                "status": "done",
                "cache_path": cache_path,
            }
        )


async def _prepare_and_cache(
    path: str,
    cache_path: str,
    provider: ParserProvider,
    signature: str,
    settings: Settings,
    *,
    state: dict[str, Any] | None,
    on_state: ParseStateCallback | None,
    should_pause: PauseCallback | None = None,
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
        fallback_marker_path = _fallback_marker_path(
            path, "mineru", signature, settings
        )
        fallback = _compatible_fallback(
            parser_state, fallback_signature, fallback_cache_path, "mineru"
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
                fallback_from="mineru",
                source_message=_state_string(fallback, "mineru_error")
                or "MinerU 解析失败",
                source_error_code=_state_string(fallback, "mineru_error_code")
                or UpstreamError.code,
                on_state=track_state,
            )
        try:
            client = MinerUClient(settings)
            parse_result = getattr(client, "parse_result", None)
            if parse_result is None:
                markdown = await client.parse(
                    path, state=parser_state, on_state=track_state, should_pause=should_pause
                )
            else:
                result = await parse_result(
                    path, state=parser_state, on_state=track_state, should_pause=should_pause
                )
                # sidecar 先于 Markdown 落盘：Markdown 缓存存在即代表其图片/结构化产物已就绪。
                markdown = await asyncio.to_thread(
                    _store_mineru_assets, cache_path, result.markdown, result.files
                )
        except ApiError as mineru_error:
            return await _prepare_markitdown_fallback(
                path,
                current_state,
                fallback_cache_path,
                fallback_signature,
                fallback_marker_path,
                fallback_from="mineru",
                source_message=_exception_message(mineru_error),
                source_error_code=mineru_error.code,
                on_state=track_state,
            )
    elif provider == "anydoc":
        return await _prepare_anydoc(
            path,
            cache_path,
            signature,
            settings,
            current_state=current_state,
            track_state=track_state,
            should_pause=should_pause,
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


async def _prepare_anydoc(
    path: str,
    cache_path: str,
    signature: str,
    settings: Settings,
    *,
    current_state: dict[str, Any],
    track_state: ParseStateCallback,
    should_pause: PauseCallback | None,
) -> PreparedDocument:
    """AnyDoc 本地转换；只有转换本身不支持文件时才回退一次 MarkItDown。

    运行中的 Rust 转换不承诺立即中断：这里只在转换前后做边界暂停检查。
    """
    if should_pause is not None and await should_pause():
        raise ParsePaused()
    await track_state({**current_state, "status": "running", "cache_path": cache_path})
    try:
        markdown = await asyncio.to_thread(anydoc_parser.convert, path)
    except anydoc_parser.AnyDocUnsupportedError as unsupported:
        # 仅 AnyDoc 转换本身的 UnsupportedError 允许回退；回调/写盘/控制流
        # 异常不在此分支，因此不会被包装成转换错误。
        if should_pause is not None and await should_pause():
            raise ParsePaused() from unsupported
        fallback_signature = _signature("markitdown", settings)
        fallback_cache_path = f"{path}.parsed.{fallback_signature}.md"
        fallback_marker_path = _fallback_marker_path(
            path, "anydoc", signature, settings
        )
        return await _prepare_markitdown_fallback(
            path,
            current_state,
            fallback_cache_path,
            fallback_signature,
            fallback_marker_path,
            fallback_from="anydoc",
            source_message=_exception_message(unsupported),
            source_error_code=unsupported.code,
            on_state=track_state,
            should_pause=should_pause,
        )
    if should_pause is not None and await should_pause():
        # 暂停发生在缓存写入前：恢复时需要重新转换，恢复后按签名复用旧缓存。
        raise ParsePaused()
    await asyncio.to_thread(_write_markdown, cache_path, markdown)
    await track_state(
        {
            **current_state,
            "provider": "anydoc",
            "signature": signature,
            "status": "done",
            "cache_path": cache_path,
        }
    )
    return PreparedDocument(path=cache_path, provider="anydoc")


async def _prepare_markitdown_fallback(
    path: str,
    parser_state: dict[str, Any],
    cache_path: str,
    signature: str,
    marker_path: str,
    *,
    fallback_from: FallbackSource,
    source_message: str,
    source_error_code: str,
    on_state: ParseStateCallback,
    should_pause: PauseCallback | None = None,
) -> PreparedDocument:
    if should_pause is not None and await should_pause():
        raise ParsePaused()
    error_key = _fallback_error_key(fallback_from)
    fallback_state = {
        "provider": "markitdown",
        "signature": signature,
        "status": "running",
        # 只用于诊断；恢复时始终从原文件路径重新推导并校验缓存路径。
        "cache_path": cache_path,
        "fallback_from": fallback_from,
        error_key: source_message,
    }
    if fallback_from == "mineru":
        fallback_state["mineru_error_code"] = source_error_code
    running_state = {
        **parser_state,
        "status": "fallback_running",
        "fallback": fallback_state,
    }
    await on_state(running_state)

    fallback_cached = False
    try:
        async with _lock_for(cache_path):
            if should_pause is not None and await should_pause():
                raise ParsePaused()
            fallback_cached = _is_cached(cache_path)
            if not fallback_cached:
                markdown = await _convert_with_markitdown(path)
                if should_pause is not None and await should_pause():
                    raise ParsePaused()
                await asyncio.to_thread(_write_markdown, cache_path, markdown)
            if should_pause is not None and await should_pause():
                raise ParsePaused()
            await asyncio.to_thread(_write_fallback_marker, marker_path)
    except ParsePaused:
        raise
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
            f"{_fallback_source_label(fallback_from)}解析失败：{source_message}；"
            f"MarkItDown 回退失败：{fallback_message}"
        )
        if fallback_from == "mineru":
            if source_error_code == ServiceUnavailableError.code:
                raise ServiceUnavailableError(message) from fallback_error
            if source_error_code == UpstreamError.code:
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
        fallback_from=fallback_from,
        fallback_error=source_message,
    )


def _compatible_fallback(
    state: dict[str, Any],
    signature: str,
    cache_path: str,
    source: FallbackSource,
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
    if not _fallback_source_matches(state, fallback, source):
        return None
    return fallback


def _fallback_source_matches(
    state: dict[str, Any], fallback: dict[str, Any], source: FallbackSource
) -> bool:
    """回退标记是否确实来自当前解析器。

    旧记录只在 `state.provider` 上标记来源，新记录同时写入 `fallback.fallback_from`。
    两者都不匹配时返回 False —— 历史 MinerU 标记不能阻止 AnyDoc 重新转换，
    反过来 AnyDoc 标记也不能被 MinerU 复用。
    """
    recorded = fallback.get("fallback_from")
    if isinstance(recorded, str):
        return recorded == source
    return state.get("provider") == source


def _fallback_error_key(source: FallbackSource) -> str:
    return f"{source}_error"


def _fallback_source_label(source: FallbackSource) -> str:
    return "AnyDoc" if source == "anydoc" else "MinerU"


def _state_string(state: dict[str, Any], key: str) -> str | None:
    value = state.get(key)
    return value if isinstance(value, str) and value else None


def _cached_fallback_document(
    path: str,
    provider: ParserProvider,
    signature: str,
    settings: Settings,
) -> PreparedDocument | None:
    if provider not in {"mineru", "anydoc"}:
        return None
    source: FallbackSource = "mineru" if provider == "mineru" else "anydoc"
    markitdown_signature = _signature("markitdown", settings)
    cache_path = f"{path}.parsed.{markitdown_signature}.md"
    marker_path = _fallback_marker_path(path, source, signature, settings)
    if not (_is_cached(marker_path) and _is_cached(cache_path)):
        return None
    return PreparedDocument(
        path=cache_path,
        provider="markitdown",
        cached=True,
        fallback_from=source,
        fallback_error=f"{_fallback_source_label(source)} 曾解析失败，已复用 MarkItDown 回退缓存",
    )


def _fallback_marker_path(
    path: str, source: FallbackSource, signature: str, settings: Settings
) -> str:
    # 历史 MinerU 标记的文件名只由签名 + MinerU 端点身份决定；保持原样以复用
    # 既有回退缓存。AnyDoc 标记带上来源，避免与 MinerU 标记互相误判。
    parts = [signature]
    if source == "anydoc":
        parts.append(source)
    parts.append(str(settings.mineru_base_url or "") if source == "mineru" else "")
    parts.append(_mineru_key_fingerprint(settings) if source == "mineru" else "")
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]
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


def _store_mineru_assets(cache_path: str, markdown: str, files: dict[str, bytes]) -> str:
    """写入 ``<缓存名>.assets/`` 并返回图片路径已指向该目录的 Markdown。"""
    assets_dir = write_assets(cache_path, files)
    if assets_dir is None:
        return markdown
    return rewrite_image_links(markdown, os.path.basename(assets_dir), files)


def remove_parsed_sidecars(path: str) -> None:
    """删除原文件及其解析缓存、回退标记和 sidecar 目录。"""
    for candidate in [path, *parsed_sidecar_paths(path)]:
        remove_path(candidate)


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
        if settings.mineru_provider == "official":
            return (
                f"mineru-official-{settings.mineru_official_model}-"
                f"{settings.mineru_parse_method}"
            )
        if settings.mineru_provider == "self_hosted":
            return (
                f"mineru-v1-{settings.mineru_tier or 'default'}-"
                f"{settings.mineru_parse_method}"
            )
        return f"mineru-{settings.mineru_version}-{settings.mineru_parse_method}"
    if provider == anydoc_parser.PROVIDER:
        return anydoc_parser.signature()
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
    if provider == "mineru":
        expected["mineru_service"] = settings.mineru_provider
        if settings.mineru_provider == "official":
            expected["mineru_model"] = settings.mineru_official_model
        elif settings.mineru_provider == "self_hosted":
            expected["mineru_tier"] = settings.mineru_tier
        else:
            expected["mineru_version"] = settings.mineru_version
        if settings.mineru_provider == "302" and "mineru_service" not in current:
            current["mineru_service"] = "302"
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
    from markitdown import MarkItDown, StreamInfo

    if path.lower().endswith(_DOCX_EXTENSION):
        with open(path, "rb") as source:
            content = _without_dangling_docx_image_relationships(source.read())
        result = MarkItDown().convert_stream(
            io.BytesIO(content),
            stream_info=StreamInfo(
                filename=os.path.basename(path),
                mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                extension=_DOCX_EXTENSION,
            ),
        )
    else:
        result = MarkItDown().convert(path)
    markdown = getattr(result, "markdown", None)
    if markdown is None:  # 兼容 0.0.x / 早期 0.1.x 返回对象
        markdown = getattr(result, "text_content", None)
    if not isinstance(markdown, str):
        raise TypeError("MarkItDown 返回了未知结果格式")
    return markdown


def _without_dangling_docx_image_relationships(content: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as source:
            entries = source.infolist()
            names = frozenset(entry.filename for entry in entries)
            replacements: dict[str, bytes] = {}
            for entry in entries:
                if not _is_word_relationships_entry(entry.filename):
                    continue
                relationships = source.read(entry)
                sanitized, removed_relationship_ids = _without_dangling_image_relationships(
                    relationships=relationships,
                    relationships_entry=entry.filename,
                    names=names,
                )
                if sanitized != relationships:
                    replacements[entry.filename] = sanitized
                if not removed_relationship_ids:
                    continue
                source_part = _source_part_for_relationships(entry.filename)
                if source_part not in names:
                    continue
                source_content = source.read(source_part)
                sanitized_source = _without_image_relationship_references(
                    content=source_content,
                    relationship_ids=removed_relationship_ids,
                )
                if sanitized_source != source_content:
                    replacements[source_part] = sanitized_source
            if not replacements:
                return content
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as destination:
                destination.comment = source.comment
                for entry in entries:
                    destination.writestr(
                        entry,
                        replacements.get(entry.filename, source.read(entry)),
                    )
            return output.getvalue()
    except (ElementTree.ParseError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return content


def _is_word_relationships_entry(name: str) -> bool:
    return name.startswith(_WORD_RELATIONSHIPS_PREFIX) and name.endswith(".rels")


def _without_dangling_image_relationships(
    *,
    relationships: bytes,
    relationships_entry: str,
    names: frozenset[str],
) -> tuple[bytes, frozenset[str]]:
    root = ElementTree.fromstring(relationships)
    removed_relationship_ids: set[str] = set()
    for relationship in root.findall(_RELATIONSHIP_TAG):
        if relationship.get("Type") != _IMAGE_RELATIONSHIP_TYPE:
            continue
        if relationship.get("TargetMode") == "External":
            continue
        target = relationship.get("Target")
        if target is None or not _relationship_target_is_missing(
            relationships_entry=relationships_entry,
            target=target,
            names=names,
        ):
            continue
        relationship_id = relationship.get("Id")
        if relationship_id is None:
            continue
        root.remove(relationship)
        removed_relationship_ids.add(relationship_id)
    if not removed_relationship_ids:
        return relationships, frozenset()
    return (
        ElementTree.tostring(root, encoding="utf-8", xml_declaration=True),
        frozenset(removed_relationship_ids),
    )


def _source_part_for_relationships(relationships_entry: str) -> str:
    return relationships_entry.replace("/_rels/", "/", 1)[: -len(".rels")]


def _without_image_relationship_references(
    *,
    content: bytes,
    relationship_ids: frozenset[str],
) -> bytes:
    root = ElementTree.fromstring(content)
    parents = {child: parent for parent in root.iter() for child in parent}
    containers: set[ElementTree.Element[str]] = set()
    for element in root.iter():
        relationship_id = element.get(_RELATIONSHIP_EMBED_ATTRIBUTE) or element.get(
            _RELATIONSHIP_ID_ATTRIBUTE
        )
        if relationship_id not in relationship_ids:
            continue
        container = _image_container(element, parents)
        if container is not None:
            containers.add(container)
    if not containers:
        return content
    for container in containers:
        parent = parents.get(container)
        if parent is not None:
            parent.remove(container)
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _image_container(
    element: ElementTree.Element[str],
    parents: dict[ElementTree.Element[str], ElementTree.Element[str]],
) -> ElementTree.Element[str] | None:
    current: ElementTree.Element[str] | None = element
    while current is not None:
        if current.tag in {_DRAWING_TAG, _PICT_TAG}:
            return current
        current = parents.get(current)
    return None


def _relationship_target_is_missing(
    *,
    relationships_entry: str,
    target: str,
    names: frozenset[str],
) -> bool:
    source_part = _source_part_for_relationships(relationships_entry)
    target_path = posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))
    return target_path not in names


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
