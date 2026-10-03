"""AnyDoc 本地解析：真实转换、编码规范化、异常映射与运行时策略。

依赖分层说明：

- 真实转换用例需要安装 ``firecrawl-anydoc``（当前 Windows 开发环境的
  ``uv sync`` 因 ``litellm`` 只有 Linux wheel 而无法建立完整虚拟环境），
  因此在缺包时显式 skip；这些用例在 API Docker 镜像内必须全部通过。
- 异常映射用例不需要安装包，用替身模块覆盖，保证任何环境都能回归。
"""

from __future__ import annotations

import asyncio
import builtins
import importlib.metadata
import importlib.util
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from sag_api.core.config import Settings
from sag_api.core.error_taxonomy import ErrorStage
from sag_api.core.errors import (
    ConfigurationError,
    UpstreamError,
    ValidationError,
)
from sag_api.parsing import anydoc as anydoc_parser
from sag_api.parsing import service
from tests.helpers import corpus

_INSTALLED = importlib.util.find_spec("anydoc") is not None
requires_anydoc = pytest.mark.skipif(
    not _INSTALLED, reason="firecrawl-anydoc 未安装（见模块 docstring 的分层说明）"
)

_CSV_ROWS = "姓名,城市,备注\n张三,北京,中文备注\n"


def _settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        data_dir="/tmp/sag-test-engine",
        upload_dir="/tmp/sag-test-uploads",
        **overrides,
    )


async def _record(states: list[dict[str, Any]], state: dict[str, Any]) -> None:
    states.append(state)


# ── 配置语义 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "anydoc"),
        # 显式选择 AnyDoc 时无论是否配置 MinerU 都必须返回 anydoc。
        ({"mineru_base_url": "https://api.302ai.cn", "mineru_api_key": "sk-test"}, "anydoc"),
        ({"mineru_provider": "official", "mineru_api_key": "token"}, "anydoc"),
    ],
)
def test_explicit_anydoc_wins_over_mineru_configuration(overrides, expected):
    configured = _settings(document_parser="anydoc", **overrides)

    assert configured.effective_document_parser == expected


@pytest.mark.parametrize(
    ("parser", "mineru_api_key", "expected"),
    [
        ("auto", None, "markitdown"),
        ("auto", "sk-test", "mineru"),
        ("markitdown", "sk-test", "markitdown"),
        ("mineru", None, "markitdown"),
        ("mineru", "sk-test", "mineru"),
    ],
)
def test_effective_document_parser_keeps_legacy_semantics(parser, mineru_api_key, expected):
    configured = _settings(
        document_parser=parser,
        mineru_base_url="https://api.302ai.cn",
        mineru_api_key=mineru_api_key,
    )

    assert configured.effective_document_parser == expected


def test_settings_accept_anydoc_from_environment(monkeypatch):
    monkeypatch.setenv("SAG_DOCUMENT_PARSER", "anydoc")

    assert Settings(_env_file=None).document_parser == "anydoc"
    assert Settings(_env_file=None).effective_document_parser == "anydoc"


def test_settings_reject_unknown_parser(monkeypatch):
    monkeypatch.setenv("SAG_DOCUMENT_PARSER", "anydoc-v2")

    with pytest.raises(ValueError):
        Settings(_env_file=None)


# ── 适配层契约（不需要安装包）────────────────────────────────────────


def test_signature_tracks_package_version_and_adapter_rules():
    signature = anydoc_parser.signature()

    assert signature == f"anydoc-0.2.4-{anydoc_parser.ADAPTER_VERSION}"


@pytest.mark.parametrize(
    ("name", "handled"),
    [
        ("report.docx", True),
        ("deck.PPTX", True),
        ("book.epub", True),
        ("table.csv", True),
        ("legacy.xls", False),
        ("sheet.xlsx", False),
        ("paper.pdf", True),
        ("notes.txt", False),
        ("page.html", False),
        ("data.json", False),
        ("values.tsv", False),
        ("readme.md", False),
    ],
)
def test_handles_path_covers_declared_formats(name, handled):
    assert anydoc_parser.handles_path(name) is handled


def test_only_csv_needs_text_normalization():
    assert anydoc_parser.requires_text_normalization("table.csv") is True
    assert anydoc_parser.requires_text_normalization("report.docx") is False


class _AnyDocStub:
    """最小 anydoc 替身：只用来验证异常分类，不做真实转换。"""

    class ConvertError(Exception):
        pass

    class UnsupportedError(ConvertError):
        pass

    class NeedsOcrError(ConvertError):
        def __init__(self, message: str, *, pages: list[int], page_count: int):
            super().__init__(message)
            self.pages = pages
            self.page_count = page_count

    class MalformedError(ConvertError):
        def __init__(self, message: str, *, part: str | None = None):
            super().__init__(message)
            self.part = part

    class EncryptedError(ConvertError):
        pass

    class ResourceLimitError(ConvertError):
        def __init__(self, message: str, *, limit: str = ""):
            super().__init__(message)
            self.limit = limit

    class MissingPartError(ConvertError):
        def __init__(self, message: str, *, part: str = ""):
            super().__init__(message)
            self.part = part

    class HostedError(ConvertError):
        pass


