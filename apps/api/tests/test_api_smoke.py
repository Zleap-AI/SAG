"""HTTP 层冒烟：跑真实 ASGI 应用（含 lifespan / 后台队列），全程离线。"""

import asyncio
import logging
import uuid

import httpx
import pytest


@pytest.mark.asyncio
async def test_end_to_end_offline():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # 系统
            assert (await c.get("/api/v1/system/health")).json()["status"] == "ok"
            caps = (await c.get("/api/v1/system/capabilities")).json()
            assert caps["llm_configured"] is False

            # 认证：注册单账号
            r = await c.post(
                "/api/v1/auth/register",
                json={"email": "a@b.com", "password": "password123", "name": "Ada"},
            )
            assert r.status_code == 201
            tok = r.json()["access_token"]
            H = {"Authorization": f"Bearer {tok}"}

            assert (await c.get("/api/v1/auth/me", headers=H)).json()["email"] == "a@b.com"
            assert (await c.get("/api/v1/auth/me")).status_code == 401
            local_login = await c.post("/api/v1/auth/login", json={"name": "Ada"})
            assert local_login.status_code == 200
            assert local_login.json()["user"]["id"] == r.json()["user"]["id"]
            login = await c.post(
                "/api/v1/auth/login",
                json={"name": "Ada", "email": "a@b.com", "password": "password123"},
            )
            assert login.status_code == 200
            assert login.json()["user"]["id"] == r.json()["user"]["id"]
            dup = await c.post(
                "/api/v1/auth/register", json={"email": "a@b.com", "password": "password123"}
            )
            assert dup.status_code == 409

            # 连接器 + 信源
            conns = (await c.get("/api/v1/sources/connectors", headers=H)).json()
            assert any(x["kind"] == "file_upload" for x in conns)

            r = await c.post("/api/v1/sources", headers=H, json={"name": "手册"})
            assert r.status_code == 201
            sid = r.json()["id"]
            assert r.json()["source_type"] == "document"
            # 共享测试库 → 用存在性/按 id 定位而非精确计数
            def _find(sources):
                return next(s for s in sources if s["id"] == sid)

            assert _find((await c.get("/api/v1/sources", headers=H)).json())["id"] == sid

            # 上传（不等待后台完成，避免 401 重试拖慢测试）
            up = await c.post(
                f"/api/v1/sources/{sid}/documents",
                headers=H,
                files={"file": ("a.md", b"# T\n\nhello world\n", "text/markdown")},
            )
            assert up.status_code == 201 and up.json()["status"] == "pending"
            assert _find((await c.get("/api/v1/sources", headers=H)).json())["document_count"] == 1

            # 统一写入接口：持续推送一批消息 → 归一为文档进入管线
            ing = await c.post(
                f"/api/v1/sources/{sid}/documents/ingest",
                headers=H,
                json={"messages": [{"author": "张三", "text": "明天评审几点？", "ts": "2026-07-07T09:00Z"}]},
            )
            assert ing.status_code == 201 and ing.json()["status"] == "pending"
            assert _find((await c.get("/api/v1/sources", headers=H)).json())["document_count"] == 2

            # 全局搜索：离线（无 embedding）单源失败被吞，返回 200 + 空结果
            gs = await c.post("/api/v1/search", headers=H, json={"query": "hello"})
            assert gs.status_code == 200
            body = gs.json()
            assert body["query"] == "hello" and isinstance(body["sections"], list)
            # 收窄到指定信源
            gs2 = await c.post(
                "/api/v1/search", headers=H, json={"query": "hello", "source_ids": [sid]}
            )
            assert gs2.status_code == 200


