"""Offline crash/replay tests using real zleap preparation and application DB checkpoints."""
from __future__ import annotations

import asyncio
import os
import pickle
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import zleap.sag
from pydantic import BaseModel
from sqlalchemy import event, func, select
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from zleap.sag._extraction_progress import ExtractionCheckpointWriteError
from zleap.sag._pipeline_adapters import BuiltinExtractAdapter
from zleap.sag.config import LanceDBVectorConfig
from zleap.sag.core.adapters import BulkResult
from zleap.sag.core.adapters.defaults import LanceDBVectorStore
from zleap.sag.core.storage.repositories.entity_repository import EntityVectorRepository
from zleap.sag.core.storage.repositories.event_entity_repository import EventEntityRepository
from zleap.sag.core.storage.repositories.event_repository import EventVectorRepository
from zleap.sag.db.models import (
    Article,
    ArticleSection,
    DataSource,
    Entity,
    EntityType,
    EventEntity,
    SourceChunk,
    SourceEvent,
)
from zleap.sag.db.schema import create_missing_relation_tables
from zleap.sag.exceptions import ExtractRollbackError, OperationConflictError
from zleap.sag.modules.extract.extractor import ChunkExtractionSuccess, EventExtractor
from zleap.sag.modules.extract.processor import ExtractionGenerationStats
from zleap.sag.modules.extract.prompts import ExtractionPromptRegistry
from zleap.sag.pipeline import ExtractStage, PipelineContext
from zleap.sag.pipeline.errors import ExtractionBatchFailure, ExtractionError

from sag_api.db.base import Base
from sag_api.db.models import Document, DocumentExtractionCheckpoint, Job, OctxOperationLease, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs.inproc import InProcessAsyncQueue
from sag_api.sag.document_extraction import DocumentExtractionStore, _ReplayableSaver
from sag_api.sag.dto import ProcessCheckpoint, extraction_display_percent
from sag_api.sag.incremental_processor import IncrementalDocumentProcessor


class ModelConfig(BaseModel):
    model: str = "offline-model"
    api_key: str = "offline-secret"


class NativeEngine:
    async def extract(self, target, options, *, observer, cancellation):
        return await ExtractStage().run(target, options, context=PipelineContext(
            extract_adapter=self._extract_adapter, observer=observer, cancellation=cancellation,
        ))


class OfflineVectors:
    """Only the vector transport is fake; repositories and publication are real."""

    provider = "offline"

    def __init__(self):
        self.records = {}

    async def ping(self):
        pass

    async def optimize(self):
        return True

    async def get_many(self, collection, ids):
        return [self.records[(collection, cid)] for cid in ids if (collection, cid) in self.records]

    async def delete(self, collection, ids):
        for cid in ids:
            self.records.pop((collection, cid), None)
        return BulkResult(succeeded_ids=tuple(ids))

    async def upsert(self, collection, records):
        for record in records:
            self.records[(collection, record.id)] = record
        return BulkResult(succeeded_ids=tuple(record.id for record in records))


class PersistentVectors(OfflineVectors):
    """A deterministic transport whose records survive abrupt worker exits."""

    def __init__(self, directory):
        super().__init__()
        self.path = Path(directory) / "vectors.pickle"
        if self.path.exists():
            self.records = pickle.loads(self.path.read_bytes())

    async def upsert(self, collection, records):
        result = await super().upsert(collection, records)
        self.path.write_bytes(pickle.dumps(self.records))
        return result

    async def delete(self, collection, ids):
        result = await super().delete(collection, ids)
        self.path.write_bytes(pickle.dumps(self.records))
        return result


def _use_entity_vectors(harness, *, lancedb=False):
    harness.vectors = LanceDBVectorStore(
        LanceDBVectorConfig(path=str(harness.directory / "lancedb")), embedding_dimensions=16,
    ) if lancedb else PersistentVectors(harness.directory)
    repositories = harness.engine._extractor.repositories
    for repository in (repositories.events, repositories.entities, repositories.event_entities):
        repository.vector_store = harness.vectors
    original = harness.engine._extractor.extract_from_chunk

    async def with_entities(chunk, config):
        result = await original(chunk, config)
        for item in result.events:
            item.extra_data = {"raw_entities": {"entities": [
                {"type": "concept", "name": "Shared concept", "description": "A real association"},
            ]}}
        return result

    harness.engine._extractor.extract_from_chunk = with_entities


async def _vector_ids(store, collection):
    if isinstance(store, OfflineVectors):
        return {cid for (name, cid) in store.records if name == collection}
    table = await store._raw()._open_table(collection)
    if table is None:
        return set()
    rows = await table.query().select(["id"]).to_arrow()
    return set(rows.column("id").to_pylist())


