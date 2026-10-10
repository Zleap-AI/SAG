"""The application must retain spreadsheet record boundaries at the engine seam."""

from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from document_requests import document_request
from openpyxl import Workbook
from zleap.sag.pipeline import DocumentSource
from zleap.sag.pipeline.chunk import ChunkStage
from zleap.sag.pipeline.parse import ParseStage

from sag_api.core.config import Settings
from sag_api.parsing import anydoc as anydoc_parser
from sag_api.parsing import prepare_document
from sag_api.sag.incremental_processor import IncrementalDocumentProcessor


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_mode", ["standard", "heading_strict"])
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("amounts", [(10, 20), (100, -20), (10.5, -20.25)])
async def test_prepared_spreadsheet_reaches_parser_with_original_records(tmp_path, cached, chunk_mode, amounts):
    original = tmp_path / "costs.XLSX"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "成本"
    for row in [
        ("产品A", None),
        ("项目", "金额"),
        ("材料", amounts[0]),
        ("以上成本合计", amounts[0]),
        ("产品B", None),
        ("项目", "金额"),
        ("材料", amounts[1]),
        ("以上成本合计", amounts[1]),
    ]:
        sheet.append(row)
    workbook.save(original)
    prepared = await prepare_document(str(original), Settings())
    if cached:
        prepared = await prepare_document(str(original), Settings())
        assert prepared.cached
    parsed_documents = []

    async def inspect_source(source, *, chunk_options):
        assert isinstance(source, DocumentSource)
        assert source.content == Path(prepared.path).read_text(encoding="utf-8")
        assert source.original_sha256 == sha256(original.read_bytes()).hexdigest()
        parsed_documents.append(await ParseStage().run(source))
        chunks = await ChunkStage().run(parsed_documents[-1], chunk_options)
        assert {c.metadata.get("record_group_id") for c in chunks.chunks} - {None} == {
            group.record_group_id for group in parsed_documents[-1].table_structure.groups
        }

    processor = IncrementalDocumentProcessor(
        SimpleNamespace(resources=SimpleNamespace(prompts=SimpleNamespace(language="en"))),
        "source",
        max_concurrency=1,
        chunk_mode=chunk_mode,
    )

    request = await processor.build_request(
        prepared.path, context=document_request("source").context, input_id="article", original_path=original
    )
    await inspect_source(request.source, chunk_options=request.chunk_options)
    groups = parsed_documents[0].table_structure.groups
    assert [g.metadata["identity_value"] for g in groups] == ["产品A", "产品B"]
    assert [(g.cell_range.row_start, g.cell_range.row_end) for g in groups] == [(1, 4), (5, 8)]
    # General-format numbers must reach the engine's record groups without losing zeroes (#217).
    assert [g.metadata["display_rows"][2][1] for g in groups] == [str(amount) for amount in amounts]


@pytest.mark.asyncio
async def test_missing_spreadsheet_original_does_not_silently_ingest_flattened_text(tmp_path):
    markdown = tmp_path / "costs.md"
    markdown.write_text("# Costs\n", encoding="utf-8")

    processor = IncrementalDocumentProcessor(
        SimpleNamespace(resources=SimpleNamespace(prompts=SimpleNamespace(language="en"))),
        "source",
        max_concurrency=1,
    )
    with pytest.raises(FileNotFoundError):
        await processor.build_request(
            markdown,
            context=document_request("source").context,
            input_id="article",
            original_path=tmp_path / "missing.xls",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["pdf", "docx", "md"])
