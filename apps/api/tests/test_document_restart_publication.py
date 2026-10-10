"""Upgrade publication eligibility and document version controls."""

from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from zleap.sag.exceptions import OperationConflictError

from sag_api.core import db
from sag_api.core.db import SessionLocal, init_db
from sag_api.core.error_taxonomy import ErrorStage
from sag_api.core.errors import ApiError
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs.inproc import InProcessAsyncQueue
from sag_api.jobs.scheduling import MANUAL_RECOVERY, get_blocked_reason
from sag_api.sag.errors import map_sag_errors
from sag_api.services.document_service import publish_document_result, resume_document
from tests.document_requests import (
    UncommittedDocumentManager,
    controller_processing_identity,
    document_receipt,
    document_request,
    processing_parameters,
)
from tests.test_document_manual_recovery import ReadOnlyRecoveryEngine


@pytest.fixture
async def identity_target(tmp_path):
    from sag_api.core.config import Settings
    from sag_api.sag import EngineManager

    path = tmp_path / "identity.md"
    path.write_text("# Input\nA fixed knowledge fact.\n")
    config = Settings(
        _env_file=None,
        data_dir=str(tmp_path / "engine"),
        llm_model="model-a",
        llm_api_key="identity-secret",
        llm_base_url="https://models.example/v1",
        embedding_model="text-embedding-3-small",
        embedding_schema_dimensions=2,
        sag_language="en",
    )
    manager = EngineManager(config)
    range_id = uuid4().hex
    try:
        await manager.provision(range_id)
        await manager.aclose_all()
        yield manager, config, range_id, path
    finally:
        await manager.aclose_all()


@pytest.mark.asyncio
async def test_processing_identity_detects_effective_model_change_without_changing_input(identity_target):
    """Omitting the effective model lets a stale confirmation admit changed work."""
    manager, config, range_id, path = identity_target
    before = await manager.document_processing_identity(range_id, str(path))
    config.llm_model = "model-b"
    after = await manager.document_processing_identity(range_id, str(path))
    assert before["configuration_digest"] != after["configuration_digest"]
    assert before["input_digest"] == after["input_digest"]
    assert "identity-secret" not in str(after)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("embedding_model", "text-embedding-3-large"),
        ("embedding_schema_dimensions", 4),
        ("embedding_request_dimensions", 4),
        ("document_chunk_max_tokens", 2_000),
        ("document_extraction_profile", "concise"),
        ("sag_language", "zh"),
        ("llm_extra_body", {"reasoning_effort": "high"}),
    ],
)
async def test_processing_identity_detects_result_configuration_changes(identity_target, field, value):
    """Missing result-affecting parameters permits a different confirmed extraction."""
    manager, config, range_id, path = identity_target
    before = await manager.document_processing_identity(range_id, str(path))
    setattr(config, field, value)
    after = await manager.document_processing_identity(range_id, str(path))
    assert before["configuration_digest"] != after["configuration_digest"]
    assert before["input_digest"] == after["input_digest"]


@pytest.mark.asyncio
async def test_processing_identity_reads_the_actual_active_entity_contract(identity_target):
    """Hashing config defaults misses an administrator's persisted schema edit."""
    from zleap.sag.core.adapters import registry
    from zleap.sag.db import EntityType

    from sag_api.sag.config_builder import build_engine_config

    manager, config, range_id, path = identity_target
    before = await manager.document_processing_identity(range_id, str(path))
    relational = build_engine_config(config).relational
    store = registry.create_relational_store(relational.provider, config=relational)
    try:
        async with store.session() as session:
            person = await session.scalar(
                select(EntityType).where(
                    EntityType.type == "person",
                    EntityType.is_default.is_(True),
                )
            )
            person.description = "The actual administrator contract has changed."
            await session.commit()
    finally:
        await store.close()
    after = await manager.document_processing_identity(range_id, str(path))
    assert before["configuration_digest"] != after["configuration_digest"]
    assert before["input_digest"] == after["input_digest"]


@pytest.mark.asyncio
async def test_processing_identity_does_not_initialize_a_missing_contract(tmp_path):
    """Opening a confirmation must not silently create a new engine store/schema."""
    from sag_api.core.config import Settings
    from sag_api.core.errors import ServiceUnavailableError
    from sag_api.sag import EngineManager

    path = tmp_path / "input.md"
    path.write_text("A fact.")
    directory = tmp_path / "uninitialized"
    manager = EngineManager(Settings(_env_file=None, data_dir=str(directory)))
    try:
        with pytest.raises(ServiceUnavailableError):
            await manager.document_processing_identity(uuid4().hex, str(path))
        assert not directory.exists()
    finally:
        await manager.aclose_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,changed", [(".pdf", True), (".md", False), (".xlsx", False)])
async def test_processing_identity_tracks_only_the_effective_parser(identity_target, suffix, changed):
    """Ignoring parser routing accepts changed PDF parsing; raw preferences over-trigger others."""
    manager, config, range_id, path = identity_target
    original = path.with_suffix(suffix)
    original.write_bytes(path.read_bytes())
    config.document_parser = "markitdown"
    before = await manager.document_processing_identity(range_id, str(original))
    config.document_parser = "anydoc"
    after = await manager.document_processing_identity(range_id, str(original))
    assert (before["configuration_digest"] != after["configuration_digest"]) is changed
    assert before["input_digest"] == after["input_digest"]


@pytest.mark.asyncio
async def test_processing_identity_detects_local_image_changes(identity_target):
    """A fixed Markdown body can still refer to changed extraction input bytes."""
    manager, _config, range_id, path = identity_target
    path.write_text("# Chart\n![Diagram](assets/chart.png)\n")
    asset = path.parent / "assets" / "chart.png"
    asset.parent.mkdir()
    asset.write_bytes(b"the original image bytes")
    before = await manager.document_processing_identity(range_id, str(path))
    asset.write_bytes(b"a replacement diagram")
    after = await manager.document_processing_identity(range_id, str(path))
    assert before["configuration_digest"] == after["configuration_digest"]
    assert before["input_digest"] != after["input_digest"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["llm", "embedding"])
async def test_processing_identity_uses_public_adapter_identity(identity_target, monkeypatch, kind):
    """An adapter formatter update must invalidate the prior processing identity."""
    from dataclasses import replace

    from zleap.sag.core.adapters import registry

    manager, config, range_id, path = identity_target
    before = await manager.document_processing_identity(range_id, str(path))
    name = "create_llm" if kind == "llm" else "create_embedding"
    original = getattr(registry, name)

    def updated_adapter(*args, **kwargs):
        client = original(*args, **kwargs)
        identity = client.reuse_identity
        client.reuse_identity = lambda: replace(identity(), formatter_version="test-new-formatter")
        return client

    monkeypatch.setattr(registry, name, updated_adapter)
    after = await manager.document_processing_identity(range_id, str(path))
    assert before["configuration_digest"] != after["configuration_digest"]


