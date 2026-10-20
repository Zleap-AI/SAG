"""按行阅读（REST `/read` 与 MCP `read`）：PDF / Office 读解析后的 Markdown，文本类仍读原文件。"""

import uuid

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect

from tests.test_document_parsing import _simple_docx, _simple_pdf


@pytest.mark.asyncio
async def test_read_returns_parsed_markdown_for_binary_documents(tmp_path):
    from sqlalchemy import select
    from zleap.sag.db.models import Article, ArticleParseStatus, DataSource

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Document, Source
    from sag_api.enums import DocumentStatus
    from sag_api.main import app
    from sag_api.mcp.server import build_source_mcp, use_scope

    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(_simple_pdf("Raw PDF layer"))
    docx = tmp_path / "notes.docx"
    _simple_docx(docx, "Raw DOCX body")
    txt = tmp_path / "plain.txt"
    txt.write_text("原文第一行\n原文第二行\n", encoding="utf-8")
    pending_pdf = tmp_path / "pending.pdf"
    pending_pdf.write_bytes(_simple_pdf("Not ingested yet"))

    # (文件名, content_type, 原文件, 入库 Markdown；None 表示尚未入库)
    cases = {
        "pdf": ("report.pdf", "application/pdf", pdf, "# 报告\n\nPDF 解析正文\n"),
        "docx": (
            "notes.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            docx,
            "DOCX 解析正文\n",
        ),
        "txt": ("plain.txt", "text/plain", txt, "规范化后的文本\n"),
        "pending": ("pending.pdf", "application/pdf", pending_pdf, None),
    }

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            reg = await c.post("/api/v1/auth/register", json={"email": "reader@t.com", "password": "password123"})
            assert reg.status_code == 201, reg.text
            headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
            src = (await c.post("/api/v1/sources", headers=headers, json={"name": "阅读"})).json()

            doc_ids: dict[str, str] = {}
            async with SessionLocal() as s:
                source = await s.get(Source, src["id"])
                scid = source.sag_source_config_id
                for key, (filename, content_type, path, markdown) in cases.items():
                    doc_ids[key] = uuid.uuid4().hex
                    s.add(
                        Document(
                            id=doc_ids[key],
                            source_id=src["id"],
                            filename=filename,
                            content_type=content_type,
                            size_bytes=path.stat().st_size,
                            storage_path=str(path),
                            status=DocumentStatus.READY if markdown else DocumentStatus.PENDING,
                            sag_source_id=f"article-{key}" if markdown else None,
                        )
                    )
                await s.commit()
                sources = tuple((await s.execute(select(Source).where(Source.id == src["id"]))).scalars())

            sf = await app.state.engine_manager.get_sag_session_factory(scid)
            async with sf() as s:
                await s.merge(DataSource(id=scid, name="阅读"))
                for key, (filename, _content_type, _path, markdown) in cases.items():
                    if markdown is None:
                        continue
                    s.add(
                        Article(
                            id=f"article-{key}",
                            data_source_id=scid,
                            document_id=doc_ids[key],
                            title=filename,
                            content=markdown,
                            status="COMPLETED",
                            parse_status=ArticleParseStatus.COMPLETED,
                        )
                    )
                await s.commit()

            async def rest_read(key: str) -> httpx.Response:
                return await c.get(f"/api/v1/sources/{src['id']}/documents/{doc_ids[key]}/read", headers=headers)

            pdf_resp = await rest_read("pdf")
            assert pdf_resp.status_code == 200, pdf_resp.text
            assert pdf_resp.json()["lines"] == ["# 报告\n", "\n", "PDF 解析正文\n"]
            assert pdf_resp.json()["total_lines"] == 3
            pdf_page = await c.get(
                f"/api/v1/sources/{src['id']}/documents/{doc_ids['pdf']}/read",
                headers=headers,
                params={"offset": 3, "limit": 1},
            )
            assert pdf_page.status_code == 200, pdf_page.text
            assert pdf_page.json()["lines"] == ["PDF 解析正文\n"]
            assert pdf_page.json()["offset"] == 3
            assert pdf_page.json()["total_lines"] == 3

            docx_resp = await rest_read("docx")
            assert docx_resp.status_code == 200, docx_resp.text
            assert docx_resp.json()["lines"] == ["DOCX 解析正文\n"]

            # 文本类保持原行为：读原文件而不是入库 Markdown。
            txt_resp = await rest_read("txt")
            assert txt_resp.status_code == 200, txt_resp.text
            assert txt_resp.json()["lines"] == ["原文第一行\n", "原文第二行\n"]

            pending_resp = await rest_read("pending")
            assert pending_resp.status_code == 404
            assert "%PDF" not in pending_resp.text

            with use_scope(app.state.engine_manager, sources):
                async with connect(build_source_mcp()) as client:
                    await client.initialize()

                    async def mcp_read(key: str) -> str:
                        result = await client.call_tool("read", {"document_id": doc_ids[key]})
                        assert not result.isError
                        return result.content[0].text

                    pdf_text = await mcp_read("pdf")
                    assert "PDF 解析正文" in pdf_text
                    assert "共 3 行" in pdf_text
                    assert "%PDF" not in pdf_text
                    pdf_page = await client.call_tool(
                        "read", {"document_id": doc_ids["pdf"], "offset": 3, "limit": 1}
                    )
                    assert "PDF 解析正文" in pdf_page.content[0].text
                    assert "# 报告" not in pdf_page.content[0].text

                    docx_text = await mcp_read("docx")
                    assert "DOCX 解析正文" in docx_text
                    assert "PK" not in docx_text

                    txt_text = await mcp_read("txt")
                    assert "原文第一行" in txt_text
                    assert "规范化后的文本" not in txt_text

                    pending_text = await mcp_read("pending")
                    assert "尚无可读文本" in pending_text
                    assert "%PDF" not in pending_text

            # 套件共享同一个数据库：清理已入库文档，避免影响 Embedding 配置变更类用例。
            async with SessionLocal() as s:
                source = await s.get(Source, src["id"])
                await s.delete(source)
                await s.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["read", "outline"])