class Harness:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.meta_engine = create_async_engine(f"sqlite+aiosqlite:///{self.directory / 'app.db'}")
        self.native_engine = create_async_engine(f"sqlite+aiosqlite:///{self.directory / 'engine.db'}")
        for engine in (self.meta_engine, self.native_engine):
            @event.listens_for(engine.sync_engine, "connect")
            def foreign_keys(connection, _record):
                connection.execute("PRAGMA foreign_keys=ON")
        self.sessions = async_sessionmaker(self.meta_engine, expire_on_commit=False)
        self.native_sessions = async_sessionmaker(self.native_engine, expire_on_commit=False)
        self.calls = []
        self.progress = []
        self.fail_chunk = None
        self.block = False
        self.started = asyncio.Event()
        self.clock_values = []
        self.vectors = OfflineVectors()

    async def seed(self):
        async with self.meta_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await create_missing_relation_tables(self.native_engine, "normal")
        source_id, article_id = str(uuid4()), str(uuid4())
        chunk_ids = [str(uuid4()) for _ in range(3)]
        async with self.native_sessions() as session:
            session.add(DataSource(id=source_id, name="Offline extraction"))
            session.add(Article(id=article_id, data_source_id=source_id, title="Offline document", content="text"))
            await session.flush()
            for index, cid in enumerate(chunk_ids):
                section_id = str(uuid4())
                session.add(ArticleSection(id=section_id, article_id=article_id, rank=index, heading="Text",
                                           content="A reusable knowledge statement. " * 3))
                session.add(SourceChunk(
                    id=cid, data_source_id=source_id, source_type="ARTICLE", source_id=article_id,
                    article_id=article_id, content="A reusable knowledge statement. " * 3,
                    raw_content="original", rank=index, chunk_length=100, references=[section_id],
                ))
            await session.commit()
        async with self.sessions() as session:
            source = Source(name="Offline", sag_source_config_id=source_id)
            session.add(source)
            await session.flush()
            document = Document(source_id=source.id, filename="offline.md", storage_path="/tmp/offline.md",
                                content_type="text/markdown", size_bytes=100, status=DocumentStatus.EXTRACTING)
            session.add(document)
            await session.flush()
            checkpoint = ProcessCheckpoint(source_id=article_id, chunk_ids=chunk_ids, chunk_version="v1")
            job = Job(type=JobType.PROCESS_DOCUMENT, status=JobStatus.RUNNING, source_id=source.id,
                      document_id=document.id, payload=checkpoint.merge_payload({}), started_at=datetime.now(UTC))
            session.add(job)
            await session.commit()

    async def setup(self):
        async with self.sessions() as session:
            self.job = await session.scalar(select(Job))
            self.document_id = self.job.document_id
            self.checkpoint = ProcessCheckpoint.from_payload(self.job.payload)
            source = await session.get(Source, self.job.source_id)
            source_config_id = source.sag_source_config_id
        self.engine = NativeEngine()
        prompts = SimpleNamespace(prompts_dir=Path(zleap.sag.__file__).parent / "prompts", language="en")
        self.engine.resources = SimpleNamespace(prompts=prompts)
        self.engine._config = SimpleNamespace(llm=ModelConfig(), embedding=ModelConfig(model="offline-embedding"))
        repositories = SimpleNamespace(events=EventVectorRepository(self.vectors),
                                       entities=EntityVectorRepository(self.vectors),
                                       event_entities=EventEntityRepository(self.vectors))
        embedding = SimpleNamespace(generate=AsyncMock(return_value=[1.0] * 16),
                                    batch_generate=AsyncMock(side_effect=lambda texts: [[1.0] * 16 for _ in texts]))
        extractor = EventExtractor(prompts, session_factory=self.native_sessions, repositories=repositories,
                                   embedding=embedding, llm=object())
        self.engine._extractor = extractor
        self.engine._extract_adapter = BuiltinExtractAdapter(session_factory=self.native_sessions,
                                                            extractor=extractor, storage_mode="normal")
        # Real preparation, relation writer, repositories, vector orchestration,
        # cached replay, and adapter validation; model/vector transports are fake.
        extractor._load_entity_types_for_chunk = AsyncMock(return_value=[])

        async def model(chunk, config):
            self.calls.append(chunk.id)
            self.clock_values.append(config.reference_time)
            if chunk.id == self.fail_chunk:
                raise RuntimeError("offline chunk failure")
            if self.block and chunk.rank == 2:
                self.started.set()
                await asyncio.Event().wait()
            events = () if chunk.rank == 0 else (SourceEvent(
                id=str(uuid4()), data_source_id=source_config_id, source_type="ARTICLE", source_id=chunk.source_id,
                article_id=chunk.source_id, chunk_id=chunk.id, title="Saved knowledge", summary="Summary",
                content="Knowledge", rank=chunk.rank, level=0,
            ),)
            return ChunkExtractionSuccess(events, ExtractionGenerationStats(generation_attempts=1))

        extractor.extract_from_chunk = model
        self.processor = IncrementalDocumentProcessor(self.engine, source_config_id, max_concurrency=1)
        self.store = DocumentExtractionStore(self.sessions, self.document_id, self.job.id, self.job.started_at)

    async def run(self, *, pause=False):
        async def checkpoint(value):
            self.current = value
            async with self.sessions() as session:
                await self.store.fence(session)
                job = await session.get(Job, self.job.id)
                job.payload = value.merge_payload(job.payload)
                await session.commit()

        async def progress(completed, total):
            self.progress.append(extraction_display_percent(completed, total, committed=False))

        async def should_pause():
            return pause and self.started.is_set()

        return await self.processor.process(None, checkpoint=self.checkpoint, on_checkpoint=checkpoint,
                                            on_progress=progress, should_pause=should_pause,
                                            extraction_store=self.store)

    async def claim_again(self):
        async with self.sessions() as session:
            job = await session.get(Job, self.job.id)
            job.status = JobStatus.RUNNING
            job.started_at += timedelta(seconds=1)
            await session.commit()
        await self.setup()

    async def visible_count(self):
        async with self.native_sessions() as session:
            return len((await session.scalars(select(SourceEvent).where(SourceEvent.not_deleted()))).all())

    async def close(self):
        await self.meta_engine.dispose()
        await self.native_engine.dispose()
        if isinstance(self.vectors, LanceDBVectorStore):
            await self.vectors.close()


@pytest.fixture
async def harness(tmp_path):
    value = Harness(tmp_path)
    await value.seed()
    await value.setup()
    yield value
    await value.close()


async def _crash_worker(directory, phase="chunk"):
    harness = Harness(directory)
    await harness.setup()
    if phase.startswith(("durable_", "lancedb_")):
        _use_entity_vectors(harness, lancedb=phase.startswith("lancedb_"))
        upsert = harness.vectors.upsert

        async def persist_then_die(collection, records):
            result = await upsert(collection, records)
            if collection == phase.split("_", 1)[1]:
                os._exit(23)
            return result

        harness.vectors.upsert = persist_then_die
    save = harness.store.save

    async def save_then_die(current, chunk_id, outcome):
        await save(current, chunk_id, outcome)
        if chunk_id == current.chunk_ids[1]:
            os._exit(23)

    if phase == "chunk":
        harness.store.save = save_then_die
    elif phase == "relation":
        original = _ReplayableSaver._save_to_database

        async def relation(self, events, config):
            await original(self, events, config)
            os._exit(23)

        _ReplayableSaver._save_to_database = relation
    elif phase == "vectors":
        original = harness.vectors.upsert

        async def vectors(collection, records):
            await original(collection, records)
            os._exit(23)

        harness.vectors.upsert = vectors
    elif phase == "published":
        original = harness.store.checkpoint

        async def checkpoint(session, current):
            if current.extraction_committed:
                os._exit(23)
            await original(session, current)

        harness.store.checkpoint = checkpoint
    elif phase == "acknowledged":
        original = harness.processor._extract

        async def extract(*args, **kwargs):
            await original(*args, **kwargs)
            os._exit(23)

        harness.processor._extract = extract
    await harness.run()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["relation", "vectors", "published", "acknowledged"])