@pytest.mark.asyncio
async def test_processing_identity_tracks_public_extraction_prompt(identity_target, monkeypatch):
    """A changed extraction template must not retain an earlier confirmation."""
    from dataclasses import replace

    from zleap.sag.modules.extract.prompts import ExtractionPromptRegistry

    manager, _config, range_id, path = identity_target
    before = await manager.document_processing_identity(range_id, str(path))
    original = ExtractionPromptRegistry.load
    monkeypatch.setattr(
        ExtractionPromptRegistry,
        "load",
        lambda self, role: replace(
            original(self, role),
            template="A changed extraction contract.",
        ),
    )
    after = await manager.document_processing_identity(range_id, str(path))
    assert before["configuration_digest"] != after["configuration_digest"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("llm_responses_send_temperature", True),
        ("llm_responses_endpoint", "https://other.example/v1/responses"),
    ],
)
async def test_processing_identity_tracks_effective_responses_configuration(identity_target, field, value):
    """Responses options are consumed outside EngineConfig and still affect extraction."""
    manager, config, range_id, path = identity_target
    config.llm_provider = "responses"
    config.llm_responses_endpoint = "https://first.example/v1/responses"
    before = await manager.document_processing_identity(range_id, str(path))
    setattr(config, field, value)
    after = await manager.document_processing_identity(range_id, str(path))
    assert before["configuration_digest"] != after["configuration_digest"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("llm_api_key", "rotated-key"),
        ("embedding_api_key", "rotated-key"),
        ("document_extract_concurrency", 7),
        ("embedding_concurrency", 7),
        ("llm_timeout_ms", 45_000),
        ("llm_max_retries", 9),
        ("embedding_timeout", 90),
        ("document_chunk_mode", "heading_strict"),
        ("mineru_version", "unselected-version"),
        ("llm_responses_endpoint", "https://unselected.example/v1/responses"),
    ],
)
async def test_processing_identity_ignores_execution_and_unselected_options(identity_target, field, value):
    """Unrelated settings must not force users to discard eligible successful work."""
    manager, config, range_id, path = identity_target
    before = await manager.document_processing_identity(range_id, str(path))
    setattr(config, field, value)
    after = await manager.document_processing_identity(range_id, str(path))
    assert before == after


@pytest.mark.asyncio
async def test_processing_identity_detects_a_different_storage_target(identity_target):
    """Identical schemas in two databases do not identify the same durable operation."""
    import shutil

    manager, config, range_id, path = identity_target
    before = await manager.document_processing_identity(range_id, str(path))
    copied = path.parent / "other-engine"
    shutil.copytree(config.data_dir, copied)
    config.data_dir = str(copied)
    after = await manager.document_processing_identity(range_id, str(path))
    assert before["configuration_digest"] != after["configuration_digest"]
    assert before["input_digest"] == after["input_digest"]


@pytest.fixture(autouse=True)
async def cleanup_upgrade_documents():
    await init_db()
    yield
    async with SessionLocal() as session:
        await session.execute(delete(Source).where(Source.name == "publication-upgrade"))
        await session.commit()


async def seed_legacy_document(status, *, with_job=True):
    async with SessionLocal() as session:
        source = Source(name="publication-upgrade", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="legacy.md",
            storage_path="/virtual/legacy.md",
            status=status,
            sag_source_id="legacy-input",
            chunk_count=4,
            event_count=2,
        )
        # Existing rows receive zero from the host's ADD COLUMN migration.
        document.control_revision = 0
        session.add(document)
        await session.flush()
        job = None
        if with_job:
            job = Job(
                type=JobType.PROCESS_DOCUMENT,
                source_id=source.id,
                document_id=document.id,
                status=JobStatus.SUCCEEDED if status == DocumentStatus.READY else JobStatus.PAUSED,
                payload={"process_checkpoint": {"chunk_version": "legacy", "source_id": "legacy-input"}},
            )
            session.add(job)
        await session.commit()
        return source.id, document.id, job.id if job else None


@pytest.mark.asyncio
async def test_upgrade_revokes_early_legacy_id_without_losing_cleanup_binding():
    """Keeping an early ingest ID on Document would expose incomplete content."""
    await init_db()
    source_id, document_id, job_id = await seed_legacy_document(DocumentStatus.PAUSED)
    queue = InProcessAsyncQueue(SessionLocal, ReadOnlyRecoveryEngine(None), concurrency=1)
    await queue._recover()
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        job = await session.get(Job, job_id)
        assert document.sag_source_id is None
        assert document.chunk_count == 0 and document.event_count == 0
        assert document.control_revision == 1
        assert job.payload["derived_source_ids"] == ["legacy-input"]
        assert job.payload["process_checkpoint"]["chunk_version"] == "legacy"
        assert job.status == JobStatus.PAUSED
        assert get_blocked_reason(job.payload) == MANUAL_RECOVERY
        assert not job.payload.get("process_request")
        assert (await session.get(Source, source_id)).chunk_count == 0


@pytest.mark.asyncio
async def test_upgrade_preserves_ready_input_and_never_requalifies_a_later_candidate():
    """Repeating qualification during candidate pause must retain the old publication."""
    await init_db()
    _, document_id, old_job_id = await seed_legacy_document(DocumentStatus.READY)
    queue = InProcessAsyncQueue(SessionLocal, ReadOnlyRecoveryEngine(None), concurrency=1)
    await queue._recover()
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        assert document.sag_source_id == "legacy-input"
        assert document.control_revision == 1
        assert document.published_operation_id is None
        assert document.chunk_count == 4 and document.event_count == 2
        assert (await session.get(Job, old_job_id)).status == JobStatus.SUCCEEDED
        document.status = DocumentStatus.PAUSED
        session.add(
            Job(
                type=JobType.PROCESS_DOCUMENT,
                source_id=document.source_id,
                document_id=document.id,
                status=JobStatus.PAUSED,
                payload={},
            )
        )
        await session.commit()
    await queue._recover()
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        assert document.sag_source_id == "legacy-input"
        assert document.chunk_count == 4 and document.event_count == 2


