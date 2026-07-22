"""连接器单元测试（无网络）。"""

import pytest

from sag_api.connectors import registry
from sag_api.connectors.web import WebConnector, _filename_for, _parse_urls
from sag_api.core.errors import ValidationError
from sag_api.enums import ConnectorKind


def test_registry_has_web_and_file():
    kinds = {c.meta.kind for c in registry.all()}
    assert ConnectorKind.FILE_UPLOAD in kinds
    assert ConnectorKind.WEB in kinds
    assert registry.get("web").meta.supports_sync is True


def test_web_parse_urls_multiline_and_comma():
    cfg = {"urls": "https://a.com/x\nhttps://b.com/y, https://c.com"}
    assert _parse_urls(cfg) == ["https://a.com/x", "https://b.com/y", "https://c.com"]


def test_web_validate_rejects_bad_url():
    c = WebConnector()
    with pytest.raises(ValidationError):
        c.validate_config({"urls": ""})
    with pytest.raises(ValidationError):
        c.validate_config({"urls": "ftp://nope"})
    with pytest.raises(ValidationError):
        c.validate_config({"urls": "not-a-url"})
    # 合法不抛
    c.validate_config({"urls": "https://example.com/docs"})


@pytest.mark.asyncio
async def test_web_discover_maps_urls_to_docs():
    c = WebConnector()
    docs = await c.discover({"urls": "https://example.com/a\nhttps://example.com/b"})
    assert [d.external_id for d in docs] == ["https://example.com/a", "https://example.com/b"]
    assert all(d.filename.endswith(".md") for d in docs)


def test_filename_derivation():
    assert _filename_for("https://example.com/docs/intro").endswith(".md")
    assert "example.com" in _filename_for("https://example.com/")
