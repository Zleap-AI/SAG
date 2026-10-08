"""Request-local checkpointing for zleap's explicit, non-managed extraction path.

Keep the built-in adapter's validation, error mapping and atomic commit. Only its
extractor is wrapped, on a shallow copy owned by this request. Successful chunk
outcomes use separate application DB rows; no shared engine methods are patched.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from copy import copy
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import DBAPIError, OperationalError
from zleap.sag._extraction_progress import ExtractionCheckpointWriteError
from zleap.sag._pipeline_adapters import BuiltinExtractAdapter
from zleap.sag.db.models import Article, ArticleSection, Entity, EventEntity, SourceEvent
from zleap.sag.exceptions import ConfigError, ExtractError, ExtractRollbackError, OperationConflictError
from zleap.sag.modules.extract.extractor import ChunkExtractionSuccess, ExtractionBatchResult
from zleap.sag.modules.extract.processor import ExtractionGenerationStats, entity_type_contract_fingerprint
from zleap.sag.modules.extract.prompts import (
    ExtractionPromptRegistry,
    ExtractionPromptRole,
    prompt_role_for_source_type,
)
from zleap.sag.modules.extract.saver import EventSaver
from zleap.sag.pipeline.errors import StalePipelineReferenceError

from sag_api.db.models import Document, DocumentExtractionCheckpoint, Job
from sag_api.enums import JobStatus, JobType
from sag_api.sag.dto import ProcessCheckpoint


class ExtractionPublicationError(ExtractError):
    def __init__(self, *, retryable=True):
        super().__init__(
            "Extraction publication failed; saved results and cleanup IDs are retained",
            code="extraction_publication_failed", retryable=retryable,
        )


async def _drain(operation):
    """Do not release a write fence while its request-owned transaction is running."""
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        await asyncio.gather(task, return_exceptions=True)
        raise


class DocumentExtractionStore:
    """Persist one immutable success under the current worker's job claim.

