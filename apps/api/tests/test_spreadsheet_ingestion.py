"""The application must retain spreadsheet record boundaries at the engine seam."""
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook
from zleap.sag.pipeline import DocumentSource
from zleap.sag.pipeline.chunk import ChunkStage
from zleap.sag.pipeline.parse import ParseStage
from zleap.sag.pipeline.spreadsheets.reader import read_workbook

from sag_api.core.config import Settings
from sag_api.parsing import anydoc as anydoc_parser
from sag_api.parsing import prepare_document
from sag_api.sag.dto import ProcessCheckpoint
from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

_COST_ROWS = [
    ("产品A", None), ("项目", "金额"), ("材料", 10), ("以上成本合计", 10),
    ("产品B", None), ("项目", "金额"), ("材料", 20), ("以上成本合计", 20),
]


def _write_costs_workbook(path: Path, rows=_COST_ROWS) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "成本"
    for row in rows:
        sheet.append(row)
    workbook.save(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_mode", ["standard", "heading_strict"])
@pytest.mark.parametrize("cached", [False, True])
async def test_prepared_spreadsheet_reaches_parser_with_original_records(tmp_path, cached, chunk_mode):
    original = tmp_path / "costs.XLSX"
    _write_costs_workbook(original)
    prepared = await prepare_document(str(original), Settings())
    if cached:
        prepared = await prepare_document(str(original), Settings())
        assert prepared.cached
    parsed_documents = []

    async def ingest(source, *, descriptor, **kwargs):
        assert isinstance(source, DocumentSource)
        assert source.content == Path(prepared.path).read_text(encoding="utf-8")
        assert source.original_sha256 == sha256(original.read_bytes()).hexdigest()
        parsed_documents.append(await ParseStage().run(source))
        chunks = await ChunkStage().run(parsed_documents[-1], kwargs["chunk_options"])
        assert {c.metadata.get("record_group_id") for c in chunks.chunks} - {None} == {
            group.record_group_id for group in parsed_documents[-1].table_structure.groups
        }
        return SimpleNamespace(
            source_id="article", chunk_ids=["chunk"], generation_id=None,
            chunk_version="v1", source_version="s1",
        )

    processor = IncrementalDocumentProcessor(
        SimpleNamespace(ingest=ingest), "source", max_concurrency=1, chunk_mode=chunk_mode
    )

    async def save(_checkpoint):
        pass

    await processor._ingest(ProcessCheckpoint(), prepared.path, save, original_path=original)
    groups = parsed_documents[0].table_structure.groups
    assert [g.metadata["identity_value"] for g in groups] == ["产品A", "产品B"]
    assert [(g.cell_range.row_start, g.cell_range.row_end) for g in groups] == [(1, 4), (5, 8)]


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_mode", ["standard", "heading_strict"])
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("extension", ["xlsx", "xls"])
async def test_anydoc_spreadsheet_reaches_real_stages_with_original_file(
    tmp_path, cached, chunk_mode, extension, monkeypatch
):
    """选择 AnyDoc 时 Excel 沿用真实 MarkItDown，并保留整数末尾零。"""
    original = tmp_path / f"costs.{extension.upper()}"
    if extension == "xls":
        original.write_bytes((Path(__file__).parent / "fixtures" / "costs-general.xls").read_bytes())
    else:
        _write_costs_workbook(original)
    original_bytes = original.read_bytes()
    settings = Settings(_env_file=None, document_parser="anydoc")

    def unexpected_anydoc(path: str) -> str:
        pytest.fail(f"Excel 不应交给 AnyDoc：{path}")

    monkeypatch.setattr(anydoc_parser, "convert", unexpected_anydoc)
    prepared = await prepare_document(str(original), settings)
    assert prepared.provider == "markitdown" and prepared.fallback_from is None
    markdown = Path(prepared.path).read_text(encoding="utf-8")
    assert "产品A" in markdown and "以上成本合计" in markdown and "10" in markdown
    if cached:
        prepared = await prepare_document(str(original), settings)
        assert prepared.cached
    parsed_documents = []
    chunk_sets = []

    async def ingest(source, *, descriptor, **kwargs):
        assert isinstance(source, DocumentSource)
        assert source.content == Path(prepared.path).read_text(encoding="utf-8")
        assert source.original_file is not None
        assert source.original_sha256 == sha256(original.read_bytes()).hexdigest()
        parsed_documents.append(await ParseStage().run(source))
        chunk_sets.append(
            await ChunkStage().run(parsed_documents[-1], kwargs["chunk_options"])
        )
        return SimpleNamespace(
            source_id="article", chunk_ids=["chunk"], generation_id=None,
            chunk_version="v1", source_version="s1",
        )

    processor = IncrementalDocumentProcessor(
        SimpleNamespace(ingest=ingest), "source", max_concurrency=1, chunk_mode=chunk_mode
    )

    async def save(_checkpoint):
        pass

    await processor._ingest(ProcessCheckpoint(), prepared.path, save, original_path=original)

    parsed = parsed_documents[0]
    assert parsed.table_structure is not None
    for value in ("产品A", "以上成本合计", "项目", "金额", "材料"):
        assert value in parsed.body
    groups = list(parsed.table_structure.groups or [])
    assert [group.metadata["identity_value"] for group in groups] == ["产品A", "产品B"]
    assert [(group.cell_range.row_start, group.cell_range.row_end) for group in groups] == [(1, 4), (5, 8)]
    assert chunk_sets[0].chunks, "real chunking must still produce at least one chunk"
    chunked_body = "\n".join(chunk.content for chunk in chunk_sets[0].chunks)
    for value in ("产品A", "产品B", "材料", "10", "20"):
        assert value in chunked_body
    assert {chunk.metadata.get("record_group_id") for chunk in chunk_sets[0].chunks} - {None} == {
        group.record_group_id for group in groups
    }
    assert "表格区域因记录边界不明确" not in chunked_body
    # 转换缓存是派生产物，上传原文件必须保持不变。
    assert original.exists() and original.suffix == f".{extension.upper()}"
    assert original.read_bytes() == original_bytes


@pytest.mark.parametrize(
    "value,number_format,expected",
    [
        (10, "General", "10"), (20, "General", "20"), (100, "General", "100"),
        (0, "General", "0"), (-100, "General", "-100"),
        (10.5, "General", "10.5"), (0.01, "General", "0.01"),
        (1000000000000, "General", "1000000000000"),
        (10.5, "0.00", "10.50"), (100, "0", "100"),
    ],
)
def test_original_workbook_numeric_values_and_display(tmp_path, value, number_format, expected):
    """Read actual workbook cells rather than testing only a formatter expression."""
    original = tmp_path / "numbers.xlsx"
    workbook = Workbook()
    workbook.active["A1"] = value
    workbook.active["A1"].number_format = number_format
    workbook.save(original)
    original_bytes = original.read_bytes()
    parsed = read_workbook(original, expected_sha256=sha256(original_bytes).hexdigest())

    cell = parsed.sheets[0].rows[0][0]
    assert cell.raw_value == Decimal(str(value))
    assert cell.display_value == expected
    assert original.read_bytes() == original_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("parser", ["markitdown", "anydoc"])
@pytest.mark.parametrize("amount,expected", [(100, "100"), (0, "0"), (-20, "-20"), (10.5, "10.5")])
async def test_numeric_evidence_survives_real_parse_and_chunk(tmp_path, parser, amount, expected):
    original = tmp_path / "costs.xlsx"
    _write_costs_workbook(original, [
        ("产品A", None), ("项目", "金额"), ("材料", amount), ("以上成本合计", amount),
        ("产品B", None), ("项目", "金额"), ("材料", amount), ("以上成本合计", amount),
    ])
    prepared = await prepare_document(str(original), Settings(_env_file=None, document_parser=parser))
    results = []

    async def ingest(source, **kwargs):
        parsed = await ParseStage().run(source)
        results.append(await ChunkStage().run(parsed, kwargs["chunk_options"]))
        return SimpleNamespace(
            source_id="numbers", chunk_ids=["chunk"], generation_id=None,
            chunk_version="v1", source_version="s1",
        )

    async def save(_checkpoint):
        pass

    processor = IncrementalDocumentProcessor(SimpleNamespace(ingest=ingest), "source", max_concurrency=1)
    await processor._ingest(ProcessCheckpoint(), prepared.path, save, original_path=original)
    body = "\n".join(chunk.content for chunk in results[0].chunks)
    assert f"| 材料 | {expected} |" in body
    assert f"| 以上成本合计 | {expected} |" in body
    assert len({chunk.metadata.get("record_group_id") for chunk in results[0].chunks} - {None}) == 2


@pytest.mark.asyncio
async def test_missing_spreadsheet_original_does_not_silently_ingest_flattened_text(tmp_path):
    markdown = tmp_path / "costs.md"
    markdown.write_text("# Costs\n", encoding="utf-8")

    async def unexpected_ingest(*args, **kwargs):
        pytest.fail("missing spreadsheet originals must fail before indexing")

    async def save(_checkpoint):
        pytest.fail("failed ingestion must not checkpoint")

    processor = IncrementalDocumentProcessor(SimpleNamespace(ingest=unexpected_ingest), "source", max_concurrency=1)
    with pytest.raises(FileNotFoundError):
        await processor._ingest(ProcessCheckpoint(), markdown, save, original_path=tmp_path / "missing.xls")


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["pdf", "docx", "md"])
async def test_non_spreadsheet_keeps_existing_markdown_input(tmp_path, extension):
    markdown = tmp_path / "document.md"
    markdown.write_text("# Existing document\n", encoding="utf-8")
    parsed = []

    async def ingest(source, *, descriptor, **kwargs):
        from zleap.sag.pipeline import FileSource

        assert source == str(markdown)
        parsed.append(await ParseStage().run(FileSource(path=source, descriptor=descriptor)))
        return SimpleNamespace(
            source_id="article", chunk_ids=["chunk"], generation_id=None,
            chunk_version="v1", source_version="s1",
        )

    async def save(_checkpoint):
        pass

    processor = IncrementalDocumentProcessor(SimpleNamespace(ingest=ingest), "source", max_concurrency=1)
    await processor._ingest(ProcessCheckpoint(), markdown, save, original_path=tmp_path / f"original.{extension}")
    assert parsed[0].body.strip() == "# Existing document"
    assert parsed[0].table_structure is None
