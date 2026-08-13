from __future__ import annotations

from io import BytesIO

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.datastructures import UploadFile


def test_vector_progress_gate_throttles_same_stage_but_flushes_transitions() -> None:
    from sag_api.services.octx_transfer_service import _VectorProgressGate

    gate = _VectorProgressGate(total=10_000, interval_seconds=1.0)

    assert gate.should_persist("entities", "reuse", 0, now=10.0)
    assert not gate.should_persist("entities", "reuse", 50, now=10.1)
    assert gate.should_persist("entities", "reuse", 100, now=10.2)
    assert gate.should_persist("event_entities", "reuse", 100, now=10.3)
    assert gate.should_persist("event_entities", "reuse", 110, now=11.4)
    assert gate.should_persist("event_entities", "generate", 110, now=11.5)


def test_export_progress_uses_real_stage_counts_and_never_rewinds():
    from sag_api.services.octx_transfer_service import _export_progress

    updates = [
        {"phase": "snapshot", "kind": "documents", "completed": 1, "total": 9},
        {"phase": "snapshot", "kind": "chunks", "completed": 500, "total": 763},
        {"phase": "snapshot", "kind": "event_entities", "completed": 18000, "total": 18874},
        {"phase": "vectors", "kind": "entity.name", "completed": 8000, "total": 11985},
        {"phase": "snapshot_complete", "kind": "complete", "completed": 1, "total": 1},
    ]

    values = [_export_progress(update) for update in updates]

    assert values == sorted(values)
    assert 0.1 < values[0] < values[-1] == 0.59


@pytest_asyncio.fixture
async def transfer_sessions(tmp_path):
    from sag_api.db import models  # noqa: F401
    from sag_api.db.base import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'meta.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):  # noqa: ANN001
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sessions
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_import_upload_is_persisted_and_preflight_publishes_release(transfer_sessions, tmp_path):
    from sag_api.db.models import Job, OctxAsset, OctxRelease
    from sag_api.enums import JobType, OctxTransferStatus
    from sag_api.octx.runner import ValidatedPackage
    from sag_api.octx.storage import OctxStorage
    from sag_api.services.octx_transfer_service import (
        create_import_transfer,
        preflight_import,
    )

    class Queue:
        ids: list[str] = []

        async def enqueue(self, job_id: str) -> None:
            self.ids.append(job_id)

    class Runner:
        async def validate_package(self, upload):
            assert upload.unchanged()
            return ValidatedPackage(
                manifest={
                    "asset": {
                        "id": "0191f6a0-0000-7000-8000-000000000101",
                        "name": "Imported",
                    },
                    "release": {
                        "version": "1.0.0",
                        "package_digest": "sha256:" + "a" * 64,
                    },
                    "capabilities": {"sag-structured": {"version": "0.1"}},
                },
                report={"valid": True, "fully_validated": True, "issues": []},
                upload_sha256=upload.sha256,
                size_bytes=upload.size_bytes,
                input_signature=upload.signature.to_dict(),
                capabilities={"sag-structured": {"version": "0.1"}},
                record_counts={
                    "documents": 1,
                    "chunks": 1,
                    "events": 1,
                    "entities": 1,
                    "chunk_events": 1,
                    "event_entities": 1,
                },
            )

    storage = OctxStorage(tmp_path / "octx", max_upload_bytes=1024)
    queue = Queue()
    async with transfer_sessions() as session:
        transfer = await create_import_transfer(
            session,
            UploadFile(filename="source.octx", file=BytesIO(b"bounded-package")),
            storage=storage,
            job_queue=queue,
        )
        assert transfer.status is OctxTransferStatus.VALIDATING
        assert len(queue.ids) == 1
        preflight_job = await session.get(Job, queue.ids[0])
        assert preflight_job is not None and preflight_job.type is JobType.OCTX_PREFLIGHT

        await preflight_import(
            session,
            transfer,
            storage=storage,
            runner=Runner(),
            job_queue=queue,
            decision_secret="secret",
        )
        assert transfer.status is OctxTransferStatus.QUEUED
        assert len(queue.ids) == 2
        import_job = await session.get(Job, queue.ids[1])
        assert import_job is not None and import_job.type is JobType.OCTX_IMPORT
        assert await session.get(OctxAsset, "0191f6a0-0000-7000-8000-000000000101") is not None
        release = await session.get(OctxRelease, transfer.release_id)
        assert release is not None
        assert storage.resolve_key(release.artifact_key).read_bytes() == b"bounded-package"


@pytest.mark.asyncio
async def test_import_creation_is_idempotent_for_client_transfer_id(transfer_sessions, tmp_path):
    """A lost POST response must be recoverable without starting a duplicate import."""
    from sqlalchemy import func, select

    from sag_api.db.models import Job, OctxTransfer
    from sag_api.octx.storage import OctxStorage
    from sag_api.services.octx_transfer_service import create_import_transfer

    class Queue:
        ids: list[str] = []

        async def enqueue(self, job_id: str) -> None:
            self.ids.append(job_id)

    storage = OctxStorage(tmp_path / "octx", max_upload_bytes=1024)
    queue = Queue()
    transfer_id = "a" * 32
    async with transfer_sessions() as session:
        first = await create_import_transfer(
            session,
            UploadFile(filename="source.octx", file=BytesIO(b"bounded-package")),
            storage=storage,
            job_queue=queue,
            transfer_id=transfer_id,
        )
        repeated = await create_import_transfer(
            session,
            UploadFile(filename="source.octx", file=BytesIO(b"bounded-package")),
            storage=storage,
            job_queue=queue,
            transfer_id=transfer_id,
        )

        assert first.id == repeated.id == transfer_id
        assert len(queue.ids) == 1
        assert await session.scalar(select(func.count()).select_from(OctxTransfer)) == 1
        assert await session.scalar(select(func.count()).select_from(Job)) == 1


def test_export_retry_reuses_transfer_staging_root_with_isolated_attempt(tmp_path):
    from sag_api.octx.storage import OctxStorage
    from sag_api.services.octx_transfer_service import _prepare_export_attempt

    storage = OctxStorage(tmp_path / "octx", max_upload_bytes=1024)
    staging = storage.staging_dir("transfer-1")
    first_attempt = staging / "export-1"
    first_attempt.mkdir(parents=True)
    marker = first_attempt / "failure.txt"
    marker.write_text("retain first-attempt diagnostics", encoding="utf-8")

    second_attempt, workspace = _prepare_export_attempt(storage, "transfer-1", attempt=2)

    assert second_attempt == staging / "export-2"
    assert workspace == second_attempt / "workspace"
    assert second_attempt.is_dir()
    assert marker.read_text(encoding="utf-8") == "retain first-attempt diagnostics"


@pytest.mark.asyncio
async def test_import_decision_response_is_serializable_after_commit(transfer_sessions):
    """Accepting a decision must not return an ORM object with expired timestamps."""
    from datetime import UTC, datetime, timedelta

    from sag_api.core.config import settings
    from sag_api.db.models import OctxAsset, OctxTransfer
    from sag_api.enums import (
        OctxAssetOwnership,
        OctxTransferDirection,
        OctxTransferStatus,
    )
    from sag_api.octx.decision_token import DecisionTokenClaims, issue_decision_token
    from sag_api.schemas.octx import OctxTransferOut
    from sag_api.services.octx_conflict_service import ImportDecision
    from sag_api.services.octx_transfer_service import submit_import_decision

    class Queue:
        ids: list[str] = []

        async def enqueue(self, job_id: str) -> None:
            self.ids.append(job_id)

    asset_id = "0191f6a0-0000-7000-8000-000000000109"
    async with transfer_sessions() as session:
        asset = OctxAsset(id=asset_id, name="AI", ownership=OctxAssetOwnership.IMPORTED)
        transfer = OctxTransfer(
            id="decision-response",
            direction=OctxTransferDirection.IMPORT,
            status=OctxTransferStatus.DECISION_REQUIRED,
            asset_id=asset_id,
            checkpoint={"asset_name": "AI"},
        )
        session.add_all([asset, transfer])
        await session.commit()
        token = issue_decision_token(
            DecisionTokenClaims(
                transfer_id=transfer.id,
                asset_id=asset_id,
                source_revisions={},
                highest_version="1.0.0",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            ),
            secret=settings.secret_key,
        )

        decided = await submit_import_decision(
            session,
            transfer.id,
            ImportDecision(action="new", decision_token=token),
            job_queue=Queue(),
        )

        response = OctxTransferOut.from_transfer(decided)
        assert response.status is OctxTransferStatus.QUEUED
        assert response.updated_at is not None