async def test_non_spreadsheet_keeps_existing_markdown_input(tmp_path, extension):
    markdown = tmp_path / "document.md"
    markdown.write_text("# Existing document\n", encoding="utf-8")
    original = tmp_path / f"original.{extension}"
    original.write_bytes(b"original source bytes")
    processor = IncrementalDocumentProcessor(
        SimpleNamespace(resources=SimpleNamespace(prompts=SimpleNamespace(language="en"))),
        "source",
        max_concurrency=1,
    )
    request = await processor.build_request(
        markdown, context=document_request("source").context, input_id="article", original_path=original
    )
    assert isinstance(request.source, DocumentSource)
    assert request.source.content == markdown.read_text()
    assert request.source.original_sha256 == sha256(original.read_bytes()).hexdigest()
    parsed = await ParseStage().run(request.source)
    assert parsed.body.strip() == "# Existing document"
    assert parsed.table_structure is None


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_mode", ["standard", "heading_strict"])
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("extension", ["xlsx", "xls"])
async def test_anydoc_spreadsheets_keep_existing_engine_behavior(tmp_path, cached, chunk_mode, extension, monkeypatch):
    """AnyDoc mode retains the existing Excel route, original file and record groups."""
    from zleap.sag.pipeline import FileSource

    original = tmp_path / f"costs.{extension.upper()}"
    if extension == "xls":
        original.write_bytes((Path(__file__).parent / "fixtures" / "costs-general.xls").read_bytes())
    else:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "成本"
        for row in [
            ("产品A", None),
            ("项目", "金额"),
            ("材料", 10),
            ("以上成本合计", 10),
            ("产品B", None),
            ("项目", "金额"),
            ("材料", 20),
            ("以上成本合计", 20),
        ]:
            sheet.append(row)
        workbook.save(original)
    original_bytes = original.read_bytes()

    def unexpected_anydoc(path):
        pytest.fail(f"Excel must stay on MarkItDown: {path}")

    monkeypatch.setattr(anydoc_parser, "convert", unexpected_anydoc)
    prepared = await prepare_document(str(original), Settings(_env_file=None, document_parser="anydoc"))
    assert prepared.provider == "markitdown" and prepared.fallback_from is None
    if cached:
        prepared = await prepare_document(str(original), Settings(_env_file=None, document_parser="anydoc"))
        assert prepared.cached
    baseline_original = tmp_path / f"baseline.{extension.upper()}"
    baseline_original.write_bytes(original_bytes)
    baseline_prepared = await prepare_document(
        str(baseline_original), Settings(_env_file=None, document_parser="markitdown")
    )
    assert baseline_prepared.provider == "markitdown"
    results = []

    async def inspect_source(source, *, chunk_options):
        assert isinstance(source, DocumentSource)
        assert source.content == Path(prepared.path).read_text(encoding="utf-8")
        assert source.original_file is not None
        assert source.original_sha256 == sha256(original_bytes).hexdigest()
        parsed = await ParseStage().run(source)
        chunks = await ChunkStage().run(parsed, chunk_options)
        # Compare with the unmodified engine, including its existing limitations.
        # This feature must preserve the Excel pipeline rather than fix its output.
        baseline_source = DocumentSource(
            content=Path(baseline_prepared.path).read_text(encoding="utf-8"),
            original_file=FileSource(path=str(baseline_original)),
            original_sha256=sha256(original_bytes).hexdigest(),
            descriptor=source.descriptor,
        )
        baseline_parsed = await ParseStage().run(baseline_source)
        baseline_chunks = await ChunkStage().run(baseline_parsed, chunk_options)
        assert parsed.body == baseline_parsed.body
        assert [chunk.content for chunk in chunks.chunks] == [chunk.content for chunk in baseline_chunks.chunks]
        results.append((parsed, chunks))

    processor = IncrementalDocumentProcessor(
        SimpleNamespace(resources=SimpleNamespace(prompts=SimpleNamespace(language="en"))),
        "source",
        max_concurrency=1,
        chunk_mode=chunk_mode,
    )
    request = await processor.build_request(
        prepared.path, context=document_request("source").context, input_id="article", original_path=original
    )
    await inspect_source(request.source, chunk_options=request.chunk_options)
    parsed, chunks = results[0]
    assert parsed.table_structure is not None
    groups = list(parsed.table_structure.groups or [])
    assert [group.metadata["identity_value"] for group in groups] == ["产品A", "产品B"]
    assert [(group.cell_range.row_start, group.cell_range.row_end) for group in groups] == [(1, 4), (5, 8)]
    assert chunks.chunks
    assert {chunk.metadata.get("record_group_id") for chunk in chunks.chunks} - {None} == {
        group.record_group_id for group in groups
    }
    assert original.read_bytes() == original_bytes
