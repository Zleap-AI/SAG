"""Business publication visibility against the installed engine and real stores."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import delete
from zleap.sag.operations import OperationContext

from sag_api.core.config import Settings
from sag_api.core.db import SessionLocal, init_db
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.sag import EngineManager
from sag_api.sag.incremental_processor import IncrementalDocumentProcessor
from sag_api.services.retrieval_service import retrieve_relevant_sections
from tests.document_models import local_models


@pytest.mark.asyncio
@pytest.mark.parametrize("published", [True, False])
@pytest.mark.parametrize("strategy", ["vector", "multi_es_fast", "multi"])
async def test_ready_engine_candidate_is_not_a_business_publication(tmp_path, published, strategy):
    """An unscoped engine query exposes a ready candidate before the app commit."""
    await init_db()
    async with SessionLocal() as session:
        source = Source(name="publication-real-read", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.commit()
        source_id = source.id
    old_file = tmp_path / "published.md"
    old_file.write_text("# Acme policy\n\nAcme Corporation migration policy is the published original version.")
    new_file = tmp_path / "candidate.md"
    new_file.write_text("# Acme policy\n\nAcme Corporation migration policy is the unpublished candidate version.")
    try:
        with local_models() as models:
            manager = EngineManager(
                Settings(
                    _env_file=None,
                    data_dir=str(tmp_path / "stores"),
                    llm_provider="openai",
                    llm_model="local",
                    llm_api_key="local",
                    llm_base_url=models.base_url,
                    embedding_model="local",
                    embedding_api_key="local",
                    embedding_base_url=models.base_url,
                    embedding_schema_dimensions=32,
                )
            )
            try:
                requests = []
                async with manager.use_concurrently(source.sag_source_config_id, source) as engine:
                    processor = IncrementalDocumentProcessor(engine, source.sag_source_config_id, max_concurrency=1)
                    for path in (old_file, new_file):
                        operation_id = uuid4().hex
                        request = await processor.build_request(
                            path,
                            context=OperationContext(
                                operation_id=operation_id,
                                idempotency_key=operation_id,
                                request_digest="0" * 64,
                                fence_scope=source.sag_source_config_id,
                                fence_token=1,
                                owner_id=operation_id,
                                isolation="input",
                            ),
                            input_id=uuid4().hex,
                        )
                        request = request.model_copy(
                            update={
                                "extraction_options": request.extraction_options.model_copy(
                                    update={
                                        "enable_article_summary": False,
                                        "enable_parent_summary": False,
                                    }
                                ),
                            }
                        )
                        result = await processor.process(request, should_pause=_never_pause)
                        assert result.status == "succeeded" and result.checkpoint == "ready"
                        requests.append(request)
                old_input = requests[0].source.descriptor.source_id
                async with SessionLocal() as session:
                    session.add(
                        Document(
                            source_id=source.id,
                            filename="policy.md",
                            storage_path=str(old_file),
                            status=DocumentStatus.PAUSED,
                            sag_source_id=old_input if published else None,
                            published_operation_id=requests[0].context.operation_id if published else None,
                        )
                    )
                    await session.commit()
                outcome = await retrieve_relevant_sections(
                    manager,
                    [source],
                    "Acme Corporation migration policy",
                    strategy=strategy,
                    top_k=10,
                )
                if published:
                    assert outcome.sections
                    assert {section.source_id for section in outcome.sections} == {old_input}
                else:
                    assert not outcome.sections
            finally:
                await manager.aclose_all()
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Source).where(Source.id == source_id))
            await session.commit()


async def _never_pause():
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["resume", "worker"])
async def test_lost_completion_response_publishes_real_receipt_without_input_or_model_replay(tmp_path, recovery):
    """The installed SDK commit survives missing bytes and changed target settings."""
    from sag_api.jobs.tasks import _process_document_unlocked
    from sag_api.services.document_service import resume_document

    await init_db()
    path = tmp_path / "complete.md"
    path.write_text("# Acme\nAcme Corporation completed the database migration.")
    async with SessionLocal() as session:
        source = Source(name="real-receipt", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.commit()
        source_id = source.id
    try:
        with local_models() as models:
            config = Settings(
                _env_file=None,
                data_dir=str(tmp_path / "stores"),
                llm_provider="openai",
                llm_model="local",
                llm_api_key="local",
                llm_base_url=models.base_url,
                embedding_model="local",
                embedding_api_key="local",
                embedding_base_url=models.base_url,
                embedding_schema_dimensions=32,
            )
            manager = EngineManager(config)
            try:
                async with manager.use_concurrently(source.sag_source_config_id, source) as engine:
                    processor = IncrementalDocumentProcessor(engine, source.sag_source_config_id, max_concurrency=1)
                    operation_id = uuid4().hex
                    request = await processor.build_request(
                        path,
                        context=OperationContext(
                            operation_id=operation_id,
                            idempotency_key=operation_id,
                            request_digest="0" * 64,
                            fence_scope=source.sag_source_config_id,
                            fence_token=1,
                            owner_id=operation_id,
                            isolation="input",
                        ),
                        input_id=uuid4().hex,
                    )
                    request = request.model_copy(
                        update={
                            "extraction_options": request.extraction_options.model_copy(
                                update={
                                    "enable_article_summary": False,
                                    "enable_parent_summary": False,
                                }
                            ),
                        }
                    )
                    receipt = await processor.process(request, should_pause=_never_pause)
                    assert receipt.status == "succeeded" and receipt.checkpoint == "ready"
                async with SessionLocal() as session:
                    document = Document(
                        source_id=source_id,
                        filename=path.name,
                        storage_path=str(path),
                        status=DocumentStatus.PAUSED if recovery == "resume" else DocumentStatus.EXTRACTING,
                    )
                    session.add(document)
                    await session.flush()
                    job = Job(
                        type=JobType.PROCESS_DOCUMENT,
                        source_id=source_id,
                        document_id=document.id,
                        status=JobStatus.PAUSED if recovery == "resume" else JobStatus.RUNNING,
                        payload={
                            "process_request": request.model_dump(mode="json"),
                            "control_revision": 1,
                            "restart_required": "resume_configuration_changed",
                        },
                    )
                    session.add(job)
                    await session.commit()
                    document_id, job_id = document.id, job.id
                path.unlink()
                config.llm_model = "changed-after-commit"
                calls_before = list(models.calls)

                class Queue:
                    async def enqueue(self, _job_id):
                        pytest.fail("a real completed receipt was requeued")

                async with SessionLocal() as session:
                    if recovery == "resume":
                        await resume_document(
                            session,
                            await session.get(Source, source_id),
                            document_id,
                            job_queue=Queue(),
                            engine_manager=manager,
                        )
                    else:
                        await _process_document_unlocked(
                            session, await session.get(Job, job_id), engine_manager=manager, lease_lost=asyncio.Event()
                        )
                async with SessionLocal() as session:
                    document = await session.get(Document, document_id)
                    source = await session.get(Source, source_id)
                    assert document.status == DocumentStatus.READY and document.has_publication
                    assert document.sag_source_id == receipt.source_id
                    assert document.published_operation_id == operation_id
                    assert (await session.get(Job, job_id)).status == JobStatus.SUCCEEDED
                    assert source.chunk_count == document.chunk_count > 0
                assert models.calls == calls_before
            finally:
                await manager.aclose_all()
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Source).where(Source.id == source_id))
            await session.commit()