The job update acquires the database write lock and fences a stale worker before
writing a chunk. A pause may finish a checkpoint; a resumed/new claim cannot be
overwritten. Failed chunks have no success row and are retried on replay.
"""

    def __init__(self, session_factory, document_id: str, job_id: str, started_at: datetime | None):
        self.session_factory = session_factory
        self.document_id = document_id
        self.job_id = job_id
        self.started_at = started_at

    def _claim(self, *, publishing=False):
        return (
            Job.id == self.job_id,
            Job.document_id == self.document_id,
            Job.type == JobType.PROCESS_DOCUMENT,
            Job.started_at == self.started_at,
            Job.status.in_([JobStatus.RUNNING] if publishing else [JobStatus.RUNNING, JobStatus.PAUSED]),
        )

    async def fence(self, session, *, publishing=False, document=False):
        if document:
            # Match control transitions' Document -> Job lock order. Publication
            # and chunk persistence only touch Job, so they never wait for Document.
            locked = await session.execute(update(Document).where(Document.id == self.document_id)
                                           .values(updated_at=Document.updated_at))
            if locked.rowcount != 1:
                raise OperationConflictError("Extraction document changed")
        claim = await session.execute(update(Job).where(*self._claim(publishing=publishing))
                                      .values(updated_at=Job.updated_at))
        if claim.rowcount != 1:
            raise OperationConflictError("Extraction worker claim changed")

    @asynccontextmanager
    async def publication(self):
        # Claim transfer/control writes wait until the whole publication drains.
        # No document row is locked here: controls lock document then job.
        async with self.session_factory() as session:
            await self.fence(session, publishing=True)
            yield session
            try:
                await session.commit()
            except Exception as exc:
                raise ExtractionCheckpointWriteError() from exc

    async def checkpoint(self, session, current):
        try:
            payload = await session.scalar(select(Job.payload).where(*self._claim()))
            await session.execute(update(Job).where(*self._claim()).values(payload=current.merge_payload(payload)))
        except Exception as exc:
            raise ExtractionCheckpointWriteError() from exc

    async def record_publication(self, session, current, identities):
        # Commit the cleanup journal before vectors can change. Reacquire and
        # verify ownership before the first mutation: a control/claim transfer
        # may win the short gap, in which case staged relations roll back.
        current.extraction_publication = identities
        await self.checkpoint(session, current)
        try:
            await session.commit()
        except Exception as exc:
            raise ExtractionCheckpointWriteError() from exc
        await self.fence(session, publishing=True)

    async def load(self, current: ProcessCheckpoint) -> dict[str, dict]:
        async with self.session_factory() as session:
            if await session.scalar(select(Job.id).where(*self._claim())) is None:
                raise OperationConflictError("Extraction worker claim changed")
            rows = (await session.scalars(select(DocumentExtractionCheckpoint).where(
                DocumentExtractionCheckpoint.document_id == self.document_id,
                DocumentExtractionCheckpoint.extraction_id == current.extraction_id,
            ))).all()
        if any(row.fingerprint != current.extraction_fingerprint for row in rows):
            raise OperationConflictError("Extraction checkpoint configuration changed")
        return {row.chunk_id: row.outcome for row in rows}

    async def save(self, current: ProcessCheckpoint, chunk_id: str, outcome: dict) -> None:
        async def write():
            async with self.session_factory() as session:
                await self.fence(session)
                key = (current.extraction_id, chunk_id)
                previous = await session.get(DocumentExtractionCheckpoint, key)
                if previous is None:
                    session.add(DocumentExtractionCheckpoint(
                        extraction_id=current.extraction_id,
                        chunk_id=chunk_id,
                        document_id=self.document_id,
                        fingerprint=current.extraction_fingerprint,
                        outcome=outcome,
                    ))
                elif (
                    previous.document_id != self.document_id
                    or previous.fingerprint != current.extraction_fingerprint
                ):
                    raise OperationConflictError("Extraction checkpoint configuration changed")
                await session.commit()

        # Drain the small transaction even when cancellation arrives during commit.
        try:
            await _drain(write())
        except OperationConflictError:
            raise
        except Exception as exc:
            raise ExtractionCheckpointWriteError() from exc


class _PublicationSession:
    """Borrow one request-owned session; the outer transaction owns its commit."""

    def __init__(self, session):
        self.session = session

    def __getattr__(self, name):
        return getattr(self.session, name)

    async def commit(self):
        await self.session.flush()


class _ReplayableSaver(EventSaver):
    def __init__(self, *, on_publication, **kwargs):
        super().__init__(**kwargs)
        self.on_publication = on_publication

    async def _save_to_database(self, events, config):
        # zleap's ordinary writer soft-deletes the old snapshot then INSERTs.
        # Replace only this checkpoint's existing IDs in the same transaction,
        # after commit() has captured the old event/association vector IDs.
        if config.generation_id is None and events:
            async with self.session_factory() as session:
                existing = (await session.scalars(select(SourceEvent).where(
                    SourceEvent.id.in_([event.id for event in events]),
                ))).all()
                if any(
                    event.data_source_id != config.data_source_id
                    or event.source_type != config.source_type
                    or event.source_id != config.source_id
                    or event.generation_id is not None
                    for event in existing
                ):
                    raise OperationConflictError("Extraction publication event identity changed")
                await session.execute(delete(SourceEvent).where(SourceEvent.id.in_([event.id for event in existing])))
        result = await super()._save_to_database(events, config)
        async with self.session_factory() as session:
            associations = list((await session.scalars(select(EventEntity.id).where(
                EventEntity.event_id.in_(result.event_ids),
            ))).all()) if result.event_ids else []
        await self.on_publication({
            "events": result.event_ids,
            "entities": result.created_entity_ids,
            "event_entities": associations,
        })
        return result


async def cleanup_extraction_publication(identities, *, session_factory, vector_store):
    """Remove only journaled vectors whose authoritative relations are absent.

    Retain the journal until the caller's durable completion/deletion commit.
    Retrying a partial delete is safe; shared durable entities are preserved.
    Caller must own the processing claim or the source maintenance window.
    """
    groups = (
        ("event_entities", EventEntity, "event_entity_vectors"),
        ("events", SourceEvent, "event_vectors_wide"),
        ("entities", Entity, "entity_vectors"),
    )
    async with session_factory() as session:
        for key, model, collection in groups:
            ids = identities.get(key, [])
            for offset in range(0, len(ids), 500):
                batch = ids[offset:offset + 500]
                durable = set((await session.scalars(select(model.id).where(model.id.in_(batch)))).all())
                missing = [value for value in batch if value not in durable]
                if not missing:
                    continue
                try:
                    result = await vector_store.delete(collection, missing)
                except (OSError, OperationalError) as exc:
                    raise ExtractionPublicationError() from exc
                if result.failed_items:
                    raise ExtractionPublicationError()


class CheckpointedExtractor:
    """Reuse prepare_batch's cached_chunks/on_chunk_result without publishing early."""

    def __init__(self, engine, store, current, on_checkpoint, on_progress):
        self.extractor = engine._extractor
        self.runtime = engine._config
        self.store = store
        self.current = current
        self.on_checkpoint = on_checkpoint
        self.on_progress = on_progress
        self.failure: Exception | None = None

    def __getattr__(self, name):
        return getattr(self.extractor, name)

    async def _fingerprint(self, config) -> str:
        chunks = await self.extractor._load_chunks(config.chunk_ids)
        if {str(chunk.id) for chunk in chunks} != set(config.chunk_ids):
            raise StalePipelineReferenceError("Extraction checkpoint chunks are missing", stage="extract")
        registry = ExtractionPromptRegistry.from_prompt_manager(self.extractor.prompt_manager)
        roles = [prompt_role_for_source_type(config.source_type)]
        if config.enable_parent_summary:
            roles.append(ExtractionPromptRole.PARENT)
        async with self.extractor.session_factory() as session:
            article = await session.get(Article, config.source_id)
            if article is None:
                raise StalePipelineReferenceError("Extraction checkpoint article is missing", stage="extract")
            references = {reference for chunk in chunks for reference in (chunk.references or [])}
            sections = (await session.scalars(select(ArticleSection).where(or_(
                ArticleSection.article_id == config.source_id, ArticleSection.id.in_(references),
            )).order_by(ArticleSection.rank, ArticleSection.id))).all()
            article_input = {"id": article.id, "title": article.title}
            if config.enable_article_summary:
                article_input["summary"] = article.summary
            section_inputs = [{
                key: getattr(section, key)
                for key in ("id", "article_id", "type", "rank", "heading", "content", "raw_content",
                            "image_url", "length", "extra_data")
            } for section in sections]
        payload = {
            "version": 2,
            "llm": self.runtime.llm.model_dump(mode="json", exclude={"api_key"}),
            "embedding": self.runtime.embedding.model_dump(mode="json", exclude={"api_key"}),
            "config": {**config.model_dump(mode="json"), "storage_mode": config.storage_mode},
            "chunk_version": self.current.chunk_version,
            "prompts": [asdict(registry.load(role)) for role in roles],
            "entities": entity_type_contract_fingerprint(await self.extractor._load_entity_types_for_chunk(config)),
            "article": article_input,
            "sections": section_inputs,
            "chunks": [{
                key: getattr(chunk, key)
                for key in ("id", "content", "raw_content", "heading", "rank", "references", "extra_data",
                            "chunk_length")
            } for chunk in sorted(chunks, key=lambda chunk: str(chunk.id))],
        }
        return sha256(json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()

    async def extract_batch(self, config, *, cancellation=None):
        try:
            return await self._extract_batch(config, cancellation=cancellation)
        except Exception as exc:
            # The built-in explicit adapter predates checkpoints and wraps some
            # checkpoint errors. Preserve their retryability at the caller boundary.
            # The saver also wraps database failures after its apparent commit.
            # Our outer transaction and durable journal permit transient retries
            # even if compensation cannot reuse an invalidated connection.
            database_error = exc.__cause__ if isinstance(exc, ExtractRollbackError) else exc
            if isinstance(exc, (
                OperationConflictError, ExtractionCheckpointWriteError,
                ExtractionPublicationError,
            )):
                self.failure = exc
            elif isinstance(database_error, DBAPIError):
                retryable = isinstance(database_error, OperationalError) or database_error.connection_invalidated
                if retryable or isinstance(exc, ExtractRollbackError):
                    self.failure = ExtractionPublicationError(retryable=retryable)
            raise

    async def _extract_batch(self, config, *, cancellation):
        current = self.current
        if current.extraction_id and current.extraction_fingerprint_version != 2:
            raise OperationConflictError(
                "Extraction checkpoint predates complete input verification; "
                "finish with the previous version or upload again"
            )
        fingerprint = await self._fingerprint(config)
        if current.extraction_fingerprint and current.extraction_fingerprint != fingerprint:
            raise OperationConflictError(
                "Extraction model, prompts, options or chunks changed; restore the configuration or upload again"
            )
        if current.extraction_id is None:
            current.extraction_id = uuid4().hex
            current.extraction_reference_time = datetime.now(UTC).isoformat()
            current.extraction_fingerprint_version = 2
        if not current.extraction_reference_time:
            raise OperationConflictError("Extraction checkpoint reference time is missing")
        current.extraction_fingerprint = fingerprint
        records = await self.store.load(current)
        if not set(records).issubset(current.chunk_ids):
            raise OperationConflictError("Extraction checkpoint chunk identity changed")
        succeeded = set(records)
        current.processed_chunk_ids = [cid for cid in current.chunk_ids if cid in succeeded]
        if current.extraction_committed:
            if succeeded != set(current.chunk_ids):
                raise OperationConflictError("Committed extraction checkpoints are missing")
            events = await self.extractor._reload_events_with_relations(current.event_ids) if current.event_ids else []
            if {event.id for event in events} != set(current.event_ids) or any(
                event.source_id != config.source_id or event.status == "DELETED" for event in events
            ):
                raise OperationConflictError("Committed extraction events changed or are missing")
            return ExtractionBatchResult(events=tuple(events), stats={
                "token_usage": current.token_usage, "zero_event_chunks": current.eventless_chunk_ids,
            })
        # Persist the small run identity/clock before the first LLM request.
        await self.on_checkpoint(current.model_copy(deep=True))
        config = config.model_copy(update={
            "reference_time": datetime.fromisoformat(current.extraction_reference_time)
            .astimezone(ZoneInfo(config.timezone)).strftime("%Y-%m-%d %H:%M"),
        })
        cached = {cid: ChunkExtractionSuccess(
            events=tuple(BuiltinExtractAdapter._deserialize_prepared_event(event) for event in record["events"]),
            stats=ExtractionGenerationStats(**record["stats"]),
        ) for cid, record in records.items()}

        async def report(_completed=0, _total=0):
            # The native batch counter includes failures; only saved successes are
            # durable progress. Cached chunks never cause a new model request.
            await self.on_progress(len(succeeded), len(current.chunk_ids))

        async def save_chunk(chunk_id, result, _failure):
            if result is None:
                return
            await self.store.save(current, chunk_id, {
                "events": [BuiltinExtractAdapter._serialize_prepared_event(event) for event in result.events],
                "stats": asdict(result.stats),
            })
            succeeded.add(chunk_id)
            current.processed_chunk_ids = [cid for cid in current.chunk_ids if cid in succeeded]
            await report()

        await report()

        async def prepare():
            return await self.extractor.prepare_batch(
                config, cached_chunks=cached, on_chunk_result=save_chunk, on_progress=report,
            )

        prepared = await (prepare() if cancellation is None else cancellation.run(prepare))
        if cancellation is not None and cancellation.is_cancelled:
            raise asyncio.CancelledError
        return await _drain(self._publish(prepared, config))

    async def _publish(self, prepared, config):
        async with self.store.publication() as claim_session:
            await cleanup_extraction_publication(
                self.current.extraction_publication,
                session_factory=self.extractor.session_factory,
                vector_store=self.extractor.repositories.events.vector_store,
            )
            async with self.extractor.session_factory() as session:
                await session.begin()

                @asynccontextmanager
                async def borrowed_session():
                    yield _PublicationSession(session)

                extractor = copy(self.extractor)
                extractor.session_factory = borrowed_session
                saver = _ReplayableSaver(
                    session_factory=borrowed_session, repositories=extractor.repositories,
                    embedding=extractor.embedding,
                    on_publication=lambda identities: self.store.record_publication(
                        claim_session, self.current, identities,
                    ),
                )
                extractor._saver = saver
                result = await extractor.commit_prepared(list(prepared.events), config, stats=dict(prepared.stats))
                await session.commit()
            # Durable acknowledgement follows relation + vector success, under
            # the same claim fence. A crash between databases replays the writer.
            current = self.current
            current.extraction_committed = True
            current.extraction_publication = {}
            current.event_ids = [event.id for event in result.events]
            current.event_count = len(result.events)
            current.processed_chunk_ids = list(current.chunk_ids)
            current.token_usage = int(result.stats.get("token_usage", 0) or 0)
            current.eventless_chunk_ids = [
                item["chunk_id"] if isinstance(item, dict) else str(item)
                for item in result.stats.get("zero_event_chunks", [])
            ]
            await self.store.checkpoint(claim_session, current)
            return result


def checkpointed_engine(engine, store, current, on_checkpoint, on_progress):
    """Return an extraction-only view sharing resources, never their mutable adapter."""
    adapter = getattr(engine, "_extract_adapter", None)
    if not isinstance(adapter, BuiltinExtractAdapter):
        raise ConfigError("Durable document extraction requires the built-in zleap extract adapter")
    request_engine = copy(engine)
    request_engine._extract_adapter = copy(adapter)
    wrapper = CheckpointedExtractor(engine, store, current, on_checkpoint, on_progress)
    request_engine._extract_adapter.extractor = wrapper
    return request_engine, wrapper
