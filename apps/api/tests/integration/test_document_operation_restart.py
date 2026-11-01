"""Fresh application runtimes recover through the installed SDK's public receipts."""

import asyncio

import pytest
from zleap.sag import DataEngine
from zleap.sag.exceptions import StorageError
from zleap.sag.modules.extract.saver import EventSaver

from sag_api.core.db import SessionLocal
from sag_api.core.errors import UpstreamError
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs import tasks
from sag_api.jobs.inproc import InProcessAsyncQueue
from sag_api.sag import EngineManager
from sag_api.services.document_service import pause_document, read_document_lines, resume_document
from tests.test_document_durable_processing import admitted_document_task as admitted_document_task


async def test_two_actual_managers_pause_a_process_b_and_resume_a_without_preemption(
    admitted_document_task, monkeypatch,
):
    session, old_manager, stock, models, source, document_a, job_a, raw_a = admitted_document_task
    source_id, document_a_id, job_a_id, range_id = source.id, document_a.id, job_a.id, source.sag_source_config_id
    await old_manager.aclose_all()
    raw_a.write_text("\n\n".join(
        f"# Section {index}\nAcme Corporation completed migration milestone {index}. "
        + f"Milestone {index} includes validation and a separately recorded operational result. " * 20
        for index in range(6)
    ), encoding="utf-8")
    raw_b = raw_a.with_name("peer.md")
    raw_b.write_text("# Peer\nAcme Corporation completed the peer rollout.", encoding="utf-8")
    async with SessionLocal() as writer:
        saved_a = await writer.get(Job, job_a_id)
        saved_a.status = JobStatus.QUEUED
        document_b = Document(source_id=source_id, filename=raw_b.name, storage_path=str(raw_b))
        writer.add(document_b)
        await writer.flush()
        job_b = Job(
            type=JobType.PROCESS_DOCUMENT, source_id=source_id, document_id=document_b.id,
            status=JobStatus.QUEUED, payload={"control_revision": 1},
        )
        writer.add(job_b)
        await writer.commit()
        document_b_id, job_b_id = document_b.id, job_b.id
    await session.rollback()

    async def no_projection_refresh(*args, **kwargs):
        return []

    monkeypatch.setattr("sag_api.services.universe_service.schedule_universe_refresh", no_projection_refresh)
    cancellations = []
    original_process = DataEngine.process_source

    async def observed_process(self, request, **kwargs):
        cancellations.append(kwargs["cancellation"])
        return await original_process(self, request, **kwargs)

    monkeypatch.setattr(DataEngine, "process_source", observed_process)
    frozen = stock.model_copy(deep=True, update={"document_extract_concurrency": 1})
    manager_a = EngineManager(frozen)
    manager_b = EngineManager(frozen.model_copy(deep=True))
    first = InProcessAsyncQueue(SessionLocal, manager_a, concurrency=2)
    second = InProcessAsyncQueue(SessionLocal, manager_b, concurrency=2)
    active_a = active_b = None
    try:
        models.block_at = 2
        active_a = asyncio.create_task(first._run_job(job_a_id))
        assert await asyncio.to_thread(models.blocked.wait, 15), "A did not reach its second real model request"
        await second._run_job(job_b_id)
        async with SessionLocal() as reader:
            waiting_b = await reader.get(Job, job_b_id)
            assert waiting_b.status is JobStatus.QUEUED and waiting_b.attempts == 0
        assert len(models.calls) == 2
        async with SessionLocal() as control:
            await pause_document(control, await control.get(Source, source_id), document_a_id)
        async with asyncio.timeout(2):
            while not cancellations[0].is_cancelled:
                await asyncio.sleep(0.01)
        models.release.set()
        await asyncio.wait_for(active_a, 20)
        async with SessionLocal() as reader:
            paused_a = await reader.get(Job, job_a_id)
            assert paused_a.status is JobStatus.PAUSED
            assert (await reader.get(Document, document_a_id)).sag_source_id is None
            request_a = paused_a.payload["process_request"]
        operation_a = request_a["context"]["operation_id"]
        async with manager_b.use_concurrently(range_id) as engine:
            paused_receipt = await engine.get_operation_status(operation_a)
        saved_chunks = set(paused_receipt.extraction_progress["succeeded_chunk_ids"])
        assert saved_chunks and len(saved_chunks) < paused_receipt.extraction_progress["chunk_total"]
        saved_chat_signatures = list(models.calls)

        models.blocked.clear()
        models.release.clear()
        models.block_at = len(models.calls) + 1
        active_b = asyncio.create_task(second._run_job(job_b_id))
        assert await asyncio.to_thread(models.blocked.wait, 15), "B did not acquire the released processing slot"
        async with SessionLocal() as control:
            resumed = await resume_document(
                control, await control.get(Source, source_id), document_a_id,
                job_queue=first, engine_manager=manager_a,
            )
            assert resumed.id == job_a_id and resumed.payload["process_request"] == request_a
        await first._run_job(job_a_id)
        assert not active_b.done()
        assert models.calls[:-1] == saved_chat_signatures
        async with SessionLocal() as reader:
            queued_a = await reader.get(Job, job_a_id)
            assert queued_a.status is JobStatus.QUEUED
            assert queued_a.payload["process_request"] == request_a
        models.release.set()
        await asyncio.wait_for(active_b, 20)
        await first._run_job(job_a_id)

        async with SessionLocal() as reader:
            published_a = await reader.get(Document, document_a_id)
            published_b = await reader.get(Document, document_b_id)
            finished_a, finished_b = await reader.get(Job, job_a_id), await reader.get(Job, job_b_id)
            assert finished_a.status is JobStatus.SUCCEEDED and finished_b.status is JobStatus.SUCCEEDED
            assert published_a.has_publication and published_b.has_publication
            assert published_a.published_operation_id == operation_a
            assert published_a.sag_source_id != published_b.sag_source_id
            assert finished_a.payload["process_request"] == request_a
            reading_source = await reader.get(Source, source_id)
            body_a = await read_document_lines(published_a, reading_source, manager_b)
            body_b = await read_document_lines(published_b, reading_source, manager_a)
        assert "Section 0" in "".join(body_a) and "peer rollout" in "".join(body_b)
        async with manager_b.use_concurrently(range_id) as engine:
            completed = await engine.get_operation_status(operation_a)
        assert completed.status == "succeeded" and completed.checkpoint == "ready"
        assert saved_chunks <= set(completed.extraction_progress["succeeded_chunk_ids"])
        assert all(models.calls.count(signature) == 1 for signature in saved_chat_signatures)
    finally:
        models.release.set()
        await asyncio.gather(*(task for task in (active_a, active_b) if task), return_exceptions=True)
        await first.stop()
        await second.stop()
        await manager_a.aclose_all()
        await manager_b.aclose_all()