@pytest.mark.asyncio
async def test_folder_import_upload_logs_batch_document_and_request_ids(caplog):
    from sag_api.main import app

    batch_id = "018f5f7e-89ab-7def-8123-0123456789a0"
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        caplog.set_level(logging.INFO, logger="sag.documents")
        documents_log = logging.getLogger("sag.documents")
        documents_log.addHandler(caplog.handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            registered = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": f"folder-import-{uuid.uuid4().hex}@t.com",
                    "password": "password123",
                    "name": "Folder Import Test",
                },
            )
            assert registered.status_code == 201
            headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
            source = await client.post("/api/v1/sources", headers=headers, json={"name": "批次导入"})
            assert source.status_code == 201

            uploaded = await client.post(
                f"/api/v1/sources/{source.json()['id']}/documents",
                headers={**headers, "X-SAG-Folder-Import-Id": batch_id},
                files={"file": ("batch.md", b"# Batch\n\ncontent", "text/markdown")},
            )

    assert uploaded.status_code == 201
    request_id = uploaded.headers["X-Request-Id"]
    document_id = uploaded.json()["id"]
    messages = [record.getMessage() for record in caplog.records if record.name == "sag.documents"]
    documents_log.removeHandler(caplog.handler)
    assert any(
        "folder_import_upload" in message
        and "outcome=accepted" in message
        and f"batch_id={batch_id}" in message
        and f"document_id={document_id}" in message
        and f"request_id={request_id}" in message
        for message in messages
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_id", ["not-a-uuid", ""])
async def test_folder_import_upload_rejects_malformed_batch_before_document_persistence(batch_id):
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            registered = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": f"invalid-folder-import-{uuid.uuid4().hex}@t.com",
                    "password": "password123",
                    "name": "Invalid Folder Import Test",
                },
            )
            assert registered.status_code == 201
            headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
            source = await client.post("/api/v1/sources", headers=headers, json={"name": "无效批次"})
            assert source.status_code == 201
            source_id = source.json()["id"]
            before = await client.get(f"/api/v1/sources/{source_id}/documents", headers=headers)
            assert before.status_code == 200

            uploaded = await client.post(
                f"/api/v1/sources/{source_id}/documents",
                headers={**headers, "X-SAG-Folder-Import-Id": batch_id},
                files={"file": ("rejected.md", b"# Rejected\n\ncontent", "text/markdown")},
            )
            after = await client.get(f"/api/v1/sources/{source_id}/documents", headers=headers)

    assert uploaded.status_code == 422
    assert after.status_code == 200
    assert {document["id"] for document in after.json()} == {
        document["id"] for document in before.json()
    }


@pytest.mark.asyncio
async def test_folder_import_upload_strips_client_paths_from_logs_and_document_name(caplog):
    from sag_api.main import app

    batch_id = "018f5f7e-89ab-7def-8123-0123456789a0"
    uploaded_files = [
        ("/private/client-folder/posix.md", "posix.md"),
        ("C:\\private\\client-folder\\windows.md", "windows.md"),
    ]
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        caplog.set_level(logging.INFO, logger="sag.documents")
        documents_log = logging.getLogger("sag.documents")
        documents_log.addHandler(caplog.handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            registered = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": f"path-folder-import-{uuid.uuid4().hex}@t.com",
                    "password": "password123",
                    "name": "Folder Import Path Test",
                },
            )
            assert registered.status_code == 201
            headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
            source = await client.post("/api/v1/sources", headers=headers, json={"name": "路径批次导入"})
            assert source.status_code == 201
            source_id = source.json()["id"]

            for supplied_filename, expected_filename in uploaded_files:
                response = await client.post(
                    f"/api/v1/sources/{source_id}/documents",
                    headers={**headers, "X-SAG-Folder-Import-Id": batch_id},
                    files={"file": (supplied_filename, b"# Path\n\ncontent", "text/markdown")},
                )
                assert response.status_code == 201
                assert response.json()["filename"] == expected_filename

            listed = await client.get(f"/api/v1/sources/{source_id}/documents", headers=headers)

    assert listed.status_code == 200
    assert {document["filename"] for document in listed.json()} == {"posix.md", "windows.md"}
    messages = [record.getMessage() for record in caplog.records if record.name == "sag.documents"]
    documents_log.removeHandler(caplog.handler)
    for supplied_filename, expected_filename in uploaded_files:
        assert all(supplied_filename not in message for message in messages)
        assert any(
            "folder_import_upload" in message
            and "outcome=accepted" in message
            and f"filename='{expected_filename}'" in message
            for message in messages
        )


