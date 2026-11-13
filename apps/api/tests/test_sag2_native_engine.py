"""Real SQLite lifecycle tests: no LLM, embedding, or JEV needed for native indexing."""

import os
from pathlib import Path

import pytest
from zleap.sag.operations import DeleteSourceRequest, OperationContext
from zleap.sag.operations import OperationContext as Sag2Context

from sag_api.core.config import Settings
from sag_api.sag.incremental_processor import IncrementalDocumentProcessor
from sag_api.sag.sag2_source_engine import Sag2SourceEngine


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["sqlite", "postgres"])
async def test_native_index_replay_read_and_delete_without_models(tmp_path, provider):
    if provider == "postgres" and not os.getenv("SAG_NATIVE_POSTGRES_PORT"):
        pytest.skip("requires dedicated native PostgreSQL test container")
    settings = Settings(
        _env_file=None, data_dir=str(tmp_path), jev_endpoint=None, jev_api_key=None,
        sag_relational_provider=provider, sag_pg_host="127.0.0.1",
        sag_pg_port=int(os.getenv("SAG_NATIVE_POSTGRES_PORT", "5432")),
        sag_pg_user="postgres", sag_pg_password="pr253-disposable", sag_pg_database="sag_native",
    )
    engine = Sag2SourceEngine(settings, "src_native")
    await engine.start()
    try:
        path = tmp_path / "manual.md"
        path.write_text("# 星港设备维护\nZX-253 每隔14天校准，负责人林澈。")
        processor = IncrementalDocumentProcessor(engine, "src_native", max_concurrency=1, language="zh")
        request = await processor.build_request(
            path,
            context=OperationContext(
                operation_id="op-native",
                idempotency_key="op-native",
                request_digest="0" * 64,
                fence_scope="src_native",
                fence_token=1,
                owner_id="test",
                isolation="input",
            ),
            input_id="doc-native",
        )
        result = await engine.process_source(request)
        assert result.status == "succeeded", result.failure_message
        assert result.result["event_ref"]["event_ids"] == []
        assert result.result["event_ref"]["entity_ids"] == []
        chunk = result.result["chunk_ref"]
        assert chunk["chunk_ids"]
        assert (await engine.get_operation_status("op-native")).operation_id == result.operation_id
        replay = await engine.process_source(request)
        assert replay.result == result.result
        stored = await engine.read_chunk("doc-native", chunk["chunk_ids"][0], data_source_id="src_native")
        assert "ZX-253" in stored.content
        await engine.delete_source(
            DeleteSourceRequest(
                context=Sag2Context(
                    operation_id="del-native",
                    idempotency_key="del-native",
                    request_digest="0" * 64,
                    fence_scope="src_native",
                    fence_token=1,
                    owner_id="test",
                ),
                data_source_id="src_native",
                source_id="doc-native",
            )
        )
        assert await engine.read_chunk("doc-native", chunk["chunk_ids"][0], data_source_id="src_native") is None
        assert not (Path(settings.data_dir) / "sag.db").exists()
    finally:
        await engine.aclose()


@pytest.mark.asyncio
async def test_manager_provisions_native_engine_without_sag1_store(tmp_path):
    from types import SimpleNamespace

    from sag_api.sag.engine_manager import EngineManager

    settings = Settings(_env_file=None, data_dir=str(tmp_path), jev_endpoint=None, jev_api_key=None)
    manager = EngineManager(settings)
    source = SimpleNamespace(engine_kind="sag2", config={}, name="native", description="")
    try:
        await manager.provision("src_manager", source)
        assert isinstance(manager._slots["src_manager"].engine, Sag2SourceEngine)
        timeline = await manager.universe_timeline(
            "src_manager", source_ids=(), source_revision="native-revision", source=source,
        )
        assert timeline.total_events == 0
        assert timeline.bundles == []
        assert not (tmp_path / "sag.db").exists()
    finally:
        await manager.release("src_manager")


from tests.test_source_default_strategy import source_client  # noqa: E402,F401


@pytest.mark.asyncio
async def test_sag2_upload_job_publishes_zero_events_without_model_keys(source_client):  # noqa: F811
    import asyncio

    from sag_api.main import app

    created = await source_client.post("/api/v1/sources", json={"name": "Native job", "engine_kind": "sag2"})
    assert created.status_code == 201, created.text
    source = created.json()
    uploaded = await source_client.post(
        f"/api/v1/sources/{source['id']}/documents",
        files={"file": ("manual.md", b"# Native\nZX-253 calibrates every 14 days.", "text/markdown")},
    )
    assert uploaded.status_code == 201, uploaded.text
    await asyncio.wait_for(app.state.job_queue._queue.join(), 20)
    document = (await source_client.get(f"/api/v1/sources/{source['id']}/documents/{uploaded.json()['id']}")).json()
    assert document["status"] == "ready", document
    assert document["event_count"] == 0
    assert document["chunk_count"] > 0
    base = f"/api/v1/sources/{source['id']}"
    doc = f"{base}/documents/{uploaded.json()['id']}"
    for endpoint in [f"{base}/octx-exports", f"{doc}/octx-exports"]:
        blocked = await source_client.post(endpoint, json={})
        assert blocked.status_code == 422, blocked.text
        assert "SAG2" in blocked.text
    outline = await source_client.get(f"{base}/outline", params={"document_id": uploaded.json()["id"]})
    assert outline.status_code == 200, outline.text
    reprocess = await source_client.post(f"{doc}/reprocess")
    assert reprocess.status_code == 200, reprocess.text
    await asyncio.wait_for(app.state.job_queue._queue.join(), 20)
    republished = (await source_client.get(doc)).json()
    assert republished["status"] == "ready", republished
    assert republished["event_count"] == 0
    deleted = await source_client.delete(doc)
    assert deleted.status_code == 202, deleted.text
    await asyncio.wait_for(app.state.job_queue._queue.join(), 20)
    assert (await source_client.get(doc)).status_code == 404
    async def completed_job(response):
        async with asyncio.timeout(20):
            while True:
                current = (await source_client.get(f"/api/v1/jobs/{response.json()['id']}")).json()
                if current["status"] in {"succeeded", "failed"}:
                    return current
                await asyncio.sleep(0.02)
    deletion = await completed_job(deleted)
    assert deletion["status"] == "succeeded", deletion
    whole = await source_client.delete(base)
    assert whole.status_code == 202, whole.text
    await asyncio.wait_for(app.state.job_queue._queue.join(), 20)
    source_deletion = await completed_job(whole)
    assert source_deletion["status"] == "succeeded", source_deletion
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Source
    async with SessionLocal() as session:
        assert await session.get(Source, source["id"]) is None


