"""Durable public deletion binding and completion before business file cleanup."""

from uuid import uuid4

import pytest
from sqlalchemy import delete
from zleap.sag.operations import DeleteSourceRequest

from sag_api.core.db import SessionLocal, init_db
from sag_api.core.errors import ApiError
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs.tasks import _delete_document_task_unlocked
from tests.document_requests import deletion_receipt, document_request
from tests.test_document_durable_processing import admitted_document_task as admitted_document_task


@pytest.fixture
async def deleting_document(tmp_path):
    await init_db()
    published = tmp_path / "published.md"
    published.write_text("published body", encoding="utf-8")
    directory = tmp_path / "candidate"
    directory.mkdir()
    candidate = directory / "candidate.md"
    candidate.write_text("candidate body", encoding="utf-8")
    async with SessionLocal() as session:
        source = Source(name="public-delete", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="published.md",
            storage_path=str(published),
            sag_source_id="published-input",
            status=DocumentStatus.DELETING,
        )
        session.add(document)
        await session.flush()
        request = document_request(source.sag_source_config_id, source_id="candidate-input")
        request = request.model_copy(update={"context": request.context.model_copy(update={"fence_token": 7})})
        session.add(
            Job(
                source_id=source.id,
                document_id=document.id,
                type=JobType.PROCESS_DOCUMENT,
                status=JobStatus.PAUSED,
                payload={
                    "process_request": request.model_dump(mode="json"),
                    "input_artifact": {"path": str(candidate), "directory": str(directory)},
                },
            )
        )
        job = Job(source_id=source.id, document_id=document.id, type=JobType.DELETE_DOCUMENT, status=JobStatus.RUNNING)
        session.add(job)
        await session.commit()
        source_id, document_id, job_id, range_id = source.id, document.id, job.id, source.sag_source_config_id
    try:
        yield source_id, document_id, job_id, range_id, published, candidate
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Source).where(Source.id == source_id))
            await session.commit()


@pytest.mark.asyncio
async def test_delete_binds_every_public_request_before_calls_and_replays_after_partial_failure(deleting_document):
    _source_id, document_id, job_id, range_id, published, candidate = deleting_document
    calls, visible_bindings = [], []
    fail_once = True

    class Engine:
        async def delete_data(self, request, *, source=None):
            calls.append(request)
            async with SessionLocal() as reader:
                visible_bindings.append(dict((await reader.get(Job, job_id)).payload.get("delete_requests") or {}))
            if request.source_id == "published-input" and fail_once:
                return deletion_receipt(request, status="failed", checkpoint="delete_hidden", retryable=True)
            return deletion_receipt(request)

    engine = Engine()
    caught = None
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        try:
            await _delete_document_task_unlocked(session, job, engine_manager=engine)
        except ApiError as error:
            caught = error
        assert caught is not None and caught.retryable is True
        assert published.is_file() and candidate.is_file()
        assert await session.get(Document, document_id) is not None
        assert all(set(binding) == {"published-input", "candidate-input"} for binding in visible_bindings)
        bound = dict(job.payload["delete_requests"])
        assert bound["candidate-input"]["context"]["fence_token"] == 8
        assert bound["published-input"]["context"]["fence_token"] == 1
        fail_once = False
        await _delete_document_task_unlocked(session, job, engine_manager=engine)
        assert await session.get(Document, document_id) is None
        assert job.payload["delete_requests"] == bound
    assert not published.exists() and not candidate.exists()
    assert [call.model_dump(mode="json") for call in calls[:2]] == [call.model_dump(mode="json") for call in calls[2:]]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["running", "permanent_failure", "wrong_identity"])