@pytest.mark.asyncio
async def test_upgrade_orphan_incomplete_input_preserves_data_for_manual_restart():
    """An absent old Job must not erase the only handle for orphan cleanup."""
    await init_db()
    _, document_id, _ = await seed_legacy_document(DocumentStatus.EXTRACTING, with_job=False)
    queue = InProcessAsyncQueue(SessionLocal, ReadOnlyRecoveryEngine(None), concurrency=1)
    await queue._recover()
    await queue._recover()
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        assert document.sag_source_id is None
        assert document.status == DocumentStatus.PAUSED
        assert document.storage_path == "/virtual/legacy.md"
        jobs = list((await session.scalars(select(Job).where(Job.document_id == document_id))).all())
        assert len(jobs) == 1
        assert jobs[0].type == JobType.PROCESS_DOCUMENT
        assert jobs[0].status == JobStatus.PAUSED
        assert jobs[0].payload["derived_source_ids"] == ["legacy-input"]
        assert get_blocked_reason(jobs[0].payload) == MANUAL_RECOVERY
        assert document.error


@pytest.mark.asyncio
async def test_new_document_does_not_enter_legacy_qualification_on_restart():
    """New candidates with an existing publication must bypass historical classification."""
    await init_db()
    async with SessionLocal() as session:
        source = Source(name="publication-upgrade", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="new.md",
            storage_path="/virtual/new.md",
            status=DocumentStatus.PAUSED,
            sag_source_id="published-input",
        )
        session.add(document)
        await session.commit()
        document_id = document.id
    queue = InProcessAsyncQueue(SessionLocal, ReadOnlyRecoveryEngine(None), concurrency=1)
    await queue._recover()
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        assert document.control_revision == 1
        assert document.sag_source_id == "published-input"


@pytest.mark.asyncio
async def test_upgrade_unconfirmed_completed_job_is_manual_when_document_is_not_ready():
    """A contradictory old Job must not silently certify a partial document."""
    await init_db()
    _, document_id, job_id = await seed_legacy_document(DocumentStatus.EXTRACTING)
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        job.status = JobStatus.SUCCEEDED
        await session.commit()
    await InProcessAsyncQueue(SessionLocal, ReadOnlyRecoveryEngine(None), concurrency=1)._recover()
    async with SessionLocal() as session:
        assert (await session.get(Document, document_id)).sag_source_id is None
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.PAUSED
        assert get_blocked_reason(job.payload) == MANUAL_RECOVERY


@pytest.mark.asyncio
async def test_host_upgrade_preserves_old_rows_for_one_time_publication_qualification(tmp_path, monkeypatch):
    """A default of one on ALTER TABLE would skip all historical qualification."""
    legacy = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-publication.db'}")
    try:
        async with legacy.begin() as connection:
            await connection.execute(text("CREATE TABLE documents (id TEXT PRIMARY KEY, sag_source_id TEXT)"))
            await connection.execute(text("INSERT INTO documents VALUES ('old', 'old-input')"))
        monkeypatch.setattr(db, "engine", legacy)
        await db._ensure_columns()
        await db._ensure_columns()
        async with legacy.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT sag_source_id, control_revision, published_operation_id FROM documents")
                )
            ).one()
            assert tuple(row) == ("old-input", 0, None)
    finally:
        await legacy.dispose()


@pytest.mark.parametrize("reason", [
    "resume_configuration_changed", "resume_input_changed", "resume_input_unavailable",
    "resume_extraction_changed", "resume_artifacts_changed",
])
def test_engine_resume_conflict_exposes_restart_reason_without_parsing_message(reason):
    """Discarding typed engine details leaves the UI unable to offer confirmed restart."""
    with pytest.raises(ApiError) as caught, map_sag_errors(stage=ErrorStage.EXTRACT):
        raise OperationConflictError("arbitrary provider-independent wording", details={"reason": reason})
    error = caught.value
    assert error.status_code == 409
    assert error.code == "document_restart_required"
    assert error.retryable is False
    envelope = error.to_envelope(request_id="request-test")["error"]
    assert envelope["restart_reason"] == reason
    assert envelope["layer"] == "engine" and envelope["stage"] == "extract"
    assert envelope["request_id"] == "request-test"


def test_other_engine_conflict_does_not_offer_document_restart():
    """Unrelated operation conflicts must retain their existing error meaning."""
    with pytest.raises(ApiError) as caught, map_sag_errors(stage=ErrorStage.EXTRACT):
        raise OperationConflictError("resume_input_changed appears only in text", details={"reason": "other_conflict"})
    assert caught.value.code == "validation_error"
    assert "restart_reason" not in caught.value.to_envelope()["error"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("controller_identity")
@pytest.mark.parametrize("reason", [
    "resume_configuration_changed", "resume_extraction_changed", "resume_artifacts_changed",
])
async def test_worker_resume_conflict_waits_for_confirmation_without_touching_publication(
    document_engine_manager, tmp_path, reason
):
    """A generic failed/retry transition would discard the user's restart decision."""
    await init_db()
    raw = tmp_path / "published.md"
    raw.write_text("# Published original\nA complete original fact.\n")
    async with SessionLocal() as session:
        source = Source(name="publication-upgrade", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="candidate.md",
            storage_path=str(raw),
            status=DocumentStatus.PENDING,
            sag_source_id="published-input",
            published_operation_id="published-operation",
            chunk_count=4,
            event_count=2,
        )
        session.add(document)
        await session.flush()
        request = document_request(source.sag_source_config_id, source_id="candidate-input")
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.QUEUED,
            payload={
                "processing_identity": controller_processing_identity(document.storage_path),
                "process_request": request.model_dump(mode="json"),
                "control_revision": 1,
            },
        )
        session.add(job)
        await session.commit()
        source_id, document_id, job_id = source.id, document.id, job.id

    class ConfigurationChangedEngine(UncommittedDocumentManager):
        async def process_document(self, _range_id, request_factory, **kwargs):
            bound = await request_factory(None, *processing_parameters())
            assert bound.context.operation_id == request.context.operation_id
            with map_sag_errors(stage=ErrorStage.EXTRACT):
                raise OperationConflictError("changed processing inputs", details={"reason": reason})

    queue = InProcessAsyncQueue(SessionLocal, ConfigurationChangedEngine(), concurrency=1)
    await queue._run_job(job_id)
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        job = await session.get(Job, job_id)
        assert document.status == DocumentStatus.PAUSED
        assert document.processing_stage == "waiting_confirmation"
        assert document.sag_source_id == "published-input"
        assert document.published_operation_id == "published-operation"
        assert document.storage_path == str(raw)
        assert document.chunk_count == 4 and document.event_count == 2
        assert job.status == JobStatus.PAUSED and job.attempts == 1
        assert job.payload["restart_required"] == reason
        assert job.payload["process_request"] == request.model_dump(mode="json")
        assert get_blocked_reason(job.payload) == MANUAL_RECOVERY
        assert queue._queue.empty()
        with pytest.raises(ApiError) as caught:
            await resume_document(
                session,
                await session.get(Source, source_id),
                document_id,
                job_queue=queue,
                engine_manager=document_engine_manager,
            )
        assert caught.value.to_envelope()["error"]["restart_reason"] == reason