def test_document_display_metadata_uses_readable_and_path_safe_fallbacks():
    from sag_api.sag.octx_importer import document_display_metadata

    assert document_display_metadata(
        {
            "path": "knowledge/documents/document-deadbeef.md",
            "metadata": {"title": "中文技术报告"},
        }
    ) == ("中文技术报告.md", "text/markdown")
    assert document_display_metadata(
        {
            "path": "knowledge/documents/safe.md",
            "metadata": {
                "sag": {
                    "filename": "../../private\\DeepSeekV3 技术报告.pdf",
                    "content_type": "application/pdf",
                }
            },
        }
    ) == ("DeepSeekV3 技术报告.pdf", "application/pdf")


@pytest.mark.asyncio
@pytest.mark.parametrize("with_events", [False, True], ids=["zero-event", "event-entity"])
async def test_structured_import_activates_source_only_after_shadow_is_ready(transfer_sessions, tmp_path, with_events):
    from octx import create_octx
    from octx.sag_align import write_structured_to_workspace
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from starlette.datastructures import UploadFile
    from zleap.sag.db.base import Base as SagBase
    from zleap.sag.db.models import SourceChunk, SourceEvent

    from sag_api.core.config import Settings
    from sag_api.db.models import Document, OctxInstallation, OctxSourceBinding, Source
    from sag_api.enums import OctxInstallationStatus, OctxTransferStatus
    from sag_api.octx.runner import OctxRunner
    from sag_api.octx.storage import OctxStorage
    from sag_api.services.octx_transfer_service import (
        create_import_transfer,
        execute_structured_import,
        preflight_import,
    )

    document_id = "018f5f7e-89ab-7def-8123-012345678aa0"
    chunk_id = "018f5f7e-89ab-7def-8123-012345678aa1"
    event_id = "018f5f7e-89ab-7def-8123-012345678aa2"
    entity_id = "018f5f7e-89ab-7def-8123-012345678aa3"
    workspace = tmp_path / "package-workspace"
    knowledge = workspace / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "index.md").write_text(
        '---\nokf_version: "1.0"\n---\n# Index\n\n- [Doc](doc.md)\n',
        encoding="utf-8",
    )
    (knowledge / "doc.md").write_text(
        f"---\noctx:\n  document_id: {document_id}\nsag:\n"
        '  filename: "DeepSeekV3 技术报告.pdf"\n'
        '  content_type: "application/pdf"\n---\n# Doc\n\nBody.\n',
        encoding="utf-8",
    )
    if with_events:
        write_structured_to_workspace(
            workspace,
            chunks=[
                {
                    "id": chunk_id,
                    "document_id": document_id,
                    "ordinal": 0,
                    "text": "Body",
                }
            ],
            events=[{"id": event_id, "title": "Event", "content": "Body"}],
            entities=[{"id": entity_id, "type": "topic", "name": "OCTX"}],
            chunk_events=[{"chunk_id": chunk_id, "event_id": event_id}],
            event_entities=[{"event_id": event_id, "entity_id": entity_id, "weight": 1.0}],
        )
    else:
        data = workspace / "data"
        relations = workspace / "relations"
        data.mkdir()
        relations.mkdir()
        (data / "chunks.jsonl").write_text(
            '{"id":"' + chunk_id + '","document_id":"' + document_id + '","ordinal":0,"text":"Body"}\n',
            encoding="utf-8",
        )
        for path in (
            data / "events.jsonl",
            data / "entities.jsonl",
            relations / "chunk-events.jsonl",
            relations / "event-entities.jsonl",
        ):
            path.write_text("", encoding="utf-8")
    package_path = tmp_path / "source.octx"
    create_octx(
        workspace,
        output=package_path,
        name="Imported Source",
        capabilities={"sag-structured": "0.1"},
    )

    class Queue:
        ids: list[str] = []

        async def enqueue(self, job_id: str) -> None:
            self.ids.append(job_id)

    class EngineManager:
        released: list[str] = []
        session_factory_requests: list[str] = []

        async def get_sag_session_factory(self, source_config_id: str):
            self.session_factory_requests.append(source_config_id)
            return sag_sessions

        async def release(self, source_config_id: str) -> None:
            self.released.append(source_config_id)

        async def get_chunk(self, source_config_id: str, chunk_id: str, **_ignored):
            assert source_config_id.startswith("octx_")
            return {"chunk_id": chunk_id, "content": "Body"}

        async def search(self, source_config_id: str, query: str, **_ignored):
            assert source_config_id.startswith("octx_")
            return type("Outcome", (), {"sections": [], "stats": {"top_k": 1}})()

    async def rebuild(source_config_id: str, checkpoint: dict, **_ignored) -> dict:
        assert source_config_id.startswith("octx_")
        assert checkpoint == {}
        graph_count = int(with_events)
        return {
            "chunks": 1,
            "events": graph_count,
            "entities": graph_count,
            "event_entities": graph_count,
        }

    sag_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sag.db'}")
    sag_sessions = async_sessionmaker(sag_engine, expire_on_commit=False)
    async with sag_engine.begin() as connection:
        await connection.run_sync(SagBase.metadata.create_all)
    storage = OctxStorage(tmp_path / "octx", max_upload_bytes=10 * 1024 * 1024)
    queue = Queue()
    engine_manager = EngineManager()
    try:
        async with transfer_sessions() as session:
            transfer = await create_import_transfer(
                session,
                UploadFile(filename="source.octx", file=BytesIO(package_path.read_bytes())),
                storage=storage,
                job_queue=queue,
            )
            await preflight_import(
                session,
                transfer,
                storage=storage,
                runner=OctxRunner(Settings(_env_file=None, octx_worker_timeout_seconds=30)),
                job_queue=queue,
                decision_secret="secret",
            )
            assert transfer.status is OctxTransferStatus.QUEUED
            await execute_structured_import(
                session,
                transfer,
                storage=storage,
                engine_manager=engine_manager,
                vector_rebuilder=rebuild,
                attempt=1,
            )
            assert transfer.status is OctxTransferStatus.READY
            source = await session.get(Source, transfer.target_source_id)
            binding = await session.get(OctxSourceBinding, source.id)
            installation = await session.get(OctxInstallation, transfer.installation_id)
            documents = (await session.execute(select(Document).where(Document.source_id == source.id))).scalars().all()
            assert source.document_count == source.chunk_count == 1
            assert source.event_count == int(with_events)
            assert binding.active_release_id == transfer.release_id
            assert installation.status is OctxInstallationStatus.ACTIVE
            assert len(documents) == 1 and documents[0].is_active is True
            assert documents[0].filename == "DeepSeekV3 技术报告.pdf"
            assert documents[0].content_type == "application/pdf"
            assert engine_manager.session_factory_requests == [source.sag_source_config_id]
        async with sag_sessions() as session:
            assert await session.scalar(select(func.count()).select_from(SourceChunk)) == 1
            assert await session.scalar(select(func.count()).select_from(SourceEvent)) == int(with_events)
    finally:
        await sag_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("trusted_vectors", [True, False])
