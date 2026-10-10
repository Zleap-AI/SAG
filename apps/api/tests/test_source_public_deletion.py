"""Source deletion is a durable application intent before engine or file cleanup."""

from uuid import uuid4

import pytest
from sqlalchemy import delete

from sag_api.core.db import SessionLocal, init_db
from sag_api.core.errors import ApiError
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType, SourceStatus
from sag_api.jobs.inproc import InProcessAsyncQueue
from sag_api.services.source_service import delete_source
from tests.document_requests import deletion_receipt
from tests.test_document_durable_processing import admitted_document_task as admitted_document_task


class RecordingQueue:
    def __init__(self):
        self.dispatched = []
        self.maintenance = []

    async def enqueue(self, job_id):
        self.dispatched.append(job_id)

    def begin_source_maintenance(self, source_id, job_id):
        self.maintenance.append((source_id, job_id))


class SourceDeletionEngine:
    def __init__(self, *, outcome="success"):
        self.calls = []
        self.outcome = outcome

    async def delete_data(self, request, *, source=None):
        self.calls.append(("delete", request))
        updates = {"status": "failed", "retryable": False} if self.outcome == "failure" else {}
        return deletion_receipt(request, **updates)

    async def release(self, range_id):
        self.calls.append(("release", range_id))

    async def begin_document_maintenance(self, range_id, *, source=None):
        self.calls.append(("begin", range_id))

    async def end_document_maintenance(self, range_id, *, source=None):
        self.calls.append(("end", range_id))


@pytest.fixture
async def deleting_source(tmp_path):
    await init_db()
    async with SessionLocal() as session:
        source = Source(name="source-delete-controller", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.flush()
        root = tmp_path / source.id
        root.mkdir()
        raw = root / "published.md"
        raw.write_text("published body", encoding="utf-8")
        document = Document(
            source_id=source.id, filename=raw.name, storage_path=str(raw),
            status=DocumentStatus.READY, sag_source_id="published-input", control_revision=1,
        )
        session.add(document)
        await session.flush()
        processing = Job(
            type=JobType.PROCESS_DOCUMENT, source_id=source.id, document_id=document.id,
            status=JobStatus.QUEUED, payload={"control_revision": 1},
        )
        session.add(processing)
        await session.commit()
        ids = source.id, document.id, processing.id, source.sag_source_config_id
    try:
        yield *ids, raw, tmp_path
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Source).where(Source.id == ids[0]))
            await session.execute(delete(Job).where(Job.payload["target_source_id"].as_string() == ids[0]))
            await session.commit()


async def test_source_delete_persists_intent_and_hides_before_cleanup(deleting_source):
    source_id, document_id, processing_id, _range_id, raw, upload_dir = deleting_source
    dispatched = []

    class Queue:
        async def enqueue(self, job_id):
            dispatched.append(job_id)

    async with SessionLocal() as session:
        result = await delete_source(
            session, source_id, upload_dir=str(upload_dir), job_queue=Queue(),
        )
    async with SessionLocal() as reader:
        remaining = await reader.get(Source, source_id)
        assert remaining is not None, "Source disappeared before the public engine deletion receipt"
        assert remaining.status == "deleting"
        document = await reader.get(Document, document_id)
        assert not document.has_publication and document.control_revision == 2
        processing = await reader.get(Job, processing_id)
        assert processing.status is JobStatus.PAUSED and processing.payload["pause_requested"]
        assert result.type == "delete_source" and result.status is JobStatus.QUEUED
        assert result.payload["target_source_id"] == source_id
        assert dispatched == [result.id]
        assert raw.is_file()


async def test_source_delete_cancels_transfer_and_binds_its_unactivated_range(deleting_source):
    from sag_api.db.models import OctxTransfer
    from sag_api.enums import OctxTransferDirection, OctxTransferStatus

    source_id, _document_id, _processing_id, range_id, _raw, upload_dir = deleting_source
    shadow_range = uuid4().hex
    async with SessionLocal() as session:
        transfer = OctxTransfer(
            direction=OctxTransferDirection.IMPORT, status=OctxTransferStatus.IMPORTING,
            target_source_id=source_id, checkpoint={"source_config_id": shadow_range},
        )
        session.add(transfer)
        await session.commit()
        job = await delete_source(session, source_id, upload_dir=str(upload_dir), job_queue=RecordingQueue())
        await session.refresh(transfer)
        assert transfer.cancellation_requested
        assert set(job.payload["delete_requests"]) == {range_id, shadow_range}