async def test_real_publication_process_death_replays_without_duplicate_ids(harness, phase):
    async with harness.native_sessions() as session:
        chunk = await session.get(SourceChunk, harness.checkpoint.chunk_ids[0])
        old_id = str(uuid4())
        session.add(SourceEvent(id=old_id, data_source_id=chunk.data_source_id, source_type="ARTICLE",
                                source_id=chunk.source_id, article_id=chunk.source_id, chunk_id=chunk.id,
                                title="Older snapshot", summary="Old", content="Old", rank=0, level=0))
        await session.commit()
    code = ("import asyncio,sys; from test_document_extraction import _crash_worker; "
            "asyncio.run(_crash_worker(*sys.argv[1:]))")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(__file__).parent), str(Path(__file__).parents[1])])}
    result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", code, str(harness.directory), phase],
                                    env=env, capture_output=True, timeout=30)
    assert result.returncode == 23, result.stderr.decode()
    async with harness.native_sessions() as session:
        old = await session.get(SourceEvent, old_id)
        assert (old.status == "DELETED") == (phase in {"published", "acknowledged"})
    await harness.claim_again()
    assert len(await harness.store.load(harness.checkpoint)) == 3
    assert harness.checkpoint.extraction_committed == (phase == "acknowledged")
    writes = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO source_event "):
            writes.append(statement)

    event.listen(harness.native_engine.sync_engine, "before_cursor_execute", observe)
    try:
        outcome = await harness.run()
    finally:
        event.remove(harness.native_engine.sync_engine, "before_cursor_execute", observe)
    assert harness.calls == []
    assert outcome.event_count == 2
    assert await harness.visible_count() == 2
    assert bool(writes) == (phase != "acknowledged")
    assert harness.current.extraction_committed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["entity_vectors", "event_vectors_wide", "event_entity_vectors"])
@pytest.mark.parametrize("backend", ["durable", "lancedb"])
async def test_recovery_removes_uncommitted_entity_and_association_vectors(harness, collection, backend):
    async with harness.native_sessions() as session:
        session.add(EntityType(id=str(uuid4()), type="concept", name="Concept", is_default=True, is_active=True))
        await session.commit()
    code = ("import asyncio,sys; from test_document_extraction import _crash_worker; "
            "asyncio.run(_crash_worker(*sys.argv[1:]))")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(__file__).parent), str(Path(__file__).parents[1])])}
    child = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-c", code, str(harness.directory), backend + "_" + collection],
        env=env, capture_output=True, timeout=45,
    )
    assert child.returncode == 23, child.stderr.decode()
    await harness.claim_again()
    _use_entity_vectors(harness, lancedb=backend == "lancedb")
    assert harness.checkpoint.extraction_publication
    assert await _vector_ids(harness.vectors, collection)
    outcome = await harness.run()
    assert harness.calls == []
    assert outcome.event_count == 2
    async with harness.native_sessions() as session:
        for model, index in ((SourceEvent, "event_vectors_wide"), (Entity, "entity_vectors"),
                             (EventEntity, "event_entity_vectors")):
            rows = set((await session.scalars(select(model.id))).all())
            vectors = await _vector_ids(harness.vectors, index)
            assert vectors == rows
    assert harness.current.extraction_publication == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("failure", ["operational", "disconnect"])
async def test_engine_commit_failure_preserves_retryability_and_replays_without_model_calls(
    harness, committed, failure,
):
    from sag_api.core.errors import UpstreamError
    from sag_api.jobs.inproc import _is_retryable
    from sag_api.sag.errors import map_sag_errors

    failed = False
    original = harness.native_sessions

    @asynccontextmanager
    async def sessions():
        async with original() as session:
            commit = session.commit

            async def lose_acknowledgement():
                nonlocal failed
                if not failed:
                    failed = True
                    if committed:
                        await commit()
                    error_type = OperationalError if failure == "operational" else DBAPIError
                    raise error_type("COMMIT", {}, OSError("Test connection failure"),
                                     connection_invalidated=failure == "disconnect")
                await commit()

            session.commit = lose_acknowledgement
            yield session

    harness.engine._extractor.session_factory = sessions
    with pytest.raises(UpstreamError) as error, map_sag_errors():
        await harness.run()
    assert _is_retryable(error.value)
    assert len(await harness.store.load(harness.current)) == 3
    assert await harness.visible_count() == (2 if committed else 0)
    await harness.claim_again()
    assert harness.checkpoint.extraction_publication
    harness.calls.clear()
    assert (await harness.run()).event_count == 2
    assert harness.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", [False, True])