async def test_export_transfer_builds_immutable_fully_validated_release(
    transfer_sessions,
    tmp_path,
    trusted_vectors,
):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from octx import open_octx, validate_octx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from zleap.sag.db.base import Base as SagBase
    from zleap.sag.db.models import (
        Article,
        ArticleParseStatus,
        Entity,
        EntityType,
        EventEntity,
        SourceChunk,
        SourceConfig,
        SourceEvent,
    )

    from sag_api.core.config import Settings
    from sag_api.db.models import Document, OctxRelease, OctxSourceBinding, Source
    from sag_api.enums import DocumentStatus, OctxTransferStatus
    from sag_api.octx.runner import OctxRunner
    from sag_api.octx.storage import OctxStorage
    from sag_api.sag.octx_importer import (
        build_structured_plan,
        import_structured_plan,
    )
    from sag_api.sag.octx_smoke_test import smoke_test_installation
    from sag_api.sag.octx_vector_protocol import embedding_identity
    from sag_api.sag.octx_vector_rebuilder import rebuild_vectors
    from sag_api.services.octx_transfer_service import (
        create_export_transfer,
        execute_export,
    )

    class Queue:
        ids: list[str] = []

        async def enqueue(self, job_id: str) -> None:
            self.ids.append(job_id)

    class EngineManager:
        @asynccontextmanager
        async def maintenance(self, source_config_id, source=None):
            assert source_config_id == "src_export"
            yield object()

    sag_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'export-sag.db'}")
    sag_sessions = async_sessionmaker(sag_engine, expire_on_commit=False)
    async with sag_engine.begin() as connection:
        await connection.run_sync(SagBase.metadata.create_all)
    article_id = str(__import__("uuid").uuid4())
    chunk_id = str(__import__("uuid").uuid4())
    event_id = str(__import__("uuid").uuid4())
    entity_id = str(__import__("uuid").uuid4())
    type_id = str(__import__("uuid").uuid4())
    async with sag_sessions() as sag_session:
        vector_identity = embedding_identity(
            SimpleNamespace(
                model="test/embedding" if trusted_vectors else "previous/embedding",
                base_url="https://embedding.invalid/v1",
                dimensions=2,
            )
        )
        sag_session.add(
            SourceConfig(
                id="src_export",
                name="Export",
                target_config={"octx_vector_identity": vector_identity},
            )
        )
        sag_session.add(
            Article(
                id=article_id,
                source_config_id="src_export",
                title="Export Doc",
                content="# Export Doc\n\nBody",
                status="COMPLETED",
                parse_status=ArticleParseStatus.COMPLETED,
            )
        )
        sag_session.add(
            EntityType(
                id=type_id,
                scope="source",
                source_config_id="src_export",
                type="topic",
                name="Topic",
                weight=1,
                similarity_threshold=0.8,
            )
        )
        await sag_session.flush()
        sag_session.add(
            SourceChunk(
                id=chunk_id,
                source_config_id="src_export",
                source_type="ARTICLE",
                source_id=article_id,
                article_id=article_id,
                content="Body",
                rank=0,
                chunk_length=4,
            )
        )
        sag_session.add(
            Entity(
                id=entity_id,
                source_config_id="src_export",
                entity_type_id=type_id,
                type="topic",
                name="OCTX",
                normalized_name="octx",
            )
        )
        sag_session.add(
            SourceEvent(
                id=event_id,
                source_config_id="src_export",
                source_type="ARTICLE",
                source_id=article_id,
                article_id=article_id,
                title="Exported",
                summary="Summary",
                content="Body",
                rank=0,
                level=0,
                chunk_id=chunk_id,
            )
        )
        await sag_session.flush()
        sag_session.add(
            EventEntity(
                id=str(__import__("uuid").uuid4()),
                event_id=event_id,
                entity_id=entity_id,
                weight=1,
            )
        )
        await sag_session.commit()

    storage = OctxStorage(tmp_path / "export-octx", max_upload_bytes=1024)
    queue = Queue()
    try:
        async with transfer_sessions() as session:
            source = Source(name="Export", sag_source_config_id="src_export")
            session.add(source)
            await session.flush()
            session.add(
                Document(
                    source_id=source.id,
                    filename="export.md",
                    storage_path=str(tmp_path / "export.md"),
                    status=DocumentStatus.READY,
                    sag_source_id=article_id,
                    is_active=True,
                )
            )
            await session.commit()
            transfer = await create_export_transfer(session, source.id, version=None, job_queue=queue)

            class Embedding:
                model = "test/embedding"
                base_url = "https://embedding.invalid/v1"
                dimensions = 2

                async def batch_generate(self, texts):
                    raise AssertionError("OCTX export must never call the embedding provider")

            class VectorStore:
                async def fetch_vector_fields(self, index, ids, fields):
                    return {record_id: {field: [0.1, 0.2] for field in fields} for record_id in ids}

            await execute_export(
                session,
                transfer,
                storage=storage,
                runner=OctxRunner(Settings(_env_file=None, octx_worker_timeout_seconds=30)),
                engine_manager=EngineManager(),
                sag_session_factory=sag_sessions,
                embedding_client=Embedding(),
                vector_store=VectorStore(),
                attempt=1,
            )
            assert transfer.status is OctxTransferStatus.READY
            binding = await session.get(OctxSourceBinding, source.id)
            release = await session.get(OctxRelease, transfer.release_id)
            assert binding.active_release_id == release.id
            artifact = storage.resolve_key(release.artifact_key)
            with open_octx(artifact) as package:
                assert package.manifest["capabilities"]["sag-structured"]["version"] == "0.1"
                vector_capability = package.manifest["capabilities"].get("vectors")
                assert (vector_capability or {}).get("version") == ("0.1" if trusted_vectors else None)
                assert len(list(package.iter_documents())) == 1
            validation = validate_octx(artifact)
            assert validation.valid and validation.fully_validated

            roundtrip_plan = tmp_path / "roundtrip.sqlite3"
            roundtrip_namespace = str(__import__("uuid").uuid4())
            build_structured_plan(
                artifact,
                roundtrip_plan,
                roundtrip_namespace,
            )
            imported = await import_structured_plan(
                roundtrip_plan,
                roundtrip_namespace,
                source_config_id="src_roundtrip",
                source_name="Round Trip",
                session_factory=sag_sessions,
            )
            assert imported.counts == {
                "documents": 1,
                "chunks": 1,
                "events": 1,
                "entities": 1,
                "event_entities": 1,
            }

            if trusted_vectors:

                class NoRoundTripEmbedding:
                    model = "test/embedding"
                    base_url = "https://embedding.invalid/v1"
                    dimensions = 2

                    async def batch_generate(self, _texts):
                        raise AssertionError("compatible production export must skip all Embedding calls")

                class RoundTripVectors:
                    indexes: list[str] = []

                    async def bulk_index(self, *, index, documents, return_details, routing):
                        assert routing == "src_roundtrip"
                        self.indexes.append(index)
                        return {"success_count": len(documents), "error_count": 0}

                roundtrip_vectors = RoundTripVectors()
                rebuilt = await rebuild_vectors(
                    "src_roundtrip",
                    {},
                    session_factory=sag_sessions,
                    embedding_client=NoRoundTripEmbedding(),
                    vector_store=roundtrip_vectors,
                    package_path=artifact,
                    plan_path=roundtrip_plan,
                )
                assert rebuilt == {
                    "chunks": 1,
                    "events": 1,
                    "entities": 1,
                    "event_entities": 1,
                }
                assert roundtrip_vectors.indexes == [
                    "source_chunks",
                    "event_vectors",
                    "entity_vectors",
                    "event_entity_vectors",
                ]

            class RoundTripEngine:
                async def get_chunk(self, *_args, **_kwargs):
                    return {"content": "Body"}

                async def search(self, *_args, **_kwargs):
                    return type("Outcome", (), {"stats": {"top_k": 1}})()

            smoke = await smoke_test_installation(
                "src_roundtrip",
                expected_counts={
                    "chunks": 1,
                    "events": 1,
                    "entities": 1,
                    "event_entities": 1,
                },
                engine_manager=RoundTripEngine(),
                sag_session_factory=sag_sessions,
            )
            assert smoke["counts"]["events"] == 1
    finally:
        await sag_engine.dispose()