@pytest.mark.asyncio
async def test_native_search_only_uses_published_inputs_and_maps_business_source(tmp_path):
    from sag_api.sag.sag2_native_search import Sag2NativeSearchHolder
    from tests.test_sag2_engine_integration import OverlapScorer

    settings = Settings(
        _env_file=None, data_dir=str(tmp_path), jev_endpoint="https://jev.invalid/score", jev_api_key="test"
    )
    engine = Sag2SourceEngine(settings, "cfg-native")
    await engine.start()
    refs = {}
    try:
        for input_id in ["published", "candidate"]:
            path = tmp_path / f"{input_id}.md"
            path.write_text(f"# {input_id}\nAlice 出生在北京，1990年移居上海。")
            processor = IncrementalDocumentProcessor(engine, "cfg-native", max_concurrency=1, language="zh")
            req = await processor.build_request(
                path,
                context=OperationContext(
                    operation_id=input_id,
                    idempotency_key=input_id,
                    request_digest="0" * 64,
                    fence_scope="cfg-native",
                    fence_token=1,
                    owner_id="test",
                    isolation="input",
                ),
                input_id=input_id,
            )
            result = await engine.process_source(req)
            assert result.status == "succeeded", result.failure_message
            refs[input_id] = result.result["chunk_ref"]["chunk_ids"]
        holder = Sag2NativeSearchHolder(settings, scorer_factory=lambda _cfg: OverlapScorer())
        try:
            outcome = await holder.search(
                [("cfg-native", None)],
                "Alice 出生在哪？",
                top_k=5,
                requested_sources=1,
                source_limit_applied=False,
                source_ids_by_config={"cfg-native": ("published",)},
            )
            assert outcome.sections
            assert all(section.source_id == "published" for section in outcome.sections)
            assert all(section.source_config_id == "cfg-native" for section in outcome.sections)
            assert refs["candidate"][0] not in {section.chunk_id for section in outcome.sections}
        finally:
            await holder.aclose()
    finally:
        await engine.aclose()


@pytest.mark.asyncio
async def test_native_cancel_is_retryable_and_resume_is_not_invalidated_by_another_input(tmp_path):
    from zleap.sag.pipeline import CancellationToken
    settings = Settings(_env_file=None, data_dir=str(tmp_path), jev_endpoint=None, jev_api_key=None)
    engine = Sag2SourceEngine(settings, "cancel-source")
    await engine.start()
    try:
        path = tmp_path / "pause.md"
        path.write_text("# Native pause\nEvidence for document resume.")
        processor = IncrementalDocumentProcessor(engine, "cancel-source", max_concurrency=1, language="zh")
        async def request(identifier):
            return await processor.build_request(path, context=OperationContext(
                operation_id=identifier, idempotency_key=identifier, request_digest="0"*64,
                fence_scope="cancel-source", fence_token=1, owner_id="test", isolation="input",
            ), input_id=identifier)
        cancellation = CancellationToken()
        async def observer(event):
            if event.stage == "chunk":
                cancellation.cancel()
        first = await request("paused-input")
        paused = await engine.process_source(first, observer=observer, cancellation=cancellation)
        assert paused.retryable is True
        assert paused.failure_code == "cancelled"
        assert (await engine.process_source(await request("other-input"))).status == "succeeded"
        resumed = await engine.process_source(first)
        assert resumed.status == "succeeded", resumed.failure_message
    finally:
        await engine.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["document", "source"])
async def test_native_deletion_receipts_authorize_application_cleanup(tmp_path, kind):
    from zleap.sag.operations import DeleteDataSourceRequest

    from sag_api.jobs.tasks import _require_delete_completion

    engine = Sag2SourceEngine(Settings(_env_file=None, data_dir=str(tmp_path)), "delete-source")
    await engine.start()
    try:
        document_request = DeleteSourceRequest(
            context=OperationContext(
                operation_id="delete-undispatched", idempotency_key="delete-undispatched",
                request_digest="0" * 64, fence_scope="delete-source", fence_token=1, owner_id="test",
            ), data_source_id="delete-source", source_id="never-dispatched",
        )
        if kind == "document":
            document_receipt = await engine.delete_source(document_request)
            _require_delete_completion(document_request, document_receipt)
            return
        request = DeleteDataSourceRequest(
            context=OperationContext(
                operation_id="delete-all", idempotency_key="delete-all", request_digest="0" * 64,
                fence_scope="delete-source", fence_token=1, owner_id="test",
            ), data_source_id="delete-source",
        )
        receipt = await engine.delete_data_source(request)
        _require_delete_completion(request, receipt)
    finally:
        await engine.aclose()