@pytest.mark.parametrize("status", [SourceStatus.DELETING, SourceStatus.DELETE_FAILED])
async def test_octx_admission_rejects_a_deleting_target_after_transfer_check(deleting_source, status):
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import OctxTransfer
    from sag_api.enums import OctxTransferDirection, OctxTransferStatus
    from sag_api.services.octx_transfer_common import _ensure_transfer_active

    source_id = deleting_source[0]
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        source.status = status
        transfer = OctxTransfer(
            direction=OctxTransferDirection.IMPORT, status=OctxTransferStatus.IMPORTING, target_source_id=source_id,
        )
        session.add(transfer)
        await session.commit()
        with pytest.raises(ConflictError):
            await _ensure_transfer_active(session, transfer, stage="before_atomic_switch")


@pytest.mark.parametrize("already_cancelled", [False, True])
async def test_retryable_octx_error_cannot_reopen_a_cancelled_transfer(deleting_source, already_cancelled):
    from sag_api.db.models import OctxTransfer
    from sag_api.enums import OctxTransferDirection, OctxTransferStatus
    from sag_api.jobs.octx_tasks import _record_transfer_error

    class RetryableError(RuntimeError):
        retryable = True

    async with SessionLocal() as session:
        transfer = OctxTransfer(
            direction=OctxTransferDirection.IMPORT,
            status=OctxTransferStatus.CANCELLED if already_cancelled else OctxTransferStatus.IMPORTING,
            cancellation_requested=not already_cancelled, target_source_id=deleting_source[0],
        )
        session.add(transfer)
        await session.commit()
        await _record_transfer_error(
            session, transfer.id, RetryableError("injected I/O failure"), retry_status=OctxTransferStatus.QUEUED,
        )
        await session.refresh(transfer)
        assert transfer.status is OctxTransferStatus.CANCELLED
        assert transfer.error["retryable"] is False


@pytest.mark.parametrize("target", ["available", "deleting", "missing", "new"])
async def test_atomic_octx_target_admission_keeps_bound_identity(deleting_source, target):
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import OctxTransfer
    from sag_api.enums import OctxTransferDirection, OctxTransferStatus
    from sag_api.services.octx_transfer_common import _claim_import_target

    source_id = deleting_source[0]
    async with SessionLocal() as session:
        transfer = OctxTransfer(
            direction=OctxTransferDirection.IMPORT, status=OctxTransferStatus.SWITCHING,
            target_source_id=None if target == "new" else source_id,
        )
        session.add(transfer)
        await session.commit()
        if target in {"deleting", "missing"}:
            async with SessionLocal() as writer:
                source = await writer.get(Source, source_id)
                if target == "missing":
                    await writer.delete(source)
                else:
                    source.status = SourceStatus.DELETING
                await writer.commit()
            with pytest.raises(ConflictError):
                await _claim_import_target(session, transfer)
        else:
            claimed = await _claim_import_target(session, transfer)
            assert (claimed.id if claimed else None) == (None if target == "new" else source_id)


@pytest.mark.parametrize("target", ["document", "source"])
async def test_committed_cleanup_wins_late_lease_exit_error(deleting_source, monkeypatch, target):
    from contextlib import asynccontextmanager

    from sag_api.core.errors import ConflictError
    from sag_api.jobs import tasks
    from sag_api.services.document_service import delete_document

    @asynccontextmanager
    async def late_loss(*args, **kwargs):
        yield
        raise ConflictError("injected lease loss after committed cleanup", retryable=False)

    monkeypatch.setattr(tasks, "acquire_source_exclusive_lease", late_loss)
    source_id, document_id, _processing_id, _range_id, raw, upload_dir = deleting_source
    async with SessionLocal() as session:
        if target == "source":
            job = await delete_source(session, source_id, upload_dir=str(upload_dir), job_queue=RecordingQueue())
        else:
            job = await delete_document(
                session, await session.get(Source, source_id), document_id, job_queue=RecordingQueue(),
            )
        job_id = job.id
    engine = SourceDeletionEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine)
    queue.begin_source_maintenance(source_id, job_id)
    queue._maintenance.ready.add(source_id)
    await queue._run_job(job_id)
    async with SessionLocal() as reader:
        job = await reader.get(Job, job_id)
        assert job.status is JobStatus.SUCCEEDED, job.error
        assert await reader.get(Document, document_id) is None
    assert not raw.exists()
    assert not queue._maintenance.jobs and not queue._maintenance.ready