@pytest.mark.asyncio
async def test_startup_recovery_requeues_transfer_and_removes_expired_lease(
    transfer_sessions,
):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from sag_api.db.models import Job, OctxOperationLease, OctxTransfer
    from sag_api.enums import (
        JobStatus,
        JobType,
        OctxTransferDirection,
        OctxTransferStatus,
    )
    from sag_api.services.octx_recovery_service import recover_octx_state

    async with transfer_sessions() as session:
        transfer = OctxTransfer(
            direction=OctxTransferDirection.IMPORT,
            status=OctxTransferStatus.IMPORTING,
        )
        session.add(transfer)
        expired_terminal = OctxTransfer(
            direction=OctxTransferDirection.EXPORT,
            status=OctxTransferStatus.READY,
            staging_key="staging/old/output.octx",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        session.add(expired_terminal)
        await session.flush()
        session.add(
            Job(
                type=JobType.OCTX_IMPORT,
                status=JobStatus.RUNNING,
                payload={"transfer_id": transfer.id},
            )
        )
        completed_job = Job(
            type=JobType.OCTX_EXPORT,
            status=JobStatus.RUNNING,
            payload={"transfer_id": expired_terminal.id},
        )
        session.add(completed_job)
        session.add(
            OctxOperationLease(
                resource_key="source:expired",
                owner_token="dead-worker",
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
                heartbeat_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        await session.commit()

        stats = await recover_octx_state(session)
        await session.refresh(transfer)
        assert transfer.status is OctxTransferStatus.QUEUED
        assert stats == {
            "requeued": 1,
            "expired": 0,
            "leases_removed": 1,
            "gc_scheduled": 1,
        }
        assert await session.get(OctxOperationLease, "source:expired") is None
        await session.refresh(completed_job)
        assert completed_job.status is JobStatus.SUCCEEDED
        assert completed_job.progress == 1.0
        assert completed_job.finished_at is not None
        gc_job = await session.scalar(select(Job).where(Job.type == JobType.OCTX_GC_TRANSFER))
        assert gc_job is not None and gc_job.status is JobStatus.QUEUED


@pytest.mark.asyncio
async def test_startup_recovery_fails_exhausted_octx_job_instead_of_requeueing(
    transfer_sessions,
):
    from sag_api.core.config import settings
    from sag_api.db.models import Job, OctxTransfer
    from sag_api.enums import JobStatus, JobType, OctxTransferDirection, OctxTransferStatus
    from sag_api.services.octx_recovery_service import recover_octx_state

    async with transfer_sessions() as session:
        transfer = OctxTransfer(
            direction=OctxTransferDirection.EXPORT,
            status=OctxTransferStatus.EXPORTING,
        )
        session.add(transfer)
        await session.flush()
        job = Job(
            type=JobType.OCTX_EXPORT,
            status=JobStatus.RUNNING,
            attempts=settings.job_max_attempts,
            payload={"transfer_id": transfer.id},
        )
        session.add(job)
        await session.commit()

        stats = await recover_octx_state(session)
        await session.refresh(transfer)
        await session.refresh(job)

        assert stats["requeued"] == 0
        assert transfer.status is OctxTransferStatus.FAILED
        assert transfer.error["code"] == "octx_recovery_attempts_exhausted"
        assert job.status is JobStatus.FAILED


@pytest.mark.asyncio
async def test_smoke_test_installation_rejects_missing_shadow_partition(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from zleap.sag.db.base import Base as SagBase

    from sag_api.core.error_taxonomy import ErrorCode
    from sag_api.core.errors import ValidationError
    from sag_api.sag.octx_smoke_test import smoke_test_installation

    sag_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'smoke-missing.db'}")
    sag_sessions = async_sessionmaker(sag_engine, expire_on_commit=False)
    async with sag_engine.begin() as connection:
        await connection.run_sync(SagBase.metadata.create_all)

    class EngineManager:
        async def get_chunk(self, *args, **kwargs):  # pragma: no cover - unused
            raise AssertionError("get_chunk should not run when partition is missing")

        async def search(self, *args, **kwargs):  # pragma: no cover - unused
            raise AssertionError("search should not run when partition is missing")

    try:
        with pytest.raises(ValidationError) as exception_info:
            await smoke_test_installation(
                "octx_shadow_missing",
                expected_counts={"chunks": 1},
                engine_manager=EngineManager(),
                sag_session_factory=sag_sessions,
            )
    finally:
        await sag_engine.dispose()

    assert exception_info.value.code is ErrorCode.OCTX_SHADOW_VALIDATION_FAILED
    assert exception_info.value.retryable is False


@pytest.mark.asyncio
async def test_smoke_test_installation_rejects_row_count_mismatch(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from zleap.sag.db.base import Base as SagBase
    from zleap.sag.db.models import SourceConfig

    from sag_api.core.error_taxonomy import ErrorCode
    from sag_api.core.errors import ValidationError
    from sag_api.sag.octx_smoke_test import smoke_test_installation

    sag_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'smoke-count.db'}")
    sag_sessions = async_sessionmaker(sag_engine, expire_on_commit=False)
    async with sag_engine.begin() as connection:
        await connection.run_sync(SagBase.metadata.create_all)
    async with sag_sessions() as session:
        session.add(SourceConfig(id="octx_shadow_empty", name="Shadow", target_config={}))
        await session.commit()

    class EngineManager:
        async def get_chunk(self, *args, **kwargs):  # pragma: no cover - unused
            raise AssertionError("get_chunk should not run when counts mismatch")

        async def search(self, *args, **kwargs):  # pragma: no cover - unused
            raise AssertionError("search should not run when counts mismatch")

    try:
        with pytest.raises(ValidationError) as exception_info:
            await smoke_test_installation(
                "octx_shadow_empty",
                expected_counts={"chunks": 5},
                engine_manager=EngineManager(),
                sag_session_factory=sag_sessions,
            )
    finally:
        await sag_engine.dispose()

    assert exception_info.value.code is ErrorCode.OCTX_SHADOW_VALIDATION_FAILED
    assert "chunks" in str(exception_info.value.message)


@pytest.mark.asyncio
async def test_smoke_test_installation_wraps_engine_search_failure(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from zleap.sag.db.base import Base as SagBase
    from zleap.sag.db.models import SourceConfig

    from sag_api.core.error_taxonomy import ErrorCode
    from sag_api.core.errors import ValidationError
    from sag_api.sag.octx_smoke_test import smoke_test_installation

    sag_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'smoke-search.db'}")
    sag_sessions = async_sessionmaker(sag_engine, expire_on_commit=False)
    async with sag_engine.begin() as connection:
        await connection.run_sync(SagBase.metadata.create_all)
    async with sag_sessions() as session:
        session.add(SourceConfig(id="octx_shadow_ok", name="Shadow", target_config={}))
        await session.commit()

    class EngineManager:
        async def get_chunk(self, *args, **kwargs):
            return {"chunk_id": "x", "content": "y"}

        async def search(self, *args, **kwargs):
            raise RuntimeError("vector backend unavailable")

    try:
        with pytest.raises(ValidationError) as exception_info:
            await smoke_test_installation(
                "octx_shadow_ok",
                expected_counts={},
                engine_manager=EngineManager(),
                sag_session_factory=sag_sessions,
            )
    finally:
        await sag_engine.dispose()

    assert exception_info.value.code is ErrorCode.OCTX_SHADOW_VALIDATION_FAILED
    assert "vector search failed" in str(exception_info.value.message)


@pytest.mark.asyncio
async def test_structured_import_reports_validation_error_when_smoke_test_fails(transfer_sessions, tmp_path):
    from octx import create_octx
    from octx.sag_align import write_structured_to_workspace
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from starlette.datastructures import UploadFile
    from zleap.sag.db.base import Base as SagBase

    from sag_api.core.config import Settings
    from sag_api.core.error_taxonomy import ErrorCode
    from sag_api.core.errors import ValidationError
    from sag_api.enums import OctxTransferStatus
    from sag_api.octx.runner import OctxRunner
    from sag_api.octx.storage import OctxStorage
    from sag_api.services.octx_transfer_service import (
        create_import_transfer,
        execute_structured_import,
        preflight_import,
    )

    document_id = "018f5f7e-89ab-7def-8123-012345670aa0"
    chunk_id = "018f5f7e-89ab-7def-8123-012345670aa1"
    event_id = "018f5f7e-89ab-7def-8123-012345670aa2"
    entity_id = "018f5f7e-89ab-7def-8123-012345670aa3"
    workspace = tmp_path / "package-workspace"
    knowledge = workspace / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "index.md").write_text('---\nokf_version: "1.0"\n---\n# Index\n\n- [Doc](doc.md)\n', encoding="utf-8")
    (knowledge / "doc.md").write_text(
        f"---\noctx:\n  document_id: {document_id}\n---\n# Doc\n\nBody.\n",
        encoding="utf-8",
    )
    write_structured_to_workspace(
        workspace,
        chunks=[{"id": chunk_id, "document_id": document_id, "ordinal": 0, "text": "Body"}],
        events=[{"id": event_id, "title": "Event", "content": "Body"}],
        entities=[{"id": entity_id, "type": "topic", "name": "OCTX"}],
        chunk_events=[{"chunk_id": chunk_id, "event_id": event_id}],
        event_entities=[{"event_id": event_id, "entity_id": entity_id, "weight": 1.0}],
    )
    package_path = tmp_path / "source.octx"
    create_octx(
        workspace,
        output=package_path,
        name="Imported Source",
        capabilities={"sag-structured": "0.1"},
    )

    class Queue:
        ids: list[str] = []

        async def enqueue(self, job_id: str) -> None:
            self.ids.append(job_id)

    class EngineManager:
        async def release(self, source_config_id: str) -> None:  # pragma: no cover
            pass

        async def get_chunk(self, source_config_id: str, chunk_id: str, **_ignored):
            return {"chunk_id": chunk_id, "content": "Body"}

        async def search(self, *args, **kwargs):
            raise RuntimeError("shadow vector backend not ready")

    async def rebuild(source_config_id: str, checkpoint: dict, **_ignored) -> dict:
        return {"chunks": 1, "events": 1, "entities": 1, "event_entities": 1}

    sag_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sag.db'}")
    sag_sessions = async_sessionmaker(sag_engine, expire_on_commit=False)
    async with sag_engine.begin() as connection:
        await connection.run_sync(SagBase.metadata.create_all)
    storage = OctxStorage(tmp_path / "octx", max_upload_bytes=10 * 1024 * 1024)
    queue = Queue()
    try:
        async with transfer_sessions() as session:
            transfer = await create_import_transfer(
                session,
                UploadFile(
                    filename="source.octx",
                    file=BytesIO(package_path.read_bytes()),
                ),
                storage=storage,
                job_queue=queue,
            )
            await preflight_import(
                session,
                transfer,
                storage=storage,
                runner=OctxRunner(Settings(_env_file=None, octx_worker_timeout_seconds=30)),
                job_queue=queue,
                decision_secret="secret",
            )
            with pytest.raises(ValidationError) as exception_info:
                await execute_structured_import(
                    session,
                    transfer,
                    storage=storage,
                    engine_manager=EngineManager(),
                    sag_session_factory=sag_sessions,
                    vector_rebuilder=rebuild,
                    attempt=1,
                )
    finally:
        await sag_engine.dispose()
    assert exception_info.value.code is ErrorCode.OCTX_SHADOW_VALIDATION_FAILED
    assert transfer.status is OctxTransferStatus.INDEXING


@pytest.mark.asyncio
async def test_import_rechecks_confirmed_source_revision_after_acquiring_lease(transfer_sessions, monkeypatch):
    """A queued update must not overwrite source changes made after user confirmation."""
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import OctxAsset, OctxRelease, OctxSourceBinding, OctxTransfer, Source
    from sag_api.enums import (
        OctxAssetOwnership,
        OctxImportAction,
        OctxReleaseOrigin,
        OctxTransferDirection,
        OctxTransferStatus,
    )
    from sag_api.services import octx_transfer_service

    called = False

    async def structured(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(octx_transfer_service, "execute_structured_import", structured)
    async with transfer_sessions() as session:
        source = Source(id="revision-source", name="Source", sag_source_config_id="revision-config")
        asset = OctxAsset(id="revision-asset", name="Asset", ownership=OctxAssetOwnership.IMPORTED)
        release = OctxRelease(
            id="revision-release",
            asset_id=asset.id,
            version="1.1.0",
            package_digest="sha256:" + "a" * 64,
            manifest={},
            artifact_key="releases/revision.octx",
            created_by=OctxReleaseOrigin.IMPORT,
        )
        session.add_all([source, asset, release])
        await session.flush()
        session.add(
            OctxSourceBinding(
                source_id=source.id,
                asset_id=asset.id,
                active_release_id=release.id,
                content_revision=6,
                released_revision=5,
            )
        )
        transfer = OctxTransfer(
            direction=OctxTransferDirection.IMPORT,
            status=OctxTransferStatus.QUEUED,
            asset_id=asset.id,
            release_id=release.id,
            target_source_id=source.id,
            selected_action=OctxImportAction.UPDATE,
            checkpoint={
                "capabilities": {"sag-structured": {"version": "0.1"}},
                "expected_source_revision": 5,
            },
        )
        session.add(transfer)
        await session.commit()

        with pytest.raises(ConflictError) as caught:
            await octx_transfer_service.execute_import(session, transfer)

    assert caught.value.code == "octx_decision_stale"
    assert called is False


@pytest.mark.asyncio
async def test_structured_import_persists_and_resumes_vector_checkpoint(transfer_sessions, tmp_path):
    from octx import create_octx
    from octx.sag_align import write_structured_to_workspace
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from starlette.datastructures import UploadFile
    from zleap.sag.db.base import Base as SagBase

    from sag_api.core.config import Settings
    from sag_api.db.models import OctxTransfer
    from sag_api.enums import OctxTransferStatus
    from sag_api.octx.runner import OctxRunner
    from sag_api.octx.storage import OctxStorage
    from sag_api.services.octx_transfer_service import (
        create_import_transfer,
        execute_structured_import,
        preflight_import,
    )

    document_id = "018f5f7e-89ab-7def-8123-012345671aa0"
    chunk_id = "018f5f7e-89ab-7def-8123-012345671aa1"
    event_id = "018f5f7e-89ab-7def-8123-012345671aa2"
    entity_id = "018f5f7e-89ab-7def-8123-012345671aa3"
    workspace = tmp_path / "package-workspace"
    knowledge = workspace / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "index.md").write_text('---\nokf_version: "1.0"\n---\n# Index\n\n- [Doc](doc.md)\n', encoding="utf-8")
    (knowledge / "doc.md").write_text(
        f"---\noctx:\n  document_id: {document_id}\n---\n# Doc\n\nBody.\n",
        encoding="utf-8",
    )
    write_structured_to_workspace(
        workspace,
        chunks=[{"id": chunk_id, "document_id": document_id, "ordinal": 0, "text": "Body"}],
        events=[{"id": event_id, "title": "Event", "content": "Body"}],
        entities=[{"id": entity_id, "type": "topic", "name": "OCTX"}],
        chunk_events=[{"chunk_id": chunk_id, "event_id": event_id}],
        event_entities=[{"event_id": event_id, "entity_id": entity_id, "weight": 1.0}],
    )
    package_path = tmp_path / "source.octx"
    create_octx(
        workspace,
        output=package_path,
        name="Imported Source",
        capabilities={"sag-structured": "0.1"},
    )

    class Queue:
        ids: list[str] = []

        async def enqueue(self, job_id: str) -> None:
            self.ids.append(job_id)

    class EngineManager:
        async def release(self, source_config_id: str) -> None:  # pragma: no cover
            pass

        async def get_chunk(self, source_config_id: str, chunk_id: str, **_ignored):
            return {"chunk_id": chunk_id, "content": "Body"}

        async def search(self, source_config_id: str, query: str, **_ignored):
            return type("Outcome", (), {"sections": [], "stats": {"top_k": 1}})()

    checkpoints_seen: list[dict] = []
    persisted_vector_progress: list[tuple[float, dict]] = []

    async def rebuild_with_checkpoint(source_config_id: str, checkpoint: dict, *, on_checkpoint=None) -> dict:
        checkpoints_seen.append(dict(checkpoint))
        if on_checkpoint is not None:
            checkpoint.update(
                {
                    "chunks_cursor": "chunk-1",
                    "current_kind": "chunks",
                    "current_batch_size": 1,
                    "batch_state": "completed",
                    "counts": {
                        "chunks": 1,
                        "events": 0,
                        "entities": 0,
                        "event_entities": 0,
                    },
                }
            )
            await on_checkpoint(checkpoint)
            async with transfer_sessions() as progress_session:
                persisted = await progress_session.get(OctxTransfer, transfer.id)
                persisted_vector_progress.append((persisted.progress, dict(persisted.checkpoint["progress_detail"])))
        return {"chunks": 1, "events": 1, "entities": 1, "event_entities": 1}

    sag_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sag.db'}")
    sag_sessions = async_sessionmaker(sag_engine, expire_on_commit=False)
    async with sag_engine.begin() as connection:
        await connection.run_sync(SagBase.metadata.create_all)
    storage = OctxStorage(tmp_path / "octx", max_upload_bytes=10 * 1024 * 1024)
    queue = Queue()
    try:
        async with transfer_sessions() as session:
            transfer = await create_import_transfer(
                session,
                UploadFile(
                    filename="source.octx",
                    file=BytesIO(package_path.read_bytes()),
                ),
                storage=storage,
                job_queue=queue,
            )
            await preflight_import(
                session,
                transfer,
                storage=storage,
                runner=OctxRunner(Settings(_env_file=None, octx_worker_timeout_seconds=30)),
                job_queue=queue,
                decision_secret="secret",
            )
            await execute_structured_import(
                session,
                transfer,
                storage=storage,
                engine_manager=EngineManager(),
                sag_session_factory=sag_sessions,
                vector_rebuilder=rebuild_with_checkpoint,
                attempt=1,
            )
            assert transfer.status is OctxTransferStatus.READY
            checkpoint = dict(transfer.checkpoint or {})
            vector_progress = checkpoint.get("vector_progress")
            assert isinstance(vector_progress, dict)
            assert vector_progress.get("chunks_cursor") == "chunk-1"
            smoke = checkpoint.get("smoke_test")
            assert isinstance(smoke, dict)
            assert checkpoint["progress_detail"]["phase"] == "complete"
            assert checkpoint["progress_detail"]["duration_seconds"] >= 0
    finally:
        await sag_engine.dispose()
    assert checkpoints_seen and checkpoints_seen[0] == {}
    assert persisted_vector_progress == [
        (
            pytest.approx(0.745),
            {
                "phase": "vectorizing",
                "current_kind": "chunks",
                "current_batch_size": 1,
                    "completed_vectors": 1,
                    "total_vectors": 4,
                    "batch_state": "completed",
                    "vector_mode": "generate",
                    "written_records": 1,
                    "role_total_records": 1,
                    "reused_records": 0,
                    "generated_records": 1,
                    "reusable_vector_roles": [],
            },
        )
    ]


@pytest.mark.asyncio
async def test_knowledge_import_persists_nested_document_progress(transfer_sessions, tmp_path, monkeypatch):
    from sag_api.db.models import OctxAsset, OctxRelease, OctxTransfer
    from sag_api.enums import (
        OctxAssetOwnership,
        OctxReleaseOrigin,
        OctxTransferDirection,
        OctxTransferStatus,
    )
    from sag_api.octx.storage import OctxStorage
    from sag_api.services import octx_transfer_service

    class CheckpointCaptured(RuntimeError):
        pass

    async def import_with_nested_mutation(*_args, checkpoint, on_checkpoint, **_kwargs):
        checkpoint.setdefault("documents", {})["knowledge/a.md"] = {
            "status": "ready",
            "logical_path": "knowledge/a.md",
        }
        checkpoint["documents"]["knowledge/b.md"] = {
            "status": "processing",
            "logical_path": "knowledge/b.md",
        }
        await on_checkpoint(checkpoint)
        raise CheckpointCaptured

    monkeypatch.setattr(
        octx_transfer_service,
        "import_knowledge_package",
        import_with_nested_mutation,
    )

    class EngineManager:
        async def get_sag_session_factory(self, _source_config_id):
            return object()

    storage = OctxStorage(tmp_path / "octx", max_upload_bytes=1024)
    async with transfer_sessions() as session:
        asset = OctxAsset(
            id="018f5f7e-89ab-7def-8123-012345678900",
            name="Knowledge",
            ownership=OctxAssetOwnership.IMPORTED,
        )
        release = OctxRelease(
            id="release-knowledge-progress",
            asset_id=asset.id,
            version="1.0.0",
            package_digest="sha256:" + "a" * 64,
            manifest={},
            artifact_key="releases/knowledge.octx",
            created_by=OctxReleaseOrigin.IMPORT,
        )
        transfer = OctxTransfer(
            id="transfer-knowledge-progress",
            direction=OctxTransferDirection.IMPORT,
            status=OctxTransferStatus.QUEUED,
            progress=0.1,
            asset_id=asset.id,
            release_id=release.id,
            checkpoint={
                "record_counts": {"documents": 4},
                "knowledge": {"documents": {}},
            },
        )
        session.add_all([asset, release, transfer])
        await session.commit()

        with pytest.raises(CheckpointCaptured):
            await octx_transfer_service.execute_knowledge_import(
                session,
                transfer,
                storage=storage,
                engine_manager=EngineManager(),
            )

    async with transfer_sessions() as session:
        persisted = await session.get(OctxTransfer, "transfer-knowledge-progress")
        assert persisted.progress == pytest.approx(0.35)
        assert persisted.checkpoint["progress_detail"] == {
            "phase": "rebuilding_documents",
            "completed_documents": 1,
            "total_documents": 4,
            "current_document": "knowledge/b.md",
        }
        assert persisted.checkpoint["knowledge"]["documents"]["knowledge/a.md"]["status"] == "ready"


@pytest.mark.asyncio
async def test_knowledge_import_activates_documents_with_foreign_keys_enabled(transfer_sessions, tmp_path, monkeypatch):
    from sqlalchemy import select

    from sag_api.db.models import Document, OctxAsset, OctxInstallation, OctxRelease, OctxTransfer, Source
    from sag_api.enums import (
        OctxAssetOwnership,
        OctxReleaseOrigin,
        OctxTransferDirection,
        OctxTransferStatus,
    )
    from sag_api.octx.storage import OctxStorage
    from sag_api.sag.octx_importer import ImportStats
    from sag_api.services import octx_transfer_service

    async def import_ready_document(_package_path, controlled_dir, *, checkpoint, on_checkpoint, **_kwargs):
        controlled_path = controlled_dir / "00000000.md"
        controlled_path.parent.mkdir(parents=True, exist_ok=True)
        controlled_path.write_text("# Imported", encoding="utf-8")
        checkpoint.setdefault("documents", {})["knowledge/imported.md"] = {
            "status": "ready",
            "logical_path": "knowledge/imported.md",
            "controlled_path": str(controlled_path),
            "sag_source_id": "article-imported",
            "octx_document_id": "018f5f7e-89ab-7def-8123-012345678901",
            "chunk_count": 1,
            "event_count": 0,
            "token_usage": 10,
        }
        await on_checkpoint(checkpoint)
        return ImportStats(counts={"documents": 1, "chunks": 1, "events": 0})

    async def smoke(*_args, **_kwargs):
        return {"sample_chunk_id": None, "search_stats": {}}

    monkeypatch.setattr(octx_transfer_service, "import_knowledge_package", import_ready_document)
    monkeypatch.setattr(octx_transfer_service, "smoke_test_installation", smoke)
    monkeypatch.setattr(octx_transfer_service.settings, "upload_dir", str(tmp_path / "uploads"))

    class EngineManager:
        async def get_sag_session_factory(self, _source_config_id):
            return object()

        async def release(self, _source_config_id):  # pragma: no cover
            raise AssertionError("new imports have no old partition")

    storage = OctxStorage(tmp_path / "octx", max_upload_bytes=1024)
    artifact = storage.resolve_key("releases/knowledge.octx")
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"package")
    asset_id = "018f5f7e-89ab-7def-8123-012345678902"
    async with transfer_sessions() as session:
        asset = OctxAsset(
            id=asset_id,
            name="Knowledge",
            ownership=OctxAssetOwnership.IMPORTED,
        )
        release = OctxRelease(
            id="release-knowledge-fk",
            asset_id=asset.id,
            version="1.0.0",
            package_digest="sha256:" + "b" * 64,
            manifest={},
            artifact_key="releases/knowledge.octx",
            created_by=OctxReleaseOrigin.IMPORT,
        )
        transfer = OctxTransfer(
            id="transfer-knowledge-fk",
            direction=OctxTransferDirection.IMPORT,
            status=OctxTransferStatus.QUEUED,
            asset_id=asset.id,
            release_id=release.id,
            checkpoint={"asset_name": "Knowledge", "record_counts": {"documents": 1}},
        )
        session.add_all([asset, release, transfer])
        await session.commit()

        result = await octx_transfer_service.execute_knowledge_import(
            session,
            transfer,
            storage=storage,
            engine_manager=EngineManager(),
        )

        assert result.status is OctxTransferStatus.READY
        assert result.checkpoint["progress_detail"]["phase"] == "complete"
        assert result.checkpoint["progress_detail"]["duration_seconds"] >= 0
        assert await session.get(Source, result.target_source_id) is not None
        assert await session.get(OctxInstallation, result.installation_id) is not None
        document = await session.scalar(select(Document).where(Document.octx_installation_id == result.installation_id))
        assert document is not None and document.source_id == result.target_source_id


@pytest.mark.asyncio
@pytest.mark.parametrize("document_status", [None, "failed"], ids=["empty", "no-ready"])
async def test_export_rejects_sources_without_any_ready_document(transfer_sessions, document_status):
    from sqlalchemy import func, select

    from sag_api.core.errors import ConflictError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus
    from sag_api.services.octx_transfer_service import create_export_transfer

    class Queue:
        async def enqueue(self, job_id: str) -> None:  # pragma: no cover
            raise AssertionError(f"unexpected export job: {job_id}")

    async with transfer_sessions() as session:
        source = Source(name="No READY", sag_source_config_id="no-ready")
        session.add(source)
        await session.flush()
        if document_status is not None:
            session.add(
                Document(
                    source_id=source.id,
                    filename="failed.md",
                    storage_path="/tmp/failed.md",
                    status=DocumentStatus.FAILED,
                    is_active=True,
                )
            )
        await session.commit()

        with pytest.raises(ConflictError) as caught:
            await create_export_transfer(session, source.id, version=None, job_queue=Queue())

        assert caught.value.code == "octx_source_not_exportable"
        assert await session.scalar(select(func.count(Job.id))) == 0


@pytest.mark.asyncio
async def test_mixed_source_requires_signed_ready_only_export_decision(
    transfer_sessions,
):
    """Queuing a mixed-status source immediately would export an ambiguous moving subset."""
    from sqlalchemy import func, select

    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import (
        DocumentStatus,
        OctxExportAction,
        OctxTransferStatus,
    )
    from sag_api.schemas.octx import OctxTransferOut
    from sag_api.services.octx_transfer_service import (
        create_export_transfer,
        submit_export_decision,
    )

    class Queue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, job_id: str) -> None:
            self.ids.append(job_id)

    queue = Queue()
    async with transfer_sessions() as session:
        source = Source(name="Mixed", sag_source_config_id="mixed-source")
        session.add(source)
        await session.flush()
        ready = Document(
            source_id=source.id,
            filename="ready.md",
            storage_path="/tmp/ready.md",
            status=DocumentStatus.READY,
            sag_source_id="article-ready",
            is_active=True,
        )
        failed = Document(
            source_id=source.id,
            filename="failed.md",
            storage_path="/tmp/failed.md",
            status=DocumentStatus.FAILED,
            error="extract failed",
            is_active=True,
        )
        session.add_all([ready, failed])
        await session.commit()

        transfer = await create_export_transfer(session, source.id, version=None, job_queue=queue)

        assert transfer.status is OctxTransferStatus.DECISION_REQUIRED
        response = OctxTransferOut.from_transfer(transfer)
        assert response.updated_at is not None
        assert queue.ids == []
        checkpoint = dict(transfer.checkpoint or {})
        assert checkpoint["allowed_actions"] == ["export_ready_only", "cancel"]
        assert checkpoint["selected_document_ids"] == [ready.id]
        assert checkpoint["selected_article_ids"] == ["article-ready"]
        assert checkpoint["excluded_documents"] == [
            {
                "id": failed.id,
                "filename": "failed.md",
                "status": "failed",
                "error": "extract failed",
            }
        ]
        token = checkpoint["decision_token"]

        confirmed = await submit_export_decision(
            session,
            transfer.id,
            action=OctxExportAction.EXPORT_READY_ONLY,
            decision_token=token,
            job_queue=queue,
        )

        assert confirmed.status is OctxTransferStatus.QUEUED
        assert len(queue.ids) == 1
        assert await session.scalar(select(func.count(Job.id))) == 1

        repeated = await submit_export_decision(
            session,
            transfer.id,
            action=OctxExportAction.EXPORT_READY_ONLY,
            decision_token=token,
            job_queue=queue,
        )
        assert repeated.id == transfer.id
        assert len(queue.ids) == 1
        assert await session.scalar(select(func.count(Job.id))) == 1


