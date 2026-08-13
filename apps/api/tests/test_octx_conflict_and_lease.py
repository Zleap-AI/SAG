from __future__ import annotations

import importlib.util
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def octx_sessions(tmp_path):
    from sag_api.db import models  # noqa: F401
    from sag_api.db.base import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'octx.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def _foreign_keys(connection, _record):  # noqa: ANN001
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def test_work_package_c_modules_exist():
    """Keeping conflict logic in handlers would produce inconsistent decisions."""
    for module in (
        "sag_api.octx.decision_token",
        "sag_api.services.octx_conflict_service",
        "sag_api.services.source_operation_service",
    ):
        try:
            spec = importlib.util.find_spec(module)
        except ModuleNotFoundError:
            spec = None
        assert spec is not None, f"missing {module}"


def test_decision_token_rejects_tampering_and_exposes_bound_revisions():
    """Trusting client-supplied source IDs or revisions would allow stale overwrites."""
    from sag_api.octx.decision_token import (
        DecisionTokenClaims,
        DecisionTokenError,
        issue_decision_token,
        verify_decision_token,
    )

    expires = datetime.now(UTC) + timedelta(minutes=5)
    secret = "test-secret-that-is-at-least-32-bytes-long"
    token = issue_decision_token(
        DecisionTokenClaims(
            transfer_id="transfer-1",
            asset_id="0191f6a0-0000-7000-8000-000000000001",
            source_revisions={"source-a": 7, "source-b": 11},
            highest_version="1.2.0",
            expires_at=expires,
        ),
        secret=secret,
    )

    verified = verify_decision_token(token, secret=secret)
    assert verified.transfer_id == "transfer-1"
    assert verified.source_revisions == {"source-a": 7, "source-b": 11}
    assert verified.highest_version == "1.2.0"

    replacement = "a" if token[-1] != "a" else "b"
    with pytest.raises(DecisionTokenError):
        verify_decision_token(token[:-1] + replacement, secret=secret)


@pytest.mark.asyncio
async def test_database_lease_is_exclusive_sorted_and_released(octx_sessions):
    """A process-local lock would allow two API workers to mutate one source concurrently."""
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import OctxOperationLease
    from sag_api.services.source_operation_service import acquire_operation_lease

    async with acquire_operation_lease(
        octx_sessions,
        ["source:source-a", "asset:asset-a"],
        owner="owner-a",
        ttl_seconds=60,
        heartbeat_seconds=10,
    ):
        async with octx_sessions() as session:
            rows = (
                (await session.execute(select(OctxOperationLease).order_by(OctxOperationLease.resource_key)))
                .scalars()
                .all()
            )
            assert [(row.resource_key, row.owner_token) for row in rows] == [
                ("asset:asset-a", "owner-a"),
                ("source:source-a", "owner-a"),
            ]

        with pytest.raises(ConflictError):
            async with acquire_operation_lease(
                octx_sessions,
                ["source:source-a"],
                owner="owner-b",
                ttl_seconds=60,
                heartbeat_seconds=10,
            ):
                pass

    async with acquire_operation_lease(
        octx_sessions,
        ["source:source-a"],
        owner="owner-b",
        ttl_seconds=60,
        heartbeat_seconds=10,
    ):
        pass


@pytest.mark.asyncio
async def test_ordinary_source_mutation_cannot_enter_during_octx_export(octx_sessions):
    """Uploads/deletes must share the exact source resource used by export jobs."""
    from sag_api.core.errors import ConflictError
    from sag_api.services.source_operation_service import (
        acquire_operation_lease,
        source_content_mutation,
    )

    async with acquire_operation_lease(
        octx_sessions,
        ["source:source-a"],
        owner="octx-export",
    ):
        with pytest.raises(ConflictError):
            async with source_content_mutation(octx_sessions, "source-a", "document-delete"):
                pass


