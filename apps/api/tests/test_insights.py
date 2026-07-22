"""实体读路径：注入事件—实体图谱后验证 entities 端点（离线）。"""

import uuid

import httpx
import pytest


@pytest.mark.asyncio
async def test_entity_read_path():
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Document, Source
    from sag_api.enums import DocumentStatus
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            tok = (
                await c.post("/api/v1/auth/register", json={"email": "book@x.com", "password": "password123"})
            ).json()["access_token"]
            H = {"Authorization": f"Bearer {tok}"}

            src = (await c.post("/api/v1/sources", headers=H, json={"name": "三国演义"})).json()
            sid = src["id"]
            async with SessionLocal() as s:
                source = await s.get(Source, sid)
                scid = source.sag_source_config_id
                document_id = uuid.uuid4().hex
                s.add(
                    Document(
                        id=document_id,
                        source_id=sid,
                        filename="三国演义.md",
                        content_type="text/markdown",
                        size_bytes=128,
                        storage_path="/tmp/three-kingdoms.md",
                        status=DocumentStatus.READY,
                        chunk_count=1,
                        event_count=2,
                        sag_source_id="d1",
                    )
                )
                source.document_count = 1
                source.chunk_count = 1
                # 模拟抽取检查点已经写入文档统计、信源聚合数尚未结算的短暂窗口。
                # 图谱必须以文档统计作为总量口径，不能把当前切片误报为全部。
                source.event_count = 0
                await s.commit()

            # 注入事件—实体图谱（模拟 extract 产物）
            from zleap.sag.db import get_session_factory
            from zleap.sag.db.models import (
                Article,
                ArticleParseStatus,
                Entity,
                EntityType,
                EventEntity,
                SourceChunk,
                SourceConfig,
                SourceEvent,
            )

            sf = get_session_factory()
            async with sf() as s:
                await s.merge(SourceConfig(id=scid, name="三国演义"))
                s.add(
                    Article(
                        id="d1",
                        source_config_id=scid,
                        source_id="d1",
                        title="三国演义",
                        content="# 三国演义\n\n关羽过五关斩六将。\n",
                        status="COMPLETED",
                        parse_status=ArticleParseStatus.COMPLETED,
                    )
                )
                et = EntityType(id=uuid.uuid4().hex, type="person", name="人物")
                s.add(et)
                await s.flush()
                ent = Entity(
                    id=uuid.uuid4().hex,
                    source_config_id=scid,
                    entity_type_id=et.id,
                    type="person",
                    name="关羽",
                    normalized_name="关羽",
                    description="蜀汉名将",
                )
                s.add(ent)
                await s.flush()
                ev = SourceEvent(
                    id=uuid.uuid4().hex,
                    source_config_id=scid,
                    source_type="doc",
                    source_id="d1",
                    title="过五关斩六将",
                    summary="关羽千里走单骑，护送二嫂寻兄。",
                    content="关羽过五关斩六将。",
                    chunk_id="chunk-1",
                )
                s.add(ev)
                await s.flush()
                event_id = ev.id
                hidden_event = SourceEvent(
                    id=uuid.uuid4().hex,
                    source_config_id=scid,
                    source_type="doc",
                    source_id="d1",
                    title="桃园结义",
                    summary="刘备、关羽、张飞结为兄弟。",
                    content="桃园三结义。",
                    chunk_id="chunk-2",
                    rank=1,
                    status="DELETED",
                )
                s.add(hidden_event)
                await s.flush()
                hidden_event_id = hidden_event.id
                entity_id = ent.id
                s.add(
                    SourceChunk(
                        id="chunk-1",
                        source_config_id=scid,
                        source_type="ARTICLE",
                        source_id="d1",
                        article_id="d1",
                        heading="三国演义",
                        content="关羽过五关斩六将。",
                    )
                )
                s.add(
                    SourceChunk(
                        id="chunk-2",
                        source_config_id=scid,
                        source_type="ARTICLE",
                        source_id="d1",
                        article_id="d1",
                        heading="桃园结义",
                        content="刘备、关羽、张飞桃园结义。",
                    )
                )
                s.add(EventEntity(id=uuid.uuid4().hex, event_id=ev.id, entity_id=ent.id, weight=1.0))
                await s.commit()

            parsed = await c.get(
                f"/api/v1/sources/{sid}/documents/{document_id}/parsed",
                headers=H,
            )
            assert parsed.status_code == 200
            assert parsed.headers["content-type"].startswith("text/markdown")
            assert parsed.text == "# 三国演义\n\n关羽过五关斩六将。\n"

            # 读实体（带热度）
            ents = (await c.get(f"/api/v1/sources/{sid}/entities?types=person", headers=H)).json()
            assert any(e["name"] == "关羽" and e["heat"] >= 1 for e in ents)

            # 图谱读路径保留真实文档—事件—实体关系，并返回展示/总量信息。
            response = await c.get(f"/api/v1/sources/{sid}/graph", headers=H)
            assert response.status_code == 200
            graph = response.json()
            assert graph["documents"][0]["id"] == document_id
            assert {event["title"] for event in graph["events"]} == {
                "过五关斩六将",
                "桃园结义",
            }
            assert all(event["document_id"] == document_id for event in graph["events"])
            assert graph["entities"][0]["name"] == "关羽"
            assert {relation["kind"] for relation in graph["relations"]} == {
                "contains",
                "mentions",
            }
            assert graph["counts"]["events"] == 2
            assert graph["counts"]["shown_events"] == len(graph["events"]) == 2
            assert graph["counts"]["shown_relations"] == 3
            assert graph["truncated"] is False
            async with sf() as s:
                assert (await s.get(SourceEvent, hidden_event_id)).status == "COMPLETED"

            # 页面选择较小预算时，展示数可以缩小，但总量仍须与文档的事项统计一致；
            # 否则 3D 图谱会把“1 / 2”错误显示成“1 / 1”，看起来像漏掉了事项。
            limited = (
                await c.get(
                    f"/api/v1/sources/{sid}/graph?event_limit=1&entity_limit=1000",
                    headers=H,
                )
            ).json()
            assert len(limited["events"]) == 1
            assert limited["counts"]["shown_events"] == 1
            assert limited["counts"]["events"] == 2
            assert limited["truncated"] is True

            # 搜索命中分块可稳定映射回事项标题、正文及真实事项—实体关系。
            from sag_api.sag import RetrievedSection

            search_graph = await app.state.engine_manager.graph_for_sections(
                [
                    RetrievedSection(
                        chunk_id="chunk-1",
                        score=0.91,
                        source_config_id=scid,
                    )
                ],
                {scid: source},
            )
            assert search_graph.events[0].title == "过五关斩六将"
            assert search_graph.events[0].summary.startswith("关羽千里走单骑")
            assert search_graph.events[0].content == "关羽过五关斩六将。"
            assert search_graph.events[0].score == pytest.approx(0.91)
            assert search_graph.entities[0].name == "关羽"
            assert search_graph.associations[0].event_id == search_graph.events[0].id

            # 事项向量与块向量并行召回：即使事项所在 chunk-2 没进入块 top-k，
            # 也必须能通过事项 id 直接回表，并优先使用事项相似度排序。
            direct_event_graph = await app.state.engine_manager.graph_for_sections(
                [
                    RetrievedSection(
                        chunk_id="chunk-1",
                        score=0.91,
                        source_config_id=scid,
                    )
                ],
                {scid: source},
                event_scores={(scid, hidden_event_id): 0.97},
            )
            assert direct_event_graph.events[0].title == "桃园结义"
            assert direct_event_graph.events[0].chunk_id == "chunk-2"
            assert direct_event_graph.events[0].score == pytest.approx(0.97)

            # 图谱允许界面按性能选择更大的展示量，同时保留防止误请求的上限。
            large = await c.get(
                f"/api/v1/sources/{sid}/graph?document_limit=2000&event_limit=2000&entity_limit=2000",
                headers=H,
            )
            assert large.status_code == 200
            invalid = await c.get(f"/api/v1/sources/{sid}/graph?event_limit=10001", headers=H)
            assert invalid.status_code == 422

            # 删除文档必须同步清理统计与引擎中的块、事件及孤立实体。
            deleted = await c.delete(
                f"/api/v1/sources/{sid}/documents/{document_id}",
                headers=H,
            )
            assert deleted.status_code == 200
            source_after_delete = (await c.get(f"/api/v1/sources/{sid}", headers=H)).json()
            assert source_after_delete["document_count"] == 0
            assert source_after_delete["chunk_count"] == 0
            assert source_after_delete["event_count"] == 0
            async with sf() as s:
                assert await s.get(Article, "d1") is None
                assert await s.get(SourceChunk, "chunk-1") is None
                assert await s.get(SourceChunk, "chunk-2") is None
                assert await s.get(SourceEvent, event_id) is None
                assert await s.get(SourceEvent, hidden_event_id) is None
                assert await s.get(Entity, entity_id) is None

            empty_source = (await c.post("/api/v1/sources", headers=H, json={"name": "空信源"})).json()
            empty_graph = (await c.get(f"/api/v1/sources/{empty_source['id']}/graph", headers=H)).json()
            assert empty_graph["documents"] == []
            assert empty_graph["events"] == []
            assert empty_graph["entities"] == []
            assert empty_graph["relations"] == []
            assert empty_graph["truncated"] is False