@pytest.mark.parametrize("visibility", ["deleting", "delete_failed", "outside_scope"])
async def test_mcp_document_reads_respect_deletion_and_source_scope(tmp_path, tool, visibility):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Source
    from sag_api.enums import DocumentStatus
    from sag_api.mcp.server import build_source_mcp, use_scope

    await init_db()
    private_text = "# 仅属于原信源的私有正文\n"
    path = tmp_path / "private.md"
    path.write_text(private_text, encoding="utf-8")
    owner = Source(name="owner", sag_source_config_id=f"src_{uuid.uuid4().hex}")
    other = Source(name="other", sag_source_config_id=f"src_{uuid.uuid4().hex}")
    document = Document(
        source_id="", filename="private.md", content_type="text/markdown",
        storage_path=str(path), size_bytes=path.stat().st_size,
        status=DocumentStatus.READY if visibility == "outside_scope" else DocumentStatus(visibility),
        sag_source_id=f"article-{uuid.uuid4().hex}",
    )
    async with SessionLocal() as session:
        session.add_all([owner, other])
        await session.flush()
        document.source_id = owner.id
        session.add(document)
        await session.commit()
    manager = SimpleNamespace(
        get_document_markdown=AsyncMock(return_value=private_text),
        list_chunk_headings=AsyncMock(return_value=[
            {"rank": 0, "heading": "仅属于原信源的私有正文", "chunk_id": "private-chunk"}
        ]),
    )
    try:
        with use_scope(manager, (other if visibility == "outside_scope" else owner,)):
            async with connect(build_source_mcp()) as client:
                await client.initialize()
                response = await client.call_tool(tool, {"document_id": document.id})
                assert not response.isError
                assert response.content[0].text == "（未找到该文档）"
                assert "私有正文" not in response.content[0].text
    finally:
        async with SessionLocal() as session:
            for source_id in (owner.id, other.id):
                source = await session.get(Source, source_id)
                await session.delete(source)
            await session.commit()