@pytest.mark.asyncio
async def test_ready_only_decision_cancels_when_no_ready_documents_remain(transfer_sessions):
    """A stale decision with zero READY documents must not leave an uncloseable dialog."""
    from sag_api.db.models import Document, Source
    from sag_api.enums import DocumentStatus, OctxExportAction, OctxTransferStatus
    from sag_api.services.octx_transfer_service import create_export_transfer, submit_export_decision

    class Queue:
        async def enqueue(self, _job_id: str) -> None:
            return None

    queue = Queue()
    async with transfer_sessions() as session:
        source = Source(name="Changing source", sag_source_config_id="changing-source")
        session.add(source)
        await session.flush()
        ready = Document(
            source_id=source.id,
            filename="ready.md",
            storage_path="/tmp/ready.md",
            status=DocumentStatus.READY,
            sag_source_id="article-ready-changing",
            is_active=True,
        )
        failed = Document(
            source_id=source.id,
            filename="failed.md",
            storage_path="/tmp/failed.md",
            status=DocumentStatus.FAILED,
            is_active=True,
        )
        session.add_all([ready, failed])
        await session.commit()

        transfer = await create_export_transfer(session, source.id, version=None, job_queue=queue)
        token = transfer.checkpoint["decision_token"]
        ready.status = DocumentStatus.FAILED
        ready.sag_source_id = None
        await session.commit()

        result = await submit_export_decision(
            session,
            transfer.id,
            action=OctxExportAction.EXPORT_READY_ONLY,
            decision_token=token,
            job_queue=queue,
        )

        assert result.status is OctxTransferStatus.CANCELLED
        assert result.checkpoint["allowed_actions"] == []
        assert "decision_token" not in result.checkpoint