@pytest.mark.parametrize("failure", ["operational", "disconnect", "integrity", "database"])
async def test_relation_reload_database_failure_preserves_retry_classification(
    harness, monkeypatch, invalidate, failure,
):
    from sag_api.core.errors import UpstreamError
    from sag_api.jobs.inproc import _is_retryable
    from sag_api.sag.errors import map_sag_errors

    async with harness.native_sessions() as session:
        session.add(EntityType(id=str(uuid4()), type="concept", name="Concept", is_default=True, is_active=True))
        await session.commit()
    _use_entity_vectors(harness)
    original = _ReplayableSaver._load_events_with_relations

    async def fail_reload(saver, event_ids):
        if invalidate:
            async with saver.session_factory() as session:
                # Invalidate the connection, retaining its pending transaction:
                # invalidating the entire Session would hide compensation failure.
                connection = await session.connection()
                await connection.invalidate()
        error_type = {"operational": OperationalError, "integrity": IntegrityError}.get(failure, DBAPIError)
        raise error_type("SELECT source_events", {}, OSError("Test relation reload failure"),
                         connection_invalidated=failure == "disconnect")

    monkeypatch.setattr(_ReplayableSaver, "_load_events_with_relations", fail_reload)
    with pytest.raises(UpstreamError) as error, map_sag_errors():
        await harness.run()
    retryable = failure in {"operational", "disconnect"}
    assert _is_retryable(error.value) is retryable
    cause = error.value
    while cause is not None and not isinstance(cause, ExtractRollbackError):
        cause = cause.__cause__
    assert isinstance(cause, ExtractRollbackError)
    assert cause.compensation_complete is (not invalidate)
    assert isinstance(cause.__cause__, DBAPIError)
    assert len(await harness.store.load(harness.current)) == 3
    assert await harness.visible_count() == 0
    assert not harness.vectors.records
    async with harness.sessions() as session:
        payload = await session.scalar(select(Job.payload).where(Job.id == harness.job.id))
        journal = ProcessCheckpoint.from_payload(payload).extraction_publication
    assert len(journal["events"]) == 2
    assert len(journal["entities"]) == 1
    assert len(journal["event_entities"]) == 2

    # Both transient and permanent failures retain recovery state; only the
    # transient cases are eligible for an automatic queue retry.
    monkeypatch.setattr(_ReplayableSaver, "_load_events_with_relations", original)
    await harness.claim_again()
    _use_entity_vectors(harness)
    harness.calls.clear()
    assert (await harness.run()).event_count == 2
    assert harness.calls == []
    assert harness.current.extraction_publication == {}
    async with harness.native_sessions() as session:
        for model, index in ((SourceEvent, "event_vectors_wide"), (Entity, "entity_vectors"),
                             (EventEntity, "event_entity_vectors")):
            assert await _vector_ids(harness.vectors, index) == set((await session.scalars(select(model.id))).all())


@pytest.mark.asyncio
async def test_permanent_engine_integrity_failure_is_not_retryable(harness):
    from sag_api.jobs.inproc import _is_retryable

    def fail(_connection):
        raise IntegrityError("COMMIT", {}, ValueError("Test integrity failure"))

    event.listen(harness.native_engine.sync_engine, "commit", fail)
    try:
        with pytest.raises(ExtractionError) as error:
            await harness.run()
        assert not _is_retryable(error.value)
    finally:
        event.remove(harness.native_engine.sync_engine, "commit", fail)


@pytest.mark.asyncio
async def test_claim_transfer_after_publication_journal_prevents_vector_mutation(harness, monkeypatch):
    record = harness.store.record_publication

    async def transfer(session, current, identities):
        commit = session.commit

        async def commit_then_transfer():
            await commit()
            async with harness.sessions() as control:
                job = await control.get(Job, harness.job.id)
                job.started_at += timedelta(seconds=1)
                await control.commit()

        session.commit = commit_then_transfer
        try:
            await record(session, current, identities)
        finally:
            session.commit = commit

    monkeypatch.setattr(harness.store, "record_publication", transfer)
    with pytest.raises(OperationConflictError, match="claim changed"):
        await harness.run()
    assert harness.vectors.records == {}
    assert await harness.visible_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_journal_commit_acknowledgement_is_required_before_vector_mutation(harness, committed):
    failed = False

    @asynccontextmanager
    async def sessions():
        async with harness.sessions() as session:
            commit = session.commit

            async def lose_acknowledgement():
                nonlocal failed
                payload = await session.scalar(select(Job.payload).where(Job.id == harness.job.id))
                if not failed and ProcessCheckpoint.from_payload(payload).extraction_publication:
                    failed = True
                    if committed:
                        await commit()
                    raise OSError("Test publication journal acknowledgement lost")
                await commit()

            session.commit = lose_acknowledgement
            yield session

    harness.store.session_factory = sessions
    with pytest.raises(ExtractionCheckpointWriteError):
        await harness.run()
    assert harness.vectors.records == {}
    assert await harness.visible_count() == 0
    await harness.claim_again()
    assert bool(harness.checkpoint.extraction_publication) == committed
    harness.calls.clear()
    assert (await harness.run()).event_count == 2
    assert harness.calls == []


@pytest.mark.asyncio
async def test_cleanup_failure_retains_journal_and_preserves_shared_durable_entity(harness, monkeypatch):
    from sag_api.sag.document_extraction import ExtractionPublicationError, cleanup_extraction_publication

    orphan_id, shared_id = str(uuid4()), str(uuid4())
    async with harness.native_sessions() as session:
        article = await session.get(Article, harness.checkpoint.source_id)
        entity_type = EntityType(id=str(uuid4()), type="concept", name="Concept", is_default=True, is_active=True)
        session.add(entity_type)
        await session.flush()
        session.add(Entity(id=shared_id, data_source_id=article.data_source_id, entity_type_id=entity_type.id,
                           type="concept", name="Shared", normalized_name="shared"))
        await session.commit()
    identities = {"entities": [orphan_id, shared_id]}
    # The cleanup only needs vector IDs, so transport values are immaterial.
    harness.vectors.records = {("entity_vectors", orphan_id): None, ("entity_vectors", shared_id): None}
    delete = harness.vectors.delete
    monkeypatch.setattr(harness.vectors, "delete", AsyncMock(side_effect=OSError("Test transport outage")))
    with pytest.raises(ExtractionPublicationError):
        await cleanup_extraction_publication(identities, session_factory=harness.native_sessions,
                                            vector_store=harness.vectors)
    assert len(harness.vectors.records) == 2
    monkeypatch.setattr(harness.vectors, "delete", delete)
    await cleanup_extraction_publication(identities, session_factory=harness.native_sessions,
                                        vector_store=harness.vectors)
    assert set(harness.vectors.records) == {("entity_vectors", shared_id)}