@pytest.mark.asyncio
async def test_recovered_legacy_reprocess_resumes_through_confirmation(restart_confirmation_api, identity_target):
    """The existing UI continue action must reach a fresh, confirmed processing job."""
    client, url, job_id, queue, _config, raw = restart_confirmation_api
    async with SessionLocal() as session:
        legacy = await session.get(Job, job_id)
        document = await session.get(Document, legacy.document_id)
        legacy.type = JobType.REPROCESS_DOCUMENT
        legacy.status = JobStatus.QUEUED
        legacy.payload = {"target_document_id": document.id, "derived_source_ids": ["published-input"]}
        document.status = DocumentStatus.PENDING
        publication = document.published_operation_id
        await session.commit()
    recovery = InProcessAsyncQueue(SessionLocal, identity_target[0], concurrency=1)
    try:
        await recovery._recover()
        async with SessionLocal() as session:
            legacy = await session.get(Job, job_id)
            document = await session.get(Document, legacy.document_id)
            assert legacy.status == JobStatus.PAUSED and document.status == DocumentStatus.PAUSED
            assert get_blocked_reason(legacy.payload) == MANUAL_RECOVERY
            assert "restart_required" not in legacy.payload
        response = await client.post(f"{url}/resume")
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "document_restart_required"
        assert error["restart_reason"] == "legacy_checkpoint"
        token = error["confirmation_token"]
        assert token and queue.ids == []
        first = await client.post(f"{url}/reprocess", json={"confirmation_token": token})
        second = await client.post(f"{url}/reprocess", json={"confirmation_token": token})
        assert first.status_code == second.status_code == 200
        assert first.json()["id"] == second.json()["id"] != job_id
        assert queue.ids == [first.json()["id"]]
        async with SessionLocal() as session:
            old = await session.get(Job, job_id)
            new = await session.get(Job, first.json()["id"])
            document = await session.get(Document, old.document_id)
            assert old.status == JobStatus.FAILED and old.payload["replacement_job_id"] == new.id
            assert old.payload["derived_source_ids"] == ["published-input"]
            assert new.type == JobType.PROCESS_DOCUMENT and new.status == JobStatus.QUEUED
            assert new.payload["control_revision"] == document.control_revision == 2
            assert new.payload["input_artifact"] and new.payload["confirmed_identity"]
            assert not {"process_request", "process_checkpoint", "reuse_options"}.intersection(new.payload)
            assert document.sag_source_id == "published-input"
            assert document.published_operation_id == publication and document.storage_path == str(raw)
            assert len(list(await session.scalars(select(Job).where(Job.document_id == document.id)))) == 3
    finally:
        await recovery.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("already_published", [False, True])
async def test_superseded_control_revision_cannot_publish_or_claim_reconciliation(already_published):
    """Matching operation/run identity alone must not let an old candidate own a new control."""
    async with SessionLocal() as session:
        source = Source(name="publication-upgrade", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.flush()
        request = document_request(source.sag_source_config_id, source_id="old-input")
        document = Document(
            source_id=source.id,
            filename="published.md",
            storage_path="/virtual/published.md",
            status=DocumentStatus.EXTRACTING,
            sag_source_id="old-input" if already_published else "published-input",
            published_operation_id=request.context.operation_id if already_published else "published-operation",
            chunk_count=4,
            event_count=2,
            processing_run_id="new-run",
            control_revision=2,
        )
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.RUNNING,
            payload={"process_request": request.model_dump(mode="json"), "control_revision": 1},
        )
        session.add(job)
        await session.commit()
        job_id, document_id = job.id, document.id
        assert not await publish_document_result(
            session,
            source,
            document,
            job,
            document_receipt(request, chunk_count=1, event_count=1),
            expected_run_id="new-run",
        )
        await session.commit()
        await session.refresh(document)
        await session.refresh(job)
        assert document.status == DocumentStatus.EXTRACTING
        assert document.control_revision == 2
        assert document.chunk_count == 4 and document.event_count == 2
        assert job.status == JobStatus.RUNNING
        assert job.id == job_id and document.id == document_id


class CandidateQueue:
    def __init__(self):
        self.ids = []
        self.maintenance = []

    async def enqueue(self, job_id):
        self.ids.append(job_id)

    def begin_source_maintenance(self, source_id, job_id):
        self.maintenance.append((source_id, job_id))


@pytest.fixture
async def restart_confirmation_api(published_document, identity_target):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from httpx import ASGITransport, AsyncClient

    from sag_api.api.v1.documents import router
    from sag_api.core.deps import get_current_user, get_current_user_or_connector, get_engine_manager, get_job_queue

    manager, config, _range_id, _path = identity_target
    source_id, document_id, _published_job_id, _published_operation, raw = published_document
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        document.status = DocumentStatus.PAUSED
        request = document_request(
            (await session.get(Source, source_id)).sag_source_config_id, source_id="paused-input"
        )
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source_id,
            document_id=document_id,
            status=JobStatus.PAUSED,
            payload={
                "control_revision": 1,
                "process_request": request.model_dump(mode="json"),
                "restart_required": "resume_configuration_changed",
            },
        )
        session.add(job)
        await session.commit()
        job_id = job.id
    queue = CandidateQueue()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    @app.exception_handler(ApiError)
    async def api_error(_request, error):
        return JSONResponse(error.to_envelope(), status_code=error.status_code)

    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[get_current_user_or_connector] = lambda: None
    app.dependency_overrides[get_engine_manager] = lambda: manager
    app.dependency_overrides[get_job_queue] = lambda: queue
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, f"/api/v1/sources/{source_id}/documents/{document_id}", job_id, queue, config, raw


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [None, "input", "configuration", "schema", "missing_baseline"])
async def test_normal_resume_checks_saved_raw_identity_before_queueing(
    restart_confirmation_api, identity_target, changed
):
    from zleap.sag.core.adapters import registry
    from zleap.sag.db import EntityType

    from sag_api.sag.config_builder import build_engine_config

    client, url, job_id, queue, config, raw = restart_confirmation_api
    manager = identity_target[0]
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        document = await session.get(Document, job.document_id)
        source = await session.get(Source, document.source_id)
        baseline = await manager.document_processing_identity(source.sag_source_config_id, str(raw), source=source)
        payload = dict(job.payload)
        payload.pop("restart_required")
        if changed != "missing_baseline":
            payload["processing_identity"] = baseline
        job.payload = payload
        await session.commit()
        original_request = payload["process_request"]
    if changed == "input":
        raw.write_text("# Changed\nThe original input has changed while paused.")
    elif changed == "configuration":
        config.llm_model = "a-new-model"
    elif changed == "schema":
        relational = build_engine_config(config).relational
        store = registry.create_relational_store(relational.provider, config=relational)
        try:
            async with store.session() as session:
                person = await session.scalar(
                    select(EntityType).where(EntityType.is_default.is_(True), EntityType.type == "person")
                )
                person.description = "Changed active contract while paused."
                await session.commit()
        finally:
            await store.close()
    response = await client.post(f"{url}/resume")
    if changed is None:
        assert response.status_code == 200
        assert queue.ids == [job_id]
    else:
        assert response.status_code == 409
        error = response.json()["error"]
        assert (
            error["restart_reason"]
            == {
                "input": "resume_input_changed",
                "configuration": "resume_configuration_changed",
                "schema": "resume_configuration_changed",
                "missing_baseline": "legacy_processing_identity",
            }[changed]
        )
        assert error["confirmation_token"]
        assert queue.ids == []
        async with SessionLocal() as session:
            job = await session.get(Job, job_id)
            document = await session.get(Document, job.document_id)
            assert job.status == JobStatus.PAUSED and document.status == DocumentStatus.PAUSED
            assert job.payload["process_request"] == original_request
            assert document.sag_source_id == "published-input" and document.storage_path == str(raw)


