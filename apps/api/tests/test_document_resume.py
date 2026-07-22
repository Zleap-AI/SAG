"""文档并发抽取的断点、暂停与继续行为。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_incremental_processor_pauses_after_inflight_chunks_and_resumes(monkeypatch):
    from sag_api.sag.dto import ProcessCheckpoint
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    processor = IncrementalDocumentProcessor(object(), "source-config", max_concurrency=2)
    active = 0
    peak_active = 0
    both_started = asyncio.Event()

    async def extract_chunk(chunk_id: str):
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        if active == 2:
            both_started.set()
        await both_started.wait()
        await asyncio.sleep(0)
        active -= 1
        return [f"event-{chunk_id}"], 100

    normalized: list[list[str]] = []
    restored: list[list[str]] = []

    async def normalize(chunk_ids: list[str]):
        normalized.append(chunk_ids)

    async def restore(event_ids: list[str]):
        restored.append(list(event_ids))

    monkeypatch.setattr(processor, "_extract_chunk", extract_chunk)
    monkeypatch.setattr(processor, "_normalize_event_ranks", normalize)
    monkeypatch.setattr(processor, "_restore_checkpoint_events", restore)

    snapshots: list[ProcessCheckpoint] = []
    pause_requested = False

    async def on_checkpoint(value: ProcessCheckpoint):
        nonlocal pause_requested
        snapshots.append(value)
        pause_requested = True

    async def should_pause():
        return pause_requested

    initial = ProcessCheckpoint(chunk_ids=["c1", "c2", "c3", "c4", "c5"])
    paused = await processor.process(
        None,
        checkpoint=initial,
        on_checkpoint=on_checkpoint,
        should_pause=should_pause,
    )

    assert peak_active == 2
    assert paused.paused is True
    assert len(paused.processed_chunk_ids) == 2
    assert paused.token_usage == 200
    assert normalized == []
    assert restored
    assert set(restored[-1]) == {"event-c1", "event-c2"}

    pause_requested = False
    resumed = await processor.process(
        None,
        checkpoint=snapshots[-1],
        on_checkpoint=lambda value: _append_checkpoint(snapshots, value),
        should_pause=should_pause,
    )

    assert resumed.paused is False
    assert set(resumed.processed_chunk_ids) == {"c1", "c2", "c3", "c4", "c5"}
    assert resumed.event_count == 5
    assert resumed.token_usage == 500
    assert normalized == [["c1", "c2", "c3", "c4", "c5"]]
    assert set(restored[-1]) == {
        "event-c1",
        "event-c2",
        "event-c3",
        "event-c4",
        "event-c5",
    }


@pytest.mark.asyncio
async def test_incremental_processor_restores_events_before_publishing_checkpoint(monkeypatch):
    """The graph must be able to read every event advertised by a live checkpoint."""
    from sag_api.sag.dto import ProcessCheckpoint
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    processor = IncrementalDocumentProcessor(object(), "source-config", max_concurrency=1)
    restored: set[str] = set()
    snapshots: list[ProcessCheckpoint] = []

    async def extract_chunk(chunk_id: str):
        return {
            "chunk-1": ["event-1", "event-2"],
            "chunk-2": ["event-3"],
        }[chunk_id], 10

    async def restore(event_ids: list[str]):
        restored.update(event_ids)

    async def publish(value: ProcessCheckpoint):
        # `on_checkpoint` persists document.event_count. Once it becomes visible to
        # the detail page, those same event ids must already be visible to /graph.
        assert set(value.event_ids) <= restored
        snapshots.append(value)

    async def no_op(_ids):
        return None

    monkeypatch.setattr(processor, "_extract_chunk", extract_chunk)
    monkeypatch.setattr(processor, "_restore_checkpoint_events", restore)
    monkeypatch.setattr(processor, "_normalize_event_ranks", no_op)

    outcome = await processor.process(
        None,
        checkpoint=ProcessCheckpoint(chunk_ids=["chunk-1", "chunk-2"]),
        on_checkpoint=publish,
        should_pause=_return_false,
    )

    assert [snapshot.event_count for snapshot in snapshots] == [2, 3]
    assert outcome.event_count == 3
    assert restored == {"event-1", "event-2", "event-3"}


@pytest.mark.asyncio
async def test_incremental_processor_passes_chunk_settings_to_zleap(monkeypatch):
    from sag_api.sag import incremental_processor as processor_module
    from sag_api.sag.dto import ProcessCheckpoint
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    seen = {}

    class FakeLoader:
        def __init__(self, *, parser=None):
            seen["fallback_title"] = parser.extract_title("普通正文，没有 Markdown 标题")
            seen["explicit_title"] = parser.extract_title("# 正文标题\n\n内容")

        async def load(self, config):
            seen["max_tokens"] = config.max_tokens
            seen["chunk_mode"] = config.chunk_mode
            return SimpleNamespace(source_id="document-1", chunk_ids=[])

    monkeypatch.setattr(processor_module, "DocumentLoader", FakeLoader)
    processor = IncrementalDocumentProcessor(
        object(),
        "source-config",
        max_concurrency=2,
        chunk_max_tokens=1_600,
        chunk_mode="heading_strict",
        document_title="人类简史",
    )

    outcome = await processor.process(
        "/tmp/book.md",
        checkpoint=ProcessCheckpoint(),
        on_checkpoint=lambda value: _append_checkpoint([], value),
        should_pause=lambda: _return_false(),
    )

    assert seen == {
        "fallback_title": "人类简史",
        "explicit_title": "正文标题",
        "max_tokens": 1_600,
        "chunk_mode": "heading_strict",
    }
    assert outcome.source_id == "document-1"


@pytest.mark.asyncio
async def test_incremental_processor_records_successful_eventless_chunks(monkeypatch):
    from sag_api.sag.dto import ProcessCheckpoint
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    processor = IncrementalDocumentProcessor(object(), "source-config", max_concurrency=1)

    async def extract_chunk(_chunk_id: str):
        return [], 42

    async def no_op(_ids):
        return None

    monkeypatch.setattr(processor, "_extract_chunk", extract_chunk)
    monkeypatch.setattr(processor, "_restore_checkpoint_events", no_op)
    monkeypatch.setattr(processor, "_normalize_event_ranks", no_op)
    snapshots: list[ProcessCheckpoint] = []

    outcome = await processor.process(
        None,
        checkpoint=ProcessCheckpoint(chunk_ids=["chunk-without-event"]),
        on_checkpoint=lambda value: _append_checkpoint(snapshots, value),
        should_pause=_return_false,
    )

    assert outcome.processed_chunk_ids == ["chunk-without-event"]
    assert outcome.eventless_chunk_ids == ["chunk-without-event"]
    assert outcome.event_count == 0
    assert snapshots[-1].eventless_chunk_ids == ["chunk-without-event"]


@pytest.mark.asyncio
async def test_incremental_processor_unwraps_taskgroup_chunk_failure(monkeypatch):
    from zleap.sag.exceptions import ExtractError

    from sag_api.sag.dto import ProcessCheckpoint
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    processor = IncrementalDocumentProcessor(object(), "source-config", max_concurrency=2)

    async def extract_chunk(chunk_id: str):
        if chunk_id == "broken":
            raise ExtractError("结构化输出达到上限并被截断")
        await asyncio.sleep(0.01)
        return ["event-ok"], 10

    async def no_op(_ids):
        return None

    monkeypatch.setattr(processor, "_extract_chunk", extract_chunk)
    monkeypatch.setattr(processor, "_restore_checkpoint_events", no_op)
    monkeypatch.setattr(processor, "_normalize_event_ranks", no_op)

    with pytest.raises(ExtractError, match="达到上限并被截断"):
        await processor.process(
            None,
            checkpoint=ProcessCheckpoint(chunk_ids=["broken", "other"]),
            on_checkpoint=lambda value: _append_checkpoint([], value),
            should_pause=_return_false,
        )


@pytest.mark.asyncio
async def test_extract_chunk_tracks_tokens_from_wrapped_llm_client(monkeypatch):
    from sag_api.sag import incremental_processor as processor_module
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    class FakeLeafClient:
        async def chat(self, messages, **kwargs):
            return SimpleNamespace(
                content='{"data": {"items": []}}',
                usage=SimpleNamespace(total_tokens=321),
            )

    class FakeRetryClient:
        def __init__(self):
            self.client = FakeLeafClient()

        async def chat(self, messages, **kwargs):
            return await self.client.chat(messages, **kwargs)

    class FakeExtractor:
        def __init__(self, **kwargs):
            self.client = FakeRetryClient()

        async def _get_llm_client(self):
            return self.client

        async def extract(self, config):
            assert "观点、事实、定义" in config.custom_requirements
            assert config.enable_strict_filtering is False
            # zleap-sag 的重试客户端会让结构化输出直接调用内层客户端。
            await self.client.client.chat([SimpleNamespace(content="西游记")])
            return [SimpleNamespace(id="event-1")]

    monkeypatch.setattr(processor_module, "EventExtractor", FakeExtractor)
    engine = SimpleNamespace(_extractor=SimpleNamespace(prompt_manager=object(), model_config={}))
    processor = IncrementalDocumentProcessor(engine, "source-config", max_concurrency=1)

    event_ids, token_usage = await processor._extract_chunk("chunk-1")

    assert event_ids == ["event-1"]
    assert token_usage == 321


@pytest.mark.asyncio
async def test_extract_chunk_normalizes_unambiguous_entity_type_alias(monkeypatch):
    import json

    from sag_api.sag import incremental_processor as processor_module
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    payload = {
        "type": "response",
        "data": {
            "items": [
                {
                    "entities": [
                        {
                            "location": "中东",
                            "name": "中东",
                            "description": "尼安德特人演化的主要地区之一",
                        }
                    ]
                }
            ],
            "meta": {"reason": "ok"},
        },
    }

    class FakeLeafClient:
        async def chat(self, messages, **kwargs):
            return SimpleNamespace(
                content=json.dumps(payload, ensure_ascii=False),
                usage=SimpleNamespace(total_tokens=42),
            )

    class FakeRetryClient:
        def __init__(self):
            self.client = FakeLeafClient()

    class FakeExtractor:
        def __init__(self, **kwargs):
            self.client = FakeRetryClient()

        async def _get_llm_client(self):
            return self.client

        async def extract(self, config):
            request = {"data": {"meta": {"entity_types": [{"type": "location", "description": "地点"}]}}}
            response = await self.client.client.chat([SimpleNamespace(content=json.dumps(request, ensure_ascii=False))])
            entity = json.loads(response.content)["data"]["items"][0]["entities"][0]
            assert entity == {
                "name": "中东",
                "description": "尼安德特人演化的主要地区之一",
                "type": "location",
            }
            return [SimpleNamespace(id="event-1")]

    monkeypatch.setattr(processor_module, "EventExtractor", FakeExtractor)
    engine = SimpleNamespace(_extractor=SimpleNamespace(prompt_manager=object(), model_config={}))
    processor = IncrementalDocumentProcessor(engine, "source-config", max_concurrency=1)

    event_ids, token_usage = await processor._extract_chunk("chunk-1")

    assert event_ids == ["event-1"]
    assert token_usage == 42


def test_extraction_response_does_not_guess_ambiguous_entity_type():
    import json

    from sag_api.sag.incremental_processor import _normalize_extraction_response

    payload = {
        "data": {
            "items": [
                {
                    "entities": [
                        {
                            "location": "中东",
                            "region": "西亚",
                            "name": "中东",
                            "description": "地区",
                        }
                    ]
                }
            ]
        }
    }
    original = json.dumps(payload, ensure_ascii=False)
    response = SimpleNamespace(content=original)

    assert _normalize_extraction_response(response, {"location", "region"}) == 0
    assert response.content == original


def test_extraction_response_does_not_invent_unknown_entity_type():
    import json

    from sag_api.sag.incremental_processor import _normalize_extraction_response

    payload = {
        "data": {
            "items": [
                {
                    "entities": [
                        {
                            "unknown": "中东",
                            "name": "中东",
                            "description": "地区",
                        }
                    ]
                }
            ]
        }
    }
    original = json.dumps(payload, ensure_ascii=False)
    response = SimpleNamespace(content=original)

    assert _normalize_extraction_response(response, {"location"}) == 0
    assert response.content == original


@pytest.mark.asyncio
async def test_extract_chunk_raises_when_sag_swallows_chunk_failure(monkeypatch):
    from sag_api.sag import incremental_processor as processor_module
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    class FakeClient:
        async def chat(self, messages, **kwargs):
            return SimpleNamespace(content="{}", usage=SimpleNamespace(total_tokens=23))

    class SwallowingExtractor:
        def __init__(self, **kwargs):
            self.client = FakeClient()

        async def _get_llm_client(self):
            return self.client

        async def extract_from_chunk(self, chunk, config):
            await self.client.chat([SimpleNamespace(content="book")])
            raise RuntimeError("response schema is invalid")

        async def extract(self, config):
            try:
                await self.extract_from_chunk(SimpleNamespace(id=config.chunk_ids[0]), config)
            except Exception:
                # Mirrors zleap-sag 0.7.x: the batch helper logs a chunk failure
                # and returns an empty event list instead of propagating it.
                return []
            raise AssertionError("expected the fake chunk to fail")

    monkeypatch.setattr(processor_module, "EventExtractor", SwallowingExtractor)
    engine = SimpleNamespace(_extractor=SimpleNamespace(prompt_manager=object(), model_config={}))
    processor = IncrementalDocumentProcessor(engine, "source-config", max_concurrency=1)

    with pytest.raises(RuntimeError, match="response schema is invalid"):
        await processor._extract_chunk("chunk-1")


async def _return_false():
    return False


async def _append_checkpoint(snapshots, value):
    snapshots.append(value)


@pytest.mark.asyncio
async def test_pause_and_resume_document_service():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import pause_document, resume_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="resume-source", sag_source_config_id="resume-source-config")
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="resume.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/resume.md",
            status=DocumentStatus.EXTRACTING,
            progress=52,
            token_usage=12_000,
        )
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.RUNNING,
            source_id=source.id,
            document_id=document.id,
            progress=0.52,
            payload={
                "process_checkpoint": {
                    "source_id": "engine-source",
                    "chunk_ids": ["c1", "c2"],
                    "processed_chunk_ids": ["c1"],
                    "event_count": 1,
                    "event_ids": ["e1"],
                    "token_usage": 12_000,
                }
            },
        )
        session.add(job)
        await session.commit()

        paused_job = await pause_document(session, source, document.id)
        assert paused_job.status == JobStatus.RUNNING
        assert paused_job.payload["pause_requested"] is True

        paused_job.status = JobStatus.PAUSED
        document.status = DocumentStatus.PAUSED
        await session.commit()

        queue = FakeQueue()
        resumed_job = await resume_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )
        assert resumed_job.status == JobStatus.QUEUED
        assert resumed_job.payload["resume_requested"] is True
        assert "pause_requested" not in resumed_job.payload
        assert document.status == DocumentStatus.EXTRACTING
        assert document.progress == 52 and document.token_usage == 12_000
        assert queue.ids == [job.id]

        queued_document = Document(
            source_id=source.id,
            filename="queued.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/queued.md",
            status=DocumentStatus.PENDING,
        )
        session.add(queued_document)
        await session.flush()
        queued_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=queued_document.id,
        )
        session.add(queued_job)
        await session.commit()

        stopped_before_start = await pause_document(session, source, queued_document.id)
        assert stopped_before_start.status == JobStatus.PAUSED
        assert queued_document.status == DocumentStatus.PAUSED


@pytest.mark.asyncio
async def test_reprocess_ready_document_replaces_all_previous_derived_data():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import reprocess_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

    class FakeEngineManager:
        def __init__(self):
            self.deleted: list[str] = []

        async def delete_document_data(self, source_config_id, document_source_id, *, source):
            assert source_config_id == source.sag_source_config_id
            self.deleted.append(document_source_id)

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="replace-source",
            sag_source_config_id="replace-source-config",
            document_count=2,
            chunk_count=99,
            event_count=88,
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="book.txt",
            content_type="text/plain",
            size_bytes=100,
            storage_path="/tmp/book.txt",
            status=DocumentStatus.READY,
            progress=100,
            chunk_count=3,
            event_count=2,
            token_usage=500,
            sag_source_id="engine-latest",
        )
        other = Document(
            source_id=source.id,
            filename="other.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/other.md",
            status=DocumentStatus.READY,
            progress=100,
            chunk_count=4,
            event_count=5,
            sag_source_id="engine-other",
        )
        session.add_all([document, other])
        await session.flush()
        session.add_all(
            [
                Job(
                    type=JobType.PROCESS_DOCUMENT,
                    status=JobStatus.SUCCEEDED,
                    source_id=source.id,
                    document_id=document.id,
                    payload={"process_checkpoint": {"source_id": "engine-old"}},
                ),
                Job(
                    type=JobType.PROCESS_DOCUMENT,
                    status=JobStatus.SUCCEEDED,
                    source_id=source.id,
                    document_id=document.id,
                    payload={"process_checkpoint": {"source_id": "engine-latest"}},
                ),
            ]
        )
        await session.commit()

        queue = FakeQueue()
        engine = FakeEngineManager()
        job = await reprocess_document(
            session,
            source,
            document.id,
            job_queue=queue,
            engine_manager=engine,
        )

        assert set(engine.deleted) == {"engine-old", "engine-latest"}
        assert document.status == DocumentStatus.PENDING
        assert document.progress == 0
        assert document.chunk_count == 0 and document.event_count == 0
        assert document.token_usage == 0 and document.sag_source_id is None
        assert source.document_count == 2
        assert source.chunk_count == 4 and source.event_count == 5
        assert job.payload == {} and queue.ids == [job.id]

        # Source rows are shared across this module's SQLite test database; do
        # not leave a freshly reprocessed source that would make universe-cache
        # contract tests correctly report their manifest as stale.
        await session.delete(source)
        await session.commit()


@pytest.mark.asyncio
async def test_job_pause_is_not_failure_or_retry(monkeypatch):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Job
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs.control import JobPaused
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.tasks import TASK_HANDLERS

    calls = 0

    async def handler(_session, _job, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise JobPaused()

    monkeypatch.setitem(TASK_HANDLERS, JobType.PROCESS_DOCUMENT, handler)
    await init_db()
    async with SessionLocal() as session:
        job = Job(type=JobType.PROCESS_DOCUMENT, status=JobStatus.QUEUED)
        session.add(job)
        await session.commit()
        job_id = job.id

    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    await queue._run(job_id)
    async with SessionLocal() as session:
        paused = await session.get(Job, job_id)
        assert paused.status == JobStatus.PAUSED
        assert paused.attempts == 1
        assert paused.error is None
        paused.status = JobStatus.QUEUED
        paused.payload = {**(paused.payload or {}), "resume_requested": True}
        paused.progress = 0.4
        await session.commit()

    await queue._run(job_id)
    async with SessionLocal() as session:
        done = await session.get(Job, job_id)
        assert done.status == JobStatus.SUCCEEDED
        assert done.attempts == 1
        assert calls == 2


@pytest.mark.asyncio
async def test_duplicate_queue_entries_claim_job_once(monkeypatch):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Job
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.tasks import TASK_HANDLERS

    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_session, _job, **_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    monkeypatch.setitem(TASK_HANDLERS, JobType.PROCESS_DOCUMENT, handler)
    await init_db()
    async with SessionLocal() as session:
        job = Job(type=JobType.PROCESS_DOCUMENT, status=JobStatus.QUEUED)
        session.add(job)
        await session.commit()
        job_id = job.id

    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=2)
    first = asyncio.create_task(queue._run(job_id))
    second = asyncio.create_task(queue._run(job_id))
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    await asyncio.gather(first, second)

    async with SessionLocal() as session:
        done = await session.get(Job, job_id)
        assert done.status == JobStatus.SUCCEEDED
        assert done.attempts == 1
        assert calls == 1