@pytest.mark.asyncio
async def test_document_delete_forwards_publication_journal_and_retries_cleanup(harness, monkeypatch):
    from sag_api.jobs import tasks

    async with harness.sessions() as session:
        job = await session.get(Job, harness.job.id)
        value = harness.checkpoint.model_copy(update={"extraction_publication": {"entities": [str(uuid4())]}})
        job.payload = value.merge_payload(job.payload)
        deletion = Job(type=JobType.DELETE_DOCUMENT, status=JobStatus.RUNNING, source_id=job.source_id,
                       document_id=harness.document_id)
        session.add(deletion)
        await session.commit()
        deletion_id = deletion.id
    cleanup = AsyncMock(side_effect=OSError("Test cleanup outage"))
    async with harness.sessions() as session:
        job = await session.get(Job, deletion_id)
        with pytest.raises(OSError):
            await tasks._delete_document_task_unlocked(
                session, job, engine_manager=SimpleNamespace(delete_document_data=cleanup),
            )
    assert cleanup.call_args.kwargs["publications"] == [value.extraction_publication]
    async with harness.sessions() as session:
        assert await session.get(Document, harness.document_id) is not None
        assert ProcessCheckpoint.from_payload((await session.get(Job, harness.job.id)).payload).extraction_publication
        job = await session.get(Job, deletion_id)
        cleanup.side_effect = None
        await tasks._delete_document_task_unlocked(
            session, job, engine_manager=SimpleNamespace(delete_document_data=cleanup),
        )
    async with harness.sessions() as session:
        assert await session.get(Document, harness.document_id) is None


@pytest.mark.asyncio
async def test_process_death_replays_saved_empty_and_nonempty_results(harness):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([
        str(Path(__file__).parent), str(Path(__file__).parents[1]), os.environ.get("PYTHONPATH", ""),
    ])}
    code = (
        "import asyncio,sys; from test_document_extraction import _crash_worker; "
        "asyncio.run(_crash_worker(sys.argv[1]))"
    )
    result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", code, str(harness.directory)],
                                    env=env, capture_output=True, timeout=30)
    assert result.returncode == 23, result.stderr.decode()
    assert await harness.visible_count() == 0
    await harness.claim_again()
    saved = await harness.store.load(harness.checkpoint)
    assert saved[harness.checkpoint.chunk_ids[0]]["events"] == []
    assert len(saved[harness.checkpoint.chunk_ids[1]]["events"]) == 1
    outcome = await harness.run()
    assert harness.calls == [harness.checkpoint.chunk_ids[2]]
    assert outcome.paused is False
    assert outcome.event_count == 2
    assert harness.progress[0] == 73
    assert await harness.visible_count() == 2
    assert harness.engine._extract_adapter.extractor is harness.engine._extractor
    assert harness.engine._extractor._on_progress is None


@pytest.mark.asyncio
async def test_pause_reuses_saved_results_and_fixed_prompt_clock(harness):
    harness.block = True
    outcome = await harness.run(pause=True)
    assert outcome.paused is True
    assert len(outcome.processed_chunk_ids) == 2
    assert await harness.visible_count() == 0
    first_clock = harness.clock_values[0]
    harness.block = False
    harness.calls.clear()
    await harness.claim_again()
    outcome = await harness.run()
    assert outcome.paused is False
    assert harness.calls == [harness.checkpoint.chunk_ids[2]]
    assert harness.clock_values[-1] == first_clock


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["model", "prompt", "chunk", "entities", "section", "title", "chunk_length"])
async def test_fingerprint_change_refuses_reuse(harness, monkeypatch, changed):
    harness.block = True
    await harness.run(pause=True)
    harness.block = False
    harness.calls.clear()
    await harness.claim_again()
    if changed == "model":
        harness.engine._config.llm.model = "changed-model"
    elif changed == "prompt":
        original = ExtractionPromptRegistry.load
        monkeypatch.setattr(ExtractionPromptRegistry, "load", lambda self, role: replace(
            original(self, role), template=original(self, role).template + " changed",
        ))
    elif changed == "entities":
        harness.engine._extractor._load_entity_types_for_chunk = AsyncMock(return_value=[SimpleNamespace(
            type="changed", name="Changed", description="Changed", value_format=None, value_constraints=None,
        )])
    elif changed == "chunk":
        async with harness.native_sessions() as session:
            chunk = await session.get(SourceChunk, harness.checkpoint.chunk_ids[0])
            chunk.content = "changed content"
            await session.commit()
    else:
        async with harness.native_sessions() as session:
            chunk = await session.get(SourceChunk, harness.checkpoint.chunk_ids[0])
            if changed == "section":
                section = await session.get(ArticleSection, chunk.references[0])
                section.content = "changed section content"
            elif changed == "title":
                article = await session.get(Article, chunk.source_id)
                article.title = "Changed title"
            else:
                chunk.chunk_length = 1
            await session.commit()
    with pytest.raises(OperationConflictError, match="changed"):
        await harness.run()
    assert harness.calls == []
    assert len(await harness.store.load(harness.checkpoint)) == 2


@pytest.mark.asyncio
async def test_failed_checkpoint_write_never_advances_success_or_publishes(harness, monkeypatch):
    monkeypatch.setattr(harness.store, "save", AsyncMock(side_effect=ExtractionCheckpointWriteError()))
    with pytest.raises(ExtractionCheckpointWriteError):
        await harness.run()
    assert await harness.store.load(harness.current) == {}
    assert harness.progress == [20]
    assert await harness.visible_count() == 0


@pytest.mark.asyncio
async def test_chunk_failure_keeps_other_checkpoints_and_retries_only_failure(harness):
    harness.fail_chunk = harness.checkpoint.chunk_ids[1]
    with pytest.raises(ExtractionBatchFailure):
        await harness.run()
    assert len(await harness.store.load(harness.current)) == 2
    assert harness.progress[-1] == 73
    assert await harness.visible_count() == 0
    harness.calls.clear()
    harness.fail_chunk = None
    await harness.claim_again()
    outcome = await harness.run()
    assert harness.calls == [harness.checkpoint.chunk_ids[1]]
    assert outcome.paused is False