@pytest.mark.asyncio
async def test_restart_confirmation_is_bound_without_creating_work(restart_confirmation_api):
    """A reason without a durable challenge cannot safely confirm the target."""
    client, url, job_id, queue, _config, raw = restart_confirmation_api
    response = await client.post(f"{url}/resume")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["restart_reason"] == "resume_configuration_changed"
    assert isinstance(error["confirmation_token"], str) and len(error["confirmation_token"]) >= 32
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        document = await session.get(Document, job.document_id)
        assert job.status == JobStatus.PAUSED
        assert job.payload["process_request"]["source"]["descriptor"]["source_id"] == "paused-input"
        assert document.status == DocumentStatus.PAUSED and document.control_revision == 1
        assert document.sag_source_id == "published-input" and document.storage_path == str(raw)
        assert len(list(await session.scalars(select(Job).where(Job.document_id == document.id)))) == 2
        assert job.payload["restart_confirmation"]["token"] == error["confirmation_token"]
    assert queue.ids == []


@pytest.mark.asyncio
async def test_repeated_restart_confirmation_creates_one_fresh_candidate(restart_confirmation_api):
    """Repeated confirmation must retire one old task and own exactly one replacement."""
    client, url, job_id, queue, _config, raw = restart_confirmation_api
    response = await client.post(f"{url}/resume")
    token = response.json()["error"]["confirmation_token"]
    first = await client.post(f"{url}/reprocess", json={"confirmation_token": token})
    assert first.status_code == 200
    second = await client.post(f"{url}/reprocess", json={"confirmation_token": token})
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"] != job_id
    assert queue.ids == [first.json()["id"]]
    async with SessionLocal() as session:
        old = await session.get(Job, job_id)
        new = await session.get(Job, first.json()["id"])
        document = await session.get(Document, old.document_id)
        assert old.status == JobStatus.FAILED and old.finished_at is not None
        assert old.payload["replacement_job_id"] == new.id
        assert old.payload["process_request"]["source"]["descriptor"]["source_id"] == "paused-input"
        assert new.type == JobType.PROCESS_DOCUMENT and new.status == JobStatus.QUEUED
        assert new.payload["control_revision"] == document.control_revision == 2
        assert "process_request" not in new.payload and "reuse_options" not in new.payload
        assert (
            new.payload["confirmed_identity"]["configuration_digest"]
            == old.payload["restart_confirmation"]["configuration_digest"]
        )
        assert document.sag_source_id == "published-input" and document.storage_path == str(raw)
        assert len(list(await session.scalars(select(Job).where(Job.document_id == document.id)))) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["model", "input", "expiry"])
async def test_changed_restart_target_requires_a_new_confirmation(restart_confirmation_api, change):
    """An old dialog must not authorize work whose model/input or validity has changed."""
    client, url, job_id, queue, config, raw = restart_confirmation_api
    token = (await client.post(f"{url}/resume")).json()["error"]["confirmation_token"]
    if change == "model":
        config.llm_model = "model-c"
    elif change == "input":
        raw.write_text("# The new input\nMust be separately confirmed.\n")
    else:
        async with SessionLocal() as session:
            job = await session.get(Job, job_id)
            job.payload = {
                **job.payload,
                "restart_confirmation": {**job.payload["restart_confirmation"], "expires_at": 0},
            }
            await session.commit()
    rejected = await client.post(f"{url}/reprocess", json={"confirmation_token": token})
    assert rejected.status_code == 409
    error = rejected.json()["error"]
    assert error["code"] == "document_restart_required"
    assert error["confirmation_token"] != token
    assert queue.ids == []
    async with SessionLocal() as session:
        old = await session.get(Job, job_id)
        document = await session.get(Document, old.document_id)
        assert old.status == JobStatus.PAUSED and document.control_revision == 1
        assert len(list(await session.scalars(select(Job).where(Job.document_id == document.id)))) == 2
        assert document.sag_source_id == "published-input"
    accepted = await client.post(f"{url}/reprocess", json={"confirmation_token": error["confirmation_token"]})
    assert accepted.status_code == 200
    assert queue.ids == [accepted.json()["id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revision", "operation", "job_identity", "unknown_token"])
async def test_restart_confirmation_cannot_authorize_another_control_or_operation(restart_confirmation_api, change):
    """A valid-looking token must not transfer authority to a newer control/operation."""
    client, url, job_id, queue, _config, _raw = restart_confirmation_api
    token = (await client.post(f"{url}/resume")).json()["error"]["confirmation_token"]
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        if change == "revision":
            document = await session.get(Document, job.document_id)
            document.control_revision += 1
        elif change == "operation":
            request = job.payload["process_request"]
            job.payload = {
                **job.payload,
                "process_request": {**request, "context": {**request["context"], "operation_id": uuid4().hex}},
            }
        elif change == "job_identity":
            job.payload = {
                **job.payload,
                "restart_confirmation": {**job.payload["restart_confirmation"], "job_id": uuid4().hex},
            }
        else:
            token = "x" * 43
        await session.commit()
    rejected = await client.post(f"{url}/reprocess", json={"confirmation_token": token})
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "conflict"
    assert queue.ids == []
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.PAUSED
        assert len(list(await session.scalars(select(Job).where(Job.document_id == job.document_id)))) == 2