@pytest.mark.asyncio
async def test_upload_waits_for_document_delete_lease(octx_sessions):
    """A quick re-upload should continue after the preceding async delete drains."""
    import asyncio

    from sag_api.services.source_operation_service import (
        acquire_operation_lease,
        source_upload_mutation,
    )

    delete_started = asyncio.Event()

    async def delete_document() -> None:
        async with acquire_operation_lease(
            octx_sessions,
            ["source:source-a"],
            owner="document-delete:job-a",
        ):
            delete_started.set()
            await asyncio.sleep(0.08)

    deletion = asyncio.create_task(delete_document())
    await delete_started.wait()
    entered = False
    async with source_upload_mutation(
        octx_sessions,
        "source-a",
        admission_timeout_seconds=0.01,
        delete_wait_timeout_seconds=0.5,
    ):
        entered = True
    await deletion
    assert entered is True


@pytest.mark.asyncio
async def test_upload_does_not_wait_for_octx_export_lease(octx_sessions):
    """The delete exception must not weaken OCTX import/export isolation."""
    from sag_api.core.errors import ConflictError
    from sag_api.services.source_operation_service import (
        acquire_operation_lease,
        source_upload_mutation,
    )

    async with acquire_operation_lease(
        octx_sessions,
        ["source:source-a"],
        owner="octx-export:job-a",
    ):
        with pytest.raises(ConflictError, match="OCTX operation resource is busy"):
            async with source_upload_mutation(
                octx_sessions,
                "source-a",
                admission_timeout_seconds=0.01,
                delete_wait_timeout_seconds=0.5,
            ):
                pass


@pytest.mark.asyncio
async def test_upload_delete_wait_timeout_is_user_facing(octx_sessions):
    from sag_api.core.errors import ConflictError
    from sag_api.services.source_operation_service import (
        acquire_operation_lease,
        source_upload_mutation,
    )

    async with acquire_operation_lease(
        octx_sessions,
        ["source:source-a"],
        owner="document-delete:job-a",
    ):
        with pytest.raises(ConflictError, match="上一份文档仍在清理，请稍后重试"):
            async with source_upload_mutation(
                octx_sessions,
                "source-a",
                admission_timeout_seconds=0.01,
                delete_wait_timeout_seconds=0.03,
            ):
                pass


@pytest.mark.asyncio
async def test_source_exclusive_lease_drains_processors_without_serializing_them(
    octx_sessions,
):
    """Long document work may overlap, while export closes admission and drains it."""
    import asyncio

    from sag_api.core.errors import ConflictError
    from sag_api.services.source_operation_service import (
        acquire_source_exclusive_lease,
        acquire_source_processing_lease,
    )

    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_processors = asyncio.Event()
    exclusive_entered = asyncio.Event()

    async def processor(job_id: str, entered: asyncio.Event) -> None:
        async with acquire_source_processing_lease(octx_sessions, "source-a", job_id):
            entered.set()
            await release_processors.wait()

    first = asyncio.create_task(processor("job-a", first_entered))
    second = asyncio.create_task(processor("job-b", second_entered))
    await asyncio.gather(first_entered.wait(), second_entered.wait())

    async def exclusive() -> None:
        async with acquire_source_exclusive_lease(
            octx_sessions,
            "source-a",
            "octx-export",
            drain_timeout_seconds=2,
        ):
            exclusive_entered.set()

    export = asyncio.create_task(exclusive())
    await asyncio.sleep(0.05)
    assert exclusive_entered.is_set() is False

    with pytest.raises(ConflictError):
        async with acquire_source_processing_lease(
            octx_sessions,
            "source-a",
            "late-job",
            admission_timeout_seconds=0.1,
        ):
            pass

    release_processors.set()
    await asyncio.gather(first, second, export)
    assert exclusive_entered.is_set() is True