@pytest.mark.parametrize("outcome", ["success", "running", "permanent_failure", "wrong_identity", "lost_response"])
async def test_source_cleanup_requires_qualified_receipt_and_preserves_bound_request(deleting_source, outcome):
    from sag_api.jobs import tasks

    cleanup = tasks._delete_source_task_unlocked
    source_id, _document_id, _processing_id, range_id, raw, upload_dir = deleting_source
    queue = RecordingQueue()
    calls, visible_requests, released = [], [], []
    fail_once = True

    class Engine:
        async def delete_data(self, request, *, source=None):
            calls.append(request)
            async with SessionLocal() as reader:
                visible = await reader.get(Job, job_id)
                visible_requests.append(dict(visible.payload["delete_requests"]))
            if outcome == "lost_response" and fail_once:
                raise RuntimeError("injected lost range-delete response")
            updates = {
                "running": {"status": "running", "checkpoint": "delete_hidden"},
                "permanent_failure": {"status": "failed", "retryable": False},
                "wrong_identity": {"data_source_id": "another-range"},
            }
            return deletion_receipt(request, **updates.get(outcome, {}))

        async def release(self, range_id):
            released.append(range_id)

    async with SessionLocal() as session:
        job = await delete_source(session, source_id, upload_dir=str(upload_dir), job_queue=queue)
        job_id, bound = job.id, dict(job.payload["delete_requests"])
        assert bound[range_id]["context"]["fence_token"] == 1
        repeated = await delete_source(session, source_id, upload_dir=str(upload_dir), job_queue=queue)
        assert repeated.id == job_id and repeated.payload["delete_requests"] == bound
        error = None
        try:
            await cleanup(session, job, engine_manager=Engine())
        except (ApiError, RuntimeError) as caught:
            error = caught
        assert visible_requests == [bound]
        assert len(calls) == 1
        if outcome != "success":
            assert error is not None
            assert raw.is_file() and await session.get(Source, source_id) is not None
            assert released == []
            if outcome == "permanent_failure":
                assert error.retryable is False
            if outcome != "lost_response":
                return
            fail_once = False
            await cleanup(session, job, engine_manager=Engine())
            assert calls[0] == calls[1]
        else:
            assert error is None
        assert not raw.exists() and await session.get(Source, source_id) is None
        assert job.source_id is None
        assert await session.get(Job, job_id) is job
        assert job.payload["delete_requests"] == bound
        assert released == [range_id]


@pytest.mark.parametrize("status", [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED])
async def test_unfinished_source_deletion_waits_for_manual_action_after_restart(deleting_source, status):
    source_id, _document_id, _processing_id, _range_id, raw, upload_dir = deleting_source
    async with SessionLocal() as session:
        job = await delete_source(session, source_id, upload_dir=str(upload_dir), job_queue=RecordingQueue())
        job_id, bound = job.id, dict(job.payload["delete_requests"])
        job.status = status
        await session.commit()
    engine = SourceDeletionEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine)
    try:
        await queue._recover()
        await queue.enqueue(job_id)
        await queue._run_job(job_id)
        async with SessionLocal() as reader:
            paused = await reader.get(Job, job_id)
            assert paused.status is JobStatus.PAUSED
            assert paused.payload["_scheduler"]["blocked_reason"] == "manual_recovery"
            assert paused.payload["delete_requests"] == bound
            assert await reader.get(Source, source_id) is not None
        assert raw.is_file() and engine.calls == []
        assert not queue._maintenance.jobs
    finally:
        await queue.stop()


async def test_source_delete_failure_stays_hidden_and_manual_retry_keeps_the_request(deleting_source):
    source_id, _document_id, _processing_id, _range_id, raw, upload_dir = deleting_source
    async with SessionLocal() as session:
        job = await delete_source(session, source_id, upload_dir=str(upload_dir), job_queue=RecordingQueue())
        job_id, bound = job.id, dict(job.payload["delete_requests"])
    engine = SourceDeletionEngine(outcome="failure")
    queue = InProcessAsyncQueue(SessionLocal, engine)
    await queue._run_job(job_id)
    async with SessionLocal() as reader:
        failed_source = await reader.get(Source, source_id)
        failed_job = await reader.get(Job, job_id)
        assert failed_source.status is SourceStatus.DELETE_FAILED
        assert failed_job.status is JobStatus.FAILED
        assert raw.is_file()
        repeated = await delete_source(reader, source_id, upload_dir=str(upload_dir), job_queue=RecordingQueue())
        assert repeated.id == job_id and repeated.payload["delete_requests"] == bound
    engine.outcome = "success"
    await queue._run_job(job_id)
    async with SessionLocal() as reader:
        completed = await reader.get(Job, job_id)
        assert completed.status is JobStatus.SUCCEEDED
        assert completed.source_id is None
        assert await reader.get(Source, source_id) is None
        assert not raw.exists()


