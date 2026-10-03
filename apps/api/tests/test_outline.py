"""文档大纲按 Markdown 标题层级组织（REST `/outline` 与 MCP `outline`）。"""

from __future__ import annotations

import uuid

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect

from sag_api.services.outline_service import (
    build_outline,
    markdown_headings,
    outline_rows,
    render_outline_text,
)

MARKDOWN = """# Annual Report

Intro text.

## 1 Introduction

```python
# not a heading
```

### 1.1 Background

## 2 [Results](https://example.com)

### Notes

## 3 Appendix

### Notes
"""


def _row(rank: int, heading: str, chunk_id: str | None = None) -> dict:
    return {"rank": rank, "heading": heading, "chunk_id": chunk_id or f"c{rank}"}


def test_markdown_headings_keep_levels_and_skip_code_fences():
    assert markdown_headings(MARKDOWN) == [
        (1, "Annual Report"),
        (2, "1 Introduction"),
        (3, "1.1 Background"),
        (2, "2 Results"),
        (3, "Notes"),
        (2, "3 Appendix"),
        (3, "Notes"),
    ]


def test_build_outline_nests_chunks_and_matches_repeated_titles_in_order():
    rows = [
        _row(0, "Annual Report"),
        _row(1, "1 Introduction"),
        _row(2, "1.1 Background"),
        _row(3, "1.1 Background"),
        _row(4, "2 Results"),
        _row(5, "Notes"),
        _row(6, "3 Appendix"),
        _row(7, "Notes"),  # 同名标题：按阅读顺序对应附录下的 “Notes”
    ]

    nodes = build_outline(MARKDOWN, rows)

    assert [(node.depth, node.level, node.title) for node in nodes] == [
        (0, 1, "Annual Report"),
        (1, 2, "1 Introduction"),
        (2, 3, "1.1 Background"),
        (1, 2, "2 Results"),
        (2, 3, "Notes"),
        (1, 2, "3 Appendix"),
        (2, 3, "Notes"),
    ]
    assert [[chunk["rank"] for chunk in node.chunks] for node in nodes] == [
        [0],
        [1],
        [2, 3],
        [4],
        [5],
        [6],
        [7],
    ]
    assert nodes[6].path == ["Annual Report", "3 Appendix", "Notes"]


def test_render_outline_text_is_indented_by_hierarchy():
    rows = [_row(0, "Annual Report"), _row(1, "1.1 Background"), _row(2, "")]

    text = render_outline_text(build_outline(MARKDOWN, rows))

    assert text.splitlines() == [
        "# Annual Report — 分块 0（chunk_id=c0）",
        "  ## 1 Introduction",
        "    ### 1.1 Background — 分块 1（chunk_id=c1）",
        "  ## 2 Results",
        "    ### Notes",
        "  ## 3 Appendix",
        "    ### Notes",
        "（无标题分块） — 分块 2（chunk_id=c2）",
    ]


def test_outline_without_markdown_falls_back_to_flat_chunk_list():
    rows = [_row(1, "B"), _row(0, "A"), _row(2, "B")]

    nodes = build_outline(None, rows)

    assert render_outline_text(nodes).splitlines() == [
        "A — 分块 0（chunk_id=c0）",
        "B — 分块 1（chunk_id=c1）、2（chunk_id=c2）",
    ]
    assert outline_rows(nodes) == [
        {"rank": 0, "heading": "A", "chunk_id": "c0", "level": None, "path": []},
        {"rank": 1, "heading": "B", "chunk_id": "c1", "level": None, "path": []},
        {"rank": 2, "heading": "B", "chunk_id": "c2", "level": None, "path": []},
    ]


@pytest.mark.asyncio
async def test_outline_endpoints_expose_heading_hierarchy():
    from sqlalchemy import select
    from zleap.sag.db.models import Article, ArticleParseStatus, DataSource, SourceChunk

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Document, Source
    from sag_api.enums import DocumentStatus
    from sag_api.main import app
    from sag_api.mcp.server import build_source_mcp, use_scope

    markdown = "# 报告\n\n## 一、背景\n\n正文\n\n### 1.1 现状\n\n正文\n\n## 二、结论\n\n正文\n"
    chunks = [("报告", 0), ("一、背景", 1), ("1.1 现状", 2), ("二、结论", 3)]

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            reg = await c.post(
                "/api/v1/auth/register", json={"email": "outline@t.com", "password": "password123"}
            )
            assert reg.status_code == 201, reg.text
            headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
            src = (await c.post("/api/v1/sources", headers=headers, json={"name": "大纲"})).json()
            doc_id = uuid.uuid4().hex
            article_id = f"article-{doc_id}"
            async with SessionLocal() as s:
                source = await s.get(Source, src["id"])
                scid = source.sag_source_config_id
                s.add(
                    Document(
                        id=doc_id,
                        source_id=src["id"],
                        filename="report.pdf",
                        content_type="application/pdf",
                        size_bytes=1,
                        storage_path="/nonexistent/report.pdf",
                        status=DocumentStatus.READY,
                        sag_source_id=article_id,
                    )
                )
                await s.commit()
                sources = tuple((await s.execute(select(Source).where(Source.id == src["id"]))).scalars())

            sf = await app.state.engine_manager.get_sag_session_factory(scid)
            async with sf() as s:
                await s.merge(DataSource(id=scid, name="大纲"))
                s.add(
                    Article(
                        id=article_id,
                        data_source_id=scid,
                        document_id=doc_id,
                        title="report.pdf",
                        content=markdown,
                        status="COMPLETED",
                        parse_status=ArticleParseStatus.COMPLETED,
                    )
                )
                for heading, rank in chunks:
                    s.add(
                        SourceChunk(
                            id=f"{doc_id}-{rank}",
                            data_source_id=scid,
                            source_type="ARTICLE",
                            source_id=article_id,
                            article_id=article_id,
                            heading=heading,
                            content="正文",
                            rank=rank,
                        )
                    )
                await s.commit()

            try:
                resp = await c.get(
                    f"/api/v1/sources/{src['id']}/outline",
                    headers=headers,
                    params={"document_id": doc_id},
                )
                assert resp.status_code == 200, resp.text
                assert [
                    (row["rank"], row["level"], row["path"]) for row in resp.json()["outline"]
                ] == [
                    (0, 1, ["报告"]),
                    (1, 2, ["报告", "一、背景"]),
                    (2, 3, ["报告", "一、背景", "1.1 现状"]),
                    (3, 2, ["报告", "二、结论"]),
                ]

                with use_scope(app.state.engine_manager, sources):
                    async with connect(build_source_mcp()) as client:
                        await client.initialize()
                        result = await client.call_tool("outline", {"document_id": doc_id})
                        assert not result.isError
                        assert result.content[0].text.splitlines() == [
                            f"# 报告 — 分块 0（chunk_id={doc_id}-0）",
                            f"  ## 一、背景 — 分块 1（chunk_id={doc_id}-1）",
                            f"    ### 1.1 现状 — 分块 2（chunk_id={doc_id}-2）",
                            f"  ## 二、结论 — 分块 3（chunk_id={doc_id}-3）",
                        ]
            finally:
                async with SessionLocal() as s:
                    source = await s.get(Source, src["id"])
                    await s.delete(source)
                    await s.commit()