@pytest.mark.parametrize(
    ("error", "expected_type", "expected_fragment"),
    [
        (
            _AnyDocStub.UnsupportedError("unsupported input: unrecognized file content"),
            anydoc_parser.AnyDocUnsupportedError,
            "不支持该文件",
        ),
        (
            _AnyDocStub.NeedsOcrError("page 2 of 3 needs OCR", pages=[2], page_count=3),
            ValidationError,
            "第 2 页（共 3 页）",
        ),
        (
            _AnyDocStub.NeedsOcrError("pages need OCR", pages=[1, 3], page_count=4),
            ValidationError,
            "第 1, 3 页",
        ),
        (
            _AnyDocStub.EncryptedError("document is encrypted"),
            ValidationError,
            "加密",
        ),
        (
            _AnyDocStub.ResourceLimitError("limit crossed", limit="max_entry_bytes"),
            ValidationError,
            "max_entry_bytes",
        ),
        (
            _AnyDocStub.MalformedError("no meaningful content", part="word/document.xml"),
            ValidationError,
            "word/document.xml",
        ),
        (
            _AnyDocStub.MissingPartError("part absent", part="xl/workbook.xml"),
            ValidationError,
            "xl/workbook.xml",
        ),
        (
            _AnyDocStub.HostedError("keyless limit reached"),
            ConfigurationError,
            "托管 OCR",
        ),
    ],
)
def test_convert_errors_map_to_expected_domain_errors(error, expected_type, expected_fragment):
    mapped = anydoc_parser.classify_convert_error(_AnyDocStub, error)

    assert isinstance(mapped, expected_type)
    assert mapped.stage is ErrorStage.PARSE
    assert mapped.retryable is False
    assert expected_fragment in mapped.message


def test_only_unsupported_error_is_marked_as_parser_fallback_candidate():
    unsupported = anydoc_parser.classify_convert_error(
        _AnyDocStub, _AnyDocStub.UnsupportedError("unrecognized")
    )
    needs_ocr = anydoc_parser.classify_convert_error(
        _AnyDocStub, _AnyDocStub.NeedsOcrError("scan", pages=[1], page_count=1)
    )

    assert isinstance(unsupported, anydoc_parser.AnyDocUnsupportedError)
    assert not isinstance(needs_ocr, anydoc_parser.AnyDocUnsupportedError)


def test_unexpected_convert_failure_is_internal_parser_error():
    mapped = anydoc_parser.classify_convert_error(
        _AnyDocStub, _AnyDocStub.ConvertError("binding panicked")
    )

    assert isinstance(mapped, anydoc_parser.AnyDocError)
    assert not isinstance(mapped, anydoc_parser.AnyDocUnsupportedError)
    assert mapped.stage is ErrorStage.PARSE
    assert mapped.retryable is False