@pytest.mark.asyncio
async def test_conflict_matrix_distinguishes_new_idempotent_upgrade_and_digest_conflict(
    octx_sessions,
):
    """Conflating release identity cases would duplicate indexes or overwrite immutable releases."""
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import (
        OctxAsset,
        OctxRelease,
        OctxSourceBinding,
        Source,
    )
    from sag_api.enums import OctxAssetOwnership, OctxReleaseOrigin
    from sag_api.services.octx_conflict_service import resolve_import_conflict

    asset_id = "0191f6a0-0000-7000-8000-000000000001"

    def package(version: str, digest_char: str = "a"):
        return SimpleNamespace(
            manifest={
                "asset": {"id": asset_id, "name": "Imported source"},
                "release": {
                    "version": version,
                    "package_digest": "sha256:" + digest_char * 64,
                },
            }
        )

    async with octx_sessions() as session:
        new_result = await resolve_import_conflict(session, package("1.0.0"))
        assert new_result.kind == "new"
        assert new_result.allowed_actions == ("new", "cancel")

        source = Source(
            id="source-a",
            name="Existing",
            sag_source_config_id="src_existing",
        )
        asset = OctxAsset(
            id=asset_id,
            name="Imported source",
            ownership=OctxAssetOwnership.IMPORTED,
        )
        release = OctxRelease(
            id="release-a",
            asset_id=asset_id,
            version="1.0.0",
            package_digest="sha256:" + "a" * 64,
            manifest=package("1.0.0").manifest,
            artifact_key="releases/a.octx",
            created_by=OctxReleaseOrigin.IMPORT,
        )
        session.add_all([source, asset, release])
        await session.flush()
        session.add(
            OctxSourceBinding(
                source_id=source.id,
                asset_id=asset_id,
                active_release_id=release.id,
                content_revision=4,
                released_revision=4,
            )
        )
        await session.commit()

        same = await resolve_import_conflict(session, package("1.0.0"))
        assert same.kind == "idempotent"
        assert same.source_id == "source-a"

        upgrade = await resolve_import_conflict(session, package("1.1.0", "b"))
        assert upgrade.kind == "decision_required"
        assert upgrade.allowed_actions == ("update", "new", "cancel")
        assert upgrade.conflicts[0].source_id == "source-a"
        assert upgrade.conflicts[0].content_revision == 4

        with pytest.raises(ConflictError) as caught:
            await resolve_import_conflict(session, package("1.0.0", "c"))
        assert caught.value.code == "octx_release_digest_conflict"


@pytest.mark.asyncio
async def test_confirmed_update_is_bound_to_transfer_source_and_revision(octx_sessions):
    """Accepting an unbound or stale decision could overwrite a source changed after preflight."""
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import (
        OctxAsset,
        OctxRelease,
        OctxSourceBinding,
        OctxTransfer,
        Source,
    )
    from sag_api.enums import (
        OctxAssetOwnership,
        OctxReleaseOrigin,
        OctxTransferDirection,
        OctxTransferStatus,
    )
    from sag_api.octx.decision_token import DecisionTokenClaims, issue_decision_token
    from sag_api.services.octx_conflict_service import (
        ImportDecision,
        confirm_import_decision,
    )

    secret = "test-secret-that-is-at-least-32-bytes-long"
    asset_id = "0191f6a0-0000-7000-8000-000000000002"
    async with octx_sessions() as session:
        source = Source(id="source-b", name="Existing", sag_source_config_id="src_b")
        asset = OctxAsset(id=asset_id, name="Asset", ownership=OctxAssetOwnership.IMPORTED)
        release = OctxRelease(
            id="release-b",
            asset_id=asset_id,
            version="1.0.0",
            package_digest="sha256:" + "b" * 64,
            manifest={},
            artifact_key="releases/b.octx",
            created_by=OctxReleaseOrigin.IMPORT,
        )
        session.add_all([source, asset, release])
        await session.flush()
        binding = OctxSourceBinding(
            source_id=source.id,
            asset_id=asset_id,
            active_release_id=release.id,
            content_revision=8,
            released_revision=8,
        )
        transfer = OctxTransfer(
            id="transfer-b",
            direction=OctxTransferDirection.IMPORT,
            status=OctxTransferStatus.DECISION_REQUIRED,
            asset_id=asset_id,
            checkpoint={"allowed_actions": ["update", "new", "cancel"]},
        )
        session.add_all([binding, transfer])
        await session.commit()

        token = issue_decision_token(
            DecisionTokenClaims(
                transfer_id=transfer.id,
                asset_id=asset_id,
                source_revisions={source.id: 8},
                highest_version="1.0.0",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            ),
            secret=secret,
        )
        confirmed = await confirm_import_decision(
            session,
            transfer.id,
            ImportDecision(
                action="update",
                target_source_id=source.id,
                discard_local_changes=False,
                decision_token=token,
            ),
            secret=secret,
        )
        assert confirmed.status is OctxTransferStatus.QUEUED
        assert confirmed.target_source_id == source.id
        assert confirmed.selected_action.value == "update"
        assert confirmed.checkpoint["expected_source_revision"] == 8

        confirmed.status = OctxTransferStatus.DECISION_REQUIRED
        binding.content_revision = 9
        await session.flush()
        with pytest.raises(ConflictError) as caught:
            await confirm_import_decision(
                session,
                transfer.id,
                ImportDecision(
                    action="update",
                    target_source_id=source.id,
                    discard_local_changes=False,
                    decision_token=token,
                ),
                secret=secret,
            )
        assert caught.value.code == "octx_decision_stale"
        assert confirmed.status is OctxTransferStatus.VALIDATING

        confirmed.status = OctxTransferStatus.DECISION_REQUIRED
        confirmed.checkpoint = {"allowed_actions": ["new", "cancel"]}
        fresh_token = issue_decision_token(
            DecisionTokenClaims(
                transfer_id=transfer.id,
                asset_id=asset_id,
                source_revisions={source.id: 9},
                highest_version="1.0.0",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            ),
            secret=secret,
        )
        with pytest.raises(ConflictError, match="not allowed"):
            await confirm_import_decision(
                session,
                transfer.id,
                ImportDecision(
                    action="update",
                    target_source_id=source.id,
                    discard_local_changes=True,
                    decision_token=fresh_token,
                ),
                secret=secret,
            )


