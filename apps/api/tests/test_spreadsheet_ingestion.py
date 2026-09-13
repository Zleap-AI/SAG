"""The application must retain spreadsheet record boundaries at the engine seam."""
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook
from zleap.sag.pipeline import DocumentSource
from zleap.sag.pipeline.chunk import ChunkStage
from zleap.sag.pipeline.parse import ParseStage

from sag_api.core.config import Settings
from sag_api.parsing import prepare_document
from sag_api.sag.dto import ProcessCheckpoint
from sag_api.sag.incremental_processor import IncrementalDocumentProcessor


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_mode", ["standard", "heading_strict"])
@pytest.mark.parametrize("cached", [False, True])
async def test_prepared_spreadsheet_reaches_parser_with_original_records(tmp_path, cached, chunk_mode):
    original = tmp_path / "costs.XLSX"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "成本"
    for row in [
        ("产品A", None), ("项目", "金额"), ("材料", 10), ("以上成本合计", 10),
        ("产品B", None), ("项目", "金额"), ("材料", 20), ("以上成本合计", 20),
    ]:
        sheet.append(row)
    workbook.save(original)
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
