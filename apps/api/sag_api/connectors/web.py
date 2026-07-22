"""网页连接器 —— 首个动态连接器。

抓取用户给定的网页正文，转为 Markdown 交给引擎处理。演示「采集层抽象」如何
接入外部信息源：`discover()` 列举 URL，`fetch()` 抓取并抽取正文 → 复用同一
ingest → extract 管线。（MVP 只抓给定 URL，不跟随链接爬取。）
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from typing import Any
from urllib.parse import urlparse

import httpx

from sag_api.connectors.base import (
    ConfigField,
    Connector,
    ConnectorMeta,
    DiscoveredDoc,
    LocalFile,
)
from sag_api.core.errors import UpstreamError, ValidationError
from sag_api.core.logging import get_logger
from sag_api.enums import ConnectorKind

log = get_logger("connectors.web")

_TIMEOUT = 20.0
_MAX_HTML_BYTES = 8 * 1024 * 1024


def _parse_urls(config: dict[str, Any]) -> list[str]:
    raw = config.get("urls") or config.get("url") or ""
    if isinstance(raw, list):
        items = [str(u).strip() for u in raw]
    else:
        items = [u.strip() for u in re.split(r"[,\n]", str(raw))]
    return [u for u in items if u]


def _filename_for(url: str) -> str:
    p = urlparse(url)
    slug = (p.path.strip("/").replace("/", "-") or p.netloc) or "page"
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", slug)[:60].strip("-") or "page"
    return f"{p.netloc}-{slug}.md" if p.netloc else f"{slug}.md"


def extract_web_title(html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


def _strip_tags(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def extract_web_markdown(html: str) -> str:
    """Extract readable Markdown from an HTML page."""
    try:
        import trafilatura

        md = trafilatura.extract(
            html,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )
        if md and md.strip():
            return md
    except Exception as e:  # noqa: BLE001
        log.warning("trafilatura 抽取失败，回退裸文本：%s", e)
    return _strip_tags(html)


class WebConnector(Connector):
    meta = ConnectorMeta(
        kind=ConnectorKind.WEB,
        title="网页",
        description="抓取网页正文并转为 Markdown 入库。适合文档站、博客、公开知识页。",
        supports_sync=True,
        config_fields=[
            ConfigField(
                key="urls",
                label="网页地址",
                type="text",
                required=True,
                placeholder="https://example.com/docs\nhttps://example.com/faq",
                help="每行一个 URL；点击「同步」抓取。",
            )
        ],
    )

    def validate_config(self, config: dict[str, Any]) -> None:
        urls = _parse_urls(config)
        if not urls:
            raise ValidationError("请至少填写一个网页地址")
        for u in urls:
            p = urlparse(u)
            if p.scheme not in ("http", "https") or not p.netloc:
                raise ValidationError(f"无效的网页地址：{u}")

    async def discover(self, config: dict[str, Any]) -> list[DiscoveredDoc]:
        return [
            DiscoveredDoc(external_id=u, filename=_filename_for(u), content_type="text/markdown")
            for u in _parse_urls(config)
        ]

    async def fetch(self, config: dict[str, Any], doc: DiscoveredDoc) -> LocalFile:
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": "sag-bot/0.1 (+https://github.com/Zleap-AI/SAG)"},
            ) as client:
                resp = await client.get(doc.external_id)
                resp.raise_for_status()
                html = resp.text[: _MAX_HTML_BYTES]
        except Exception as e:  # noqa: BLE001
            raise UpstreamError(f"抓取失败 {doc.external_id}：{e}") from e

        body = extract_web_markdown(html)
        if not body.strip():
            raise UpstreamError(f"未能从页面提取到正文：{doc.external_id}")

        title = extract_web_title(html) or doc.filename
        content = f"# {title}\n\n> 来源：{doc.external_id}\n\n{body}\n"

        digest = hashlib.md5(doc.external_id.encode()).hexdigest()[:12]
        path = os.path.join(tempfile.gettempdir(), f"sag-web-{digest}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return LocalFile(
            path=path,
            filename=doc.filename,
            content_type="text/markdown",
            size_bytes=len(content.encode("utf-8")),
        )