@pytest.fixture
async def published_document(tmp_path, monkeypatch):
    from sag_api.core.config import settings

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "candidates"))
    raw = tmp_path / "published.md"
    raw.write_text("# Published original\nThe original complete publication.\n")
    async with SessionLocal() as session:
        source = Source(
            name="publication-upgrade",
            sag_source_config_id=uuid4().hex,
            document_count=1,
            chunk_count=4,
            event_count=2,
        )
        session.add(source)
        await session.flush()
        request = document_request(source.sag_source_config_id, source_id="published-input")
        document = Document(
            source_id=source.id,
            filename="published.md",
            storage_path=str(raw),
            size_bytes=raw.stat().st_size,
            status=DocumentStatus.READY,
            sag_source_id="published-input",
            published_operation_id=request.context.operation_id,
            chunk_count=4,
            event_count=2,
            token_usage=120,
            parser_provider="original",
            parser_status="done",
            vector_identity={"model": "published-embedding"},
            progress=100,
        )
        session.add(document)
        await session.flush()
        old_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.SUCCEEDED,
            payload={"process_request": request.model_dump(mode="json"), "control_revision": 1},
        )
        session.add(old_job)
        await session.commit()
        return source.id, document.id, old_job.id, request.context.operation_id, raw


@pytest.mark.asyncio
async def test_reprocess_keeps_publication_and_freezes_a_separate_candidate(
    published_document, document_engine_manager
):
    """Clearing publication or reusing its raw location makes reprocessing destructive."""
    from pathlib import Path

    from sag_api.services.document_service import reprocess_document

    source_id, document_id, old_job_id, published_operation, raw = published_document
    queue = CandidateQueue()
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        document = await session.get(Document, document_id)
        job = await reprocess_document(
            session, source, document_id, job_queue=queue, engine_manager=document_engine_manager
        )
        assert document.sag_source_id == "published-input"
        assert document.published_operation_id == published_operation
        assert document.has_publication
        assert document.storage_path == str(raw)
        assert (document.chunk_count, document.event_count, document.token_usage) == (4, 2, 120)
        assert document.vector_identity == {"model": "published-embedding"}
        assert (document.parser_provider, document.parser_status) == ("original", "done")
        assert (source.document_count, source.chunk_count, source.event_count) == (1, 4, 2)
        assert document.status == DocumentStatus.PENDING and document.control_revision == 2
        assert job.type == JobType.PROCESS_DOCUMENT and job.id != old_job_id
        assert job.payload["control_revision"] == 2
        assert "process_request" not in job.payload
        candidate = Path(job.payload["input_artifact"]["path"])
        assert candidate != raw
        assert candidate.read_text() == "# Published original\nThe original complete publication.\n"
        raw.write_text("# A later mutation\nMust not enter the frozen candidate.\n")
        assert candidate.read_text() == "# Published original\nThe original complete publication.\n"
        assert queue.ids == [job.id] and queue.maintenance == []
        assert (await session.get(Job, old_job_id)).status == JobStatus.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["paused", "failed", "published"])
@pytest.mark.usefixtures("controller_identity")
async def test_candidate_uses_frozen_raw_and_switches_artifacts_only_on_publication(
    published_document, outcome, document_engine_manager
):
    """Parsing the published raw or switching its provenance early mixes versions."""
    from pathlib import Path
    from types import SimpleNamespace

    from sag_api.jobs import tasks
    from sag_api.jobs.control import JobPaused
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor
    from sag_api.services.document_service import reprocess_document

    source_id, document_id, old_job_id, published_operation, raw = published_document
    queue = CandidateQueue()
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        job = await reprocess_document(
            session, source, document_id, job_queue=queue, engine_manager=document_engine_manager
        )
        candidate_path = job.payload["input_artifact"]["path"]
        assert candidate_path != str(raw)
        raw.write_text("# Later raw\nMust not be parsed by this candidate.\n")
        # Provenance belongs to the old publication until the transaction switches it.
        document = await session.get(Document, document_id)
        document.parser_provider = "mineru"
        document.parser_status = "done"
        job.status = JobStatus.RUNNING
        await session.commit()
        job_id = job.id

        class CandidateManager(UncommittedDocumentManager):
            async def process_document(self, config_id, request_factory, **kwargs):
                processor = IncrementalDocumentProcessor(
                    SimpleNamespace(resources=SimpleNamespace(prompts=SimpleNamespace(language="en"))),
                    config_id,
                    max_concurrency=1,
                )
                bound = await request_factory(processor, *processing_parameters())
                assert bound.context.operation_id != published_operation
                assert bound.source.descriptor.source_id != "published-input"
                assert bound.source.content == "# Published original\nThe original complete publication.\n"
                assert bound.source.original_file.path == candidate_path
                assert bound.reuse_options is None
                async with SessionLocal() as reading:
                    current = await reading.get(Document, document_id)
                    saved_job = await reading.get(Job, job_id)
                    assert current.sag_source_id == "published-input"
                    assert current.storage_path == str(raw)
                    assert (current.parser_provider, current.parser_status) == ("mineru", "done")
                    assert saved_job.payload["process_request"] == bound.model_dump(mode="json")
                if outcome == "failed":
                    raise RuntimeError("candidate model failed")
                if outcome == "paused":
                    return None
                return document_receipt(bound, chunk_count=2, event_count=1, token_usage=60)

        if outcome == "published":
            await tasks.process_document(session, job, engine_manager=CandidateManager())
        elif outcome == "paused":
            with pytest.raises(JobPaused):
                await tasks.process_document(session, job, engine_manager=CandidateManager())
        else:
            with pytest.raises(RuntimeError, match="candidate model failed"):
                await tasks.process_document(session, job, engine_manager=CandidateManager())
        await session.refresh(document)
        await session.refresh(job)
        await session.refresh(source)
        if outcome == "published":
            assert document.status == DocumentStatus.READY
            assert document.storage_path == candidate_path
            assert (document.parser_provider, document.parser_status) == ("original", "done")
            assert document.sag_source_id == job.payload["process_request"]["source"]["descriptor"]["source_id"]
            assert document.published_operation_id == job.payload["process_request"]["context"]["operation_id"]
            assert (document.chunk_count, document.event_count, document.token_usage) == (2, 1, 60)
            assert (source.chunk_count, source.event_count) == (2, 1)
            assert job.status == JobStatus.SUCCEEDED
        else:
            assert document.status == (DocumentStatus.PAUSED if outcome == "paused" else DocumentStatus.FAILED)
            assert document.sag_source_id == "published-input"
            assert document.published_operation_id == published_operation
            assert document.storage_path == str(raw)
            assert (document.parser_provider, document.parser_status) == ("mineru", "done")
            assert (document.chunk_count, document.event_count, document.token_usage) == (4, 2, 120)
            assert (source.chunk_count, source.event_count) == (4, 2)
        assert Path(candidate_path).read_text() == "# Published original\nThe original complete publication.\n"
        assert (await session.get(Job, old_job_id)).status == JobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_concurrent_reprocess_creates_one_candidate_and_one_control_revision(
    published_document, document_engine_manager
):
    """A status-only claim can allow two restarts to own the same document."""
    import asyncio

    from sag_api.services.document_service import reprocess_document

    source_id, document_id, *_ = published_document
    queue = CandidateQueue()

    async def restart():
        async with SessionLocal() as session:
            source = await session.get(Source, source_id)
            return await reprocess_document(
                session, source, document_id, job_queue=queue, engine_manager=document_engine_manager
            )

    first, second = await asyncio.gather(restart(), restart())
    assert first.id == second.id
    assert queue.ids == [first.id] and queue.maintenance == []
    async with SessionLocal() as session:
        jobs = list((await session.scalars(select(Job).where(Job.document_id == document_id))).all())
        assert len(jobs) == 2
        current = await session.get(Document, document_id)
        assert current.control_revision == 2
        assert current.sag_source_id == "published-input"


