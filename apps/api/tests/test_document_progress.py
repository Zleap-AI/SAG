"""Extraction display progress is independent of durable batch checkpoints."""

import asyncio
from uuid import uuid4

import pytest
from document_requests import document_receipt, document_request
from sqlalchemy import delete

from sag_api.core.db import SessionLocal, init_db
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs import tasks
from sag_api.jobs.control import JobPaused
from sag_api.sag.dto import extraction_display_percent
from tests.document_requests import UncommittedDocumentManager, controller_processing_identity, processing_parameters


@pytest.mark.parametrize(
    ("completed", "total", "committed", "expected"),
    [
        (0, 2033, False, 20),
        (811, 2033, False, 52),
        (10, 20, False, 60),
        (2032, 2033, False, 99),
        (2033, 2033, False, 99),
        (2033, 2033, True, 100),
        (0, 0, True, 20),
    ],
)
def test_extraction_display_percent(completed, total, committed, expected):
    assert extraction_display_percent(completed, total, committed=committed) == expected


@pytest.fixture
async def progress_job(controller_identity):
    await init_db()
    async with SessionLocal() as session:
        source = Source(name="progress-test", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.flush()
        request = document_request(source.sag_source_config_id)
        payload = {
            "processing_identity": controller_processing_identity("/tmp/progress.md"),
            "process_request": request.model_dump(mode="json"),
            "control_revision": 1,
            "custom_control": "preserved",
        }
        document = Document(
            source_id=source.id,
            filename="progress.md",
            storage_path="/tmp/progress.md",
            content_type="text/markdown",
            size_bytes=1,
            status=DocumentStatus.PENDING,
            progress=52,
        )
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.RUNNING,
            source_id=source.id,
            document_id=document.id,
            progress=0.52,
            payload=payload,
        )
        session.add(job)
        await session.commit()
        document_id, job_id = document.id, job.id
        source_id = source.id

    try:
        yield request, payload, document_id, job_id
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Job).where(Job.id == job_id))
            await session.execute(delete(Document).where(Document.id == document_id))
            await session.execute(delete(Source).where(Source.id == source_id))
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["success", "failure", "pause", "control_transition"])
async def test_task_persists_live_percent_without_payload_writes_and_starts_extraction_at_twenty(ending, progress_job):
    request, payload, document_id, job_id = progress_job

    async def read():
        async with SessionLocal() as session:
            doc = await session.get(Document, document_id)
            job = await session.get(Job, job_id)
            return doc.status, doc.progress, job.progress, job.payload, doc.updated_at

    class Manager(UncommittedDocumentManager):
        async def process_document(self, _source_config_id, request_factory, *, on_stage, on_progress, **kwargs):
            bound = await request_factory(None, *processing_parameters())
            assert bound == request
            await on_stage("extracting")
            assert (await read())[1:3] == (20, 0.2)
            await on_progress(811, 2033)
            assert (await read())[1:4] == (52, 0.52, payload)
            await on_progress(812, 2033)  # raw count advances within the same percentage
            last_updated = (await read())[4]
            await on_progress(812, 2033)  # same count does not write again
            await on_progress(800, 2033)  # an older concurrent callback
            assert (await read())[4] == last_updated
            if ending == "control_transition":
                from sqlalchemy import update

                async with SessionLocal() as control:
                    await control.execute(
                        update(Document)
                        .where(Document.id == document_id)
                        .values(
                            status=DocumentStatus.PAUSING,
                        )
                    )
                    await control.commit()
                await on_progress(2033, 2033)
                assert (await read())[1:4] == (52, 0.52, payload)
                return None
            await on_progress(2033, 2033)
            assert (await read())[1:4] == (99, 0.99, payload)
            if ending == "failure":
                raise RuntimeError("batch failed before commit")
            if ending == "pause":
                return None
            # Only the public success receipt can take the task from 99 to 100.
            return document_receipt(bound, chunk_count=2033)

    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        if ending == "failure":
            with pytest.raises(RuntimeError, match="batch failed"):
                await tasks._process_document_unlocked(
                    session, job, engine_manager=Manager(), lease_lost=asyncio.Event()
                )
        elif ending in {"pause", "control_transition"}:
            with pytest.raises(JobPaused):
                await tasks._process_document_unlocked(
                    session, job, engine_manager=Manager(), lease_lost=asyncio.Event()
                )
        else:
            await tasks._process_document_unlocked(session, job, engine_manager=Manager(), lease_lost=asyncio.Event())
    state = await read()
    assert state[0] == {"success": DocumentStatus.READY, "failure": DocumentStatus.FAILED}.get(
        ending,
        DocumentStatus.PAUSED,
    )
    assert state[1] == {"success": 100, "control_transition": 52}.get(ending, 99)
    if ending != "success":
        assert state[3] == payload
