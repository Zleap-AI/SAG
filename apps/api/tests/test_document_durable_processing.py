"""The document adapter binds public requests and owns no engine checkpoints."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from zleap.sag.operations import OperationContext, OperationResult, ProcessSourceRequest
from zleap.sag.pipeline import StageEvent, StageEventType, StageName

from sag_api.sag.incremental_processor import IncrementalDocumentProcessor
from tests.document_requests import UncommittedDocumentManager, controller_processing_identity, processing_parameters


class PublicEngine:
    def __init__(self):
        self.resources = SimpleNamespace(prompts=SimpleNamespace(language="en"))
        self.requests = []

    def __getattr__(self, name):
        if name.startswith("_") or name in {"ingest", "extract"}:
            raise AssertionError(f"document processing must use the public durable API: {name}")
        raise AttributeError(name)

    async def process_source(self, request, *, observer, cancellation):
        self.requests.append(request)
        await observer(
            StageEvent(
                run_id=request.context.operation_id,
                stage=StageName.EXTRACT,
                type=StageEventType.PROGRESS,
                completed=1,
                total=2,
            )
        )
        await asyncio.sleep(0)
        now = datetime.now(UTC)
        return OperationResult(
            operation_id=request.context.operation_id,
            idempotency_key=request.context.idempotency_key,
            request_digest=request.context.request_digest,
            operation_type="process_source",
            data_source_id=request.context.fence_scope,
            source_id=request.source.descriptor.source_id,
            status="succeeded",
            checkpoint="ready",
            fence_scope=request.context.fence_scope,
            fence_token=request.context.fence_token,
            generation_id="complete-generation",
            attempts=1,
            created_at=now,
            updated_at=now,
            result={},
        )


def context(data_source_id):
    identity = str(uuid4())
    return OperationContext(
        operation_id=identity,
        idempotency_key=identity,
        request_digest="a" * 64,
        fence_scope=data_source_id,
        fence_token=1,
        owner_id="test-worker",
        isolation="input",
    )


async def never_pause():
    return False


async def test_manager_factory_and_model_use_the_actual_admitted_configuration(tmp_path):
    """A fresh config reconstruction or live policy read mixes targets within one operation."""
    from sag_api.core.config import Settings
    from sag_api.core.litellm_policy import install_litellm_policy, uninstall_litellm_policy
    from sag_api.db.models import Source
    from sag_api.sag import EngineManager
    from sag_api.services.chatbot_service import SettingsView
    from tests.document_models import local_models

    path = tmp_path / "snapshot.md"
    path.write_text("# Acme\nAcme Corporation completed the database migration.")
    with local_models() as models:
        stock = Settings(
            _env_file=None,
            data_dir=str(tmp_path / "engine"),
            sag_language="zh",
            llm_provider="openai",
            llm_model="local",
            llm_api_key="local",
            llm_base_url=models.base_url,
            llm_extra_body={"marker": "old"},
            embedding_model="local",
            embedding_api_key="local",
            embedding_base_url=models.base_url,
            embedding_schema_dimensions=32,
            document_chunk_max_tokens=128,
        )
        manager = EngineManager(stock)
        source = Source(
            id=uuid4().hex,
            name="admitted snapshot",
            sag_source_config_id=uuid4().hex,
            config={"engine": {"language": "en"}},
        )
        handle = install_litellm_policy(SettingsView(stock))
        try:
            await manager.provision(source.sag_source_config_id, source)
            # A newly supplied Source must not replace the config of an existing admitted slot.
            source.config = {"engine": {"language": "zh"}}

            async def bind(processor, *runtime):
                assert len(runtime) == 2, "factory needs the actual slot config and frozen runtime settings"
                config, frozen = runtime
                assert config.language == "en"
                assert frozen is not stock and frozen.llm_extra_body == {"marker": "old"}
                stock.llm_extra_body["marker"] = "new"
                stock.document_chunk_max_tokens = 256
                assert frozen.llm_extra_body == {"marker": "old"}
                request = await processor.build_request(
                    path,
                    context=context(source.sag_source_config_id),
                    input_id=uuid4().hex,
                )
                assert request.chunk_options.max_tokens == 128
                return request.model_copy(
                    update={
                        "extraction_options": request.extraction_options.model_copy(
                            update={
                                "enable_article_summary": False,
                                "enable_parent_summary": False,
                            }
                        ),
                    }
                )

            result = await manager.process_document(source.sag_source_config_id, bind, source=source)
            assert result.status == "succeeded" and result.checkpoint == "ready"
            assert models.requests and all(body.get("marker") == "old" for body in models.requests)
        finally:
            uninstall_litellm_policy(handle)
            await manager.aclose_all()


@pytest.fixture
async def admitted_document_task(tmp_path):
    from sqlalchemy import delete

    from sag_api.core.config import Settings
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import JobStatus, JobType
    from sag_api.sag import EngineManager
    from tests.document_models import local_models

    await init_db()
    path = tmp_path / "admitted.md"
    path.write_text("# Acme\nAcme Corporation completed the database migration.")
    with local_models() as models:
        stock = Settings(
            _env_file=None,
            data_dir=str(tmp_path / "engine"),
            sag_language="en",
            llm_provider="openai",
            llm_model="local",
            llm_api_key="local",
            llm_base_url=models.base_url,
            embedding_model="local",
            embedding_api_key="local",
            embedding_base_url=models.base_url,
            embedding_schema_dimensions=32,
            document_chunk_max_tokens=128,
        )
        manager = EngineManager(stock)
        async with SessionLocal() as session:
            source = Source(name="admission identity", sag_source_config_id=uuid4().hex)
            session.add(source)
            await session.flush()
            document = Document(source_id=source.id, filename=path.name, storage_path=str(path))
            session.add(document)
            await session.flush()
            job = Job(
                type=JobType.PROCESS_DOCUMENT,
                status=JobStatus.RUNNING,
                source_id=source.id,
                document_id=document.id,
                payload={"control_revision": 1},
            )
            session.add(job)
            await session.commit()
            source_id = source.id
            try:
                await manager.provision(source.sag_source_config_id, source)
                yield session, manager, stock, models, source, document, job, path
            finally:
                models.release.set()
                await manager.aclose_all()
                await session.rollback()
                await session.execute(delete(Source).where(Source.id == source_id))
                await session.commit()


async def test_job_persists_raw_identity_with_bound_request_before_first_model_call(admitted_document_task):
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Job
    from sag_api.jobs import tasks

    session, manager, _stock, models, source, _document, job, path = admitted_document_task
    expected = await manager.document_processing_identity(source.sag_source_config_id, str(path), source=source)
    models.block_at = 1
    running = asyncio.create_task(tasks.process_document(session, job, engine_manager=manager))
    try:
        assert await asyncio.to_thread(models.blocked.wait, 10), "first model call never started"
        async with SessionLocal() as read:
            saved = await read.get(Job, job.id)
            assert saved.payload.get("processing_identity") == expected
            assert saved.payload["process_request"]["source"]["original_sha256"] == expected["original_sha256"]
    finally:
        models.release.set()
        await running


@pytest.mark.parametrize("loss", ["takeover", "heartbeat_error"])
async def test_real_worker_lease_loss_cancels_dispatch_and_waits_for_manual_resume(
    admitted_document_task,
    monkeypatch,
    loss,
):
    from sqlalchemy import delete, select, update
    from zleap.sag import DataEngine

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import OctxOperationLease
    from sag_api.enums import DocumentStatus, JobStatus
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.services import source_operation_service as leases

    session, manager, stock, models, source, document, job, path = admitted_document_task
    stock.document_extract_concurrency = 1
    path.write_text(
        "\n\n".join(f"# Topic {index}\n" + "Acme completed the database migration. " * 20 for index in range(16))
    )
    job.status = JobStatus.QUEUED
    await session.commit()
    heartbeat_done = asyncio.Event()
    original_heartbeat = leases._heartbeat

    async def fast_heartbeat(factory, keys, owner, ttl, _interval, stop):
        try:
            if loss == "heartbeat_error" and any(":processor:" in key for key in keys):
                assert await asyncio.to_thread(models.blocked.wait, 10)
                raise RuntimeError("lease renewal unavailable")
            await original_heartbeat(factory, keys, owner, ttl, 0.02, stop)
        finally:
            if any(":processor:" in key for key in keys):
                heartbeat_done.set()

    tokens = []
    original_process = DataEngine.process_source

    async def observed_process(self, request, **kwargs):
        tokens.append(kwargs["cancellation"])
        return await original_process(self, request, **kwargs)

    monkeypatch.setattr(leases, "_heartbeat", fast_heartbeat)
    monkeypatch.setattr(DataEngine, "process_source", observed_process)
    models.block_at = 1
    queue = InProcessAsyncQueue(SessionLocal, manager, concurrency=2)
    running = asyncio.create_task(queue._run_job(job.id))
    replacement_key = f"source:{source.id}:processor:serial"
    try:
        assert await asyncio.to_thread(models.blocked.wait, 10), "first model call never started"
        if loss == "takeover":
            async with SessionLocal() as control:
                await control.execute(
                    update(OctxOperationLease)
                    .where(
                        OctxOperationLease.resource_key == replacement_key,
                    )
                    .values(owner_token="document:successor:unique")
                )
                await control.commit()
        await asyncio.wait_for(heartbeat_done.wait(), 2)
        deadline = asyncio.get_running_loop().time() + 1
        while not tokens[0].is_cancelled and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert tokens[0].is_cancelled, "lost business lease did not reach the public SDK cancellation token"
    finally:
        models.release.set()
        await asyncio.wait_for(running, 15)
        if loss == "takeover":
            async with SessionLocal() as control:
                row = await control.scalar(
                    select(OctxOperationLease).where(
                        OctxOperationLease.resource_key == replacement_key,
                    )
                )
                assert row is not None and row.owner_token == "document:successor:unique"
                await control.execute(
                    delete(OctxOperationLease).where(
                        OctxOperationLease.resource_key == replacement_key,
                    )
                )
                await control.commit()
    await session.refresh(job)
    await session.refresh(document)
    assert job.status == JobStatus.PAUSED
    assert not queue._retry_tasks
    assert document.status == DocumentStatus.PAUSED and not document.has_publication
    assert len(models.calls) == 1
    request = ProcessSourceRequest.model_validate(job.payload["process_request"])
    async with manager.use(source.sag_source_config_id, source=source) as engine:
        result = await engine.get_operation_status(request.context.operation_id)
    assert result.failure_code == "cancelled" and len(result.extraction_progress["succeeded_chunk_ids"]) == 1
    assert result.extraction_progress["chunk_total"] > 1


async def test_actual_ready_publication_survives_lease_loss_at_exit(admitted_document_task, monkeypatch):
    from sqlalchemy import delete, select, update
    from zleap.sag import DataEngine

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import OctxOperationLease
    from sag_api.enums import DocumentStatus, JobStatus
    from sag_api.jobs.inproc import InProcessAsyncQueue

    session, manager, _stock, models, source, document, job, _path = admitted_document_task
    job.status = JobStatus.QUEUED
    await session.commit()
    key = f"source:{source.id}:processor:serial"
    original_process = DataEngine.process_source

    async def lost_after_commit(self, request, **kwargs):
        result = await original_process(self, request, **kwargs)
        assert result.status == "succeeded" and result.checkpoint == "ready"
        async with SessionLocal() as control:
            await control.execute(
                update(OctxOperationLease)
                .where(
                    OctxOperationLease.resource_key == key,
                )
                .values(owner_token="document:successor:unique")
            )
            await control.commit()
        return result

    monkeypatch.setattr(DataEngine, "process_source", lost_after_commit)
    queue = InProcessAsyncQueue(SessionLocal, manager, concurrency=2)
    try:
        await queue._run_job(job.id)
        await session.refresh(job)
        await session.refresh(document)
        await session.refresh(source)
        assert job.status == JobStatus.SUCCEEDED
        assert document.status == DocumentStatus.READY and document.has_publication
        request = ProcessSourceRequest.model_validate(job.payload["process_request"])
        assert document.published_operation_id == request.context.operation_id
        assert document.sag_source_id == request.source.descriptor.source_id
        assert source.chunk_count == document.chunk_count and source.event_count == document.event_count
        calls = list(models.calls)
        await queue._run_job(job.id)
        assert models.calls == calls and not queue._retry_tasks
        async with SessionLocal() as control:
            row = await control.scalar(select(OctxOperationLease).where(OctxOperationLease.resource_key == key))
            assert row is not None and row.owner_token == "document:successor:unique"
    finally:
        async with SessionLocal() as control:
            await control.execute(delete(OctxOperationLease).where(OctxOperationLease.resource_key == key))
            await control.commit()


@pytest.mark.parametrize(
    "changed", ["input", "llm_model", "embedding_model", "document_chunk_max_tokens", "llm_extra_body"]
)
async def test_confirmed_task_rechecks_actual_admission_before_parsing_or_models(
    admitted_document_task, changed, monkeypatch
):
    from sag_api.core.errors import DocumentRestartRequiredError
    from sag_api.jobs import tasks

    session, manager, stock, models, source, _document, job, path = admitted_document_task
    confirmed = await manager.document_processing_identity(source.sag_source_config_id, str(path), source=source)
    job.payload = {**job.payload, "confirmed_identity": confirmed}
    await session.commit()
    if changed == "input":
        path.write_text("# Changed\nA replacement fact after confirmation.")
    else:
        setattr(
            stock,
            changed,
            {"marker": "changed"}
            if changed == "llm_extra_body"
            else 256
            if changed == "document_chunk_max_tokens"
            else "changed-model",
        )
        await manager.apply_settings(stock, reset_engines=True)
    parses = []
    original = tasks.prepare_document

    async def parse(*args, **kwargs):
        parses.append(args[0])
        return await original(*args, **kwargs)

    monkeypatch.setattr(tasks, "prepare_document", parse)
    with pytest.raises(DocumentRestartRequiredError) as caught:
        await tasks.process_document(session, job, engine_manager=manager)
    assert caught.value.reason == "confirmation_target_changed"
    assert parses == [] and models.requests == []
    await session.refresh(job)
    assert job.payload["confirmed_identity"] == confirmed
    assert "process_request" not in job.payload


async def test_input_changed_during_parse_does_not_bind_or_call_models(admitted_document_task, monkeypatch):
    from sag_api.core.errors import DocumentRestartRequiredError
    from sag_api.jobs import tasks

    session, manager, _stock, models, _source, _document, job, path = admitted_document_task
    original = tasks.prepare_document

    async def parse(*args, **kwargs):
        parsed = await original(*args, **kwargs)
        path.write_text("# Replaced during parse\nDifferent input bytes.")
        return parsed

    monkeypatch.setattr(tasks, "prepare_document", parse)
    with pytest.raises(DocumentRestartRequiredError) as caught:
        await tasks.process_document(session, job, engine_manager=manager)
    assert caught.value.reason == "resume_input_changed"
    assert models.requests == []
    await session.refresh(job)
    assert job.payload["processing_identity"]
    assert "process_request" not in job.payload


async def test_non_markdown_resume_uses_bound_body_after_parser_cache_disappears(admitted_document_task, monkeypatch):
    from sag_api.core.db import SessionLocal
    from sag_api.enums import DocumentStatus, JobStatus
    from sag_api.jobs import tasks
    from sag_api.jobs.control import JobPaused
    from sag_api.parsing.service import PreparedDocument
    from sag_api.services.document_service import pause_document, resume_document

    session, manager, stock, models, source, document, job, path = admitted_document_task
    stock.document_extract_concurrency = 1
    original_path = path.with_suffix(".pdf")
    path.rename(original_path)
    document.filename = original_path.name
    document.storage_path = str(original_path)
    await session.commit()
    cache = path.with_name("parsed-cache.md")
    cache.write_text(
        "\n\n".join(f"# Topic {index}\n" + "Acme completed the database migration. " * 20 for index in range(16))
    )
    parses = []

    async def parse(*args, **kwargs):
        parses.append(args[0])
        return PreparedDocument(path=str(cache), provider="markitdown")

    monkeypatch.setattr(tasks, "prepare_document", parse)
    models.block_at = 1
    running = asyncio.create_task(tasks.process_document(session, job, engine_manager=manager))
    try:
        assert await asyncio.to_thread(models.blocked.wait, 10)
        async with SessionLocal() as control:
            await pause_document(control, await control.get(type(source), source.id), document.id)
        models.release.set()
        with pytest.raises(JobPaused):
            await running
    finally:
        models.release.set()
        if not running.done():
            await running
    await session.refresh(job)
    bound = job.payload["process_request"]
    baseline = job.payload["processing_identity"]
    job.status = JobStatus.PAUSED
    await session.commit()
    cache.unlink()
    await session.refresh(document)

    class Queue:
        async def enqueue(self, _job_id):
            pass

    await resume_document(session, source, document.id, job_queue=Queue(), engine_manager=manager)
    job.status = JobStatus.RUNNING
    await session.commit()
    await tasks.process_document(session, job, engine_manager=manager)
    await session.refresh(job)
    await session.refresh(document)
    assert job.payload["process_request"] == bound and job.payload["processing_identity"] == baseline
    assert document.status == DocumentStatus.READY and document.has_publication
    published_body = await manager.get_document_markdown(
        source.sag_source_config_id,
        document.sag_source_id,
        source=source,
    )
    # The existing loader normalizes whitespace at chunk boundaries.
    assert published_body.split() == bound["source"]["content"].split()
    assert parses == [str(original_path)] and not cache.exists()


async def test_bound_inline_request_survives_job_payload_roundtrip_without_shadow_chunks(tmp_path):
    engine = PublicEngine()
    data_source_id = str(uuid4())
    processor = IncrementalDocumentProcessor(engine, data_source_id, max_concurrency=1)
    markdown = tmp_path / "input.md"
    markdown.write_text("# A\nA useful durable fact.")
    input_id = str(uuid4())
    original_context = context(data_source_id)
    request = await processor.build_request(markdown, context=original_context, input_id=input_id)
    payload = {"pause_requested": False, "process_request": request.model_dump(mode="json")}
    replay = ProcessSourceRequest.model_validate(payload["process_request"])
    assert replay == request
    assert replay.context.isolation == "input"
    assert replay.context.operation_id == original_context.operation_id
    assert replay.context.fence_token == original_context.fence_token
    assert replay.source.descriptor.source_id == input_id
    assert replay.source.descriptor.data_source_id == data_source_id
    assert replay.source.content == markdown.read_text()
    assert "process_checkpoint" not in payload
    assert "chunk_ids" not in payload
    assert engine.requests == []
    first = await processor.process(replay, should_pause=never_pause)
    second = await processor.process(replay, should_pause=never_pause)
    assert first.status == second.status == "succeeded"
    assert [r.context.operation_id for r in engine.requests] == [original_context.operation_id] * 2
    assert [r.source.descriptor.source_id for r in engine.requests] == [input_id] * 2


async def test_request_local_observers_keep_two_documents_progress_independent(tmp_path):
    engine = PublicEngine()
    data_source_id = str(uuid4())
    processor = IncrementalDocumentProcessor(engine, data_source_id, max_concurrency=1)
    markdown = tmp_path / "input.md"
    markdown.write_text("# A\nA useful durable fact.")
    requests = [
        await processor.build_request(
            markdown,
            context=context(data_source_id),
            input_id=str(uuid4()),
        )
        for _ in range(2)
    ]
    progress = [[], []]

    async def run(index):
        async def on_progress(completed, total):
            progress[index].append((completed, total))

        return await processor.process(
            requests[index],
            should_pause=never_pause,
            on_progress=on_progress,
        )

    results = await asyncio.gather(run(0), run(1))
    assert all(result.status == "succeeded" for result in results)
    assert progress == [[(1, 2)], [(1, 2)]]
    assert len({request.context.operation_id for request in engine.requests}) == 2


async def test_lost_control_check_cancels_execution_and_preserves_the_actual_result(tmp_path):
    engine = PublicEngine()
    data_source_id = str(uuid4())
    processor = IncrementalDocumentProcessor(engine, data_source_id, max_concurrency=1)
    markdown = tmp_path / "input.md"
    markdown.write_text("# A\nA useful durable fact.")
    request = await processor.build_request(markdown, context=context(data_source_id), input_id=str(uuid4()))
    stopped = asyncio.Event()

    async def check_control():
        raise RuntimeError("control database unavailable")

    async def execute(request, *, observer, cancellation):
        while not cancellation.is_cancelled:
            await asyncio.sleep(0)
        stopped.set()
        raise asyncio.CancelledError

    async def status(operation_id):
        assert operation_id == request.context.operation_id
        return None  # Cancellation reached the public kernel before admission.

    engine.process_source = execute
    engine.get_operation_status = status
    with pytest.raises(RuntimeError, match="control database unavailable"):
        await asyncio.wait_for(processor.process(request, should_pause=check_control), timeout=0.5)
    assert stopped.is_set()


@pytest.mark.parametrize("control_failure", [False, True])
async def test_completed_public_receipt_wins_over_late_control_signal(tmp_path, control_failure):
    engine = PublicEngine()
    data_source_id = str(uuid4())
    processor = IncrementalDocumentProcessor(engine, data_source_id, max_concurrency=1)
    markdown = tmp_path / "input.md"
    markdown.write_text("# A\nA useful durable fact.")
    request = await processor.build_request(markdown, context=context(data_source_id), input_id=str(uuid4()))
    receipt = engine.process_source

    async def check_control():
        if control_failure:
            raise RuntimeError("control database unavailable")
        return True

    async def final_commit(request, *, observer, cancellation):
        while not cancellation.is_cancelled:
            await asyncio.sleep(0)
        return await receipt(request, observer=observer, cancellation=cancellation)

    engine.process_source = final_commit
    result = await asyncio.wait_for(processor.process(request, should_pause=check_control), timeout=0.5)
    assert result.status == "succeeded"
    assert result.operation_id == request.context.operation_id


@pytest.mark.usefixtures("controller_identity")
async def test_job_saves_bound_request_before_execution_and_replays_without_parsing(tmp_path, monkeypatch):
    from sqlalchemy import delete

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs import tasks
    from sag_api.jobs.control import JobPaused

    await init_db()
    markdown = tmp_path / "job.md"
    markdown.write_text("# Durable\nOne published fact.")
    parses = []

    async def prepare(*args, **kwargs):
        parses.append(args[0])
        return SimpleNamespace(
            path=markdown,
            provider="markdown",
            cached=False,
            fallback_from=None,
            fallback_error=None,
            parser_version="test",
            source_sha256="a" * 64,
        )

    monkeypatch.setattr(tasks, "prepare_document", prepare)
    engine = PublicEngine()
    replayed = []

    class Manager(UncommittedDocumentManager):
        async def process_document(self, source_config_id, request_factory, **kwargs):
            processor = IncrementalDocumentProcessor(engine, source_config_id, max_concurrency=1)
            request = await request_factory(processor, *processing_parameters())
            # A separate transaction must see the request before any engine work.
            async with SessionLocal() as read:
                saved = await read.get(Job, job_id)
                assert saved.payload["process_request"] == request.model_dump(mode="json")
                assert saved.payload["control_revision"] == 1
                assert saved.payload["pause_requested"] is False
                current = await read.get(Document, document_id)
                assert current.sag_source_id is None
            replayed.append(request)
            return None  # Public cancellation before admission, no fake checkpoint.

    async with SessionLocal() as session:
        source = Source(name="durable-request", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.flush()
        document = Document(source_id=source.id, filename="job.md", storage_path=str(markdown))
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.RUNNING,
            source_id=source.id,
            document_id=document.id,
            payload={"pause_requested": False},
        )
        session.add(job)
        await session.commit()
        source_id, document_id, job_id = source.id, document.id, job.id
    try:
        for _ in range(2):
            async with SessionLocal() as session:
                document = await session.get(Document, document_id)
                document.status = DocumentStatus.PENDING
                await session.commit()
                job = await session.get(Job, job_id)
                with pytest.raises(JobPaused):
                    await tasks._process_document_unlocked(
                        session, job, engine_manager=Manager(), lease_lost=asyncio.Event()
                    )
        assert len(parses) == 1
        assert replayed[0] == replayed[1]
        async with SessionLocal() as session:
            job = await session.get(Job, job_id)
            assert "process_checkpoint" not in job.payload
            document = await session.get(Document, document_id)
            assert document.status == DocumentStatus.PAUSED
            assert document.sag_source_id is None
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Job).where(Job.id == job_id))
            await session.execute(delete(Document).where(Document.id == document_id))
            await session.execute(delete(Source).where(Source.id == source_id))
            await session.commit()


async def test_knowledge_package_saves_public_request_and_replays_same_input(tmp_path):
    from octx import create_octx

    from sag_api.sag.octx_importer import import_knowledge_package

    folder = tmp_path / "knowledge"
    folder.mkdir()
    (folder / "index.md").write_text('---\nokf_version: "1.0"\n---\n# Index\n\n- [Doc](doc.md)\n')
    (folder / "doc.md").write_text("# Doc\nA reusable knowledge fact.")
    package = tmp_path / "knowledge.octx"
    create_octx(tmp_path / "workspace", source=folder, output=package, name="Knowledge")
    engine = PublicEngine()
    saved = []
    checkpoint = {}
    attempts = []

    async def persist(value):
        from copy import deepcopy

        saved.append(deepcopy(value))

    class Manager(UncommittedDocumentManager):
        async def process_document(self, source_config_id, request_factory, **kwargs):
            processor = IncrementalDocumentProcessor(engine, source_config_id, max_concurrency=1)
            request = await request_factory(processor, *processing_parameters())
            assert any(
                doc.get("process_request") == request.model_dump(mode="json") for doc in saved[-1]["documents"].values()
            )
            attempts.append(request)
            return None

    for _ in range(2):
        with pytest.raises(RuntimeError, match="paused"):
            await import_knowledge_package(
                package,
                tmp_path / "controlled",
                source_config_id="knowledge-partition",
                engine_manager=Manager(),
                checkpoint=checkpoint,
                on_checkpoint=persist,
            )
    assert attempts[0] == attempts[1]
    assert "process_checkpoint" not in next(iter(checkpoint["documents"].values()))


async def test_legacy_unfinished_checkpoint_does_not_silently_start_a_new_operation(tmp_path, monkeypatch):
    from sqlalchemy import delete

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs import tasks

    await init_db()
    called = []

    async def prepare(*args, **kwargs):
        called.append("parse")
        raise AssertionError("legacy task must require confirmation before parsing")

    monkeypatch.setattr(tasks, "prepare_document", prepare)
    async with SessionLocal() as session:
        source = Source(name="legacy-unfinished", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id, filename="old.md", storage_path=str(tmp_path / "old.md"), status=DocumentStatus.PENDING
        )
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.RUNNING,
            source_id=source.id,
            document_id=document.id,
            payload={"process_checkpoint": {"source_id": "old-input", "chunk_ids": ["saved"]}},
        )
        session.add(job)
        await session.commit()
        source_id, document_id, job_id = source.id, document.id, job.id
        try:
            with pytest.raises(Exception, match="旧任务.*确认"):
                await tasks._process_document_unlocked(
                    session, job, engine_manager=object(), lease_lost=asyncio.Event()
                )
            assert called == []
            await session.refresh(job)
            assert "process_request" not in job.payload
            assert job.payload["process_checkpoint"]["source_id"] == "old-input"
        finally:
            await session.execute(delete(Job).where(Job.id == job_id))
            await session.execute(delete(Document).where(Document.id == document_id))
            await session.execute(delete(Source).where(Source.id == source_id))
            await session.commit()


async def test_installed_wheel_resumes_successful_and_zero_event_chunks_after_processing_b(tmp_path):
    from document_models import local_models
    from zleap.sag import DataEngine, EngineConfig
    from zleap.sag.config import EmbeddingConfig, LLMConfig
    from zleap.sag.pipeline import SearchOptions, SearchRequest, SearchScope

    data_source_id = uuid4().hex
    markdown = tmp_path / "a.md"
    markdown.write_text(
        "\n\n".join(
            f"# Topic {index}\n" + ("ZERO_ONLY " if index == 0 else "Acme Corporation completed a migration. ") * 15
            for index in range(24)
        )
    )
    other = tmp_path / "b.md"
    other.write_text("# B\nAcme Corporation completed another project.")
    with local_models() as models:
        config = EngineConfig(
            storage_mode="normal",
            data_dir=str(tmp_path / "stores"),
            llm=LLMConfig(api_key="local", model="local", base_url=models.base_url, max_retries=0),
            embedding=EmbeddingConfig(api_key="local", model="local", base_url=models.base_url, schema_dimensions=32),
        )
        async with DataEngine(config, data_source_id=data_source_id) as engine:
            processor = IncrementalDocumentProcessor(
                engine, data_source_id, max_concurrency=1, chunk_mode="heading_strict", chunk_max_tokens=128
            )
            a = await processor.build_request(markdown, context=context(data_source_id), input_id=uuid4().hex)
            # Remove summaries to count chunk model calls exactly; no engine internals are replaced.
            a = a.model_copy(
                update={
                    "extraction_options": a.extraction_options.model_copy(
                        update={
                            "enable_article_summary": False,
                            "enable_parent_summary": False,
                        }
                    )
                }
            )
            pause = False

            async def pause_check():
                return pause

            async def progress(completed, total):
                nonlocal pause
                if completed >= 10:
                    pause = True

            first = await asyncio.wait_for(processor.process(a, should_pause=pause_check, on_progress=progress), 30)
            assert first.failure_code == "cancelled"
            successes = set(first.extraction_progress["succeeded_chunk_ids"])
            assert 0 < len(successes) < first.extraction_progress["chunk_total"]
            assert first.extraction_progress["chunk_failed"] == 0
            assert len(models.zero_calls) == 1
            a_calls = list(models.calls)
            assert len(a_calls) == len(successes)
            hidden = await engine.search(
                SearchRequest(
                    query="Acme",
                    scope=SearchScope(
                        data_source_ids=(data_source_id,),
                        source_ids=(a.source.descriptor.source_id,),
                    ),
                    options=SearchOptions(strategy="vector", top_k=50, return_type="chunk"),
                )
            )
            assert not hidden.chunks
            b = await processor.build_request(other, context=context(data_source_id), input_id=uuid4().hex)
            b = b.model_copy(update={"extraction_options": a.extraction_options})
            completed_b = await processor.process(b, should_pause=never_pause)
            assert completed_b.status == "succeeded"
            before_resume = len(models.calls)
            replay = ProcessSourceRequest.model_validate(a.model_dump(mode="json"))
            resumed = await asyncio.wait_for(processor.process(replay, should_pause=never_pause), 30)
            assert resumed.status == "succeeded"
            assert set(resumed.extraction_progress["succeeded_chunk_ids"]) >= successes
            assert len(models.calls) - before_resume == resumed.extraction_progress["chunk_total"] - len(successes)
            assert not set(a_calls) & set(models.calls[before_resume:])
            assert len(models.zero_calls) == 1
            receipt_calls = len(models.calls)
            assert (await processor.process(replay, should_pause=never_pause)).status == "succeeded"
            assert len(models.calls) == receipt_calls


@pytest.mark.parametrize("concurrent_change", ["pause", "request", "unqualified_request"])
@pytest.mark.usefixtures("controller_identity")
async def test_binding_cannot_overwrite_a_concurrent_pause_or_bound_request(tmp_path, concurrent_change):
    from document_requests import document_request
    from sqlalchemy import delete, update
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.db import engine as app_database
    from sag_api.core.errors import DocumentRestartRequiredError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs import tasks
    from sag_api.jobs.control import JobPaused

    await init_db()
    markdown = tmp_path / "pause-race.md"
    markdown.write_text("# Pause race\nOne durable fact.")
    async with SessionLocal() as session:
        source = Source(name="pause-binding-race", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.flush()
        document = Document(source_id=source.id, filename=markdown.name, storage_path=str(markdown))
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.RUNNING,
            document_id=document.id,
            source_id=source.id,
            payload={"pause_requested": False, "custom": "kept"},
        )
        session.add(job)
        await session.commit()
        source_id, document_id, job_id = source.id, document.id, job.id
        competing = document_request(source.sag_source_config_id, source_id="already-bound-input")

    injected = False

    class ConcurrentPauseSession(AsyncSession):
        async def refresh(self, instance, attribute_names=None, **kwargs):
            nonlocal injected
            await super().refresh(instance, attribute_names=attribute_names, **kwargs)
            if isinstance(instance, Job) and attribute_names == ["payload"] and not injected:
                injected = True
                assert instance.payload["pause_requested"] is False
                async with SessionLocal() as control:
                    change = (
                        {"pause_requested": True}
                        if concurrent_change == "pause"
                        else {
                            "process_request": competing.model_dump(mode="json"),
                            **(
                                {"processing_identity": controller_processing_identity(str(markdown))}
                                if concurrent_change == "request"
                                else {}
                            ),
                        }
                    )
                    await control.execute(
                        update(Job)
                        .where(Job.id == job_id)
                        .values(
                            payload={**instance.payload, **change},
                        )
                    )
                    await control.commit()

    public_engine = PublicEngine()

    class Manager(UncommittedDocumentManager):
        async def process_document(self, source_config_id, request_factory, **kwargs):
            processor = IncrementalDocumentProcessor(public_engine, source_config_id, max_concurrency=1)
            request = await request_factory(processor, *processing_parameters())
            async with SessionLocal() as read:
                payload = (await read.get(Job, job_id)).payload
                assert payload["pause_requested"] is (concurrent_change == "pause")
                if concurrent_change == "request":
                    assert request == competing
                assert payload["custom"] == "kept"
                assert payload["process_request"] == request.model_dump(mode="json")
            return None

    sessions = async_sessionmaker(app_database, class_=ConcurrentPauseSession, expire_on_commit=False)
    try:
        async with sessions() as session:
            expected = DocumentRestartRequiredError if concurrent_change == "unqualified_request" else JobPaused
            with pytest.raises(expected) as caught:
                await tasks._process_document_unlocked(
                    session, await session.get(Job, job_id), engine_manager=Manager(), lease_lost=asyncio.Event()
                )
            if concurrent_change == "unqualified_request":
                assert caught.value.reason == "legacy_processing_identity"
                await session.refresh(await session.get(Job, job_id))
                assert "processing_identity" not in (await session.get(Job, job_id)).payload
        assert injected
        assert public_engine.requests == []
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Job).where(Job.id == job_id))
            await session.execute(delete(Document).where(Document.id == document_id))
            await session.execute(delete(Source).where(Source.id == source_id))
            await session.commit()


async def test_unpublished_bound_input_remains_registered_for_document_cleanup(tmp_path):
    from document_requests import document_request
    from sqlalchemy import delete

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document

    class Queue:
        def begin_source_maintenance(self, *args):
            pass

        async def enqueue_durably(self, job_id):
            pass

    await init_db()
    path = tmp_path / "candidate.md"
    path.write_text("# Unpublished candidate\n")
    async with SessionLocal() as session:
        source = Source(name="candidate-cleanup", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id, filename=path.name, storage_path=str(path), status=DocumentStatus.PENDING
        )
        session.add(document)
        await session.flush()
        request = document_request(source.sag_source_config_id, source_id="unpublished-input")
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=document.id,
            payload={"process_request": request.model_dump(mode="json")},
        )
        session.add(job)
        await session.commit()
        source_id, document_id = source.id, document.id
        try:
            cleanup = await delete_document(session, source, document_id, job_queue=Queue())
            assert cleanup.status == JobStatus.QUEUED
            assert cleanup.payload["derived_source_ids"] == ["unpublished-input"]
            await session.refresh(document)
            assert document.status == DocumentStatus.DELETING
            assert document.sag_source_id is None
            assert path.exists()
        finally:
            await session.execute(delete(Job).where(Job.source_id == source_id))
            await session.execute(delete(Document).where(Document.id == document_id))
            await session.execute(delete(Source).where(Source.id == source_id))
            await session.commit()