@pytest.mark.asyncio
async def test_repeated_export_request_reuses_active_transfer(
    transfer_sessions,
):
    """Repeated requests from another tab must point at the same server task."""
    from sqlalchemy import func, select

    from sag_api.db.models import Document, Job, OctxTransfer, Source
    from sag_api.enums import (
        DocumentStatus,
        OctxTransferDirection,
        OctxTransferStatus,
    )
    from sag_api.services.octx_transfer_service import create_export_transfer

    class Queue:
        ids: list[str] = []

        async def enqueue(self, job_id: str) -> None:
            self.ids.append(job_id)

    queue = Queue()
    async with transfer_sessions() as session:
        source = Source(name="One export", sag_source_config_id="one-export")
        session.add(source)
        await session.flush()
        session.add(
            Document(
                source_id=source.id,
                filename="ready.md",
                storage_path="/tmp/ready.md",
                status=DocumentStatus.READY,
                sag_source_id="article-ready",
                is_active=True,
            )
        )
        await session.commit()

        first = await create_export_transfer(session, source.id, version=None, job_queue=queue)
        repeated = await create_export_transfer(session, source.id, version=None, job_queue=queue)

        assert repeated.id == first.id
        assert repeated.status is OctxTransferStatus.QUEUED
        assert repeated.direction is OctxTransferDirection.EXPORT
        assert len(queue.ids) == 1
        assert await session.scalar(select(func.count(OctxTransfer.id))) == 1
        assert await session.scalar(select(func.count(Job.id))) == 1


