"""Extraction display progress is independent of durable batch checkpoints."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete
from zleap.sag.modules.extract.config import ExtractionWritePlan
from zleap.sag.modules.extract.extractor import EventExtractor, ExtractConfig
from zleap.sag.pipeline.errors import PipelineCancelledError

from sag_api.core.db import SessionLocal, init_db
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs import tasks
from sag_api.jobs.control import JobPaused
from sag_api.sag.dto import ProcessCheckpoint, ProcessOutcome, extraction_display_percent
from sag_api.sag.incremental_processor import IncrementalDocumentProcessor


@pytest.mark.parametrize(
    ("completed", "total", "committed", "expected"),
    [(0, 2033, False, 20), (811, 2033, False, 52), (10, 20, False, 60), (2032, 2033, False, 99),
     (2033, 2033, False, 99), (2033, 2033, True, 100), (0, 0, True, 20)],
)
def test_extraction_display_percent(completed, total, committed, expected):
    assert extraction_display_percent(completed, total, committed=committed) == expected


def _checkpoint(total=20):
    return ProcessCheckpoint(
        source_id="article", chunk_ids=[f"c{i}" for i in range(total)], chunk_version="v1",
    )


async def _never_pause():
    return False


async def _ignore(_value):
    pass


def _processor(extractor, extract):
    return IncrementalDocumentProcessor(
        SimpleNamespace(
            _extractor=extractor, extract=extract,
            resources=SimpleNamespace(prompts=SimpleNamespace(language="en")),
        ),
        "source-config", max_concurrency=1,
    )


def _extractor():
    return EventExtractor(
        SimpleNamespace(), session_factory=object(), repositories=object(), embedding=object(), llm=object(),
    )


@pytest.mark.asyncio
async def test_installed_extractor_reports_before_commit_without_checkpointing(monkeypatch):
    """Exercise the real 0.13.0 batch counter with deterministic, empty chunk results."""
    extractor = _extractor()
    checkpoint = _checkpoint()
    progress, snapshots = [], []
    chunks = [SimpleNamespace(id=cid, source_type="article", content="text") for cid in checkpoint.chunk_ids]
    monkeypatch.setattr(extractor, "_load_chunks", AsyncMock(return_value=chunks))
    monkeypatch.setattr(extractor, "_ensure_article_summary", AsyncMock(return_value={}))
    monkeypatch.setattr(extractor, "extract_from_chunk", AsyncMock(
        return_value=SimpleNamespace(events=(), stats=None),
    ))

    async def save_events(*_args, **_kwargs):
        assert progress == [60, 99]
        assert snapshots == []

    monkeypatch.setattr(extractor, "_save_events", save_events)

    async def extract(chunk_set, options, **kwargs):
        config = ExtractConfig(
            storage_mode="normal", write_plan=ExtractionWritePlan.from_storage_mode("normal"),
            data_source_id="source-config", source_type="ARTICLE", source_id="article", source_version="v1",
            chunk_ids=list(chunk_set.chunk_ids), max_concurrency=1,
        )
        result = await extractor.extract_batch(config, cancellation=kwargs["cancellation"])
        return SimpleNamespace(event_ids=(), event_count=0, stats=result.stats)

    async def on_progress(completed, total):
        progress.append(extraction_display_percent(completed, total, committed=False))
        assert checkpoint.processed_chunk_ids == []

    async def on_checkpoint(value):
        snapshots.append(value)

    outcome = await _processor(extractor, extract).process(
        None, checkpoint=checkpoint, on_checkpoint=on_checkpoint, on_progress=on_progress, should_pause=_never_pause,
    )
    assert outcome.paused is False
    assert snapshots[0].processed_chunk_ids == checkpoint.chunk_ids
    assert extractor._on_progress is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["success", "failure", "pause", "cancel"])
async def test_callback_restored_on_every_exit_and_display_failure_is_best_effort(ending):
    extractor = _extractor()
    previous = AsyncMock()
    extractor._on_progress = previous
    callback = AsyncMock(side_effect=RuntimeError("display database unavailable"))

    async def extract(*args, **kwargs):
        await extractor._on_progress(10, 20)
        if ending == "failure":
            raise RuntimeError("batch failed")
        if ending == "pause":
            raise PipelineCancelledError("paused", stage="extract", run_id="r", code="pipeline_cancelled")
        if ending == "cancel":
            raise asyncio.CancelledError
        return SimpleNamespace(event_ids=(), event_count=0, stats={})

    call = _processor(extractor, extract).process(
        None, checkpoint=_checkpoint(), on_checkpoint=_ignore, on_progress=callback, should_pause=_never_pause,
    )
    if ending == "failure":
        with pytest.raises(RuntimeError, match="batch failed"):
            await call
    elif ending == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await call
    else:
        assert (await call).paused is (ending == "pause")
    callback.assert_awaited_once_with(10, 20)
    assert extractor._on_progress is previous


@pytest.mark.asyncio
async def test_jobs_sharing_extractor_cannot_borrow_each_others_callback():
    extractor = _extractor()
    entered, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    first_progress, second_progress = AsyncMock(), AsyncMock()

    async def first_extract(*args, **kwargs):
        entered.set()
        await release.wait()
        await extractor._on_progress(10, 20)
        return SimpleNamespace(event_ids=(), event_count=0, stats={})

    async def second_extract(*args, **kwargs):
        second_started.set()
        await extractor._on_progress(20, 20)
        return SimpleNamespace(event_ids=(), event_count=0, stats={})

    first = asyncio.create_task(_processor(extractor, first_extract).process(
        None, checkpoint=_checkpoint(), on_checkpoint=_ignore, on_progress=first_progress, should_pause=_never_pause,
    ))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(_processor(extractor, second_extract).process(
        None, checkpoint=_checkpoint(), on_checkpoint=_ignore, on_progress=second_progress, should_pause=_never_pause,
    ))
    await asyncio.sleep(0)
    assert not second_started.is_set()
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 1)
    first_progress.assert_awaited_once_with(10, 20)
    second_progress.assert_awaited_once_with(20, 20)
    assert extractor._on_progress is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["pause", "pause_on_release", "cancel"])
async def test_waiting_job_can_exit_without_extracting_or_blocking_the_next_job(ending):
    extractor = _extractor()
    owner_entered, release_owner = asyncio.Event(), asyncio.Event()
    waiter_polled, pause_waiter = asyncio.Event(), asyncio.Event()
    started, progress, snapshots = [], [], []

    async def extract(chunk_set, *_args, **_kwargs):
        started.append(chunk_set.source_id)
        if chunk_set.source_id == "owner":
            owner_entered.set()
            await release_owner.wait()
        await extractor._on_progress(20, 20)
        return SimpleNamespace(event_ids=(), event_count=0, stats={})

    async def should_pause_waiter():
        waiter_polled.set()
        await pause_waiter.wait()
        return True

    def process(name, should_pause):
        async def on_progress(completed, total):
            progress.append((name, completed, total))

        async def on_checkpoint(value):
            snapshots.append(value.source_id)

        return _processor(extractor, extract).process(
            None, checkpoint=_checkpoint().model_copy(update={"source_id": name}),
            on_checkpoint=on_checkpoint, on_progress=on_progress, should_pause=should_pause,
        )

    owner = asyncio.create_task(process("owner", _never_pause))
    running = [owner]
    try:
        await asyncio.wait_for(owner_entered.wait(), 1)
        owner_callback = extractor._on_progress
        waiter = asyncio.create_task(process("waiter", should_pause_waiter))
        running.append(waiter)
        await asyncio.wait_for(waiter_polled.wait(), 1)
        assert started == ["owner"]

        if ending == "cancel":
            waiter.cancel()
        else:
            pause_waiter.set()
        if ending == "pause_on_release":
            release_owner.set()

        done, _pending = await asyncio.wait({waiter}, timeout=1)
        assert waiter in done, "pausing a lock waiter must not wait for the owner to finish"
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await waiter
        else:
            outcome = await waiter
            assert outcome.paused is True
            assert outcome.processed_chunk_ids == []
        assert "waiter" not in started
        assert "waiter" not in snapshots
        if ending != "pause_on_release":
            assert not owner.done()
            assert extractor._on_progress is owner_callback

        release_owner.set()
        assert (await asyncio.wait_for(owner, 1)).paused is False
        assert (await asyncio.wait_for(process("next", _never_pause), 1)).paused is False
        assert started == ["owner", "next"]
        assert progress == [("owner", 20, 20), ("next", 20, 20)]
        assert snapshots == ["owner", "next"]
        assert extractor._on_progress is None
    finally:
        for task in running:
            if not task.done():
                task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


@pytest.fixture
async def progress_job():
    await init_db()
    checkpoint = _checkpoint(2033)
    payload = checkpoint.merge_payload({"custom_control": "preserved"})
    async with SessionLocal() as session:
        source = Source(name="progress-test", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id, filename="progress.md", storage_path="/tmp/progress.md",
            content_type="text/markdown", size_bytes=1, status=DocumentStatus.PENDING, progress=52,
        )
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.PROCESS_DOCUMENT, status=JobStatus.RUNNING, source_id=source.id,
            document_id=document.id, progress=0.52, payload=payload,
        )
        session.add(job)
        await session.commit()
        document_id, job_id = document.id, job.id
        source_id = source.id

    try:
        yield checkpoint, payload, document_id, job_id
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Job).where(Job.id == job_id))
            await session.execute(delete(Document).where(Document.id == document_id))
            await session.execute(delete(Source).where(Source.id == source_id))
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["success", "failure", "pause", "control_transition"])
async def test_task_persists_live_percent_without_payload_writes_and_starts_extraction_at_twenty(ending, progress_job):
    checkpoint, payload, document_id, job_id = progress_job

    async def read():
        async with SessionLocal() as session:
            doc = await session.get(Document, document_id)
            job = await session.get(Job, job_id)
            return doc.status, doc.progress, job.progress, job.payload, doc.updated_at

    class Manager:
        async def process_document(self, *args, on_stage, on_progress, on_checkpoint, **kwargs):
            await on_stage("extracting")
            assert (await read())[1:3] == (20, 0.2)
            await on_progress(811, 2033)
            assert (await read())[1:4] == (52, 0.52, payload)
            last_updated = (await read())[4]
            await on_progress(812, 2033)  # same integer percentage
            await on_progress(800, 2033)  # an older concurrent callback
            assert (await read())[4] == last_updated
            if ending == "control_transition":
                from sqlalchemy import update

                async with SessionLocal() as control:
                    await control.execute(update(Document).where(Document.id == document_id).values(
                        status=DocumentStatus.PAUSING,
                    ))
                    await control.commit()
                await on_progress(2033, 2033)
                assert (await read())[1:4] == (52, 0.52, payload)
                return ProcessOutcome(paused=True)
            await on_progress(2033, 2033)
            assert (await read())[1:4] == (99, 0.99, payload)
            if ending == "failure":
                raise RuntimeError("batch failed before commit")
            if ending == "pause":
                return ProcessOutcome(paused=True)
            final = checkpoint.model_copy(update={"processed_chunk_ids": checkpoint.chunk_ids})
            await on_checkpoint(final)
            assert (await read())[1:3] == (100, 1)
            return ProcessOutcome(source_id="article", chunk_count=2033, processed_chunk_ids=checkpoint.chunk_ids)

    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        if ending == "failure":
            with pytest.raises(RuntimeError, match="batch failed"):
                await tasks._process_document_unlocked(session, job, engine_manager=Manager())
        elif ending in {"pause", "control_transition"}:
            with pytest.raises(JobPaused):
                await tasks._process_document_unlocked(session, job, engine_manager=Manager())
        else:
            await tasks._process_document_unlocked(session, job, engine_manager=Manager())
    state = await read()
    assert state[0] == {"success": DocumentStatus.READY, "failure": DocumentStatus.FAILED}.get(
        ending, DocumentStatus.PAUSED,
    )
    assert state[1] == {"success": 100, "control_transition": 52}.get(ending, 99)
    if ending != "success":
        assert state[3] == payload
