"""Real HTTP, queue, parsing, chunk storage and retrieval with offline model stubs."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from zleap.sag import DataEngine
from zleap.sag.core.ai.embedding import EmbeddingClient

from sag_api.core.config import settings
from tests.helpers import corpus
from tests.test_dsh_integration import _connector_api_resource


@pytest.fixture
def anydoc_api(monkeypatch):
    calls = []

    async def vector(_client, _text):
        return [1.0] + [0.0] * ((_client.schema_dimensions or settings.effective_embedding_schema_dimensions) - 1)

    async def vectors(_client, texts):
        return [await vector(_client, text) for text in texts]

    async def no_remote_extraction(_engine, target, *args, **kwargs):
        calls.append(target)
        return SimpleNamespace(event_ids=(), event_count=0, stats={"zero_event_chunks": list(target.chunk_ids)})

    monkeypatch.setattr(EmbeddingClient, "generate", vector)
    monkeypatch.setattr(EmbeddingClient, "batch_generate", vectors)
    monkeypatch.setattr(DataEngine, "extract", no_remote_extraction)
    @asynccontextmanager
    async def resource():
        # MCP's AnyIO cancel scope must enter and exit in the test's same task.
        async with _connector_api_resource() as (client, app, jwt, connector, source_id, _source_ids):
            for config in (settings, app.state.engine_manager._settings):
                monkeypatch.setattr(config, "document_parser", "anydoc")
                monkeypatch.setattr(config, "llm_api_key", "offline-test-placeholder")
            yield client, jwt, connector, source_id, calls

    return resource


async def _wait_for_document(client, jwt, source_id, document_id):
    async with asyncio.timeout(60):
        while True:
            response = await client.get(f"/api/v1/sources/{source_id}/documents/{document_id}", headers=jwt)
            assert response.status_code == 200, response.text
            document = response.json()
            if document["status"] in {"ready", "failed"}:
                return document
            await asyncio.sleep(0.1)


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["docx", "csv", "xls"])
async def test_anydoc_upload_is_indexed_and_retrievable(anydoc_api, tmp_path, extension):
    async with anydoc_api() as resource:
        await _assert_upload_is_retrievable(resource, tmp_path, extension)


async def _assert_upload_is_retrievable(resource, tmp_path, extension):
    client, jwt, connector, source_id, extraction_calls = resource
    original = tmp_path / f"acceptance-{uuid4().hex}.{extension}"
    if extension == "docx":
        corpus.write_docx(original, "验收资料", "产品A材料预算10元，产品B材料预算20元。")
    elif extension == "csv":
        original.write_bytes("项目,金额\n产品A材料,10\n产品B材料,20\n".encode("gb18030"))
    else:
        original.write_bytes((Path(__file__).parent / "fixtures" / "costs-general.xls").read_bytes())
    original_bytes = original.read_bytes()
    uploaded = await client.post(
        f"/api/v1/sources/{source_id}/documents", headers=jwt,
        files={"file": (original.name, original_bytes, "application/octet-stream")},
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    document = await _wait_for_document(client, jwt, source_id, document_id)
    assert document["status"] == "ready", document
    assert document["parser_provider"] == ("markitdown" if extension == "xls" else "anydoc")
    assert document["parser_status"] == "done"
    assert document["chunk_count"] > 0
    assert extraction_calls

    response = await client.post(
        "/api/v1/search", headers=connector,
        json={"query": "材料", "source_ids": [source_id], "strategy": "vector", "top_k": 10},
    )
    assert response.status_code == 200, response.text
    assert response.json()["stats"]["chunk_recall"] == "batch-vector"
    sections = response.json()["sections"]
    assert sections
    body = "\n".join(section["content"] for section in sections)
    for value in ("产品A", "产品B", "10", "20"):
        assert value in body
    for section in sections:
        assert section["source_id"] == source_id
        chunk = await client.get(
            f"/api/v1/sources/{source_id}/chunks/{section['chunk_id']}", headers=jwt,
        )
        assert chunk.status_code == 200, chunk.text
        assert chunk.json()["content"] == section["content"]

    downloaded = await client.get(f"/api/v1/sources/{source_id}/documents/{document_id}/file", headers=jwt)
    assert downloaded.status_code == 200
    assert downloaded.content == original_bytes == original.read_bytes()


@pytest.mark.asyncio
async def test_anydoc_scan_pdf_reports_parse_failure_without_indexing(anydoc_api, tmp_path):
    async with anydoc_api() as resource:
        await _assert_scan_pdf_is_rejected(resource, tmp_path)


async def _assert_scan_pdf_is_rejected(resource, tmp_path):
    client, jwt, _connector, source_id, extraction_calls = resource
    original = tmp_path / "scan.pdf"
    corpus.write_scanned_pdf(original)
    uploaded = await client.post(
        f"/api/v1/sources/{source_id}/documents", headers=jwt,
        files={"file": (original.name, original.read_bytes(), "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    document = await _wait_for_document(client, jwt, source_id, uploaded.json()["id"])
    assert document["status"] == "failed", document
    assert document["error_stage"] == "parse"
    assert document["parser_provider"] == "anydoc"
    assert document["chunk_count"] == 0
    assert extraction_calls == []