@pytest.mark.asyncio
async def test_stale_ready_only_decision_refreshes_selection_without_queueing(
    transfer_sessions,
):
    """Using an old READY selection after status changes would export the wrong documents."""
    from sqlalchemy import func, select

    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, OctxExportAction, OctxTransferStatus
    from sag_api.services.octx_transfer_service import (
        create_export_transfer,
        submit_export_decision,
    )

    class Queue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, job_id: str) -> None:
            self.ids.append(job_id)

    queue = Queue()
    async with transfer_sessions() as session:
        source = Source(name="Changing", sag_source_config_id="changing-source")
        session.add(source)
        await session.flush()
        first = Document(
            source_id=source.id,
            filename="first.md",
            storage_path="/tmp/first.md",
            status=DocumentStatus.READY,
            sag_source_id="article-first",
            is_active=True,
        )
        second = Document(
            source_id=source.id,
            filename="second.md",
            storage_path="/tmp/second.md",
            status=DocumentStatus.EXTRACTING,
            is_active=True,
        )
        session.add_all([first, second])
        await session.commit()
        transfer = await create_export_transfer(session, source.id, version=None, job_queue=queue)
        old_token = transfer.checkpoint["decision_token"]

        second.status = DocumentStatus.READY
        second.sag_source_id = "article-second"
        await session.commit()
        refreshed = await submit_export_decision(
            session,
            transfer.id,
            action=OctxExportAction.EXPORT_READY_ONLY,
            decision_token=old_token,
            job_queue=queue,
        )

        assert refreshed.status is OctxTransferStatus.DECISION_REQUIRED
        assert refreshed.checkpoint["selected_document_ids"] == sorted([first.id, second.id])
        assert refreshed.checkpoint["decision_token"] != old_token
        assert queue.ids == []
        assert await session.scalar(select(func.count(Job.id))) == 0