@pytest.mark.asyncio
async def test_document_content_mutations_advance_octx_binding_revision(octx_sessions, tmp_path):
    """Missing a revision hook would let a preflight decision overwrite later local edits."""
    from sag_api.db.models import (
        OctxAsset,
        OctxRelease,
        OctxSourceBinding,
        Source,
    )
    from sag_api.enums import (
        DocumentStatus,
        JobStatus,
        OctxAssetOwnership,
        OctxReleaseOrigin,
    )
    from sag_api.services.document_service import (
        create_document_from_upload,
        delete_document,
        reprocess_document,
    )

    class Queue:
        async def enqueue(self, _job_id: str) -> None:
            return None

        def begin_source_maintenance(self, _source_id: str, _job_id: str) -> None:
            return None

    asset_id = "0191f6a0-0000-7000-8000-000000000003"
    async with octx_sessions() as session:
        source = Source(id="source-c", name="Bound", sag_source_config_id="src_c")
        asset = OctxAsset(id=asset_id, name="Asset", ownership=OctxAssetOwnership.LOCAL)
        release = OctxRelease(
            id="release-c",
            asset_id=asset_id,
            version="1.0.0",
            package_digest="sha256:" + "d" * 64,
            manifest={},
            artifact_key="releases/c.octx",
            created_by=OctxReleaseOrigin.EXPORT,
        )
        session.add_all([source, asset, release])
        await session.flush()
        binding = OctxSourceBinding(
            source_id=source.id,
            asset_id=asset_id,
            active_release_id=release.id,
            content_revision=2,
            released_revision=2,
        )
        session.add(binding)
        await session.commit()

        document, first_job = await create_document_from_upload(
            session,
            source,
            filename="new.md",
            content_type="text/markdown",
            data=b"# New",
            upload_dir=str(tmp_path / "uploads"),
            job_queue=Queue(),
        )
        await session.refresh(binding)
        assert binding.content_revision == 3

        document.status = DocumentStatus.READY
        document.sag_source_id = "article-c"
        first_job.status = JobStatus.SUCCEEDED
        await session.commit()
        await reprocess_document(
            session,
            source,
            document.id,
            job_queue=Queue(),
        )
        await session.refresh(binding)
        assert binding.content_revision == 4

        await delete_document(
            session,
            source,
            document.id,
            job_queue=Queue(),
        )
        await session.refresh(binding)
        assert binding.content_revision == 5


