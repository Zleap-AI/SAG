"""Processing labels follow actual pipeline boundaries, including extractor waits."""

import asyncio
from types import SimpleNamespace

import pytest
from zleap.sag.pipeline.events import StageEvent, StageEventType, StageName

from sag_api.sag.dto import ProcessCheckpoint
from sag_api.sag.incremental_processor import IncrementalDocumentProcessor


async def _ignore(_value):
    pass


async def _never_pause():
    return False


def _processor(**engine):
    return IncrementalDocumentProcessor(
        SimpleNamespace(resources=SimpleNamespace(prompts=SimpleNamespace(language="en")), **engine),
        "source", max_concurrency=1,
    )


def _checkpoint():
    return ProcessCheckpoint(source_id="article", chunk_ids=["c1"], chunk_version="v1")


@pytest.mark.asyncio
async def test_ingest_stage_events_reach_the_document_before_extraction():
    stages = []

    async def on_stage(stage):
        stages.append(stage)

    async def ingest(*_args, observer=None, **_kwargs):
        for stage in (StageName.PARSE, StageName.CHUNK, StageName.INDEX):
            if observer is not None:
                await observer(StageEvent(stage=stage, type=StageEventType.STARTED, run_id="ingest"))
        return SimpleNamespace(
            source_id="article", chunk_ids=("c1",), generation_id=None,
            chunk_version="v1", source_version="s1",
        )

    async def extract(*_args, observer, **_kwargs):
        await observer(StageEvent(stage=StageName.EXTRACT, type=StageEventType.STARTED, run_id="extract"))
        assert stages == ["loading", "parsing", "chunking", "indexing", "waiting_extraction", "extracting"]
        return SimpleNamespace(event_ids=(), event_count=0, stats={})

    outcome = await _processor(ingest=ingest, extract=extract).process(
        "/tmp/document.md", checkpoint=ProcessCheckpoint(), on_checkpoint=_ignore,
        should_pause=_never_pause, on_stage=on_stage,
    )
    assert not outcome.paused
    assert stages[-1] == "finalizing"


@pytest.mark.asyncio
async def test_waiting_document_does_not_claim_extraction_has_started():
    class Extractor:
        _on_progress = None

    extractor = Extractor()
    entered, release, second_notice = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stages = []

    async def first_extract(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(event_ids=(), event_count=0, stats={})

    async def second_extract(*_args, observer, **_kwargs):
        await observer(StageEvent(stage=StageName.EXTRACT, type=StageEventType.STARTED, run_id="extract"))
        assert stages == ["waiting_extraction", "extracting"]
        return SimpleNamespace(event_ids=(), event_count=0, stats={})

    async def on_stage(stage):
        stages.append(stage)
        second_notice.set()

    async def run(extract, callback=None):
        return await _processor(_extractor=extractor, extract=extract).process(
            None, checkpoint=_checkpoint(), on_checkpoint=_ignore,
            should_pause=_never_pause, on_stage=callback,
        )

    first = asyncio.create_task(run(first_extract))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(run(second_extract, on_stage))
        await asyncio.wait_for(second_notice.wait(), 1)
        assert stages == ["waiting_extraction"]
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        assert stages == ["waiting_extraction", "extracting", "finalizing"]
    finally:
        release.set()
        for task in (first, second):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)


@pytest.mark.asyncio
async def test_stage_display_failure_does_not_abort_extraction_or_checkpoint():
    snapshots = []

    async def on_stage(_stage):
        raise RuntimeError("display unavailable")

    async def on_checkpoint(value):
        snapshots.append(value)

    async def extract(*_args, **_kwargs):
        return SimpleNamespace(event_ids=("event",), event_count=1, stats={})

    outcome = await _processor(extract=extract).process(
        None, checkpoint=_checkpoint(), on_checkpoint=on_checkpoint,
        should_pause=_never_pause, on_stage=on_stage,
    )
    assert not outcome.paused
    assert snapshots[0].processed_chunk_ids == ["c1"]


@pytest.mark.asyncio
async def test_engine_capacity_wait_is_not_reported_as_active_extraction():
    entered, release = asyncio.Event(), asyncio.Event()
    stages = []

    async def on_stage(stage):
        stages.append(stage)

    async def extract(*_args, observer, **_kwargs):
        entered.set()
        await release.wait()
        await observer(StageEvent(stage=StageName.EXTRACT, type=StageEventType.STARTED, run_id="extract"))
        assert stages[-1] == "extracting"
        return SimpleNamespace(event_ids=(), event_count=0, stats={})

    task = asyncio.create_task(_processor(extract=extract).process(
        None, checkpoint=_checkpoint(), on_checkpoint=_ignore,
        should_pause=_never_pause, on_stage=on_stage,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert stages == ["waiting_extraction"]
        release.set()
        await asyncio.wait_for(task, 1)
        assert stages[-1] == "finalizing"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