async def test_deleting_source_is_excluded_even_if_a_stale_document_has_a_publication(deleting_source):
    from sag_api.core.errors import NotFoundError
    from sag_api.services.document_service import get_public_document
    from sag_api.services.retrieval_service import published_document_inputs
    from sag_api.services.source_service import search_source_candidates

    source_id, document_id, _processing_id, range_id, _raw, upload_dir = deleting_source
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        await delete_source(session, source_id, upload_dir=str(upload_dir), job_queue=RecordingQueue())
        document = await session.get(Document, document_id)
        document.status = DocumentStatus.READY
        await session.commit()
        assert document.has_publication
        assert await search_source_candidates(session, [source_id]) == []
        assert (await published_document_inputs([source]))[range_id] == ()
        with pytest.raises(NotFoundError):
            await get_public_document(session, source, document_id)


async def test_upload_to_deleting_source_is_rejected_before_writing_files(deleting_source):
    from sag_api.core.errors import ConflictError
    from sag_api.services.document_service import create_document_from_upload

    source_id, _document_id, _processing_id, _range_id, raw, upload_dir = deleting_source
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        await delete_source(session, source_id, upload_dir=str(upload_dir), job_queue=RecordingQueue())
        with pytest.raises(ConflictError):
            await create_document_from_upload(
                session, source, filename="late.md", content_type="text/markdown", data=b"late upload",
                upload_dir=str(upload_dir), job_queue=RecordingQueue(),
            )
    assert sorted(raw.parent.iterdir()) == [raw]


@pytest.mark.parametrize("state", ["active", "deleting", "delete_failed", "deleting_during_read"])
async def test_retained_chunk_respects_source_deletion_before_and_after_read(deleting_source, state):
    from datetime import UTC, datetime, timedelta

    from sag_api.api.v1.sources import get_chunk
    from sag_api.core.errors import NotFoundError
    from sag_api.db.models import OctxAsset, OctxInstallation, OctxRelease
    from sag_api.enums import OctxAssetOwnership, OctxInstallationStatus, OctxReleaseOrigin
    from sag_api.sag.dto import ChunkInfo

    source_id, _document_id, _processing_id, range_id, *_ = deleting_source
    retained_range = uuid4().hex
    calls = []

    class Reader:
        async def get_chunk(self, config_id, chunk_id, *, source=None, source_ids=None):
            calls.append(config_id)
            if config_id == range_id:
                return None
            if state == "deleting_during_read":
                async with SessionLocal() as writer:
                    saved = await writer.get(Source, source_id)
                    saved.status = SourceStatus.DELETING
                    await writer.commit()
            return ChunkInfo(chunk_id=chunk_id, content="retained original")

    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        source.status = SourceStatus.ACTIVE if state.endswith("during_read") else SourceStatus(state)
        asset = OctxAsset(id=str(uuid4()), name="retained", ownership=OctxAssetOwnership.IMPORTED)
        session.add(asset)
        await session.flush()
        release = OctxRelease(
            asset_id=asset.id, version="1", package_digest="sha256:" + "a" * 64,
            manifest={}, artifact_key="unused.octx", created_by=OctxReleaseOrigin.IMPORT,
        )
        session.add(release)
        await session.flush()
        installation = OctxInstallation(
            source_id=source_id, release_id=release.id, sag_source_config_id=retained_range,
            id_namespace=str(uuid4()), status=OctxInstallationStatus.RETAINED,
            retain_until=datetime.now(UTC) + timedelta(days=1),
        )
        session.add(installation)
        await session.commit()
        try:
            if state == "active":
                chunk = await get_chunk(source_id, "old-chunk", session=session, engine_manager=Reader())
                assert chunk["content"] == "retained original" and chunk["source_id"] == source_id
                assert calls == [range_id, retained_range]
            else:
                with pytest.raises(NotFoundError):
                    await get_chunk(source_id, "old-chunk", session=session, engine_manager=Reader())
                assert calls == ([range_id, retained_range] if state.endswith("during_read") else [])
        finally:
            await session.delete(installation)
            await session.flush()
            await session.delete(release)
            await session.flush()
            await session.delete(asset)
            await session.commit()


async def test_upload_binds_the_initialized_document_control_revision(deleting_source):
    from sag_api.services.document_service import create_document_from_upload

    source_id, _document_id, _processing_id, _range_id, _raw, upload_dir = deleting_source
    async with SessionLocal() as session:
        document, job = await create_document_from_upload(
            session, await session.get(Source, source_id), filename="new.md", content_type="text/markdown",
            data=b"# Uploaded content", upload_dir=str(upload_dir), job_queue=RecordingQueue(),
        )
        assert job.payload["control_revision"] == document.control_revision == 1