@pytest.mark.asyncio
async def test_old_reprocess_job_cannot_execute_destructive_cleanup(published_document, document_engine_manager):
    """Leaving the retired cleanup handler callable would still delete a publication."""
    source_id, document_id, _, published_operation, raw = published_document
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        document.status = DocumentStatus.PENDING
        legacy = Job(
            type=JobType.REPROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source_id,
            document_id=document_id,
            payload={"target_document_id": document_id, "derived_source_ids": ["published-input"]},
        )
        session.add(legacy)
        await session.commit()
        job_id = legacy.id

    class CleanupEngine:
        def __init__(self):
            self.deleted = []

        async def delete_data(self, request, *, source):
            from tests.document_requests import deletion_receipt

            self.deleted.append(request.source_id)
            return deletion_receipt(request)

    engine = CleanupEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=1)
    await queue._run_job(job_id)
    assert engine.deleted == []
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        legacy = await session.get(Job, job_id)
        assert document.sag_source_id == "published-input"
        assert document.published_operation_id == published_operation
        assert document.storage_path == str(raw)
        assert document.status == DocumentStatus.PAUSED
        assert document.processing_stage == "waiting_confirmation"
        assert legacy.status == JobStatus.PAUSED and legacy.attempts == 0
        assert legacy.payload["restart_required"] == "legacy_checkpoint"
        assert get_blocked_reason(legacy.payload) == MANUAL_RECOVERY
        assert len(list((await session.scalars(select(Job).where(Job.document_id == document_id))).all())) == 2
        with pytest.raises(ApiError) as caught:
            from sag_api.services.document_service import reprocess_document

            await reprocess_document(
                session,
                await session.get(Source, source_id),
                document_id,
                job_queue=queue,
                engine_manager=document_engine_manager,
            )
        assert caught.value.code == "document_restart_required"
        assert queue._queue.empty()


@pytest.mark.asyncio
async def test_delete_after_candidate_publication_keeps_files_until_engine_cleanup(
    published_document, document_engine_manager
):
    """Knowing only the current raw path leaks all earlier and paused artifacts."""
    from pathlib import Path
    from types import SimpleNamespace

    from sag_api.jobs import tasks
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor
    from sag_api.services.document_service import delete_document, reprocess_document

    source_id, document_id, *_rest, old_raw = published_document
    queue = CandidateQueue()
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        candidate = await reprocess_document(
            session, source, document_id, job_queue=queue, engine_manager=document_engine_manager
        )
        raw_path = Path(candidate.payload["input_artifact"]["path"])
        processor = IncrementalDocumentProcessor(
            SimpleNamespace(resources=SimpleNamespace(prompts=SimpleNamespace(language="en"))),
            source.sag_source_config_id,
            max_concurrency=1,
        )
        bound = await processor.build_request(
            raw_path,
            context=document_request(source.sag_source_config_id).context,
            input_id="replacement-input",
            original_path=raw_path,
        )
        candidate.payload = {**candidate.payload, "process_request": bound.model_dump(mode="json")}
        await session.commit()
        document = await session.get(Document, document_id)
        assert await publish_document_result(
            session,
            source,
            document,
            candidate,
            document_receipt(bound),
            expected_run_id=None,
        )
        await session.commit()
        assert document.storage_path == str(raw_path)
        unfinished = await reprocess_document(
            session, source, document_id, job_queue=queue, engine_manager=document_engine_manager
        )
        unfinished_path = Path(unfinished.payload["input_artifact"]["path"])
        # Parser crashes can leave non-sidecar temporary files in the owned directory.
        residual = unfinished_path.parent / ".parsed-assets-interrupted"
        residual.write_bytes(b"candidate asset")
        peer = unfinished_path.parent.parent.parent / "peer-document" / "peer-job" / "peer.md"
        peer.parent.mkdir(parents=True)
        peer.write_text("Peer document must survive.")
        cleanup = await delete_document(session, source, document_id, job_queue=queue)

        class CleanupEngine:
            fail = True
            deleted = []

            async def delete_data(self, request, *, source):
                from tests.document_requests import deletion_receipt

                assert request.data_source_id == source.sag_source_config_id
                assert old_raw.exists() and raw_path.exists() and unfinished_path.exists()
                if self.fail:
                    raise RuntimeError("engine cleanup unavailable")
                self.deleted.append(request.source_id)
                return deletion_receipt(request)

        engine = CleanupEngine()
        with pytest.raises(RuntimeError, match="engine cleanup unavailable"):
            await tasks.delete_document_task(session, cleanup, engine_manager=engine)
        assert old_raw.exists() and raw_path.exists() and unfinished_path.exists()
        engine.fail = False
        await tasks.delete_document_task(session, cleanup, engine_manager=engine)
        assert set(engine.deleted) == {"published-input", "replacement-input"}
        assert await session.get(Document, document_id) is None
        assert not old_raw.exists()
        assert not raw_path.parent.exists()
        assert not unfinished_path.parent.exists()
        assert peer.read_text() == "Peer document must survive."


