from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def gc_sessions(tmp_path):
    from sag_api.db import models  # noqa: F401
    from sag_api.db.base import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'meta.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sessions
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_installation_gc_rejects_current_active_partition(gc_sessions):
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import OctxAsset, OctxInstallation, OctxRelease, Source
    from sag_api.enums import (
        ConnectorKind,
        OctxAssetOwnership,
        OctxInstallationStatus,
        OctxReleaseOrigin,
        SourceStatus,
        SourceType,
    )
    from sag_api.services.octx_gc_service import gc_installation

    async with gc_sessions() as session:
        source = Source(
            name="Active",
            source_type=SourceType.DOCUMENT,
            connector_kind=ConnectorKind.FILE_UPLOAD,
            sag_source_config_id="src_active",
            status=SourceStatus.ACTIVE,
        )
        asset = OctxAsset(
            id="0191f6a0-0000-7000-8000-000000000201",
            name="Active",
            ownership=OctxAssetOwnership.IMPORTED,
        )
        session.add_all([source, asset])
        await session.flush()
        release = OctxRelease(
            asset_id=asset.id,
            version="1.0.0",
            package_digest="sha256:" + "a" * 64,
            manifest={},
            artifact_key="releases/a.octx",
            created_by=OctxReleaseOrigin.IMPORT,
        )
        session.add(release)
        await session.flush()
        installation = OctxInstallation(
            source_id=source.id,
            release_id=release.id,
            sag_source_config_id="src_active",
            id_namespace="0191f6a0-0000-7000-8000-000000000202",
            status=OctxInstallationStatus.ACTIVE,
        )
        session.add(installation)
        await session.commit()

        with pytest.raises(ConflictError):
            await gc_installation(
                session,
                installation.id,
                sag_session_factory=None,
                vector_client=object(),
            )


@pytest.mark.asyncio
async def test_installation_gc_deletes_only_expired_retained_partition(
    gc_sessions, tmp_path
):
    from zleap.sag.db.base import Base as SagBase
    from zleap.sag.db.models import SourceConfig

    from sag_api.db.models import (
        Document,
        OctxAsset,
        OctxInstallation,
        OctxRelease,
        Source,
    )
    from sag_api.enums import (
        ConnectorKind,
        DocumentStatus,
        OctxAssetOwnership,
        OctxInstallationStatus,
        OctxReleaseOrigin,
        SourceStatus,
        SourceType,
    )
    from sag_api.services.octx_gc_service import gc_installation

    sag_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sag.db'}")
    sag_sessions = async_sessionmaker(sag_engine, expire_on_commit=False)
    async with sag_engine.begin() as connection:
        await connection.run_sync(SagBase.metadata.create_all)
    async with sag_sessions() as sag_session:
        sag_session.add(SourceConfig(id="src_retained", name="Old", target_config={}))
        await sag_session.commit()

    deleted: list[tuple[str, str]] = []

    class Table:
        async def delete(self, expression: str) -> None:
            deleted.append((self.name, expression))

    class VectorClient:
        async def _open_table(self, index: str):
            table = Table()
            table.name = index
            return table

    try:
        async with gc_sessions() as session:
            source = Source(
                name="Current",
                source_type=SourceType.DOCUMENT,
                connector_kind=ConnectorKind.FILE_UPLOAD,
                sag_source_config_id="src_current",
                status=SourceStatus.ACTIVE,
            )
            asset = OctxAsset(
                id="0191f6a0-0000-7000-8000-000000000211",
                name="Old",
                ownership=OctxAssetOwnership.IMPORTED,
            )
            session.add_all([source, asset])
            await session.flush()
            release = OctxRelease(
                asset_id=asset.id,
                version="1.0.0",
                package_digest="sha256:" + "b" * 64,
                manifest={},
                artifact_key="releases/b.octx",
                created_by=OctxReleaseOrigin.IMPORT,
            )
            session.add(release)
            await session.flush()
            installation = OctxInstallation(
                source_id=source.id,
                release_id=release.id,
                sag_source_config_id="src_retained",
                id_namespace="0191f6a0-0000-7000-8000-000000000212",
                status=OctxInstallationStatus.RETAINED,
                retain_until=datetime.now(UTC) - timedelta(seconds=1),
            )
            session.add(installation)
            await session.flush()
            controlled = tmp_path / "uploads" / source.id / "old.md"
            controlled.parent.mkdir(parents=True)
            controlled.write_text("old", encoding="utf-8")
            document = Document(
                source_id=source.id,
                filename="old.md",
                storage_path=str(controlled),
                status=DocumentStatus.READY,
                octx_installation_id=installation.id,
                is_active=False,
            )
            session.add(document)
            await session.commit()

            result = await gc_installation(
                session,
                installation.id,
                sag_session_factory=sag_sessions,
                vector_client=VectorClient(),
                upload_root=tmp_path / "uploads",
            )
            await session.refresh(installation)
            assert installation.status is OctxInstallationStatus.GC
            assert result["documents"] == 1
            assert await session.get(Document, document.id) is None
            assert not controlled.exists()
        async with sag_sessions() as sag_session:
            assert await sag_session.get(SourceConfig, "src_retained") is None
        assert {index for index, _ in deleted} == {
            "source_chunks",
            "event_vectors",
            "event_entity_vectors",
            "entity_vectors",
        }
        assert all(
            expression == "source_config_id = 'src_retained'"
            for _, expression in deleted
        )
    finally:
        await sag_engine.dispose()


@pytest.mark.asyncio
async def test_transfer_gc_removes_only_expired_terminal_staging(gc_sessions, tmp_path):
    from sag_api.db.models import OctxTransfer
    from sag_api.enums import OctxTransferDirection, OctxTransferStatus
    from sag_api.octx.storage import OctxStorage
    from sag_api.services.octx_gc_service import gc_expired_transfers

    storage = OctxStorage(tmp_path / "octx", max_upload_bytes=1024)
    expired_dir = storage.staging_dir("expired")
    active_dir = storage.staging_dir("active")
    expired_dir.mkdir(parents=True)
    active_dir.mkdir(parents=True)
    (expired_dir / "input.octx").write_bytes(b"old")
    (active_dir / "input.octx").write_bytes(b"active")
    async with gc_sessions() as session:
        session.add_all(
            [
                OctxTransfer(
                    id="expired",
                    direction=OctxTransferDirection.IMPORT,
                    status=OctxTransferStatus.READY,
                    staging_key="staging/expired/input.octx",
                    expires_at=datetime.now(UTC) - timedelta(seconds=1),
                ),
                OctxTransfer(
                    id="active",
                    direction=OctxTransferDirection.IMPORT,
                    status=OctxTransferStatus.QUEUED,
                    staging_key="staging/active/input.octx",
                    expires_at=datetime.now(UTC) - timedelta(seconds=1),
                ),
            ]
        )
        await session.commit()
        result = await gc_expired_transfers(session, storage=storage)
        assert result == {"transfers": 1}
        assert not expired_dir.exists()
        assert active_dir.exists()
