"""MinerU 结果包中的图片与结构化产物（sidecar）。

MinerU 的结果 zip 除 Markdown 外还带有图片和结构化 JSON：

- MinerU ≤3 / 官方 v4：``full.md``、``images/``、``*_content_list.json``、``layout.json``；
- MinerU 4.x V1：``markdown.md``、``images/``、``middle_json.json``、``structured_content.json``。

这里把它们与 Markdown 一起取出，写到解析缓存旁的 ``<缓存名>.assets/`` 目录，
并把 Markdown 里的相对图片路径改写为指向该目录，同时从 content_list /
middle_json 提取标题层级与页码（``headings.json``），供引用显示页码。
"""

from __future__ import annotations

import io
import json
import os
import posixpath
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Any

from sag_api.core.errors import UpstreamError

ASSETS_SUFFIX = ".assets"
HEADINGS_FILE = "headings.json"
CONTENT_LIST_FILE = "content_list.json"
MIDDLE_JSON_FILE = "middle.json"
STRUCTURED_CONTENT_FILE = "structured_content.json"

_MARKDOWN_NAMES = {"full.md", "full.markdown", "markdown.md"}
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")
_TITLE_BLOCK_TYPES = {"title", "doc_title", "paragraph_title"}
# Markdown 图片 ``![alt](target)`` 与表格 HTML 中的 ``<img src="target">``。
_MARKDOWN_IMAGE = re.compile(r"(!\[[^\]]*\]\()(<[^>\n]+>|[^)\s]+)((?:\s+\"[^\"]*\")?\))")
_HTML_IMAGE = re.compile(r"(<img\b[^>]*?\bsrc=)([\"'])([^\"']+)(\2)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class MinerUResult:
    """一次 MinerU 解析的 Markdown 及其 sidecar（相对路径 → 字节）。"""

    markdown: str
    files: dict[str, bytes] = field(default_factory=dict)


def result_from_zip(content: bytes, size_limit: int) -> MinerUResult:
    """从结果 zip 中取 Markdown、图片与结构化 JSON；总解压体积受 ``size_limit`` 约束。"""
    from sag_api.parsing.mineru import _require_markdown

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = [info for info in archive.infolist() if not info.is_dir()]
            candidates = [
                info
                for info in entries
                if info.filename.lower().endswith((".md", ".markdown"))
            ]
            if not candidates:
                raise UpstreamError("MinerU 结果压缩包中没有 Markdown 文件")
            candidates.sort(
                key=lambda info: (
                    # MinerU ≤3 / 官方 v4 为 full.md；MinerU 4.x V1 结果包为 markdown.md。
                    os.path.basename(info.filename).lower() not in _MARKDOWN_NAMES,
                    -info.file_size,
                )
            )
            chosen = candidates[0]
            if chosen.file_size > size_limit:
                raise UpstreamError("MinerU Markdown 结果超过允许大小")
            markdown = _require_markdown(archive.read(chosen).decode("utf-8", errors="replace"))
            root = posixpath.dirname(chosen.filename)
            budget = size_limit - chosen.file_size
            files: dict[str, bytes] = {}
            for info in entries:
                target = _sidecar_name(info.filename, root)
                if target is None or target in files:
                    continue
                if info.file_size > budget:
                    # sidecar 是增强信息：超过体积上限时只保留 Markdown，不让解析失败。
                    continue
                files[target] = archive.read(info)
                budget -= info.file_size
    except zipfile.BadZipFile as exc:
        raise UpstreamError("MinerU 返回的结果压缩包已损坏") from exc
    headings = extract_headings(files)
    if headings:
        files[HEADINGS_FILE] = json.dumps(headings, ensure_ascii=False, indent=2).encode()
    return MinerUResult(markdown=markdown, files=files)


def _sidecar_name(name: str, root: str) -> str | None:
    """把 zip 内路径映射为 sidecar 相对路径；与 Markdown 不在同一目录树或不安全的条目忽略。"""
    if "\\" in name or name.startswith("/"):
        return None
    if root:
        if not name.startswith(root + "/"):
            return None
        relative = name[len(root) + 1 :]
    else:
        relative = name
    normalized = posixpath.normpath(relative)
    if normalized.startswith(("../", "/")) or normalized in {".", ".."}:
        return None
    lower = normalized.lower()
    if lower.startswith("images/") and lower.endswith(_IMAGE_EXTENSIONS):
        return normalized
    if "/" in normalized:
        return None
    if lower.endswith("_content_list.json") or lower == "content_list.json":
        return CONTENT_LIST_FILE
    if lower in {"middle_json.json", "layout.json", "middle.json"} or lower.endswith("_middle.json"):
        return MIDDLE_JSON_FILE
    if lower == STRUCTURED_CONTENT_FILE:
        return STRUCTURED_CONTENT_FILE
    return None


def extract_headings(files: dict[str, bytes]) -> list[dict[str, Any]]:
    """按阅读顺序提取 ``{"title", "level", "page_idx"}``；优先 content_list，其次 middle_json。"""
    content_list = _load_json(files.get(CONTENT_LIST_FILE))
    if isinstance(content_list, list):
        headings = _headings_from_content_list(content_list)
        if headings:
            return headings
    middle = _load_json(files.get(MIDDLE_JSON_FILE))
    if isinstance(middle, dict):
        return _headings_from_middle_json(middle)
    return []


def _headings_from_content_list(items: list[Any]) -> list[dict[str, Any]]:
    headings: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        level = item.get("text_level")
        title = item.get("text")
        if not _valid_level(level) or not isinstance(title, str) or not title.strip():
            continue
        headings.append(_heading(title, level, item.get("page_idx")))
    return headings


def _headings_from_middle_json(middle: dict[str, Any]) -> list[dict[str, Any]]:
    pages = middle.get("pages") if isinstance(middle.get("pages"), list) else middle.get("pdf_info")
    if not isinstance(pages, list):
        return []
    headings: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_idx = page.get("page_idx")
        blocks = page.get("blocks") if "blocks" in page else page.get("para_blocks")
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict) or block.get("type") not in _TITLE_BLOCK_TYPES:
                continue
            title = _block_text(block)
            if not title:
                continue
            level = block.get("level")
            headings.append(_heading(title, level if _valid_level(level) else 1, page_idx))
    return headings