@pytest.mark.asyncio
async def test_reprocess_copies_relative_markdown_images_into_candidate(published_document, document_engine_manager):
    """Moving Markdown without its relative assets makes the same input unreadable."""
    from pathlib import Path

    from zleap.sag.modules.load.images import ImageFetcher

    from sag_api.services.document_service import reprocess_document

    source_id, document_id, *_rest, raw = published_document
    image = raw.parent / "images" / "diagram.png"
    image.parent.mkdir()
    image.write_bytes(b"the prior image bytes")
    raw.write_text("# A figure\n\n![A diagram](images/diagram.png)\n")
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        candidate = await reprocess_document(
            session, source, document_id, job_queue=CandidateQueue(), engine_manager=document_engine_manager
        )
        copied_raw = Path(candidate.payload["input_artifact"]["path"])
        image.write_bytes(b"later image bytes")
        fetcher = ImageFetcher(max_bytes=1024 * 1024)
        try:
            loaded = await fetcher.fetch("images/diagram.png", source_path=copied_raw)
            assert loaded.data == b"the prior image bytes"
        finally:
            await fetcher.aclose()
        assert copied_raw.read_text() == "# A figure\n\n![A diagram](images/diagram.png)\n"


@pytest.mark.parametrize("status", list(DocumentStatus))
@pytest.mark.parametrize("published", [False, True])
def test_document_response_exposes_publication_independently_of_candidate_status(status, published):
    from datetime import UTC, datetime

    from sag_api.schemas.document import DocumentOut

    now = datetime.now(UTC)
    document = Document(
        id="publication-response",
        source_id="response-source",
        filename="published.md",
        content_type="text/markdown",
        size_bytes=12,
        storage_path="/published.md",
        status=status,
        sag_source_id="published-input" if published else None,
        control_revision=1,
        is_active=True,
        chunk_count=3,
        event_count=2,
        progress=0,
        token_usage=0,
        error=None,
        created_at=now,
        updated_at=now,
    )
    response = DocumentOut.model_validate(document).model_dump()
    assert response["has_publication"] is (
        published and status not in {DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED}
    )
    assert response["status"] == status
    assert "published_operation_id" not in response
    assert "sag_source_id" not in response


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "paused", "published"])
@pytest.mark.parametrize("new_status", [DocumentStatus.EXTRACTING, DocumentStatus.PAUSING])
@pytest.mark.usefixtures("controller_identity")
async def test_retired_candidate_cannot_write_the_replacement_control(
    published_document, outcome, new_status, document_engine_manager
):
    from types import SimpleNamespace

    from sqlalchemy import update

    from sag_api.jobs import tasks
    from sag_api.jobs.control import JobPaused
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor
    from sag_api.services.document_service import reprocess_document

    source_id, document_id, _old_job_id, old_operation, _raw = published_document
    queue = CandidateQueue()
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        job = await reprocess_document(
            session, source, document_id, job_queue=queue, engine_manager=document_engine_manager
        )
        job.status = JobStatus.RUNNING
        await session.commit()
        job_id, revision = job.id, job.payload["control_revision"]

    class RetiringManager(UncommittedDocumentManager):
        async def process_document(self, config_id, request_factory, **options):
            processor = IncrementalDocumentProcessor(
                SimpleNamespace(resources=SimpleNamespace(prompts=SimpleNamespace(language="zh"))),
                config_id,
                max_concurrency=1,
            )
            request = await request_factory(processor, *processing_parameters())
            async with SessionLocal() as control:
                await control.execute(
                    update(Document)
                    .where(Document.id == document_id)
                    .values(
                        control_revision=revision + 1,
                        processing_run_id="replacement-run",
                        status=new_status,
                        processing_stage="replacement-stage",
                        progress=17,
                    )
                )
                await control.commit()
            if outcome == "failed":
                raise ValueError("a retired model request failed")
            if outcome == "paused":
                return None
            return document_receipt(request, chunk_count=99, event_count=99)

    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        with pytest.raises(JobPaused):
            await tasks.process_document(session, job, engine_manager=RetiringManager())
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        assert document.control_revision == revision + 1
        assert document.status == new_status
        assert document.processing_stage == "replacement-stage"
        assert document.processing_run_id == "replacement-run" and document.progress == 17
        assert document.sag_source_id == "published-input" and document.published_operation_id == old_operation
        assert (document.chunk_count, document.event_count, document.token_usage) == (4, 2, 120)


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["pause", "queue", "retry", "restart"])
async def test_retired_queue_transition_cannot_change_the_replacement(
    published_document, transition, document_engine_manager
):
    from sag_api.core.errors import DocumentRestartRequiredError
    from sag_api.jobs import inproc
    from sag_api.services.document_service import hold_document_for_restart, reprocess_document

    source_id, document_id, _old_job_id, _old_operation, _raw = published_document
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        job = await reprocess_document(
            session, source, document_id, job_queue=CandidateQueue(), engine_manager=document_engine_manager
        )
        document = await session.get(Document, document_id)
        document.control_revision += 1
        document.processing_run_id = "replacement-run"
        document.processing_stage = "replacement-stage"
        document.status = DocumentStatus.PAUSING if transition == "pause" else DocumentStatus.EXTRACTING
        document.error = "replacement-message"
        expected_status = document.status
        await session.commit()
        if transition == "restart":
            changed = await hold_document_for_restart(
                session,
                job,
                DocumentRestartRequiredError("resume_configuration_changed"),
            )
            assert changed is False
        else:
            handler = {
                "pause": inproc._converge_document_paused,
                "queue": inproc._mark_document_queued,
                "retry": inproc._mark_document_waiting_retry,
            }[transition]
            await handler(session, job)
            await session.commit()
        await session.refresh(document)
        assert document.status == expected_status
        assert document.processing_run_id == "replacement-run"
        assert document.processing_stage == "replacement-stage"
        assert document.error == "replacement-message"


@pytest.mark.asyncio
async def test_retired_candidate_control_stops_engine_admission(published_document, document_engine_manager):
    from sqlalchemy import update

    from sag_api.jobs import tasks
    from sag_api.jobs.control import JobPaused
    from sag_api.services.document_service import reprocess_document

    source_id, document_id, _old_job_id, _old_operation, _raw = published_document
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        job = await reprocess_document(
            session, source, document_id, job_queue=CandidateQueue(), engine_manager=document_engine_manager
        )
        job.status = JobStatus.RUNNING
        await session.commit()
        revision = job.payload["control_revision"]

        pause_observation = []

        class Manager(UncommittedDocumentManager):
            async def process_document(self, _config, _request_factory, **options):
                async with SessionLocal() as control:
                    await control.execute(
                        update(Document)
                        .where(Document.id == document_id)
                        .values(
                            control_revision=revision + 1,
                            processing_run_id="replacement-run",
                        )
                    )
                    await control.commit()
                pause_observation.append(await options["should_pause"]())
                return None

        with pytest.raises(JobPaused):
            await tasks.process_document(session, job, engine_manager=Manager())
        assert pause_observation == [True]