@pytest.mark.asyncio
async def test_uploaded_document_reaches_ready_through_background_queue():
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Document, Job, Source, User
    from sag_api.enums import JobStatus, JobType
    from sag_api.main import app
    from sag_api.sag.dto import ProcessCheckpoint, ProcessOutcome

    class OfflineDocumentEngine:
        async def process_document(self, _config_id, _path, **kwargs):
            await kwargs["on_stage"]("loading")
            checkpoint = ProcessCheckpoint(
                source_id="offline-engine-document",
                chunk_ids=["chunk-1", "chunk-2"],
                processed_chunk_ids=["chunk-1"],
                event_count=1,
                event_ids=["event-1"],
                token_usage=100,
            )
            await kwargs["on_checkpoint"](checkpoint.model_copy(deep=True))
            await kwargs["on_stage"]("extracting")
            checkpoint.processed_chunk_ids = ["chunk-1", "chunk-2"]
            checkpoint.event_count = 2
            checkpoint.event_ids = ["event-1", "event-2"]
            checkpoint.token_usage = 200
            await kwargs["on_checkpoint"](checkpoint.model_copy(deep=True))
            return ProcessOutcome(
                source_id=checkpoint.source_id,
                chunk_count=2,
                event_count=2,
                chunk_ids=checkpoint.chunk_ids,
                processed_chunk_ids=checkpoint.processed_chunk_ids,
                event_ids=checkpoint.event_ids,
                token_usage=checkpoint.token_usage,
            )

        async def universe_overview_stats(self, *_args, **_kwargs):
            return {
                "event_count": 0,
                "entity_count": 0,
                "relation_count": 0,
                "time_buckets": [],
            }

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        app.state.job_queue._engine_manager = OfflineDocumentEngine()
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            registered = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": f"upload-{uuid.uuid4().hex}@t.com",
                    "password": "password123",
                    "name": "Upload Test",
                },
            )
            assert registered.status_code == 201
            user_id = registered.json()["user"]["id"]
            headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
            created = await client.post(
                "/api/v1/sources",
                headers=headers,
                json={"name": "上传主流程"},
            )
            assert created.status_code == 201
            source_id = created.json()["id"]

            uploaded = await client.post(
                f"/api/v1/sources/{source_id}/documents",
                headers=headers,
                files={"file": ("flow.md", b"# Flow\n\ncontent", "text/markdown")},
            )
            assert uploaded.status_code == 201
            assert uploaded.json()["status"] == "pending"
            document_id = uploaded.json()["id"]

            for _ in range(200):
                response = await client.get(
                    f"/api/v1/sources/{source_id}/documents/{document_id}",
                    headers=headers,
                )
                assert response.status_code == 200
                if response.json()["status"] == "ready":
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("uploaded document did not reach ready")

            body = response.json()
            assert body["progress"] == 100
            assert body["chunk_count"] == 2
            assert body["event_count"] == 2
            assert body["token_usage"] == 200

            for _ in range(200):
                async with SessionLocal() as session:
                    document = await session.get(Document, document_id)
                    source = await session.get(Source, source_id)
                    job = await session.scalar(
                        select(Job).where(
                            Job.document_id == document_id,
                            Job.type == JobType.PROCESS_DOCUMENT,
                        )
                    )
                    if job.status == JobStatus.SUCCEEDED:
                        break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("document job did not reach succeeded")

            assert document.sag_source_id == "offline-engine-document"
            assert source.document_count == 1
            assert source.chunk_count == 2
            assert source.event_count == 2

            for _ in range(200):
                async with SessionLocal() as session:
                    active_universe_jobs = list(
                        (
                            await session.scalars(
                                select(Job).where(
                                    Job.type == JobType.INDEX_UNIVERSE,
                                    Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                                )
                            )
                        ).all()
                    )
                if not active_universe_jobs:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("follow-up universe jobs did not finish")

            async with SessionLocal() as session:
                source = await session.get(Source, source_id)
                user = await session.get(User, user_id)
                await session.delete(source)
                await session.delete(user)
                await session.commit()