def _block_text(block: dict[str, Any]) -> str:
    """兼容 4.x ``content``（字符串或 inline 片段）与 ≤3 ``lines[].spans[].content``。"""
    content = block.get("content")
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    if isinstance(content, list):
        parts.extend(_inline_text(item) for item in content)
    for line in block.get("lines") or []:
        if isinstance(line, dict):
            parts.extend(_inline_text(span) for span in line.get("spans") or [])
    return " ".join(part for part in parts if part).strip()


def _inline_text(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("content", "text"):
            value = item.get(key)
            if isinstance(value, str):
                return value.strip()
    return ""


def _valid_level(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 6


def _heading(title: str, level: int, page_idx: Any) -> dict[str, Any]:
    page = page_idx if isinstance(page_idx, int) and not isinstance(page_idx, bool) else None
    return {"title": " ".join(title.split()), "level": level, "page_idx": page}


def _load_json(content: bytes | None) -> Any:
    if not content:
        return None
    try:
        return json.loads(content.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


def assets_dir_for(cache_path: str) -> str:
    """``x.pdf.parsed.<sig>.md`` → ``x.pdf.parsed.<sig>.assets``（与缓存同前缀，便于一并清理）。"""
    stem, _ = os.path.splitext(cache_path)
    return stem + ASSETS_SUFFIX


def rewrite_image_links(markdown: str, assets_dirname: str, files: dict[str, bytes]) -> str:
    """把指向已保存图片的相对链接改为 ``<assets 目录名>/images/...``；其余链接原样保留。"""
    images = {name for name in files if name.startswith("images/")}
    if not images:
        return markdown
    prefix = assets_dirname.replace(" ", "%20")

    def resolve(target: str) -> str | None:
        bare = target[1:-1] if target.startswith("<") and target.endswith(">") else target
        normalized = posixpath.normpath(bare.removeprefix("./"))
        if normalized in images:
            return f"{prefix}/{normalized}"
        return None

    def markdown_image(match: re.Match[str]) -> str:
        replaced = resolve(match.group(2))
        return match.group(0) if replaced is None else f"{match.group(1)}{replaced}{match.group(3)}"

    def html_image(match: re.Match[str]) -> str:
        replaced = resolve(match.group(3))
        if replaced is None:
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}{replaced}{match.group(4)}"

    return _HTML_IMAGE.sub(html_image, _MARKDOWN_IMAGE.sub(markdown_image, markdown))


def write_assets(cache_path: str, files: dict[str, bytes]) -> str | None:
    """原子地替换 sidecar 目录；没有 sidecar 时清掉旧目录并返回 None。"""
    target = assets_dir_for(cache_path)
    parent = os.path.dirname(target) or "."
    if not files:
        shutil.rmtree(target, ignore_errors=True)
        return None
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".parsed-assets-", dir=parent)
    try:
        for name, payload in files.items():
            destination = os.path.join(staging, *name.split("/"))
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with open(destination, "wb") as output:
                output.write(payload)
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target


def remove_path(path: str) -> None:
    """删除文件或 sidecar 目录，忽略已不存在或无权限的条目。"""
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.lexists(path):
            os.remove(path)
    except OSError:
        pass