async def test_actual_prepared_restart_waits_for_manual_resume_without_reextracting(
    admitted_document_task, monkeypatch,
):
    session, old_manager, stock, models, source, document, job, _path = admitted_document_task
    source_id, document_id, job_id, range_id = source.id, document.id, job.id, source.sag_source_config_id
    http_calls = []
    original_respond = models.respond

    def respond(path, body):
        http_calls.append(path)
        return original_respond(path, body)

    monkeypatch.setattr(models, "respond", respond)
    original_commit = EventSaver.commit
    phase = "interrupted"
    commit_calls = []

    async def commit(self, events, config, *, reuse_vectors=None, operation_guard=None):
        commit_calls.append(phase)
        if phase == "interrupted":
            raise StorageError("injected failure before generation commit", retryable=True)
        return await original_commit(
            self, events, config, reuse_vectors=reuse_vectors, operation_guard=operation_guard,
        )

    monkeypatch.setattr(EventSaver, "commit", commit)
    with pytest.raises(UpstreamError) as caught:
        await tasks.process_document(session, job, engine_manager=old_manager)
    await session.refresh(job)
    saved_request = job.payload["process_request"]
    saved_identity = job.payload["processing_identity"]
    operation_id = saved_request["context"]["operation_id"]
    async with old_manager.use_concurrently(range_id, source) as engine:
        interrupted = await engine.get_operation_status(operation_id)
    assert interrupted.status == "failed" and interrupted.retryable
    assert caught.value.retryable is True and caught.value.code == interrupted.failure_code
    assert interrupted.checkpoint == "extract_prepared"
    succeeded_ids = set(interrupted.extraction_progress["succeeded_chunk_ids"])
    assert succeeded_ids
    assert commit_calls == ["interrupted"]
    await session.rollback()
    await old_manager.aclose_all()

    phase = "startup"
    calls = []
    statuses = []

    def capture_method(name):
        original = getattr(DataEngine, name)

        async def called(self, *args, **kwargs):
            calls.append((phase, name))
            result = await original(self, *args, **kwargs)
            if name == "get_operation_status":
                statuses.append((phase, result))
            return result

        return called

    for name in ("get_operation_status", "process_source", "delete_source", "delete_data_source"):
        monkeypatch.setattr(DataEngine, name, capture_method(name))

    async def no_projection_refresh(*args, **kwargs):
        return []

    monkeypatch.setattr("sag_api.services.universe_service.schedule_universe_refresh", no_projection_refresh)
    fresh = EngineManager(stock.model_copy(deep=True))
    queue = InProcessAsyncQueue(SessionLocal, fresh, concurrency=1)
    calls_before_startup = list(http_calls)
    chat_before_startup = list(models.calls)
    try:
        await queue.start()
        await queue.enqueue(job_id)
        await queue._run_job(job_id)
        async with SessionLocal() as reader:
            paused_job = await reader.get(Job, job_id)
            paused_document = await reader.get(Document, document_id)
            assert paused_job.status is JobStatus.PAUSED
            assert paused_document.status is DocumentStatus.PAUSED
            assert paused_job.payload["_scheduler"]["blocked_reason"] == "manual_recovery"
            assert paused_job.payload["process_request"] == saved_request
            assert paused_job.payload["processing_identity"] == saved_identity
            assert paused_document.sag_source_id is None
            assert paused_document.published_operation_id is None
        assert calls and all(name == "get_operation_status" for _, name in calls)
        assert any(result.checkpoint == "extract_prepared" for _, result in statuses if result)
        assert commit_calls == ["interrupted"]
        assert http_calls == calls_before_startup
        assert models.calls == chat_before_startup

        phase = "resumed"
        async with SessionLocal() as control:
            resumed = await resume_document(
                control, await control.get(Source, source_id), document_id,
                job_queue=queue, engine_manager=fresh,
            )
            assert resumed.id == job_id
            assert resumed.payload["process_request"] == saved_request
        await asyncio.wait_for(queue._queue.join(), timeout=30)
        async with SessionLocal() as reader:
            finished_job = await reader.get(Job, job_id)
            published = await reader.get(Document, document_id)
            assert finished_job.status is JobStatus.SUCCEEDED, finished_job.error
            assert published.status is DocumentStatus.READY
            assert published.published_operation_id == operation_id
            assert published.sag_source_id == saved_request["source"]["descriptor"]["source_id"]
            assert finished_job.payload["process_request"] == saved_request
        async with fresh.use_concurrently(range_id) as engine:
            completed = await engine.get_operation_status(operation_id)
        assert completed.status == "succeeded" and completed.checkpoint == "ready"
        assert succeeded_ids <= set(completed.extraction_progress["succeeded_chunk_ids"])
        assert models.calls == chat_before_startup
        assert commit_calls == ["interrupted", "resumed"]
        assert sum(name == "process_source" for _, name in calls) == 1
        assert not any(name.startswith("delete_") for _, name in calls)
    finally:
        await queue.stop()
        await fresh.aclose_all()


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_sigkill_real_queue_restarts_with_manual_gate_and_preserved_chunks(tmp_path, backend):
    """Both checkpoint classes use real child processes; no lease table is reset."""
    import json
    import os
    import subprocess
    import sys
    from collections import Counter
    from pathlib import Path

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sag_api.core.config import Settings
    from sag_api.sag.config_builder import build_engine_config
    from tests.document_models import local_models

    pg_port = os.environ.get("SAG_DOCUMENT_TEST_PG_PORT")
    if backend == "postgres" and not pg_port:
        pytest.skip("disposable PostgreSQL endpoint not explicitly configured")
    api_root = Path(__file__).resolve().parents[2]
    worker = Path(__file__).with_name("document_runtime_worker.py")

    async def exercise(kill_at):
        root = tmp_path / kill_at
        root.mkdir()
        sql_url = f"sqlite+aiosqlite:///{root / 'business.db'}"
        sql_engine = create_async_engine(sql_url)
        sessions = async_sessionmaker(sql_engine, expire_on_commit=False)
        children = []
        outputs = []
        http_calls = []
        with local_models() as models:
            actual_respond = models.respond

            def respond(path, body):
                http_calls.append(path)
                return actual_respond(path, body)

            models.respond = respond
            if kill_at == "successful_chunk":
                models.block_at = 2
            env = {key: value for key, value in os.environ.items() if not key.startswith("SAG_")}
            env.update({
                "PYTHONPATH": str(api_root), "SAG_DATABASE_URL": sql_url,
                "SAG_DATA_DIR": str(root / "engine"), "SAG_UPLOAD_DIR": str(root / "uploads"),
                "SAG_DSH_CONNECTION_FILE": str(root / "connection.json"), "SAG_ENGINE_WARMUP_COUNT": "0",
                "SAG_SAG_LANGUAGE": "en", "SAG_LLM_PROVIDER": "openai", "SAG_LLM_MODEL": "local",
                "SAG_LLM_API_KEY": "local", "SAG_LLM_BASE_URL": models.base_url, "SAG_LLM_MAX_RETRIES": "0",
                "SAG_EMBEDDING_MODEL": "local", "SAG_EMBEDDING_API_KEY": "local",
                "SAG_EMBEDDING_BASE_URL": models.base_url, "SAG_EMBEDDING_SCHEMA_DIMENSIONS": "32",
                "SAG_DOCUMENT_CHUNK_MAX_TOKENS": "128", "SAG_DOCUMENT_EXTRACT_CONCURRENCY": "1",
                "SAG_SAG_RELATIONAL_PROVIDER": backend, "SAG_SAG_VECTOR_PROVIDER": "lancedb",
                "SAG_SAG_PG_HOST": "127.0.0.1", "SAG_SAG_PG_PORT": pg_port or "5432",
                "SAG_SAG_PG_USER": "sag_isolation", "SAG_SAG_PG_PASSWORD": "local-test-only",
                "SAG_SAG_PG_DATABASE": "sag_isolation",
            })

            def spawn(mode):
                output = (root / f"{mode}.log").open("w")
                outputs.append(output)
                child = subprocess.Popen(
                    [sys.executable, str(worker), str(root), mode, kill_at],
                    cwd=root, env=env, stdout=output, stderr=output,
                )
                children.append(child)
                return child

            async def wait_file(name, child, timeout=30):
                logfile = root / ("create.log" if child is children[0] else "recover.log")
                async with asyncio.timeout(timeout):
                    while not (root / name).exists():
                        assert child.poll() is None, logfile.read_text()
                        await asyncio.sleep(0.05)
                return json.loads((root / name).read_text())

            sdk = None
            try:
                original = spawn("create")
                identities = await wait_file("identities.json", original)
                stock = Settings(_env_file=None, **json.loads((root / "settings.json").read_text()))
                sdk = DataEngine(build_engine_config(stock), data_source_id=identities["range"], health_check=False)
                await sdk.start()
                async with asyncio.timeout(30):
                    while True:
                        assert original.poll() is None, (root / "create.log").read_text()
                        async with sessions() as session:
                            bound = (await session.get(Job, identities["jobs"][0])).payload.get("process_request")
                        receipt = await sdk.get_operation_status(bound["context"]["operation_id"]) if bound else None
                        # The marker can appear after this iteration read an older receipt.
                        reached = (
                            models.blocked.is_set() and receipt is not None
                            and receipt.extraction_progress["chunk_succeeded"] == 1
                            if kill_at == "successful_chunk" else (
                                (root / "prepared.json").exists() and receipt is not None
                                and receipt.checkpoint == "extract_prepared"
                            )
                        )
                        if reached:
                            break
                        await asyncio.sleep(0.05)
                assert receipt.status == "running"
                assert receipt.checkpoint == ("extract_prepared" if kill_at == "extract_prepared" else "indexed")
                saved_ids = set(receipt.extraction_progress["succeeded_chunk_ids"])
                assert saved_ids
                operation = bound["context"]["operation_id"]
                input_id = bound["source"]["descriptor"]["source_id"]
                first_signature = models.calls[0]
                original.kill()
                assert await asyncio.to_thread(original.wait, timeout=5) == -9
                models.release.set()
                before_startup_http = list(http_calls)
                fresh = spawn("recover")
                startup = await wait_file("startup.json", fresh)
                assert all(job["status"] == "paused" for job in startup["jobs"])
                assert all(document["published"] is None for document in startup["documents"])
                assert startup["jobs"][0]["payload"]["process_request"] == bound
                assert all(job["payload"]["_scheduler"]["blocked_reason"] == "manual_recovery"
                           for job in startup["jobs"])
                await asyncio.sleep(0.3)
                calls = [json.loads(line) for line in (root / "sdk-calls.jsonl").read_text().splitlines()]
                startup_calls = [call["method"] for call in calls if call["phase"] == "recover"]
                assert startup_calls and set(startup_calls) == {"get_operation_status"}
                assert http_calls == before_startup_http
                (root / "command.json").write_text(json.dumps({"resume": [1, 0]}))
                peer = await wait_file("completed-1.json", fresh, timeout=100)
                before_a = Counter(models.calls)
                completed = await wait_file("completed-0.json", fresh, timeout=60)
                await wait_file("finished.json", fresh)
                assert await asyncio.to_thread(fresh.wait, timeout=10) == 0, (root / "recover.log").read_text()
                assert peer["input"] != input_id and "peer rollout" in "".join(peer["body"])
                assert completed["job"] == identities["jobs"][0]
                assert completed["payload"]["process_request"] == bound
                assert completed["input"] == input_id and completed["operation"] == operation
                assert "Milestone 0" in "".join(completed["body"])
                assert Counter(models.calls)[first_signature] == before_a[first_signature] == 1
                if kill_at == "extract_prepared":
                    assert Counter(models.calls) == before_a
                finished = await sdk.get_operation_status(operation)
                assert finished.status == "succeeded" and finished.checkpoint == "ready"
                assert saved_ids <= set(finished.extraction_progress["succeeded_chunk_ids"])
                async with sessions() as session:
                    source = await session.get(Source, identities["source"])
                    assert source.chunk_count == completed["chunks"] + peer["chunks"]
                    assert source.event_count == completed["events"] + peer["events"]
                (root / "acceptance.json").write_text(json.dumps({
                    "backend": backend, "kill_at": kill_at, "saved_chunks": sorted(saved_ids),
                    "model_calls": len(models.calls), "a": completed, "b": peer,
                }, ensure_ascii=False))
            finally:
                models.release.set()
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    await asyncio.to_thread(child.wait, timeout=5)
                for output in outputs:
                    output.close()
                if sdk is not None:
                    await sdk.aclose()
                await sql_engine.dispose()

    outcomes = await asyncio.gather(
        exercise("successful_chunk"), exercise("extract_prepared"), return_exceptions=True,
    )
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome
