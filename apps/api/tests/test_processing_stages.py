"""Processing labels follow public kernel events, including actual capacity waits."""

import asyncio
from types import SimpleNamespace

from document_requests import document_receipt, document_request
from zleap.sag.pipeline.events import StageEvent, StageEventType, StageName

from sag_api.sag.incremental_processor import IncrementalDocumentProcessor


async def never_pause():
    return False


def processor(process_source):
    return IncrementalDocumentProcessor(SimpleNamespace(process_source=process_source), "source", max_concurrency=1)


async def test_public_stage_events_reach_the_document_in_kernel_order():
    stages = []
    request = document_request("source")

    async def on_stage(stage):
        stages.append(stage)

    async def process_source(bound, *, observer, cancellation):
        for stage in (StageName.PARSE, StageName.CHUNK, StageName.INDEX, StageName.EXTRACT):
            await observer(StageEvent(stage=stage, type=StageEventType.STARTED, run_id=bound.context.operation_id))
        assert stages == ["parsing", "chunking", "indexing", "extracting"]
        await observer(
            StageEvent(stage=StageName.EXTRACT, type=StageEventType.SUCCEEDED, run_id=bound.context.operation_id)
        )
        return document_receipt(bound)

    result = await processor(process_source).process(request, should_pause=never_pause, on_stage=on_stage)
    assert result.status == "succeeded"
    assert stages[-1] == "finalizing"


async def test_capacity_wait_does_not_claim_extraction_started_before_a_public_event():
    entered, release = asyncio.Event(), asyncio.Event()
    stages = []
    request = document_request("source")

    async def on_stage(stage):
        stages.append(stage)

    async def process_source(bound, *, observer, cancellation):
        entered.set()
        await release.wait()
        await observer(
            StageEvent(stage=StageName.EXTRACT, type=StageEventType.STARTED, run_id=bound.context.operation_id)
        )
        assert stages == ["extracting"]
        await observer(
            StageEvent(stage=StageName.EXTRACT, type=StageEventType.SUCCEEDED, run_id=bound.context.operation_id)
        )
        return document_receipt(bound)

    task = asyncio.create_task(processor(process_source).process(request, should_pause=never_pause, on_stage=on_stage))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert stages == []
        release.set()
        result = await asyncio.wait_for(task, 1)
        assert result.status == "succeeded"
        assert stages == ["extracting", "finalizing"]
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stage_display_failure_does_not_change_the_public_receipt():
    request = document_request("source")
    receipt = document_receipt(request, event_count=1)

    async def on_stage(_stage):
        raise RuntimeError("display unavailable")

    async def process_source(bound, *, observer, cancellation):
        await observer(
            StageEvent(stage=StageName.EXTRACT, type=StageEventType.STARTED, run_id=bound.context.operation_id)
        )
        await observer(
            StageEvent(stage=StageName.EXTRACT, type=StageEventType.SUCCEEDED, run_id=bound.context.operation_id)
        )
        return receipt

    assert await processor(process_source).process(request, should_pause=never_pause, on_stage=on_stage) == receipt