async def test_delete_keeps_files_until_every_receipt_is_qualified(deleting_document, outcome):
    _source_id, document_id, job_id, range_id, published, candidate = deleting_document

    class Engine:
        async def delete_data(self, request, *, source=None):
            changes = {
                "running": {"status": "running", "checkpoint": "delete_hidden"},
                "permanent_failure": {"status": "failed", "retryable": False},
                "wrong_identity": {"source_id": "different-input"},
            }
            return deletion_receipt(request, **changes[outcome])

    caught = None
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        try:
            await _delete_document_task_unlocked(session, job, engine_manager=Engine())
        except ApiError as error:
            caught = error
        assert caught is not None
        if outcome == "permanent_failure":
            assert caught.retryable is False
        assert published.is_file() and candidate.is_file()
        document = await session.get(Document, document_id)
        assert document is not None and document.status is DocumentStatus.DELETING


@pytest.mark.parametrize("status", [JobStatus.PAUSED, JobStatus.FAILED])
async def test_document_delete_management_resumes_the_same_bound_cleanup(deleting_document, status):
    from sqlalchemy import func, select

    from sag_api.api.v1.documents import list_
    from sag_api.core.errors import NotFoundError
    from sag_api.jobs.scheduling import MANUAL_RECOVERY, set_scheduler
    from sag_api.services.document_service import delete_document, get_public_document

    source_id, document_id, job_id, _range_id, published, candidate = deleting_document
    dispatches = []

    class Queue:
        def begin_source_maintenance(self, _source_id, _job_id):
            pass

        async def enqueue(self, job_id):
            dispatches.append(job_id)

    class FailedDeletion:
        async def delete_data(self, request, *, source=None):
            return deletion_receipt(request, status="failed", retryable=False)

    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        with pytest.raises(ApiError):
            await _delete_document_task_unlocked(session, job, engine_manager=FailedDeletion())
        bound = dict(job.payload["delete_requests"])
        job.status = status
        job.payload = set_scheduler(job.payload, blocked_reason=MANUAL_RECOVERY)
        document = await session.get(Document, document_id)
        document.status = DocumentStatus.DELETE_FAILED if status == JobStatus.FAILED else DocumentStatus.DELETING
        await session.commit()
        source = await session.get(Source, source_id)
        with pytest.raises(NotFoundError):
            await get_public_document(session, source, document_id)
        listing = await list_(source_id, session=session)
        assert [item.id for item in listing] == [document_id], "cleanup metadata disappeared from management"
        assert listing[0].deletion_job.id == job_id and listing[0].deletion_job.status == status
        assert not listing[0].has_publication
        resumed = await delete_document(session, source, document_id, job_queue=Queue())
        assert resumed.id == job_id and resumed.status == JobStatus.QUEUED
        assert resumed.payload["delete_requests"] == bound
        assert resumed.payload["_scheduler"].get("blocked_reason") is None
        repeated = await delete_document(session, source, document_id, job_queue=Queue())
        assert repeated.id == job_id and repeated.payload["delete_requests"] == bound
        assert await session.scalar(
            select(func.count(Job.id)).where(Job.type == JobType.DELETE_DOCUMENT, Job.document_id == document_id)
        ) == 1
        assert published.is_file() and candidate.is_file()
    assert dispatches == [job_id, job_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_response", [False, True])
async def test_actual_public_delete_preserves_peer_and_shared_entity(
    admitted_document_task, monkeypatch, lost_response
):
    from sqlalchemy import select
    from zleap.sag import DataEngine
    from zleap.sag.db.models import Entity, EventEntity, SourceEvent
    from zleap.sag.pipeline import SearchOptions, SearchRequest, SearchScope

    from sag_api.jobs import tasks
    from sag_api.services.document_service import delete_document

    session, manager, _stock, models, source, document_a, job_a, raw_a = admitted_document_task
    await tasks.process_document(session, job_a, engine_manager=manager)
    await session.refresh(document_a)
    assert document_a.status is DocumentStatus.READY
    input_a = document_a.sag_source_id
    raw_b = raw_a.parent / "peer.md"
    raw_b.write_text("# Peer project\nAcme Corporation completed another migration.", encoding="utf-8")
    document_b = Document(source_id=source.id, filename=raw_b.name, storage_path=str(raw_b))
    session.add(document_b)
    await session.flush()
    job_b = Job(
        type=JobType.PROCESS_DOCUMENT,
        source_id=source.id,
        document_id=document_b.id,
        status=JobStatus.RUNNING,
        payload={"control_revision": 1},
    )
    session.add(job_b)
    await session.commit()
    await tasks.process_document(session, job_b, engine_manager=manager)
    await session.refresh(document_b)
    assert document_b.status is DocumentStatus.READY
    input_b = document_b.sag_source_id
    engine_sessions = await manager.get_sag_session_factory(source.sag_source_config_id)
    async with engine_sessions() as engine_session:
        shared = await engine_session.scalar(select(Entity.id).where(Entity.name == "Acme Corporation"))
        linked_inputs = set(
            await engine_session.scalars(
                select(SourceEvent.source_id)
                .join(EventEntity, EventEntity.event_id == SourceEvent.id)
                .where(EventEntity.entity_id == shared)
            )
        )
    assert shared is not None and {input_a, input_b} <= linked_inputs
    cleanup = await delete_document(session, source, document_a.id)
    calls = []
    bindings = []
    lose_once = lost_response
    original_delete = DataEngine.delete_source

    async def public_delete(engine, request):
        nonlocal lose_once
        async with SessionLocal() as reader:
            bound = (await reader.get(Job, cleanup.id)).payload["delete_requests"]
            calls.append(request)
            bindings.append((bound[request.source_id], raw_a.is_file(), raw_b.is_file()))
        result = await original_delete(engine, request)
        if lose_once:
            lose_once = False
            raise RuntimeError("delete response lost after engine completion")
        return result

    monkeypatch.setattr(DataEngine, "delete_source", public_delete)
    model_calls = len(models.calls)
    if lost_response:
        with pytest.raises(RuntimeError, match="delete response lost"):
            await tasks.delete_document_task(session, cleanup, engine_manager=manager)
        assert raw_a.is_file() and raw_b.is_file()
        assert await session.get(Document, document_a.id) is not None
        request = DeleteSourceRequest.model_validate(cleanup.payload["delete_requests"][input_a])
        async with manager.use_concurrently(source.sag_source_config_id, source) as engine:
            receipt = await engine.get_operation_status(request.context.operation_id)
        assert receipt.status == "succeeded" and receipt.checkpoint == "ready"
    await tasks.delete_document_task(session, cleanup, engine_manager=manager)
    assert len(models.calls) == model_calls
    assert [request.source_id for request in calls] == [input_a] * (2 if lost_response else 1)
    assert bindings == [(calls[0].model_dump(mode="json"), True, True)] * len(calls)
    assert calls[0].context.fence_token == 2
    assert not raw_a.exists() and raw_b.is_file()
    assert await session.get(Document, document_a.id) is None
    await session.refresh(document_b)
    assert document_b.sag_source_id == input_b and document_b.status is DocumentStatus.READY
    assert source.chunk_count == document_b.chunk_count and source.event_count == document_b.event_count
    async with engine_sessions() as engine_session:
        assert await engine_session.get(Entity, shared) is not None
        surviving = set(await engine_session.scalars(select(SourceEvent.source_id)))
        assert input_a not in surviving and input_b in surviving
    monkeypatch.setattr(DataEngine, "delete_source", original_delete)
    request = DeleteSourceRequest.model_validate(cleanup.payload["delete_requests"][input_a])
    replay = await manager.delete_data(request, source=source)
    assert replay.status == "succeeded" and replay.checkpoint == "ready"
    assert len(models.calls) == model_calls
    async with manager.use_concurrently(source.sag_source_config_id, source) as engine:
        for input_id in (input_a, input_b):
            result = await engine.search(
                SearchRequest(
                    query="Acme",
                    scope=SearchScope(data_source_ids=(source.sag_source_config_id,), source_ids=(input_id,)),
                    options=SearchOptions(strategy="vector", return_type="chunk", top_k=10),
                )
            )
            assert bool(result.chunks) is (input_id == input_b)