@pytest.mark.asyncio
async def test_source_graph_can_filter_one_or_multiple_documents(monkeypatch):
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Document, Source
    from sag_api.enums import DocumentStatus
    from sag_api.main import app
    from sag_api.sag import GraphEventInfo, SourceGraphInfo

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            token = (
                await client.post(
                    "/api/v1/auth/register",
                    json={
                        "email": f"graph-filter-{uuid.uuid4().hex}@x.com",
                        "password": "password123",
                    },
                )
            ).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            source_payload = (
                await client.post(
                    "/api/v1/sources",
                    headers=headers,
                    json={"name": "文档筛选"},
                )
            ).json()
            source_id = source_payload["id"]

            async with SessionLocal() as session:
                source = await session.get(Source, source_id)
                assert source is not None
                first = Document(
                    source_id=source_id,
                    filename="第一篇.md",
                    content_type="text/markdown",
                    size_bytes=10,
                    storage_path="/tmp/first.md",
                    status=DocumentStatus.READY,
                    chunk_count=1,
                    event_count=2,
                    sag_source_id="engine-first",
                )
                second = Document(
                    source_id=source_id,
                    filename="第二篇.md",
                    content_type="text/markdown",
                    size_bytes=10,
                    storage_path="/tmp/second.md",
                    status=DocumentStatus.READY,
                    chunk_count=1,
                    event_count=3,
                    sag_source_id="engine-second",
                )
                session.add_all([first, second])
                source.document_count = 2
                source.chunk_count = 2
                source.event_count = 5
                await session.commit()
                first_id, second_id = first.id, second.id

            seen_source_ids: list[list[str]] = []

            async def fake_source_graph(
                _source_config_id,
                source_ids,
                **_kwargs,
            ):
                seen_source_ids.append(list(source_ids))
                return SourceGraphInfo(
                    events=[
                        GraphEventInfo(
                            id=f"event-{engine_source_id}",
                            source_id=engine_source_id,
                            title=f"事项 {engine_source_id}",
                        )
                        for engine_source_id in source_ids
                    ]
                )

            monkeypatch.setattr(
                app.state.engine_manager,
                "source_graph",
                fake_source_graph,
            )

            single = await client.get(
                f"/api/v1/sources/{source_id}/graph",
                headers=headers,
                params=[("document_ids", second_id)],
            )
            assert single.status_code == 200
            single_graph = single.json()
            assert [document["id"] for document in single_graph["documents"]] == [second_id]
            assert single_graph["events"][0]["document_id"] == second_id
            assert single_graph["counts"]["documents"] == 1
            assert single_graph["counts"]["events"] == 3
            assert seen_source_ids[-1] == ["engine-second"]

            multiple = await client.get(
                f"/api/v1/sources/{source_id}/graph",
                headers=headers,
                params=[
                    ("document_ids", first_id),
                    ("document_ids", second_id),
                    ("document_ids", second_id),
                ],
            )
            assert multiple.status_code == 200
            multiple_graph = multiple.json()
            assert {document["id"] for document in multiple_graph["documents"]} == {
                first_id,
                second_id,
            }
            assert multiple_graph["counts"]["documents"] == 2
            assert multiple_graph["counts"]["events"] == 5
            assert set(seen_source_ids[-1]) == {"engine-first", "engine-second"}

            empty = await client.get(
                f"/api/v1/sources/{source_id}/graph",
                headers=headers,
                params=[("document_ids", "")],
            )
            assert empty.status_code == 200
            assert empty.json()["documents"] == []
            assert empty.json()["events"] == []
            assert empty.json()["counts"]["documents"] == 0
            assert empty.json()["counts"]["events"] == 0
            assert seen_source_ids[-1] == []

            all_documents = await client.get(
                f"/api/v1/sources/{source_id}/graph",
                headers=headers,
            )
            assert all_documents.status_code == 200
            assert all_documents.json()["counts"]["documents"] == 2
            assert all_documents.json()["counts"]["events"] == 5

            async with SessionLocal() as session:
                source = await session.get(Source, source_id)
                assert source is not None
                await session.delete(source)
                await session.commit()