@pytest.mark.parametrize("lost_response", [False, True])
async def test_actual_source_delete_removes_owned_inputs_and_preserves_another_range(
    admitted_document_task, monkeypatch, lost_response,
):
    from sqlalchemy import select
    from zleap.sag import DataEngine
    from zleap.sag.db.models import SourceEvent
    from zleap.sag.pipeline import SearchOptions, SearchRequest, SearchScope

    from sag_api.jobs import tasks

    session, manager, _stock, models, source_a, document_a, job_a, raw_a = admitted_document_task
    await tasks.process_document(session, job_a, engine_manager=manager)
    await session.refresh(document_a)
    input_a = document_a.sag_source_id
    source_a_id, range_a = source_a.id, source_a.sag_source_config_id
    source_b = Source(name="actual-delete-peer", sag_source_config_id=uuid4().hex)
    session.add(source_b)
    await session.flush()
    raw_b = raw_a.parent / "other-range.md"
    raw_b.write_text("# Peer project\nAcme Corporation completed the other project.", encoding="utf-8")
    document_b = Document(source_id=source_b.id, filename=raw_b.name, storage_path=str(raw_b))
    session.add(document_b)
    await session.flush()
    job_b = Job(
        type=JobType.PROCESS_DOCUMENT, source_id=source_b.id, document_id=document_b.id,
        status=JobStatus.RUNNING, payload={"control_revision": 1},
    )
    session.add(job_b)
    await session.commit()
    source_b_id = source_b.id
    try:
        await tasks.process_document(session, job_b, engine_manager=manager)
        await session.refresh(document_b)
        input_b = document_b.sag_source_id
        engine_sessions = await manager.get_sag_session_factory(source_b.sag_source_config_id)
        calls, visible_bindings = [], []
        original_delete = DataEngine.delete_data_source
        fail_once = lost_response

        async def observed_delete(self, request):
            calls.append(request)
            async with SessionLocal() as reader:
                visible = await reader.get(Job, deleting_job_id)
                visible_bindings.append((dict(visible.payload["delete_requests"]), raw_a.is_file()))
            result = await original_delete(self, request)
            if fail_once:
                raise RuntimeError("injected response loss after public ready")
            return result

        monkeypatch.setattr(DataEngine, "delete_data_source", observed_delete)
        deleting_job = await delete_source(
            session, source_a_id, upload_dir=str(raw_a.parent), job_queue=RecordingQueue(),
        )
        deleting_job_id, bound = deleting_job.id, dict(deleting_job.payload["delete_requests"])
        models_before = list(models.calls)
        if lost_response:
            with pytest.raises(RuntimeError):
                await tasks._delete_source_task_unlocked(session, deleting_job, engine_manager=manager)
            assert raw_a.is_file() and await session.get(Source, source_a_id) is not None
            async with manager.use_concurrently(range_a, source_a) as engine:
                ready = await engine.get_operation_status(bound[range_a]["context"]["operation_id"])
            assert ready.status == "succeeded" and ready.checkpoint == "ready"
            fail_once = False
        await tasks._delete_source_task_unlocked(session, deleting_job, engine_manager=manager)
        assert all(binding == (bound, True) for binding in visible_bindings)
        assert all(request.model_dump(mode="json") == bound[range_a] for request in calls)
        assert await session.get(Source, source_a_id) is None
        assert deleting_job.source_id is None and deleting_job.payload["delete_requests"] == bound
        assert not raw_a.exists() and raw_b.is_file()
        assert models.calls == models_before
        await session.refresh(document_b)
        assert document_b.sag_source_id == input_b and document_b.status is DocumentStatus.READY
        async with engine_sessions() as engine_session:
            visible_inputs = set(await engine_session.scalars(select(SourceEvent.source_id)))
            assert input_a not in visible_inputs and input_b in visible_inputs
        async with manager.use_concurrently(source_b.sag_source_config_id, source_b) as engine:
            result = await engine.search(SearchRequest(
                query="Acme",
                scope=SearchScope(data_source_ids=(source_b.sag_source_config_id,), source_ids=(input_b,)),
                options=SearchOptions(strategy="vector", return_type="chunk", top_k=3),
            ))
        assert result.chunks
    finally:
        await manager.release(source_b.sag_source_config_id)
        await session.rollback()
        await session.execute(delete(Source).where(Source.id == source_b_id))
        await session.commit()