def test_import_retry_reuses_shadow_identity_from_transfer_checkpoint():
    """Allocating a new shadow partition on retry would orphan the first attempt's data."""
    from sag_api.db.models import OctxTransfer
    from sag_api.enums import OctxTransferDirection, OctxTransferStatus
    from sag_api.services.octx_transfer_service import _ensure_shadow_identity

    transfer = OctxTransfer(
        direction=OctxTransferDirection.IMPORT,
        status=OctxTransferStatus.QUEUED,
        checkpoint={},
    )

    first = _ensure_shadow_identity(transfer)
    second = _ensure_shadow_identity(transfer)

    assert first == second
    assert transfer.checkpoint["id_namespace"] == first[0]
    assert transfer.checkpoint["source_config_id"] == first[1]


def test_knowledge_import_promotes_markdown_out_of_transfer_staging(tmp_path):
    """Transfer GC must not delete Markdown referenced by active Documents."""
    from sag_api.services.octx_transfer_service import _promote_knowledge_documents

    staging = tmp_path / "staging" / "00000000.md"
    staging.parent.mkdir()
    staging.write_text("# Durable\n", encoding="utf-8")
    states = {
        "knowledge/doc.md": {
            "status": "ready",
            "controlled_path": str(staging),
            "logical_path": "knowledge/doc.md",
        }
    }

    _promote_knowledge_documents(states, tmp_path / "uploads")
    promoted = states["knowledge/doc.md"]["controlled_path"]

    assert promoted == str(tmp_path / "uploads" / "00000000.md")
    assert (tmp_path / "uploads" / "00000000.md").read_text(encoding="utf-8") == "# Durable\n"
    _promote_knowledge_documents(states, tmp_path / "uploads")


@pytest.mark.asyncio
async def test_running_transfer_observes_external_cancellation_between_phases(
    transfer_sessions,
):
    """A worker must refresh cancellation state before publishing or switching side effects."""
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import OctxTransfer
    from sag_api.enums import OctxTransferDirection, OctxTransferStatus
    from sag_api.services.octx_transfer_service import _ensure_transfer_active

    async with transfer_sessions() as setup:
        transfer = OctxTransfer(
            direction=OctxTransferDirection.EXPORT,
            status=OctxTransferStatus.EXPORTING,
            cancellation_requested=False,
        )
        setup.add(transfer)
        await setup.commit()
        transfer_id = transfer.id

    async with transfer_sessions() as worker, transfer_sessions() as control:
        running = await worker.get(OctxTransfer, transfer_id)
        cancelled = await control.get(OctxTransfer, transfer_id)
        cancelled.cancellation_requested = True
        cancelled.status = OctxTransferStatus.CANCELLED
        await control.commit()

        with pytest.raises(ConflictError) as caught:
            await _ensure_transfer_active(worker, running, stage="before_publish")

        assert caught.value.code == "octx_transfer_cancelled"
        assert running.status is OctxTransferStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancelled_export_cannot_be_overwritten_by_final_ready_commit(
    transfer_sessions,
):
    """Cancellation that wins the final race must remain terminal."""
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import OctxTransfer
    from sag_api.enums import OctxTransferDirection, OctxTransferStatus
    from sag_api.services.octx_transfer_service import _commit_export_ready

    async with transfer_sessions() as setup:
        transfer = OctxTransfer(
            direction=OctxTransferDirection.EXPORT,
            status=OctxTransferStatus.PACKAGING,
            cancellation_requested=False,
        )
        setup.add(transfer)
        await setup.commit()
        transfer_id = transfer.id

    async with transfer_sessions() as worker, transfer_sessions() as control:
        running = await worker.get(OctxTransfer, transfer_id)
        cancelled = await control.get(OctxTransfer, transfer_id)
        cancelled.cancellation_requested = True
        cancelled.status = OctxTransferStatus.CANCELLED
        await control.commit()

        with pytest.raises(ConflictError) as caught:
            await _commit_export_ready(worker, running)

        assert caught.value.code == "octx_transfer_cancelled"

    async with transfer_sessions() as verify:
        persisted = await verify.get(OctxTransfer, transfer_id)
        assert persisted.status is OctxTransferStatus.CANCELLED
        assert persisted.cancellation_requested is True