@pytest.mark.asyncio
async def test_recovery_restores_actual_success_count_before_dispatch(harness):
    harness.block = True
    await harness.run(pause=True)
    async with harness.sessions() as session:
        job = await session.get(Job, harness.job.id)
        job.status = JobStatus.RUNNING
        # Simulate process death after result writes and before display/checkpoint updates.
        checkpoint = ProcessCheckpoint.from_payload(job.payload)
        checkpoint.processed_chunk_ids = []
        job.payload = checkpoint.merge_payload(job.payload)
        document = await session.get(Document, harness.document_id)
        document.progress = 20
        await session.commit()
    queue = InProcessAsyncQueue(engine_manager=SimpleNamespace(), session_factory=harness.sessions)
    await queue._recover()
    async with harness.sessions() as session:
        job = await session.get(Job, harness.job.id)
        document = await session.get(Document, harness.document_id)
        assert job.status == JobStatus.QUEUED
        assert document.status == DocumentStatus.EXTRACTING
        assert document.progress == 73
        assert job.progress == 0.73
        assert len(job.payload["process_checkpoint"]["processed_chunk_ids"]) == 2


@pytest.mark.asyncio
async def test_stale_worker_cannot_write_after_new_claim(harness):
    harness.block = True
    await harness.run(pause=True)
    old_store, current = harness.store, harness.current
    await harness.claim_again()
    with pytest.raises(OperationConflictError, match="claim changed"):
        await old_store.save(current, current.chunk_ids[2], {"events": [], "stats": {}})
    assert len(await harness.store.load(harness.checkpoint)) == 2


@pytest.mark.asyncio
async def test_commit_failure_replays_all_results_without_another_model_call(harness, monkeypatch):
    publish = harness.engine._extractor._save_events
    monkeypatch.setattr(harness.engine._extractor, "_save_events", AsyncMock(
        side_effect=RuntimeError("publish failed"),
    ))
    with pytest.raises(ExtractionError, match="extractor_failed"):
        await harness.run()
    assert len(await harness.store.load(harness.current)) == 3
    assert harness.progress[-1] == 99
    assert await harness.visible_count() == 0
    harness.calls.clear()
    await harness.claim_again()
    harness.engine._extractor._save_events = publish
    outcome = await harness.run()
    assert harness.calls == []
    assert outcome.paused is False
    assert outcome.event_count == 2


@pytest.mark.asyncio
async def test_pause_preserves_previous_published_snapshot(harness):
    async with harness.native_sessions() as session:
        chunk = await session.get(SourceChunk, harness.checkpoint.chunk_ids[0])
        session.add(SourceEvent(
            id=str(uuid4()), data_source_id=chunk.data_source_id, source_type="ARTICLE", source_id=chunk.source_id,
            article_id=chunk.source_id, chunk_id=chunk.id, title="Previous ready snapshot", summary="Previous",
            content="Previous", rank=0, level=0,
        ))
        await session.commit()
    harness.block = True
    await harness.run(pause=True)
    async with harness.native_sessions() as session:
        events = (await session.scalars(select(SourceEvent).where(SourceEvent.not_deleted()))).all()
        assert [event.title for event in events] == ["Previous ready snapshot"]
    harness.block = False
    await harness.claim_again()
    await harness.run()
    async with harness.native_sessions() as session:
        events = (await session.scalars(select(SourceEvent).where(SourceEvent.not_deleted()))).all()
        assert len(events) == 2


@pytest.mark.asyncio
async def test_success_is_immutable_and_api_key_change_does_not_invalidate_replay(harness):
    harness.block = True
    await harness.run(pause=True)
    saved = await harness.store.load(harness.current)
    await harness.store.save(harness.current, harness.current.chunk_ids[1], {"events": [], "stats": {}})
    assert await harness.store.load(harness.current) == saved
    harness.block = False
    harness.calls.clear()
    await harness.claim_again()
    harness.engine._config.llm.api_key = "changed-offline-secret"
    await harness.run()
    assert harness.calls == [harness.checkpoint.chunk_ids[2]]


@pytest.mark.asyncio
async def test_explicit_ready_reprocess_removes_saved_results(harness, monkeypatch):
    from sag_api.services import document_service

    await harness.run()
    async with harness.sessions() as session:
        document = await session.get(Document, harness.document_id)
        document.status = DocumentStatus.READY
        document.sag_source_id = harness.current.source_id
        job = await session.get(Job, harness.job.id)
        job.status = JobStatus.SUCCEEDED
        await session.commit()
        source = await session.get(Source, document.source_id)
        queue = SimpleNamespace(begin_source_maintenance=lambda *_args: None, enqueue_durably=AsyncMock())
        monkeypatch.setattr(document_service, "touch_source_revision", AsyncMock())
        restarted = await document_service.reprocess_document(session, source, document.id, job_queue=queue)
        assert "process_checkpoint" not in restarted.payload
        assert (await session.scalars(select(DocumentExtractionCheckpoint))).all() == []