@pytest.mark.asyncio
async def test_successful_document_processing_advances_binding_revision(octx_sessions, monkeypatch):
    """Finishing extraction changes searchable content and must invalidate prior decisions."""
    from sag_api.db.models import (
        Document,
        Job,
        OctxAsset,
        OctxRelease,
        OctxSourceBinding,
        Source,
    )
    from sag_api.enums import (
        DocumentStatus,
        JobStatus,
        JobType,
        OctxAssetOwnership,
        OctxReleaseOrigin,
    )
    from sag_api.jobs import tasks
    from sag_api.sag.dto import ProcessOutcome

    lease_held = False

    @asynccontextmanager
    async def lease(_sessions, source_id, job_id, **_kwargs):
        nonlocal lease_held
        assert source_id == "source-d"
        assert job_id == "job-d"
        lease_held = True
        try:
            yield
        finally:
            lease_held = False

    class Engine:
        async def process_document(self, *_args, **_kwargs):
            assert lease_held is True
            return ProcessOutcome(source_id="article-d", chunk_count=2, event_count=3, token_usage=5)

    asset_id = "0191f6a0-0000-7000-8000-000000000004"
    async with octx_sessions() as session:
        source = Source(id="source-d", name="Bound", sag_source_config_id="src_d")
        asset = OctxAsset(id=asset_id, name="Asset", ownership=OctxAssetOwnership.LOCAL)
        release = OctxRelease(
            id="release-d",
            asset_id=asset_id,
            version="1.0.0",
            package_digest="sha256:" + "e" * 64,
            manifest={},
            artifact_key="releases/d.octx",
            created_by=OctxReleaseOrigin.EXPORT,
        )
        session.add_all([source, asset, release])
        await session.flush()
        binding = OctxSourceBinding(
            source_id=source.id,
            asset_id=asset_id,
            active_release_id=release.id,
            content_revision=1,
            released_revision=1,
        )
        document = Document(
            id="document-d",
            source_id=source.id,
            filename="d.md",
            storage_path="unused.md",
            status=DocumentStatus.PENDING,
        )
        job = Job(
            id="job-d",
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.RUNNING,
            source_id=source.id,
            document_id=document.id,
            payload={
                "process_checkpoint": {
                    "source_id": "article-d",
                    "chunk_ids": ["chunk-d"],
                    "processed_chunk_ids": [],
                    "event_count": 0,
                    "event_ids": [],
                    "eventless_chunk_ids": [],
                    "token_usage": 0,
                }
            },
        )
        session.add_all([binding, document, job])
        await session.commit()

        monkeypatch.setattr(tasks, "acquire_source_processing_lease", lease)
        await tasks.process_document(session, job, engine_manager=Engine())

        await session.refresh(binding)
        assert binding.content_revision == 2


@pytest.mark.asyncio
async def test_retained_installation_documents_are_hidden_from_active_queries(octx_sessions):
    from sag_api.core.errors import NotFoundError
    from sag_api.db.models import Document, Source
    from sag_api.enums import DocumentStatus
    from sag_api.services.document_service import get_document, list_documents

    async with octx_sessions() as session:
        source = Source(id="source-active-docs", name="Source", sag_source_config_id="src")
        session.add(source)
        await session.flush()
        active = Document(
            id="active-doc",
            source_id=source.id,
            filename="active.md",
            storage_path="active.md",
            status=DocumentStatus.READY,
            is_active=True,
        )
        retained = Document(
            id="retained-doc",
            source_id=source.id,
            filename="retained.md",
            storage_path="retained.md",
            status=DocumentStatus.READY,
            is_active=False,
        )
        session.add_all([active, retained])
        await session.commit()

        assert [document.id for document in await list_documents(session, source.id)] == [active.id]
        with pytest.raises(NotFoundError):
            await get_document(session, source, retained.id)