def test_missing_package_reports_configuration_error(monkeypatch):
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "anydoc":
            raise ImportError("No module named 'anydoc'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    monkeypatch.delitem(sys.modules, "anydoc", raising=False)

    with pytest.raises(ConfigurationError, match="AnyDoc 未安装"):
        anydoc_parser.convert("/tmp/whatever.docx")


def test_native_extension_load_failure_reports_configuration_error(monkeypatch):
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "anydoc":
            raise OSError("DLL load failed while importing _anydoc")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    monkeypatch.delitem(sys.modules, "anydoc", raising=False)

    with pytest.raises(ConfigurationError, match="原生扩展加载失败"):
        anydoc_parser.convert("/tmp/whatever.docx")


def test_empty_conversion_result_is_rejected(monkeypatch):
    class EmptyMarkdown:
        @staticmethod
        def to_markdown(_path, **_kwargs) -> str:
            return "   \n"

    monkeypatch.setitem(sys.modules, "anydoc", EmptyMarkdown())

    with pytest.raises(ValidationError, match="未从文件中解析出有效文本"):
        anydoc_parser.convert("/tmp/empty.docx")


def test_unpinned_conversion_result_type_is_rejected(monkeypatch):
    class WrongType:
        @staticmethod
        def to_markdown(_path, **_kwargs) -> bytes:
            return b"# bytes"

    monkeypatch.setitem(sys.modules, "anydoc", WrongType())

    with pytest.raises(anydoc_parser.AnyDocError, match="未知结果格式"):
        anydoc_parser.convert("/tmp/whatever.docx")


# ── 真实转换（需要 firecrawl-anydoc）────────────────────────────────


@requires_anydoc
def test_real_anydoc_anydoc_version_is_pinned_package():
    assert importlib.metadata.version("firecrawl-anydoc") == "0.2.4"


@requires_anydoc
def test_real_anydoc_converts_chinese_docx_and_epub(tmp_path):
    docx = tmp_path / "报告.docx"
    corpus.write_docx(docx, "季度成本报告", "材料成本同比上升 12%。")
    epub = tmp_path / "手册.epub"
    corpus.write_epub(epub, "产品手册", "本手册说明离线部署步骤。")

    docx_markdown = anydoc_parser.convert(str(docx))
    epub_markdown = anydoc_parser.convert(str(epub))

    assert "季度成本报告" in docx_markdown
    assert "材料成本同比上升 12%。" in docx_markdown
    assert "产品手册" in epub_markdown
    assert "本手册说明离线部署步骤。" in epub_markdown


@requires_anydoc
def test_real_anydoc_converts_text_and_scanned_pdf(tmp_path):
    text_pdf = tmp_path / "text.pdf"
    corpus.write_text_pdf(text_pdf, "AnyDoc text layer marker")
    markdown = anydoc_parser.convert(str(text_pdf))
    assert "AnyDoc text layer marker" in markdown

    scanned = tmp_path / "scanned.pdf"
    corpus.write_scanned_pdf(scanned)
    with pytest.raises(ValidationError) as captured:
        anydoc_parser.convert(str(scanned))
    assert "第 1 页（共 1 页）" in captured.value.message
    assert "MinerU" in captured.value.message


@requires_anydoc
def test_real_anydoc_mixed_pdf_reports_every_scanned_page(tmp_path):
    mixed = tmp_path / "mixed.pdf"
    corpus.write_mixed_pdf(mixed, "First page marker")

    with pytest.raises(ValidationError) as captured:
        anydoc_parser.convert(str(mixed))

    message = captured.value.message
    assert "第 2 页（共 2 页）" in message
    # 整份提示 OCR：不得返回第一页的部分正文。
    assert "First page marker" not in message


@requires_anydoc
def test_real_anydoc_converts_xlsx_table(tmp_path):
    xlsx = tmp_path / "成本.xlsx"
    written = corpus.write_xlsx(
        xlsx,
        [("姓名", "数量"), ("张三", 3), ("李四", 5)],
    )
    if not written:
        pytest.skip("openpyxl 不可用")

    markdown = anydoc_parser.convert(str(xlsx))

    assert "姓名" in markdown and "数量" in markdown
    assert "张三" in markdown and "李四" in markdown
    assert "| --- |" in markdown


@requires_anydoc
def test_real_anydoc_uses_ocr_reject_for_scanned_pdf(tmp_path, monkeypatch):
    """存在 Firecrawl 环境变量时也必须走本地 reject，不得请求托管服务。"""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-should-never-be-used")
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://127.0.0.1:1")
    scanned = tmp_path / "scanned.pdf"
    corpus.write_scanned_pdf(scanned)

    with pytest.raises(ValidationError, match="不做 OCR"):
        anydoc_parser.convert(str(scanned))


@pytest.mark.parametrize(
    ("name", "encoding"),
    [
        ("utf8.csv", "utf-8"),
        ("utf8bom.csv", "utf-8-sig"),
        ("utf16.csv", "utf-16"),
        ("gb18030.csv", "gb18030"),
    ],
)
@requires_anydoc
def test_real_anydoc_csv_uses_decoded_utf8_and_keeps_original_bytes(tmp_path, name, encoding):
    path = tmp_path / name
    original = _CSV_ROWS.encode(encoding)
    path.write_bytes(original)

    markdown = anydoc_parser.convert(str(path))

    assert "姓名" in markdown and "城市" in markdown and "备注" in markdown
    assert "张三" in markdown and "北京" in markdown and "中文备注" in markdown
    assert "| --- |" in markdown
    # 不覆盖上传原文件。
    assert path.read_bytes() == original


@requires_anydoc
def test_real_anydoc_csv_encoding_failure_returns_domain_error(tmp_path, monkeypatch):
    """解码失败必须变成明确错误，绝不能把回退编码产生的乱码写进缓存。"""
    from sag_api.parsing import text as text_parser

    path = tmp_path / "broken.csv"
    path.write_bytes(b"name,value\n\xff\xfe\x00\x81,1\n")

    def fail_decode(_path: str):
        raise text_parser.TextDecodingError("无法可靠识别文本编码")

    monkeypatch.setattr(anydoc_parser, "read_text_file", fail_decode)

    with pytest.raises(ValidationError, match="CSV 文本编码识别失败"):
        anydoc_parser.convert(str(path))


@requires_anydoc
def test_real_anydoc_decodes_gb18030_before_anydoc_conversion(tmp_path, monkeypatch):
    """GB18030 中文 CSV 由 SAG 解码后再按 UTF-8 交给 AnyDoc，避免静默乱码。

    AnyDoc 的 CSV 非 UTF-8 回退编码是 Windows-1252：直接传原始字节会得到
    形如 ``ÐÕÃû`` 的乱码且不抛错，因此这里同时断言传给 AnyDoc 的字节是
    合法 UTF-8，并且上传原文件字节未被改写。
    """
    import anydoc

    path = tmp_path / "gb.csv"
    original = _CSV_ROWS.encode("gb18030")
    path.write_bytes(original)
    received: list[bytes] = []
    real_to_markdown_bytes = anydoc.to_markdown_bytes

    def capture(data, format=None, **kwargs):  # noqa: A002 - 跟随上游签名
        received.append(bytes(data))
        return real_to_markdown_bytes(data, format, **kwargs)

    monkeypatch.setattr(anydoc, "to_markdown_bytes", capture)

    markdown = anydoc_parser.convert(str(path))

    assert received == [_CSV_ROWS.encode("utf-8")]
    assert "姓名" in markdown and "张三" in markdown and "北京" in markdown
    assert "ÐÕÃû" not in markdown
    assert path.read_bytes() == original


@requires_anydoc
def test_real_anydoc_rejects_unsupported_extension(tmp_path):
    text = tmp_path / "notes.txt"
    text.write_text("纯文本", encoding="utf-8")

    with pytest.raises(anydoc_parser.AnyDocError, match="不支持该文件"):
        anydoc_parser.convert(str(text))


@requires_anydoc
def test_real_anydoc_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        anydoc_parser.convert(str(tmp_path / "missing.docx"))


@requires_anydoc
@pytest.mark.parametrize("extension", [".xls", ".xlsx", ".docx", ".pptx", ".epub", ".pdf", ".csv"])
def test_real_anydoc_format_detection_accepts_every_extension(extension):
    import anydoc

    assert anydoc.format_from_extension(extension) is not None


@requires_anydoc
def test_legacy_xls_converts_through_anydoc(tmp_path):
    payload = (Path(__file__).parent / "fixtures" / "legacy-names.xls").read_bytes()
    path = tmp_path / "legacy.xls"
    path.write_bytes(payload)

    markdown = anydoc_parser.convert(str(path))

    assert "姓名" in markdown and "张三" in markdown


# ── 服务层路由与缓存 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anydoc_routing_only_claims_supported_formats(tmp_path, monkeypatch):
    seen: list[str] = []

    def fake_convert(path: str) -> str:
        seen.append(path)
        return "# AnyDoc\n\n正文"

    monkeypatch.setattr(anydoc_parser, "convert", fake_convert)
    monkeypatch.setattr(service, "_markitdown_sync", lambda _path: "# MarkItDown\n")
    settings = _settings(document_parser="anydoc")

    docx = tmp_path / "report.docx"
    docx.write_bytes(b"fake-office")
    parsed_docx = await service.prepare_document(str(docx), settings)
    assert parsed_docx.provider == "anydoc"
    assert parsed_docx.path.endswith(f".parsed.{anydoc_parser.signature()}.md")
    assert "正文" in Path(parsed_docx.path).read_text(encoding="utf-8")

    html = tmp_path / "page.html"
    html.write_text("<h1>标题</h1>", encoding="utf-8")
    parsed_html = await service.prepare_document(str(html), settings)
    assert parsed_html.provider == "markitdown"

    markdown = tmp_path / "already.md"
    markdown.write_text("# 原文\n", encoding="utf-8")
    parsed_markdown = await service.prepare_document(str(markdown), settings)
    assert parsed_markdown.provider == "original"
    assert parsed_markdown.path == str(markdown)

    text = tmp_path / "notes.txt"
    text.write_bytes("纯文本正文".encode())
    parsed_text = await service.prepare_document(str(text), settings)
    assert parsed_text.provider == "markitdown"

    assert seen == [str(docx)]


@pytest.mark.asyncio
async def test_anydoc_does_not_claim_pdf_when_mineru_is_selected(tmp_path, monkeypatch):
    calls: list[str] = []

    class FakeMinerU:
        def __init__(self, _settings):
            pass

        async def parse(self, path, *, state=None, on_state=None, should_pause=None):
            calls.append(path)
            return "# From MinerU\n"

    monkeypatch.setattr(service, "MinerUClient", FakeMinerU)
    monkeypatch.setattr(anydoc_parser, "convert", lambda _path: "# Must not run\n")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake")

    parsed = await service.prepare_document(
        str(pdf), _settings(
            document_parser="mineru", mineru_api_key="sk-test", mineru_base_url="https://example.test"
        )
    )

    assert parsed.provider == "mineru"
    assert calls == [str(pdf)]


@pytest.mark.asyncio
async def test_anydoc_cache_is_independent_from_markitdown(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_convert(path: str) -> str:
        calls.append(path)
        return "# AnyDoc cached\n"

    monkeypatch.setattr(anydoc_parser, "convert", fake_convert)
    monkeypatch.setattr(service, "_markitdown_sync", lambda _path: "# MarkItDown\n")
    docx = tmp_path / "report.docx"
    docx.write_bytes(b"fake-office")
    anydoc_settings = _settings(document_parser="anydoc")
    markitdown_settings = _settings(document_parser="markitdown")

    first = await service.prepare_document(str(docx), anydoc_settings)
    cached = await service.prepare_document(str(docx), anydoc_settings)
    other = await service.prepare_document(str(docx), markitdown_settings)

    assert first.path != other.path
    assert cached.cached is True and cached.provider == "anydoc"
    assert Path(cached.path).read_text(encoding="utf-8") == "# AnyDoc cached\n"
    assert len(calls) == 1
    # 每个解析器各自的旁挂缓存都可被删除流程清理。
    sidecars = service.parsed_sidecar_paths(str(docx))
    assert str(first.path) in sidecars and str(other.path) in sidecars


@pytest.mark.asyncio
async def test_anydoc_adapter_version_change_invalidates_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(anydoc_parser, "ADAPTER_VERSION", "v1")
    docx = tmp_path / "report.docx"
    docx.write_bytes(b"fake-office")
    monkeypatch.setattr(anydoc_parser, "convert", lambda _path: "# v1\n")
    settings = _settings(document_parser="anydoc")
    first = await service.prepare_document(str(docx), settings)

    monkeypatch.setattr(anydoc_parser, "ADAPTER_VERSION", "v2")
    monkeypatch.setattr(anydoc_parser, "convert", lambda _path: "# v2\n")
    second = await service.prepare_document(str(docx), settings)

    assert first.path.endswith(".parsed.anydoc-0.2.4-v1.md")
    assert second.path.endswith(".parsed.anydoc-0.2.4-v2.md")
    assert second.cached is False


@pytest.mark.asyncio
async def test_anydoc_conversion_failure_does_not_write_cache(tmp_path, monkeypatch):
    docx = tmp_path / "report.docx"
    docx.write_bytes(b"fake-office")

    def unsupported(_path: str) -> str:
        raise anydoc_parser.AnyDocUnsupportedError("AnyDoc 不支持该文件：unrecognized")

    monkeypatch.setattr(anydoc_parser, "convert", unsupported)
    monkeypatch.setattr(service, "_markitdown_sync", lambda _path: "# fallback\n")
    settings = _settings(document_parser="anydoc")
    states: list[dict[str, Any]] = []

    parsed = await service.prepare_document(
        str(docx), settings, on_state=lambda state: _record(states, state)
    )

    assert parsed.provider == "markitdown"
    assert parsed.path.endswith(".parsed.markitdown.md")
    assert parsed.fallback_from == "anydoc"
    assert states[-1]["status"] == "fallback_done"
    assert states[-1]["provider"] == "anydoc"


@pytest.mark.asyncio
async def test_anydoc_unsupported_falls_back_to_markitdown_only_once(tmp_path, monkeypatch):
    docx = tmp_path / "report.docx"
    docx.write_bytes(b"fake-office")
    convert_calls = 0
    markitdown_calls = 0

    def unsupported(_path: str) -> str:
        nonlocal convert_calls
        convert_calls += 1
        raise anydoc_parser.AnyDocUnsupportedError("AnyDoc 不支持该文件：unrecognized")

    def convert(_path: str) -> str:
        nonlocal markitdown_calls
        markitdown_calls += 1
        return "# Local fallback\n"

    monkeypatch.setattr(anydoc_parser, "convert", unsupported)
    monkeypatch.setattr(service, "_markitdown_sync", convert)
    settings = _settings(document_parser="anydoc")
    states: list[dict[str, Any]] = []

    first = await service.prepare_document(
        str(docx), settings, on_state=lambda state: _record(states, state)
    )
    second = await service.prepare_document(
        str(docx),
        settings,
        state=states[-1],
        on_state=lambda state: _record(states, state),
    )

    assert convert_calls == 1 and markitdown_calls == 1
    assert first.fallback_from == "anydoc" and first.fallback_error is not None
    assert second.path == first.path and second.cached is True
    assert second.fallback_from == "anydoc"
    assert Path(second.path).read_text(encoding="utf-8") == "# Local fallback\n"
    assert states[-1]["fallback"]["fallback_from"] == "anydoc"


@pytest.mark.asyncio
async def test_anydoc_non_unsupported_failure_does_not_fall_back(tmp_path, monkeypatch):
    docx = tmp_path / "report.docx"
    docx.write_bytes(b"fake-office")
    markitdown_calls = 0

    def malformed(_path: str) -> str:
        raise ValidationError("AnyDoc 无法从文件中解析出有效内容", retryable=False)

    def convert(_path: str) -> str:
        nonlocal markitdown_calls
        markitdown_calls += 1
        return "# must not run\n"

    monkeypatch.setattr(anydoc_parser, "convert", malformed)
    monkeypatch.setattr(service, "_markitdown_sync", convert)
    states: list[dict[str, Any]] = []

    with pytest.raises(ValidationError, match="有效内容"):
        await service.prepare_document(
            str(docx),
            _settings(document_parser="anydoc"),
            on_state=lambda state: _record(states, state),
        )

    assert markitdown_calls == 0
    assert not Path(f"{docx}.parsed.markitdown.md").exists()
    assert states[-1]["status"] == "running"


@pytest.mark.asyncio
async def test_anydoc_state_callback_failure_does_not_trigger_fallback(tmp_path, monkeypatch):
    docx = tmp_path / "report.docx"
    docx.write_bytes(b"fake-office")
    markitdown_calls = 0

    def convert(_path: str) -> str:
        nonlocal markitdown_calls
        markitdown_calls += 1
        return "# must not run\n"

    monkeypatch.setattr(anydoc_parser, "convert", lambda _path: "# AnyDoc\n")
    monkeypatch.setattr(service, "_markitdown_sync", convert)

    async def fail_to_persist(_state: dict[str, Any]) -> None:
        raise RuntimeError("database commit failed")

    with pytest.raises(RuntimeError, match="database commit failed"):
        await service.prepare_document(
            str(docx), _settings(document_parser="anydoc"), on_state=fail_to_persist
        )

    assert markitdown_calls == 0
    assert not Path(f"{docx}.parsed.markitdown.md").exists()


@pytest.mark.asyncio
async def test_anydoc_cache_write_failure_does_not_trigger_fallback(tmp_path, monkeypatch):
    docx = tmp_path / "report.docx"
    docx.write_bytes(b"fake-office")
    markitdown_calls = 0

    def convert(_path: str) -> str:
        nonlocal markitdown_calls
        markitdown_calls += 1
        return "# must not run\n"

    def fail_write(_path: str, _markdown: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(anydoc_parser, "convert", lambda _path: "# AnyDoc\n")
    monkeypatch.setattr(service, "_markitdown_sync", convert)
    monkeypatch.setattr(service, "_write_markdown", fail_write)

    with pytest.raises(OSError, match="disk full"):
        await service.prepare_document(str(docx), _settings(document_parser="anydoc"))

    assert markitdown_calls == 0


@pytest.mark.asyncio
async def test_anydoc_pause_before_conversion_does_not_write_cache(tmp_path, monkeypatch):
    from sag_api.parsing.mineru import ParsePaused

    docx = tmp_path / "report.docx"
    docx.write_bytes(b"fake-office")
    called: list[str] = []

    def convert(path: str) -> str:
        called.append(path)
        return "# AnyDoc\n"

    monkeypatch.setattr(anydoc_parser, "convert", convert)
    settings = _settings(document_parser="anydoc")

    async def always_pause() -> bool:
        return True

    with pytest.raises(ParsePaused):
        await service.prepare_document(
            str(docx), settings, should_pause=always_pause
        )

    assert called == []
    assert not Path(f"{docx}.parsed.{anydoc_parser.signature()}.md").exists()


@pytest.mark.asyncio
async def test_anydoc_pause_after_conversion_skips_cache_write(tmp_path, monkeypatch):
    from sag_api.parsing.mineru import ParsePaused

    docx = tmp_path / "report.docx"
    docx.write_bytes(b"fake-office")
    monkeypatch.setattr(anydoc_parser, "convert", lambda _path: "# AnyDoc\n")
    checks = 0

    async def pause_on_second_check() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 2

    cache_path = Path(f"{docx}.parsed.{anydoc_parser.signature()}.md")
    with pytest.raises(ParsePaused):
        await service.prepare_document(
            str(docx),
            _settings(document_parser="anydoc"),
            should_pause=pause_on_second_check,
        )

    assert checks == 2
    assert not cache_path.exists()

    # 恢复：同一签名下重新转换并正常落缓存。
    monkeypatch.setattr(anydoc_parser, "convert", lambda _path: "# AnyDoc resumed\n")
    resumed = await service.prepare_document(str(docx), _settings(document_parser="anydoc"))
    assert resumed.cached is False
    assert "resumed" in Path(resumed.path).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_concurrent_anydoc_conversion_runs_once(tmp_path, monkeypatch):
    docx = tmp_path / "report.docx"
    docx.write_bytes(b"fake-office")
    calls = 0

    def convert(_path: str) -> str:
        nonlocal calls
        calls += 1
        return "# AnyDoc once\n"

    monkeypatch.setattr(anydoc_parser, "convert", convert)
    settings = _settings(document_parser="anydoc")

    results = await asyncio.gather(
        service.prepare_document(str(docx), settings),
        service.prepare_document(str(docx), settings),
    )

    assert calls == 1
    assert results[0].path == results[1].path
    assert sorted(result.cached for result in results) == [False, True]


@pytest.mark.asyncio
async def test_anydoc_fallback_marker_is_isolated_from_mineru(tmp_path, monkeypatch):
    """历史 MinerU 回退标记不得阻止 AnyDoc 重新转换，反之亦然。"""
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake")
    markitdown_signature = service._signature("markitdown", _settings())

    mineru_settings = _settings(
        document_parser="mineru",
        mineru_api_key="sk-test",
    )
    anydoc_settings = _settings(document_parser="anydoc")

    class FailingMinerU:
        def __init__(self, _settings):
            pass

        async def parse(self, path, *, state=None, on_state=None, should_pause=None):
            raise UpstreamError("remote unavailable")

    monkeypatch.setattr(service, "MinerUClient", FailingMinerU)
    monkeypatch.setattr(service, "_markitdown_sync", lambda _path: "# MarkItDown\n")
    monkeypatch.setattr(anydoc_parser, "convert", lambda _path: "# AnyDoc\n")

    await service.prepare_document(str(pdf), mineru_settings)

    # AnyDoc 选中后必须重新转换，而不是复用“MinerU 曾失败”的回退缓存。
    parsed = await service.prepare_document(str(pdf), anydoc_settings)

    assert parsed.provider == "anydoc"
    assert parsed.cached is False
    assert parsed.fallback_from is None
    assert Path(f"{pdf}.parsed.{markitdown_signature}.md").exists()


@pytest.mark.asyncio
async def test_anydoc_reuses_markitdown_cache_after_recorded_fallback(tmp_path, monkeypatch):
    docx = tmp_path / "report.docx"
    docx.write_bytes(b"fake-office")

    def unsupported(_path: str) -> str:
        raise anydoc_parser.AnyDocUnsupportedError("AnyDoc 不支持该文件：unrecognized")

    monkeypatch.setattr(anydoc_parser, "convert", unsupported)
    convert_calls = 0

    def convert(_path: str) -> str:
        nonlocal convert_calls
        convert_calls += 1
        return "# Local fallback\n"

    monkeypatch.setattr(service, "_markitdown_sync", convert)
    settings = _settings(document_parser="anydoc")

    first = await service.prepare_document(str(docx), settings)
    second = await service.prepare_document(str(docx), settings)

    assert convert_calls == 1
    assert first.path == second.path
    assert second.cached is True
    assert second.fallback_from == "anydoc"
    assert "AnyDoc" in (second.fallback_error or "")


@pytest.mark.asyncio
async def test_anydoc_plain_text_still_uses_sag_decoder(tmp_path, monkeypatch):
    """未交给 AnyDoc 的纯文本沿用原路径，且不触碰 anydoc。"""
    text = tmp_path / "骆驼祥子.txt"
    expected = "《骆驼祥子》\r\n作者：老舍"
    text.write_bytes(expected.encode("gb18030"))

    def forbidden(_path: str) -> str:
        raise AssertionError("plain text must use SAG's own decoder")

    monkeypatch.setattr(service, "_markitdown_sync", forbidden)
    monkeypatch.setattr(anydoc_parser, "convert", forbidden)
    parsed = await service.prepare_document(str(text), _settings(document_parser="anydoc"))

    assert parsed.provider == "markitdown"
    assert Path(parsed.path).read_text(encoding="utf-8").startswith(
        expected.replace("\r\n", "\n")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", [".xls", ".xlsx", ".XLSX"])
async def test_anydoc_mode_keeps_excel_on_markitdown(tmp_path, monkeypatch, extension):
    original = tmp_path / f"costs{extension}"
    original.write_bytes(b"original spreadsheet")
    Path(f"{original}.parsed.anydoc-0.2.4-v1.md").write_text(
        "# obsolete AnyDoc table\n", encoding="utf-8"
    )
    calls = []

    def unexpected_anydoc(_path):
        pytest.fail("Excel must keep the existing record-aware conversion")

    def markitdown(path):
        calls.append(path)
        return "# Costs\n"

    monkeypatch.setattr(anydoc_parser, "convert", unexpected_anydoc)
    monkeypatch.setattr(service, "_markitdown_sync", markitdown)
    states = []
    settings = _settings(document_parser="anydoc")
    first = await service.prepare_document(
        str(original), settings, on_state=lambda state: _record(states, state)
    )
    second = await service.prepare_document(str(original), settings)

    assert calls == [str(original)]
    assert first.provider == second.provider == "markitdown"
    assert first.fallback_from is second.fallback_from is None
    assert second.cached and second.path == first.path
    assert states[-1]["provider"] == "markitdown"
    assert original.read_bytes() == b"original spreadsheet"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["convert_error", "unknown_result"])
async def test_unexpected_sdk_failure_never_falls_back(tmp_path, monkeypatch, failure):
    original = tmp_path / "report.docx"
    original.write_bytes(b"original")
    error = _AnyDocStub.ConvertError("unexpected binding failure")

    def broken(_path, **_kwargs):
        if failure == "convert_error":
            raise error
        return 42

    def unexpected_fallback(_path):
        pytest.fail("unexpected SDK errors must not trigger MarkItDown")

    monkeypatch.setattr(_AnyDocStub, "to_markdown", staticmethod(broken), raising=False)
    monkeypatch.setitem(sys.modules, "anydoc", _AnyDocStub)
    monkeypatch.setattr(service, "_markitdown_sync", unexpected_fallback)

    with pytest.raises(anydoc_parser.AnyDocError) as caught:
        await service.prepare_document(str(original), _settings(document_parser="anydoc"))

    assert not isinstance(caught.value, anydoc_parser.AnyDocUnsupportedError)
    assert caught.value.stage is ErrorStage.PARSE and caught.value.retryable is False
    if failure == "convert_error":
        assert caught.value.__cause__ is error
    assert not list(tmp_path.glob("*.parsed.*"))


@pytest.mark.asyncio
async def test_v1_fallback_marker_does_not_bypass_fixed_anydoc(tmp_path, monkeypatch):
    original = tmp_path / "report.docx"
    original.write_bytes(b"original")
    settings = _settings(document_parser="anydoc")
    Path(f"{original}.parsed.markitdown.md").write_text("# old fallback\n", encoding="utf-8")
    old_marker = service._fallback_marker_path(str(original), "anydoc", "anydoc-0.2.4-v1", settings)
    Path(old_marker).write_text("markitdown\n", encoding="utf-8")
    calls = []

    def convert(path):
        calls.append(path)
        return "# Fixed adapter\n"

    monkeypatch.setattr(anydoc_parser, "convert", convert)
    prepared = await service.prepare_document(str(original), settings)

    assert calls == [str(original)]
    assert prepared.provider == "anydoc" and prepared.fallback_from is None
    assert not prepared.cached
    assert Path(prepared.path).read_text(encoding="utf-8") == "# Fixed adapter\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_during", ["anydoc", "markitdown"])
async def test_pause_in_unsupported_path_does_not_write_or_fail(tmp_path, monkeypatch, pause_during):
    from sag_api.parsing.mineru import ParsePaused

    original = tmp_path / "report.docx"
    original.write_bytes(b"original")
    paused = False
    fallback_calls = []
    states = []

    async def should_pause():
        return paused

    def unsupported(_path):
        nonlocal paused
        paused = pause_during == "anydoc"
        raise anydoc_parser.AnyDocUnsupportedError("unsupported")

    def fallback(path):
        nonlocal paused
        fallback_calls.append(path)
        paused = True
        return "# Converted during pause\n"

    monkeypatch.setattr(anydoc_parser, "convert", unsupported)
    monkeypatch.setattr(service, "_markitdown_sync", fallback)

    with pytest.raises(ParsePaused):
        await service.prepare_document(
            str(original), _settings(document_parser="anydoc"),
            should_pause=should_pause, on_state=lambda state: _record(states, state),
        )

    assert fallback_calls == ([] if pause_during == "anydoc" else [str(original)])
    assert not list(tmp_path.glob("*.parsed.*"))
    assert states[-1]["status"] == ("running" if pause_during == "anydoc" else "fallback_running")
    assert all(state.get("status") != "fallback_failed" for state in states)
    assert original.read_bytes() == b"original"


@pytest.mark.asyncio
async def test_pause_after_fallback_cache_write_does_not_publish_marker(tmp_path, monkeypatch):
    from sag_api.parsing.mineru import ParsePaused

    original = tmp_path / "report.docx"
    original.write_bytes(b"original")
    paused = False
    settings = _settings(document_parser="anydoc")
    states = []
    write_markdown = service._write_markdown

    async def should_pause():
        return paused

    def unsupported(_path):
        raise anydoc_parser.AnyDocUnsupportedError("unsupported")

    def write_then_pause(path, markdown):
        nonlocal paused
        write_markdown(path, markdown)
        paused = True

    monkeypatch.setattr(anydoc_parser, "convert", unsupported)
    monkeypatch.setattr(service, "_markitdown_sync", lambda _path: "# Valid fallback\n")
    monkeypatch.setattr(service, "_write_markdown", write_then_pause)

    with pytest.raises(ParsePaused):
        await service.prepare_document(
            str(original), settings, should_pause=should_pause,
            on_state=lambda state: _record(states, state),
        )

    assert Path(f"{original}.parsed.markitdown.md").read_text(encoding="utf-8") == "# Valid fallback\n"
    assert not list(tmp_path.glob("*.marker"))
    assert states[-1]["status"] == "fallback_running"


def test_corpus_epub_is_a_valid_zip(tmp_path):
    """样本生成器自检：EPUB 的首个条目必须是未压缩的 mimetype。"""
    epub = tmp_path / "sample.epub"
    corpus.write_epub(epub, "标题", "正文")

    with zipfile.ZipFile(epub) as archive:
        first = archive.infolist()[0]
        assert first.filename == "mimetype"
        assert first.compress_type == zipfile.ZIP_STORED
        assert archive.read("mimetype") == b"application/epub+zip"