@pytest.mark.asyncio
async def test_database_insert_failure_is_mandatory_and_does_not_advance(harness):
    harness.block = True
    await harness.run(pause=True)

    def fail_insert(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO document_extraction_checkpoints"):
            raise RuntimeError("offline database insert failure")

    event.listen(harness.meta_engine.sync_engine, "before_cursor_execute", fail_insert)
    try:
        with pytest.raises(ExtractionCheckpointWriteError):
            await harness.store.save(harness.current, harness.current.chunk_ids[2], {"events": [], "stats": {}})
    finally:
        event.remove(harness.meta_engine.sync_engine, "before_cursor_execute", fail_insert)
    assert len(await harness.store.load(harness.current)) == 2


@pytest.mark.asyncio
async def test_cancellation_drains_checkpoint_transaction(harness):
    harness.block = True
    await harness.run(pause=True)
    entered, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def sessions():
        async with harness.sessions() as session:
            original = session.commit

            async def commit():
                entered.set()
                await release.wait()
                await original()

            session.commit = commit
            yield session

    harness.store.session_factory = sessions
    write = asyncio.create_task(harness.store.save(
        harness.current, harness.current.chunk_ids[2], {"events": [], "stats": {}},
    ))
    await asyncio.wait_for(entered.wait(), 2)
    write.cancel()
    await asyncio.sleep(0)
    assert not write.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await write
    assert len(await harness.store.load(harness.current)) == 3


@pytest.mark.asyncio
async def test_stale_worker_cannot_replay_a_complete_cache(harness, monkeypatch):
    monkeypatch.setattr(harness.engine._extractor, "_save_events",
                        AsyncMock(side_effect=RuntimeError("before publish")))
    with pytest.raises(ExtractionError):
        await harness.run()
    async with harness.sessions() as session:
        job = await session.get(Job, harness.job.id)
        harness.checkpoint = ProcessCheckpoint.from_payload(job.payload)
        job.started_at += timedelta(seconds=1)
        payload = job.payload
        await session.commit()
    harness.calls.clear()
    with pytest.raises(OperationConflictError, match="claim changed"):
        await harness.run()
    assert harness.calls == []
    assert await harness.visible_count() == 0
    async with harness.sessions() as session:
        assert (await session.get(Job, harness.job.id)).payload == payload


@pytest.mark.asyncio
async def test_publication_fence_blocks_claim_transfer_and_drains_cancellation(harness, monkeypatch):
    entered, release, transferred = asyncio.Event(), asyncio.Event(), asyncio.Event()
    upsert = harness.vectors.upsert

    async def blocked(collection, records):
        entered.set()
        await release.wait()
        return await upsert(collection, records)

    monkeypatch.setattr(harness.vectors, "upsert", blocked)
    running = asyncio.create_task(harness.run())
    await asyncio.wait_for(entered.wait(), 5)
    assert await harness.visible_count() == 0

    async def transfer():
        async with harness.sessions() as session:
            job = await session.get(Job, harness.job.id)
            job.started_at += timedelta(seconds=1)
            await session.commit()
            transferred.set()

    transfer_task = asyncio.create_task(transfer())
    running.cancel()
    await asyncio.sleep(0.05)
    running.cancel()  # A second cancellation must not release the publication fence.
    await asyncio.sleep(0.05)
    assert not running.done()
    assert not transferred.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    await asyncio.wait_for(transfer_task, 5)
    assert await harness.visible_count() == 2
    async with harness.sessions() as session:
        checkpoint = ProcessCheckpoint.from_payload((await session.get(Job, harness.job.id)).payload)
        assert checkpoint.extraction_committed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [DocumentStatus.PAUSING, DocumentStatus.PAUSED])
async def test_paused_recovery_restores_success_rows(harness, status):
    harness.block = True
    await harness.run(pause=True)
    async with harness.sessions() as session:
        job = await session.get(Job, harness.job.id)
        job.status = JobStatus.PAUSED
        checkpoint = ProcessCheckpoint.from_payload(job.payload)
        checkpoint.processed_chunk_ids = []
        job.payload = checkpoint.merge_payload(job.payload)
        job.progress = 0.20
        document = await session.get(Document, harness.document_id)
        document.status = status
        document.progress = 20
        await session.commit()
    queue = InProcessAsyncQueue(engine_manager=SimpleNamespace(), session_factory=harness.sessions)
    await queue._recover()
    async with harness.sessions() as session:
        job = await session.get(Job, harness.job.id)
        document = await session.get(Document, harness.document_id)
        assert job.status == JobStatus.PAUSED
        assert document.status == DocumentStatus.PAUSED
        assert document.progress == 73
        assert job.progress == 0.73
        assert len(job.payload["process_checkpoint"]["processed_chunk_ids"]) == 2


@pytest.mark.asyncio
async def test_incomplete_legacy_fingerprint_fails_closed(harness):
    harness.block = True
    await harness.run(pause=True)
    async with harness.sessions() as session:
        job = await session.get(Job, harness.job.id)
        payload = {**job.payload, "process_checkpoint": {**job.payload["process_checkpoint"]}}
        payload["process_checkpoint"].pop("extraction_fingerprint_version")
        job.payload = payload
        await session.commit()
    await harness.claim_again()
    harness.calls.clear()
    with pytest.raises(OperationConflictError, match="predates complete input verification"):
        await harness.run()
    assert harness.calls == []


@pytest.mark.asyncio
async def test_vector_failure_restores_previous_snapshot(harness, monkeypatch):
    async with harness.native_sessions() as session:
        old = SourceEvent(id=str(uuid4()), data_source_id=harness.processor._source_config_id,
                          source_type="ARTICLE", source_id=harness.checkpoint.source_id,
                          article_id=harness.checkpoint.source_id, title="Previous snapshot", status="ACTIVE",
                          summary="Previous", content="Previous")
        session.add(old)
        await session.commit()
        old_id = old.id
    upsert = harness.vectors.upsert

    async def fail_after_write(collection, records):
        await upsert(collection, records)
        raise RuntimeError("Vector transport lost its acknowledgement")

    monkeypatch.setattr(harness.vectors, "upsert", fail_after_write)
    with pytest.raises(ExtractionError):
        await harness.run()
    async with harness.native_sessions() as session:
        active = (await session.scalars(select(SourceEvent).where(SourceEvent.not_deleted()))).all()
        assert [event.id for event in active] == [old_id]
    async with harness.sessions() as session:
        assert not ProcessCheckpoint.from_payload((await session.get(Job, harness.job.id)).payload).extraction_committed
    monkeypatch.setattr(harness.vectors, "upsert", upsert)
    await harness.claim_again()
    harness.calls.clear()
    assert (await harness.run()).event_count == 2
    assert harness.calls == []


