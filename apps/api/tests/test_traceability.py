"""引用溯源：chunk 原文端点 + citations 的 sag source_id 语义。"""

import uuid

import httpx
import pytest


@pytest.mark.asyncio
async def test_chunk_endpoint_and_citation_refs():
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Source
    from sag_api.generation.prompt import build_citations
    from sag_api.main import app
    from sag_api.sag.dto import RetrievedSection

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            tok = (
                await c.post(
                    "/api/v1/auth/register",
                    json={"email": "trace@x.com", "password": "password123"},
                )
            ).json()["access_token"]
            H = {"Authorization": f"Bearer {tok}"}

            src = (await c.post("/api/v1/sources", headers=H, json={"name": "手册"})).json()
            sid = src["id"]
            async with SessionLocal() as s:
                scid = (await s.get(Source, sid)).sag_source_config_id

            # 注入一个分块（模拟 ingest 产物）
            await app.state.engine_manager.provision(scid)
            from zleap.sag.db import get_session_factory
            from zleap.sag.db.models import SourceChunk, SourceConfig

            chunk_id = uuid.uuid4().hex
            full_text = "导出支持 Markdown / PDF / JSON。" * 30  # 远超引用预览上限
            sf = get_session_factory()
            async with sf() as s:
                await s.merge(SourceConfig(id=scid, name="手册"))
                s.add(
                    SourceChunk(
                        id=chunk_id,
                        source_config_id=scid,
                        source_type="doc",
                        source_id="d1",
                        heading="导出与备份",
                        content=full_text,
                    )
                )
                await s.commit()

            # 原文端点：返回完整内容 + sag 信源标识
            r = await c.get(f"/api/v1/sources/{sid}/chunks/{chunk_id}", headers=H)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["content"] == full_text and len(body["content"]) > 240
            assert body["heading"] == "导出与备份"
            assert body["source_id"] == sid and body["source_name"] == "手册"

            # 不存在 / 跨源访问 → 404
            assert (await c.get(f"/api/v1/sources/{sid}/chunks/{uuid.uuid4().hex}", headers=H)).status_code == 404

            # citations：source_id 应为 sag 信源 id（非引擎内部 id）
            section = RetrievedSection(
                chunk_id=chunk_id,
                heading="导出与备份",
                content=full_text,
                score=0.9,
                source_id="engine-internal-id",
                source_config_id=scid,
            )
            cites = build_citations([section], {scid: {"id": sid, "name": "手册"}})
            assert cites[0]["source_id"] == sid
            assert cites[0]["source_name"] == "手册"
            assert cites[0]["snippet"].endswith("…") and len(cites[0]["snippet"]) <= 722
            assert "summary" not in cites[0]
