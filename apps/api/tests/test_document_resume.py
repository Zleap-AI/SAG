"""文档并发抽取的断点、暂停与继续行为。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sag_api.enums import DocumentStatus


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hidden_status",
    [DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED],
)
async def test_public_document_reads_hide_logically_deleted_rows(hidden_status):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import NotFoundError
    from sag_api.db.models import Document, Source
    from sag_api.services.document_service import get_document, get_public_document

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name=f"hidden-read-{uuid4().hex}",
            sag_source_config_id=(f"hidden-read-config-{uuid4().hex}")[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="hidden.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/hidden.md",
            status=hidden_status,
        )
        session.add(document)
        await session.commit()

        assert await get_document(session, source, document.id) is not None
        with pytest.raises(NotFoundError, match="文档不存在"):
            await get_public_document(session, source, document.id)



@pytest.mark.asyncio
async def _processor_with_fake_engine(
    *,
    ingest=None,
    extract=None,
    max_concurrency=30,
    chunk_mode="standard",
    document_title="doc",
    language="zh",
):
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    engine = SimpleNamespace(
        ingest=ingest,
        extract=extract,
        resources=SimpleNamespace(prompts=SimpleNamespace(language=language)),
    )
    processor = IncrementalDocumentProcessor(
        engine,
        "source-config",
        max_concurrency=max_concurrency,
        chunk_max_tokens=1000,
        chunk_mode=chunk_mode,
        document_title=document_title,
    )
    return processor


def _chunk_set_ref(*, chunk_ids=("c1", "c2"), generation_id="gen-1"):
    from zleap.sag.pipeline import ChunkSetRef, SourceType, WriteStatus

    return ChunkSetRef(
        data_source_id="source-config",
        source_type=SourceType.ARTICLE,
        source_id="article-1",
        source_version="sv-1",
        chunk_version="cv-1",
        generation_id=generation_id,
        chunk_ids=tuple(chunk_ids),
        client_key_to_chunk_id={},
        relation_status=WriteStatus.SUCCEEDED,
        vector_status=WriteStatus.SUCCEEDED,
    )


def _event_ref(*, event_ids=("e1",), stats=None, zero_chunks=()):
    from types import SimpleNamespace

    return SimpleNamespace(
        event_ids=tuple(event_ids),
        event_count=len(event_ids),
        stats=dict(stats or {"zero_event_chunks": list(zero_chunks)}),
    )


@pytest.mark.asyncio
async def test_ingest_builds_generation_checkpoint_and_outcome():
    """0.8.2:ingest 返回的 ChunkSetRef 定位信息(generation/version)固化进断点。"""
    from sag_api.sag.dto import ProcessCheckpoint, ProcessOutcome

    captured: dict = {}

    async def ingest(path, *, descriptor, chunk_options, index_options):
        captured["path"] = path
        captured["chunk_options"] = chunk_options
        captured["index_options"] = index_options
        assert descriptor.title == "doc"
        return _chunk_set_ref()

    async def extract(chunk_set, options, *, observer, cancellation):
        captured["extract_chunk_ids"] = chunk_set.chunk_ids
        captured["options"] = options
        return _event_ref()

    processor = await _processor_with_fake_engine(ingest=ingest, extract=extract)
    snapshots: list[ProcessCheckpoint] = []

    async def on_checkpoint(value):
        snapshots.append(value.model_copy(deep=True))

    outcome = await processor.process(
        "/tmp/doc.md",
        checkpoint=ProcessCheckpoint(),
        on_checkpoint=on_checkpoint,
        should_pause=_return_false,
    )

    assert isinstance(outcome, ProcessOutcome)
    assert outcome.source_id == "article-1"
    assert outcome.chunk_count == 2
    assert outcome.event_count == 1
    assert outcome.processed_chunk_ids == ["c1", "c2"]
    assert outcome.paused is False
    # ingest 断点:generation 信息已持久化
    assert snapshots[0].generation_id == "gen-1"
    assert snapshots[0].chunk_version == "cv-1"
    assert snapshots[0].source_version == "sv-1"
    assert snapshots[0].chunk_ids == ["c1", "c2"]
    assert captured["chunk_options"].max_tokens == 1000
    assert captured["chunk_options"].strategy == "standard"


@pytest.mark.asyncio
async def test_extract_receives_contract_limits_and_concurrency():
    """0.8.2:实体契约与并发经 ExtractionOptions 传给引擎,不再由 SAG 拦截。"""
    from sag_api.sag.dto import ProcessCheckpoint

    captured: dict = {}

    async def extract(chunk_set, options, *, observer, cancellation):
        captured["options"] = options
        return _event_ref()

    processor = await _processor_with_fake_engine(extract=extract, max_concurrency=30)

    await processor.process(
        None,
        checkpoint=ProcessCheckpoint(
            source_id="article-1",
            chunk_ids=["c1", "c2"],
            generation_id="gen-1",
            chunk_version="cv-1",
            source_version="sv-1",
        ),
        on_checkpoint=_noop_checkpoint,
        should_pause=_return_false,
    )

    options = captured["options"]
    assert options.contract == "rich"
    assert options.source_type == "article"
    assert options.limits.min_entities_per_event == 1
    assert options.execution.max_concurrency == 30
    assert "观点、事实、定义" in options.guidance_rules[0]  # 默认中文知识型事项要求仍然透传


@pytest.mark.asyncio
async def test_extract_guidance_matches_english_engine_prompt_language():
    """英文引擎不能继续收到中文的 SAG 业务规则。"""
    from sag_api.sag.dto import ProcessCheckpoint

    captured: dict = {}

    async def extract(chunk_set, options, *, observer, cancellation):
        captured["options"] = options
        return _event_ref()

    processor = await _processor_with_fake_engine(extract=extract, language="en")

    await processor.process(
        None,
        checkpoint=ProcessCheckpoint(
            source_id="article-1",
            chunk_ids=["c1", "c2"],
            generation_id="gen-1",
            chunk_version="cv-1",
            source_version="sv-1",
        ),
        on_checkpoint=_noop_checkpoint,
        should_pause=_return_false,
    )

    (guidance,) = captured["options"].guidance_rules
    assert "For books, reports, papers" in guidance
    assert "观点、事实、定义" not in guidance


@pytest.mark.asyncio
async def test_progress_observer_updates_checkpoint():
    """0.8.2:EXTRACT 的 PROGRESS 事件映射为断点进度(节流写入)。"""
    from zleap.sag.pipeline.events import StageEvent, StageEventType, StageName

    from sag_api.sag.dto import ProcessCheckpoint

    received: list[StageEvent] = []

    async def extract(chunk_set, options, *, observer, cancellation):
        await observer(
            StageEvent(run_id="r", stage=StageName.EXTRACT, type=StageEventType.PROGRESS, completed=1, total=2)
        )
        await observer(
            StageEvent(run_id="r", stage=StageName.EXTRACT, type=StageEventType.PROGRESS, completed=2, total=2)
        )
        received.extend([])
        return _event_ref()

    processor = await _processor_with_fake_engine(extract=extract)
    snapshots: list[ProcessCheckpoint] = []

    async def on_checkpoint(value):
        snapshots.append(value.model_copy(deep=True))

    outcome = await processor.process(
        None,
        checkpoint=ProcessCheckpoint(
            source_id="article-1",
            chunk_ids=["c1", "c2"],
            generation_id="gen-1",
            chunk_version="cv-1",
            source_version="sv-1",
        ),
        on_checkpoint=on_checkpoint,
        should_pause=_return_false,
    )

    # 完成事件 + 最终落盘断点
    progress_snapshots = [s for s in snapshots if len(s.processed_chunk_ids) == 2]
    assert progress_snapshots, "PROGRESS 完成事件应触发断点写入"
    assert outcome.processed_chunk_ids == ["c1", "c2"]


@pytest.mark.asyncio
async def test_pause_via_cancellation_returns_paused_outcome():
    """0.8.2:暂停经 CancellationToken 驱动,取消后返回 paused 结果。"""
    from zleap.sag.pipeline.errors import PipelineCancelledError

    from sag_api.sag.dto import ProcessCheckpoint

    async def extract(chunk_set, options, *, observer, cancellation):
        while not cancellation.is_cancelled:
            await asyncio.sleep(0.01)
        raise PipelineCancelledError("流水线已取消", stage="extract", run_id="r", code="pipeline_cancelled")

    processor = await _processor_with_fake_engine(extract=extract)

    async def should_pause():
        return True

    outcome = await processor.process(
        None,
        checkpoint=ProcessCheckpoint(
            source_id="article-1",
            chunk_ids=["c1", "c2"],
            generation_id="gen-1",
            chunk_version="cv-1",
            source_version="sv-1",
        ),
        on_checkpoint=_append_checkpoint,
        should_pause=should_pause,
    )

    assert outcome.paused is True
    assert outcome.event_count == 0


@pytest.mark.asyncio
async def test_resume_skips_ingest_and_rebuilds_chunk_set():
    """断点携带 generation 信息时,恢复路径不再重新 ingest。"""
    from sag_api.sag.dto import ProcessCheckpoint

    calls: list[str] = []

    async def ingest(*_args, **_kwargs):
        calls.append("ingest")
        raise AssertionError("resume must not re-ingest")

    captured: dict = {}

    async def extract(chunk_set, options, *, observer, cancellation):
        captured["chunk_set"] = chunk_set
        return _event_ref(event_ids=("e1", "e2"), stats={})

    processor = await _processor_with_fake_engine(ingest=ingest, extract=extract)

    outcome = await processor.process(
        None,
        checkpoint=ProcessCheckpoint(
            source_id="article-1",
            chunk_ids=["c1", "c2"],
            generation_id="gen-1",
            chunk_version="cv-1",
            source_version="sv-1",
        ),
        on_checkpoint=_noop_checkpoint,
        should_pause=_return_false,
    )

    assert calls == []
    assert captured["chunk_set"].data_source_id == "source-config"
    assert captured["chunk_set"].source_id == "article-1"
    assert captured["chunk_set"].chunk_ids == ("c1", "c2")
    assert captured["chunk_set"].generation_id == "gen-1"
    assert outcome.event_count == 2


@pytest.mark.asyncio
async def test_ingest_without_generation_continues_non_durable_extract():
    """0.10.0 普通 ingest 的 generation_id=None 仍必须继续 Extract。"""
    from sag_api.sag.dto import ProcessCheckpoint

    captured: dict = {}

    async def ingest(path, *, descriptor, chunk_options, index_options):
        return _chunk_set_ref(chunk_ids=("c1",), generation_id=None)

    async def extract(chunk_set, options, *, observer, cancellation):
        captured["chunk_set"] = chunk_set
        return _event_ref(event_ids=("e1",), stats={})

    processor = await _processor_with_fake_engine(ingest=ingest, extract=extract)
    snapshots: list[ProcessCheckpoint] = []

    async def on_checkpoint(value):
        snapshots.append(value.model_copy(deep=True))

    outcome = await processor.process(
        "/tmp/fresh-upload.md",
        checkpoint=ProcessCheckpoint(),
        on_checkpoint=on_checkpoint,
        should_pause=_return_false,
    )

    assert snapshots[0].generation_id is None
    assert snapshots[0].chunk_version == "cv-1"
    assert captured["chunk_set"].generation_id is None
    assert captured["chunk_set"].chunk_version == "cv-1"
    assert outcome.event_count == 1


@pytest.mark.asyncio
async def test_checkpoint_without_chunk_version_raises():
    """真正缺少 chunk_version 的旧断点不能伪造 ChunkSetRef。"""
    from sag_api.sag.dto import ProcessCheckpoint

    async def extract(*_args, **_kwargs):
        raise AssertionError("should not extract")

    processor = await _processor_with_fake_engine(extract=extract)

    with pytest.raises(RuntimeError, match="chunk_version"):
        await processor.process(
            None,
            checkpoint=ProcessCheckpoint(source_id="article-1", chunk_ids=["c1"]),
            on_checkpoint=_noop_checkpoint,
            should_pause=_return_false,
        )


@pytest.mark.asyncio
async def test_zero_event_chunks_mapped_from_stats():
    """0.8.2:无事件分块经 stats.zero_event_chunks 映射,不再逐块计数。"""
    from sag_api.sag.dto import ProcessCheckpoint

    async def extract(chunk_set, options, *, observer, cancellation):
        return _event_ref(event_ids=(), zero_chunks=["c2"])

    processor = await _processor_with_fake_engine(extract=extract)

    outcome = await processor.process(
        None,
        checkpoint=ProcessCheckpoint(
            source_id="article-1",
            chunk_ids=["c1", "c2"],
            generation_id="gen-1",
            chunk_version="cv-1",
            source_version="sv-1",
        ),
        on_checkpoint=_noop_checkpoint,
        should_pause=_return_false,
    )

    assert outcome.eventless_chunk_ids == ["c2"]
    assert outcome.event_count == 0
    assert outcome.paused is False


@pytest.mark.asyncio
async def test_extraction_batch_failure_propagates():
    """zleap 批次失败必须上抛(带 layer/stage 由 EngineManager 翻译),不得吞掉。"""
    from zleap.sag.pipeline.errors import PipelineError

    from sag_api.sag.dto import ProcessCheckpoint

    async def extract(chunk_set, options, *, observer, cancellation):
        raise PipelineError("chunk 重试耗尽", stage="extract", code="chunk_retry_exhausted")

    processor = await _processor_with_fake_engine(extract=extract)

    with pytest.raises(PipelineError):
        await processor.process(
            None,
            checkpoint=ProcessCheckpoint(
                source_id="article-1",
                chunk_ids=["c1"],
                generation_id="gen-1",
                chunk_version="cv-1",
                source_version="sv-1",
            ),
            on_checkpoint=_append_checkpoint,
            should_pause=_return_false,
        )


def test_event_entity_attempt_setting_is_bounded():
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    engine = SimpleNamespace()
    low = IncrementalDocumentProcessor(engine, "c", max_concurrency=1, event_entity_attempts=0)
    high = IncrementalDocumentProcessor(engine, "c", max_concurrency=1, event_entity_attempts=99)
    assert low._event_entity_attempts == 1
    assert high._event_entity_attempts == 3


async def _return_false():
    return False


async def _noop_checkpoint(_value):
    return None


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
        source = Source(name="resume-source", sag_source_config_id="resume-source-config"[:36])
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
                    "generation_id": None,
                    "chunk_version": "chunk-version-1",
                    "source_version": "source-version-1",
                }
            },
        )
        session.add(job)
        await session.commit()

        paused_job = await pause_document(session, source, document.id)
        assert paused_job.status == JobStatus.PAUSED
        await session.refresh(document)
        assert document.status == DocumentStatus.PAUSING

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
        assert resumed_job.payload["process_checkpoint"]["generation_id"] is None
        assert resumed_job.payload["process_checkpoint"]["chunk_version"] == "chunk-version-1"
        assert resumed_job.payload["_scheduler"]["priority"] == 10
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
async def test_resume_legacy_checkpoint_restarts_from_file_or_blocks_without_it(tmp_path):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import resume_document

    class FakeQueue:
        def __init__(self):
            self.ids = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

    await init_db()
    source_file = tmp_path / "legacy.md"
    source_file.write_text("legacy", encoding="utf-8")
    async with SessionLocal() as session:
        source = Source(
            name=f"legacy-resume-{uuid4().hex}",
            sag_source_config_id=(f"legacy-resume-{uuid4().hex}")[:36],
        )
        session.add(source)
        await session.flush()
        documents = [
            Document(
                source_id=source.id,
                filename="legacy.md",
                storage_path=str(source_file),
                status=DocumentStatus.PAUSED,
            ),
            Document(
                source_id=source.id,
                filename="missing.md",
                storage_path=str(tmp_path / "missing.md"),
                status=DocumentStatus.PAUSED,
            ),
        ]
        session.add_all(documents)
        await session.flush()
        jobs = [
            Job(
                type=JobType.PROCESS_DOCUMENT,
                status=JobStatus.PAUSED,
                source_id=source.id,
                document_id=document.id,
                payload={
                    "process_checkpoint": {
                        "source_id": "legacy-source",
                        "chunk_ids": ["chunk-1"],
                    }
                },
            )
            for document in documents
        ]
        session.add_all(jobs)
        await session.commit()

        queue = FakeQueue()
        resumed = await resume_document(
            session, source, documents[0].id, job_queue=queue
        )
        assert "process_checkpoint" not in resumed.payload
        assert documents[0].status is DocumentStatus.PENDING
        assert queue.ids == [jobs[0].id]

        with pytest.raises(ConflictError, match="原文件"):
            await resume_document(session, source, documents[1].id, job_queue=queue)
        await session.refresh(jobs[1])
        assert jobs[1].status is JobStatus.PAUSED


@pytest.mark.asyncio
async def test_delete_document_persists_deleting_and_queues_cleanup():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []
            self.maintenance: list[tuple[str, str]] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, source_id: str, job_id: str):
            self.maintenance.append((source_id, job_id))

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="delete-source", sag_source_config_id="delete-source-config"[:36])
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="deleting.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/deleting.md",
            status=DocumentStatus.EXTRACTING,
        )
        session.add(document)
        await session.flush()
        process_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.RUNNING,
        )
        session.add(process_job)
        await session.commit()

        queue = FakeQueue()
        delete_job = await delete_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )

        await session.refresh(document)
        await session.refresh(process_job)
        assert document.status == DocumentStatus.DELETING
        assert process_job.payload["pause_requested"] is True
        assert delete_job.type == JobType.DELETE_DOCUMENT
        assert delete_job.status == JobStatus.QUEUED
        assert delete_job.payload["_scheduler"]["priority"] == 0
        assert queue.ids == [delete_job.id]
        assert queue.maintenance == [(source.id, delete_job.id)]

        # Idempotent retries must also repair a stale visible state instead of
        # returning an active delete job while the document appears extracting.
        document.status = DocumentStatus.EXTRACTING
        await session.commit()
        repeated = await delete_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )
        await session.refresh(document)
        assert repeated.id == delete_job.id
        assert document.status == DocumentStatus.DELETING


@pytest.mark.asyncio
async def test_concurrent_delete_requests_share_one_cleanup_job():
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []
            self.maintenance: list[tuple[str, str]] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, source_id: str, job_id: str):
            self.maintenance.append((source_id, job_id))

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="concurrent-delete-request",
            sag_source_config_id="concurrent-delete-request-config"[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="extracting.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/extracting.md",
            status=DocumentStatus.EXTRACTING,
            sag_source_id="engine-extracting",
        )
        session.add(document)
        await session.flush()
        session.add(
            Job(
                type=JobType.PROCESS_DOCUMENT,
                source_id=source.id,
                document_id=document.id,
                status=JobStatus.RUNNING,
            )
        )
        await session.commit()
        source_id, document_id = source.id, document.id

    queue = FakeQueue()

    async def remove():
        async with SessionLocal() as session:
            source = await session.get(Source, source_id)
            return await delete_document(
                session,
                source,
                document_id,
                job_queue=queue,
            )

    first, second = await asyncio.gather(remove(), remove())

    async with SessionLocal() as session:
        jobs = list(
            (
                await session.scalars(
                    select(Job).where(
                        Job.document_id == document_id,
                        Job.type == JobType.DELETE_DOCUMENT,
                    )
                )
            ).all()
        )
    assert first.id == second.id
    assert [job.id for job in jobs] == [first.id]
    assert set(queue.ids) == {first.id}
    assert set(queue.maintenance) == {(source_id, first.id)}


@pytest.mark.asyncio
async def test_pause_and_resume_only_control_process_jobs():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import pause_document, resume_document

    class FakeQueue:
        async def enqueue(self, _job_id: str):
            raise AssertionError("delete jobs must never be resumed as extraction jobs")

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="action-guards", sag_source_config_id="action-guards-config"[:36])
        session.add(source)
        await session.flush()
        deleting = Document(
            source_id=source.id,
            filename="deleting.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/deleting.md",
            status=DocumentStatus.DELETING,
        )
        paused = Document(
            source_id=source.id,
            filename="paused.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/paused.md",
            status=DocumentStatus.PAUSED,
        )
        session.add_all([deleting, paused])
        await session.flush()
        running_delete = Job(
            type=JobType.DELETE_DOCUMENT,
            source_id=source.id,
            document_id=deleting.id,
            status=JobStatus.RUNNING,
        )
        paused_delete = Job(
            type=JobType.DELETE_DOCUMENT,
            source_id=source.id,
            document_id=paused.id,
            status=JobStatus.PAUSED,
        )
        session.add_all([running_delete, paused_delete])
        await session.commit()

        with pytest.raises(ConflictError):
            await pause_document(session, source, deleting.id)
        with pytest.raises(ConflictError):
            await resume_document(session, source, paused.id, job_queue=FakeQueue())

        await session.refresh(running_delete)
        await session.refresh(paused_delete)
        assert running_delete.status == JobStatus.RUNNING
        assert "pause_requested" not in running_delete.payload
        assert paused_delete.status == JobStatus.PAUSED


@pytest.mark.asyncio
async def test_delete_document_job_removes_document_after_processing_stops(tmp_path, monkeypatch):
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.services import universe_service

    class FakeEngine:
        def __init__(self):
            self.deleted: list[str] = []

        async def delete_document_data(self, _config_id, document_source_id, *, source):
            assert source.sag_source_config_id == "delete-worker-config"
            self.deleted.append(document_source_id)

    await init_db()

    async def partially_scheduled_refresh(session, _job_queue, *, source_id, reason):
        session.add(
            Job(
                type=JobType.INDEX_UNIVERSE,
                source_id=source_id,
                status=JobStatus.QUEUED,
                payload={"reason": reason},
            )
        )
        await session.flush()
        raise RuntimeError("refresh scheduling failed")

    monkeypatch.setattr(universe_service, "schedule_universe_refresh", partially_scheduled_refresh)
    path = tmp_path / "deleting.md"
    path.write_text("content")
    async with SessionLocal() as session:
        source = Source(name="delete-worker", sag_source_config_id="delete-worker-config"[:36])
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="deleting.md",
            content_type="text/markdown",
            size_bytes=7,
            storage_path=str(path),
            status=DocumentStatus.DELETING,
            sag_source_id="engine-document",
        )
        session.add(document)
        await session.flush()
        other_document = Document(
            source_id=source.id,
            filename="keep.md",
            content_type="text/markdown",
            size_bytes=4,
            storage_path=str(tmp_path / "keep.md"),
            status=DocumentStatus.EXTRACTING,
        )
        session.add(other_document)
        await session.flush()
        blocked_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=other_document.id,
            status=JobStatus.QUEUED,
            payload={
                "_scheduler": {
                    "priority": 50,
                    "blocked_reason": "source_maintenance",
                }
            },
        )
        session.add(blocked_job)
        delete_job = Job(
            type=JobType.DELETE_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.QUEUED,
        )
        session.add(delete_job)
        await session.commit()
        document_id, delete_job_id, blocked_job_id = document.id, delete_job.id, blocked_job.id
        source_id = source.id

    engine = FakeEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=1)
    queue.begin_source_maintenance(source_id, delete_job_id)
    await queue._run(delete_job_id)

    async with SessionLocal() as session:
        assert await session.get(Document, document_id) is None
        completed_delete = await session.get(Job, delete_job_id)
        assert completed_delete is not None
        assert completed_delete.status == JobStatus.SUCCEEDED
        assert completed_delete.document_id is None
        assert completed_delete.payload["target_document_id"] == document_id
        source = await session.scalar(select(Source).where(Source.name == "delete-worker"))
        assert source is not None and source.document_count == 1
        resumed = await session.get(Job, blocked_job_id)
        assert resumed.status == JobStatus.QUEUED
        assert resumed.payload["resume_requested"] is True
        assert resumed.payload["_scheduler"] == {"priority": 10}
        universe_jobs = list(
            (
                await session.scalars(
                    select(Job).where(Job.type == JobType.INDEX_UNIVERSE)
                )
            ).all()
        )
        assert universe_jobs == []
    assert engine.deleted == ["engine-document"]
    assert not path.exists()
    assert queue.source_maintenance_requested(source_id) is False
    queued_ids: list[str] = []
    while not queue._queue.empty():
        queued_ids.append((await queue._queue.get())[-1])
    assert blocked_job_id in queued_ids


@pytest.mark.asyncio
async def test_reprocess_ready_document_replaces_all_previous_derived_data():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import reprocess_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []
            self.maintenance: list[tuple[str, str]] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, source_id: str, job_id: str):
            self.maintenance.append((source_id, job_id))

    class FakeEngineManager:
        def __init__(self):
            self.deleted: list[str] = []

        async def delete_document_data(self, source_config_id, document_source_id, *, source):
            raise AssertionError("reprocess request must not wait for engine cleanup")

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="replace-source",
            sag_source_config_id="replace-source-config"[:36],
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
            parser_provider="mineru",
            mineru_provider="official",
            mineru_model="pipeline",
            parser_status="done",
            fallback_from="mineru",
            fallback_reason="previous fallback",
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
        )

        assert engine.deleted == []
        assert document.status == DocumentStatus.PENDING
        assert document.progress == 0
        assert document.chunk_count == 0 and document.event_count == 0
        assert document.token_usage == 0 and document.sag_source_id is None
        assert document.parser_provider is None
        assert document.mineru_provider is None and document.mineru_model is None
        assert document.parser_status is None
        assert document.fallback_from is None and document.fallback_reason is None
        assert source.document_count == 2
        assert source.chunk_count == 4 and source.event_count == 5
        assert job.type == JobType.REPROCESS_DOCUMENT
        assert job.payload["target_document_id"] == document.id
        assert set(job.payload["derived_source_ids"]) == {"engine-old", "engine-latest"}
        assert job.payload["_scheduler"]["priority"] == 0
        assert queue.ids == [job.id]
        assert queue.maintenance == [(source.id, job.id)]

        # Source rows are shared across this module's SQLite test database; do
        # not leave a freshly reprocessed source that would make universe-cache
        # contract tests correctly report their manifest as stale.
        await session.delete(source)
        await session.commit()


@pytest.mark.asyncio
async def test_reprocess_cleanup_job_deletes_old_data_before_queuing_processing():
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.tasks import reprocess_document_task

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
        source = Source(name="reprocess-worker", sag_source_config_id="reprocess-config"[:36])
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="ready.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/ready.md",
            status=DocumentStatus.PENDING,
        )
        session.add(document)
        await session.flush()
        cleanup = Job(
            type=JobType.REPROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.RUNNING,
            payload={
                "target_document_id": document.id,
                "derived_source_ids": ["engine-old", "engine-latest", "engine-old"],
            },
        )
        session.add(cleanup)
        await session.commit()

        queue = FakeQueue()
        engine = FakeEngineManager()
        await reprocess_document_task(
            session,
            cleanup,
            engine_manager=engine,
            job_queue=queue,
        )
        await reprocess_document_task(
            session,
            cleanup,
            engine_manager=engine,
            job_queue=queue,
        )

        process_jobs = list(
            (
                await session.scalars(
                    select(Job).where(
                        Job.document_id == document.id,
                        Job.type == JobType.PROCESS_DOCUMENT,
                    )
                )
            ).all()
        )
        assert engine.deleted == ["engine-latest", "engine-old"]
        assert len(process_jobs) == 1
        assert process_jobs[0].status == JobStatus.QUEUED
        assert process_jobs[0].payload == {}
        assert cleanup.payload["cleanup_completed"] is True
        assert queue.ids == [process_jobs[0].id, process_jobs[0].id]


@pytest.mark.asyncio
async def test_reprocess_cleanup_does_not_queue_processing_after_delete_request():
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.tasks import reprocess_document_task

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

    class FakeEngineManager:
        def __init__(self):
            self.deleted: list[str] = []

        async def delete_document_data(self, _config_id, document_source_id, *, source):
            assert source.sag_source_config_id
            self.deleted.append(document_source_id)

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="delete-during-reprocess-cleanup",
            sag_source_config_id="delete-during-reprocess-cleanup-config"[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="deleting.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/deleting.md",
            status=DocumentStatus.DELETING,
        )
        session.add(document)
        await session.flush()
        cleanup = Job(
            type=JobType.REPROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.RUNNING,
            payload={
                "target_document_id": document.id,
                "derived_source_ids": ["engine-old"],
            },
        )
        session.add(cleanup)
        await session.commit()

        queue = FakeQueue()
        engine = FakeEngineManager()
        await reprocess_document_task(
            session,
            cleanup,
            engine_manager=engine,
            job_queue=queue,
        )

        process_jobs = list(
            (
                await session.scalars(
                    select(Job).where(
                        Job.document_id == document.id,
                        Job.type == JobType.PROCESS_DOCUMENT,
                    )
                )
            ).all()
        )
        assert engine.deleted == ["engine-old"]
        assert process_jobs == []
        assert cleanup.payload["cleanup_completed"] is True
        assert queue.ids == []


@pytest.mark.asyncio
async def test_concurrent_reprocess_requests_share_one_cleanup_job():
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import reprocess_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []
            self.maintenance: list[tuple[str, str]] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, source_id: str, job_id: str):
            self.maintenance.append((source_id, job_id))

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="concurrent-reprocess",
            sag_source_config_id="concurrent-reprocess-config"[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="ready.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/ready.md",
            status=DocumentStatus.READY,
            progress=100,
            sag_source_id="engine-ready",
        )
        session.add(document)
        await session.flush()
        session.add(
            Job(
                type=JobType.PROCESS_DOCUMENT,
                source_id=source.id,
                document_id=document.id,
                status=JobStatus.SUCCEEDED,
                payload={"process_checkpoint": {"source_id": "engine-ready"}},
            )
        )
        await session.commit()
        source_id, document_id = source.id, document.id

    queue = FakeQueue()

    async def retry():
        async with SessionLocal() as session:
            source = await session.get(Source, source_id)
            return await reprocess_document(
                session,
                source,
                document_id,
                job_queue=queue,
            )

    first, second = await asyncio.gather(retry(), retry())

    async with SessionLocal() as session:
        cleanup_jobs = list(
            (
                await session.scalars(
                    select(Job).where(
                        Job.document_id == document_id,
                        Job.type == JobType.REPROCESS_DOCUMENT,
                    )
                )
            ).all()
        )
        document = await session.get(Document, document_id)
        assert document.status == DocumentStatus.PENDING
    assert first.id == second.id
    assert [job.id for job in cleanup_jobs] == [first.id]
    assert queue.ids == [first.id]
    assert queue.maintenance == [(source_id, first.id)]


@pytest.mark.asyncio
async def test_retrying_failed_reprocess_cleanup_stays_in_maintenance_flow():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import reprocess_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []
            self.maintenance: list[tuple[str, str]] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, source_id: str, job_id: str):
            self.maintenance.append((source_id, job_id))

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="failed-reprocess-cleanup",
            sag_source_config_id="failed-reprocess-cleanup-config"[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="failed.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/failed.md",
            status=DocumentStatus.FAILED,
            error="cleanup failed",
        )
        session.add(document)
        await session.flush()
        failed_cleanup = Job(
            type=JobType.REPROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.FAILED,
            payload={
                "target_document_id": document.id,
                "derived_source_ids": ["engine-old"],
                "_scheduler": {"priority": 0},
            },
            error="cleanup failed",
        )
        session.add(failed_cleanup)
        await session.commit()

        queue = FakeQueue()
        retried = await reprocess_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )

        assert retried.type == JobType.REPROCESS_DOCUMENT
        assert retried.status == JobStatus.QUEUED
        assert retried.payload["derived_source_ids"] == ["engine-old"]
        assert document.status == DocumentStatus.PENDING
        assert document.error is None
        assert queue.ids == [retried.id]
        assert queue.maintenance == [(source.id, retried.id)]


@pytest.mark.asyncio
async def test_delete_after_reprocess_request_uses_maintenance_cleanup():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document, reprocess_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []
            self.maintenance: list[tuple[str, str]] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, source_id: str, job_id: str):
            self.maintenance.append((source_id, job_id))

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="delete-after-reprocess",
            sag_source_config_id="delete-after-reprocess-config"[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="ready.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/ready.md",
            status=DocumentStatus.READY,
            progress=100,
            sag_source_id="engine-ready",
        )
        session.add(document)
        await session.flush()
        session.add(
            Job(
                type=JobType.PROCESS_DOCUMENT,
                source_id=source.id,
                document_id=document.id,
                status=JobStatus.SUCCEEDED,
                payload={"process_checkpoint": {"source_id": "engine-ready"}},
            )
        )
        await session.commit()

        queue = FakeQueue()
        reprocess_job = await reprocess_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )
        delete_job = await delete_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )

        await session.refresh(document)
        assert document.status == DocumentStatus.DELETING
        assert reprocess_job.status == JobStatus.QUEUED
        assert delete_job.type == JobType.DELETE_DOCUMENT
        assert delete_job.status == JobStatus.QUEUED
        assert await session.get(Document, document.id) is not None
        assert queue.maintenance == [
            (source.id, reprocess_job.id),
            (source.id, delete_job.id),
        ]


@pytest.mark.asyncio
async def test_delete_after_failed_reprocess_keeps_old_engine_ids_for_cleanup():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document

    class FakeQueue:
        async def enqueue(self, _job_id: str):
            return None

        def begin_source_maintenance(self, _source_id: str, _job_id: str):
            return None

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="failed-reprocess-delete", sag_source_config_id="failed-config"[:36])
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="failed.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/failed.md",
            status=DocumentStatus.FAILED,
            sag_source_id=None,
        )
        session.add(document)
        await session.flush()
        session.add(
            Job(
                type=JobType.REPROCESS_DOCUMENT,
                source_id=source.id,
                document_id=document.id,
                status=JobStatus.FAILED,
                payload={
                    "target_document_id": document.id,
                    "derived_source_ids": ["engine-old", "engine-older"],
                },
            )
        )
        await session.commit()

        delete_job = await delete_document(
            session,
            source,
            document.id,
            job_queue=FakeQueue(),
        )

        assert set(delete_job.payload["derived_source_ids"]) == {
            "engine-old",
            "engine-older",
        }


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


@pytest.mark.asyncio
async def test_pause_cannot_overwrite_delete_that_commits_after_job_lookup(monkeypatch):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document, pause_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, _source_id: str, _job_id: str):
            return None

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="pause-delete-cas",
            sag_source_config_id="pause-delete-cas-config"[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="pause-delete.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/pause-delete.md",
            status=DocumentStatus.EXTRACTING,
            sag_source_id="engine-pause-delete",
        )
        session.add(document)
        await session.flush()
        process_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.RUNNING,
        )
        session.add(process_job)
        await session.commit()
        source_id, document_id, process_job_id = (
            source.id,
            document.id,
            process_job.id,
        )

        queue = FakeQueue()
        real_scalar = session.scalar
        delete_injected = False

        async def scalar_then_delete(statement, *args, **kwargs):
            nonlocal delete_injected
            result = await real_scalar(statement, *args, **kwargs)
            if (
                not delete_injected
                and isinstance(result, Job)
                and result.id == process_job_id
            ):
                delete_injected = True
                await session.commit()
                async with SessionLocal() as delete_session:
                    deleting_source = await delete_session.get(Source, source_id)
                    await delete_document(
                        delete_session,
                        deleting_source,
                        document_id,
                        job_queue=queue,
                    )
            return result

        monkeypatch.setattr(session, "scalar", scalar_then_delete)

        with pytest.raises(ConflictError, match="删除|状态"):
            await pause_document(session, source, document_id)

        await session.refresh(document)
        assert document.status == DocumentStatus.DELETING


@pytest.mark.asyncio
async def test_resume_cannot_overwrite_delete_that_commits_after_job_lookup(monkeypatch):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document, resume_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, _source_id: str, _job_id: str):
            return None

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="resume-delete-cas",
            sag_source_config_id="resume-delete-cas-config"[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="resume-delete.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/resume-delete.md",
            status=DocumentStatus.PAUSED,
        )
        session.add(document)
        await session.flush()
        process_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.PAUSED,
            payload={"process_checkpoint": {"source_id": "engine-resume-delete"}},
        )
        session.add(process_job)
        await session.commit()
        source_id, document_id, process_job_id = (
            source.id,
            document.id,
            process_job.id,
        )

        queue = FakeQueue()
        real_scalar = session.scalar
        delete_injected = False

        async def scalar_then_delete(statement, *args, **kwargs):
            nonlocal delete_injected
            result = await real_scalar(statement, *args, **kwargs)
            if (
                not delete_injected
                and isinstance(result, Job)
                and result.id == process_job_id
            ):
                delete_injected = True
                await session.commit()
                async with SessionLocal() as delete_session:
                    deleting_source = await delete_session.get(Source, source_id)
                    await delete_document(
                        delete_session,
                        deleting_source,
                        document_id,
                        job_queue=queue,
                    )
            return result

        monkeypatch.setattr(session, "scalar", scalar_then_delete)

        with pytest.raises(ConflictError, match="删除|状态"):
            await resume_document(session, source, document_id, job_queue=queue)

        await session.refresh(document)
        await session.refresh(process_job)
        assert document.status == DocumentStatus.DELETING
        assert process_job.status == JobStatus.PAUSED


@pytest.mark.asyncio
async def test_pause_rejects_ready_document_while_job_completion_is_committing():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import pause_document

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="pause-finish-cas",
            sag_source_config_id="pause-finish-cas-config"[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="pause-finish.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/pause-finish.md",
            status=DocumentStatus.READY,
            progress=100,
        )
        session.add(document)
        await session.flush()
        session.add(
            Job(
                type=JobType.PROCESS_DOCUMENT,
                source_id=source.id,
                document_id=document.id,
                status=JobStatus.RUNNING,
                progress=0.99,
            )
        )
        await session.commit()

        with pytest.raises(ConflictError, match="结束|状态"):
            await pause_document(session, source, document.id)


@pytest.mark.parametrize("engine_mode", ["complete", "paused", "error"])
@pytest.mark.asyncio
async def test_process_exit_cannot_overwrite_concurrent_delete(
    monkeypatch,
    engine_mode,
):
    from types import SimpleNamespace

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.control import JobPaused
    from sag_api.jobs.tasks import process_document
    from sag_api.services.document_service import delete_document

    class FakeEngine:
        async def process_document(self, *_args, **_kwargs):
            if engine_mode == "error":
                raise RuntimeError("inflight extraction failed")
            return SimpleNamespace(
                paused=engine_mode == "paused",
                chunk_count=1,
                event_count=1,
                source_id="engine-exit-delete",
                token_usage=50,
            )

    class FakeQueue:
        async def enqueue(self, _job_id: str):
            return None

        def begin_source_maintenance(self, _source_id: str, _job_id: str):
            return None

        def source_maintenance_requested(self, _source_id: str):
            return False

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name=f"{engine_mode}-delete-cas",
            sag_source_config_id=(f"{engine_mode}-delete-cas-config")[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename=f"{engine_mode}-delete.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path=f"/tmp/{engine_mode}-delete.md",
            status=DocumentStatus.EXTRACTING,
        )
        session.add(document)
        await session.flush()
        process_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.RUNNING,
            payload={
                "process_checkpoint": {
                    "source_id": "engine-exit-delete",
                    "chunk_ids": ["chunk-1"],
                    "processed_chunk_ids": [],
                }
            },
        )
        session.add(process_job)
        await session.commit()
        source_id, document_id = source.id, document.id

        real_refresh = session.refresh
        delete_injected = False

        async def refresh_then_delete(instance, *args, **kwargs):
            nonlocal delete_injected
            await real_refresh(instance, *args, **kwargs)
            if not delete_injected and isinstance(instance, Document):
                delete_injected = True
                await session.commit()
                async with SessionLocal() as delete_session:
                    deleting_source = await delete_session.get(Source, source_id)
                    await delete_document(
                        delete_session,
                        deleting_source,
                        document_id,
                        job_queue=FakeQueue(),
                    )

        monkeypatch.setattr(session, "refresh", refresh_then_delete)

        with pytest.raises(JobPaused):
            await process_document(
                session,
                process_job,
                engine_manager=FakeEngine(),
                job_queue=FakeQueue(),
            )

        async with SessionLocal() as verification_session:
            saved_document = await verification_session.get(Document, document_id)
            assert saved_document.status == DocumentStatus.DELETING


@pytest.mark.asyncio
async def test_resume_uses_supervised_dispatch_after_persisting_queued_state():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import resume_document

    class DurableQueue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, _job_id: str):
            raise AssertionError("persisted resume must use supervised dispatch")

        async def enqueue_durably(self, job_id: str):
            self.ids.append(job_id)

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="resume-durable-dispatch",
            sag_source_config_id="resume-durable-dispatch-config"[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="resume-durable-dispatch.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/resume-durable-dispatch.md",
            status=DocumentStatus.PAUSED,
        )
        session.add(document)
        await session.flush()
        process_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.PAUSED,
        )
        session.add(process_job)
        await session.commit()

        queue = DurableQueue()
        resumed = await resume_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )

        assert resumed.status == JobStatus.QUEUED
        assert queue.ids == [process_job.id]