@pytest.mark.asyncio
async def test_completed_document_retry_preserves_counts(harness, monkeypatch):
    from sag_api.jobs import tasks
    from sag_api.jobs.inproc import _mark_document_waiting_retry
    from sag_api.services import universe_service

    outcome = await harness.run()
    monkeypatch.setattr(tasks, "SessionLocal", harness.sessions)
    refresh = AsyncMock()
    monkeypatch.setattr(universe_service, "schedule_universe_refresh", refresh)
    manager = SimpleNamespace(process_document=AsyncMock(side_effect=AssertionError("Completed extraction repeated")))
    async with harness.sessions() as session:
        job = await session.get(Job, harness.job.id)
        document = await session.get(Document, harness.document_id)
        document.status = DocumentStatus.READY
        document.sag_source_id = outcome.source_id
        document.chunk_count = outcome.chunk_count
        document.event_count = outcome.event_count
        source = await session.get(Source, job.source_id)
        source.chunk_count, source.event_count = outcome.chunk_count, outcome.event_count
        await session.commit()
        await _mark_document_waiting_retry(session, job)
        assert document.status == DocumentStatus.READY
        await tasks._process_document_unlocked(session, job, engine_manager=manager, job_queue=SimpleNamespace())
        assert document.status == DocumentStatus.READY
        assert (source.chunk_count, source.event_count) == (3, 2)
    manager.process_document.assert_not_awaited()
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_worker_cannot_overwrite_metadata_or_mark_document_failed(harness, monkeypatch):
    from sag_api.jobs import tasks

    monkeypatch.setattr(tasks, "SessionLocal", harness.sessions)

    async def transfer_then_callback(*args, **kwargs):
        async with harness.sessions() as session:
            job = await session.get(Job, harness.job.id)
            job.started_at += timedelta(seconds=1)
            job.payload = {**job.payload, "new_claim": True}
            await session.commit()
        checkpoint = kwargs["checkpoint"].model_copy(update={"extraction_id": "stale"})
        await kwargs["on_checkpoint"](checkpoint)

    async with harness.sessions() as session:
        job = await session.get(Job, harness.job.id)
        with pytest.raises(OperationConflictError, match="claim changed"):
            await asyncio.wait_for(tasks.process_document(
                session, job, engine_manager=SimpleNamespace(process_document=transfer_then_callback),
            ), 5)
    async with harness.sessions() as session:
        job = await session.get(Job, harness.job.id)
        assert job.payload["new_claim"] is True
        assert not ProcessCheckpoint.from_payload(job.payload).extraction_id
        assert (await session.get(Document, harness.document_id)).status == DocumentStatus.EXTRACTING
        assert await session.scalar(select(func.count()).select_from(OctxOperationLease)) == 0


@pytest.mark.asyncio
async def test_worker_cancellation_releases_fence_before_lease_cleanup(harness, monkeypatch):
    from sag_api.jobs import tasks

    monkeypatch.setattr(tasks, "SessionLocal", harness.sessions)
    entered = asyncio.Event()
    async with harness.sessions() as session:
        job = await session.get(Job, harness.job.id)

        async def blocked(*args, **kwargs):
            await kwargs["extraction_store"].fence(session, document=True)
            entered.set()
            await asyncio.Event().wait()

        worker = asyncio.create_task(tasks.process_document(
            session, job, engine_manager=SimpleNamespace(process_document=blocked),
        ))
        await asyncio.wait_for(entered.wait(), 5)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, 5)
    async with harness.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(OctxOperationLease)) == 0
        await harness.store.fence(session, document=True)
        await session.commit()


@pytest.mark.asyncio
async def test_cached_event_fields_and_hierarchy_survive_publication(harness):
    model = harness.engine._extractor.extract_from_chunk
    parent_id, child_id = str(uuid4()), str(uuid4())
    clock = datetime(2025, 1, 2, 3, 4)

    async def rich_model(chunk, config):
        result = await model(chunk, config)
        if chunk.rank != 1:
            return result
        parent = result.events[0]
        parent.id = parent_id
        parent.start_time = clock
        parent.end_time = clock + timedelta(hours=1)
        parent.references = list(chunk.references)
        parent.keywords = ["星", "orbit"]
        parent.category, parent.priority, parent.status = "science", "high", "ACTIVE"
        parent.extra_data = {"raw_entities": {"entities": []}, "raw_data": {"nested": ["星", None, 1.25]}}
        child = SourceEvent(id=child_id, data_source_id=parent.data_source_id,
                            source_type="ARTICLE", source_id=parent.source_id, article_id=parent.article_id,
                            chunk_id=chunk.id, title="Child", summary="Child", content="Child",
                            parent_id=parent_id, level=1, rank=1)
        return replace(result, events=(parent, child))

    harness.engine._extractor.extract_from_chunk = rich_model
    harness.block = True
    await harness.run(pause=True)
    saved = await harness.store.load(harness.current)
    payload = saved[harness.current.chunk_ids[1]]["events"][0]
    harness.block = False
    await harness.claim_again()
    harness.calls.clear()
    outcome = await harness.run()
    assert outcome.event_count == 3
    assert harness.calls == [harness.checkpoint.chunk_ids[2]]
    async with harness.native_sessions() as session:
        parent, child = await session.get(SourceEvent, parent_id), await session.get(SourceEvent, child_id)
        assert (parent.start_time, parent.end_time) == (clock, clock + timedelta(hours=1))
        assert parent.references == payload["references"]
        assert parent.keywords == payload["keywords"]
        assert parent.extra_data == payload["extra_data"]
        assert (parent.category, parent.priority, parent.status) == ("science", "high", "ACTIVE")
        assert (child.parent_id, child.level) == (parent_id, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_publication_acknowledgement_failure_is_retryable(harness, committed):
    failed = False

    @asynccontextmanager
    async def sessions():
        async with harness.sessions() as session:
            commit = session.commit

            async def lose_acknowledgement():
                nonlocal failed
                payload = await session.scalar(select(Job.payload).where(Job.id == harness.job.id))
                if not failed and ProcessCheckpoint.from_payload(payload).extraction_committed:
                    failed = True
                    if committed:
                        await commit()
                    raise OSError("Application database acknowledgement lost")
                await commit()

            session.commit = lose_acknowledgement
            yield session

    harness.store.session_factory = sessions
    with pytest.raises(ExtractionCheckpointWriteError) as error:
        await harness.run()
    assert error.value.retryable is True
    assert await harness.visible_count() == 2
    async with harness.sessions() as session:
        checkpoint = ProcessCheckpoint.from_payload((await session.get(Job, harness.job.id)).payload)
        assert checkpoint.extraction_committed is committed
    await harness.claim_again()
    harness.calls.clear()
    outcome = await harness.run()
    assert outcome.event_count == 2
    assert harness.calls == []
