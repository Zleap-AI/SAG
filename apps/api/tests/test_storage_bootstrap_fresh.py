from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from zleap.sag import DataEngine

import sag_api.db.models  # noqa: F401
from sag_api.core.config import Settings
from sag_api.db.base import Base
from sag_api.db.models import (
    Agent,
    AgentBinding,
    Document,
    ExplorationSession,
    ExplorationStep,
    Job,
    Message,
    OctxAsset,
    OctxDocumentBinding,
    OctxInstallation,
    OctxOperationLease,
    OctxRelease,
    OctxSourceBinding,
    OctxTransfer,
    Setting,
    Source,
    Thread,
    UniverseDirtySource,
    UniverseOverview,
    UniversePartition,
    User,
)
from sag_api.enums import (
    BindingTargetType,
    ConnectorKind,
    DocumentStatus,
    JobStatus,
    JobType,
    MessageRole,
    OctxAssetOwnership,
    OctxInstallationStatus,
    OctxReleaseOrigin,
    OctxTransferDirection,
    OctxTransferStatus,
    SourceStatus,
    SourceType,
)
from sag_api.upgrades.active_engine import ActiveEngineStore
from sag_api.upgrades.backup import _tree_stats
from sag_api.upgrades.contracts import StorageUpgradeContext
from sag_api.upgrades.fresh_workspace import (
    FRESH_WORKSPACE_ID,
    KNOWLEDGE_TABLES,
    FreshKnowledgeWorkspaceAdapter,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "zleap_sag_071"


async def _count(session_factory, table: str) -> int:
    async with session_factory() as session:
        return int(await session.scalar(text(f'SELECT count(*) FROM "{table}"')) or 0)


async def _seed_all_domains(session_factory) -> None:
    """Seed every knowledge-owned table so an FK ordering regression is observable."""
    async with session_factory() as session:
        user = User(email="fresh@example.test", password_hash="hash", name="Fresh")
        setting = Setting(scope="global", key="theme", value={"name": "dark"})
        source = Source(
            name="Legacy source",
            source_type=SourceType.DOCUMENT,
            connector_kind=ConnectorKind.FILE_UPLOAD,
            sag_source_config_id="fresh-source",
            status=SourceStatus.ACTIVE,
        )
        agent = Agent(name="Knowledge agent")
        asset = OctxAsset(
            id="0191f6a0-0000-7000-8000-000000000101",
            name="Legacy package",
            ownership=OctxAssetOwnership.LOCAL,
        )
        session.add_all([user, setting, source, agent, asset])
        await session.flush()

        thread = Thread(agent_id=agent.id, title="Legacy chat")
        exploration = ExplorationSession(user_id=user.id, title="Legacy exploration")
        overview = UniverseOverview(user_id=user.id, status="ready", is_active=True)
        release = OctxRelease(
            asset_id=asset.id,
            version="1.0.0",
            package_digest="sha256:" + "a" * 64,
            manifest={},
            artifact_key="releases/legacy.octx",
            created_by=OctxReleaseOrigin.EXPORT,
        )
        session.add_all([thread, exploration, overview, release])
        await session.flush()

        installation = OctxInstallation(
            source_id=source.id,
            release_id=release.id,
            sag_source_config_id="fresh-installation",
            id_namespace="0191f6a0-0000-7000-8000-000000000102",
            status=OctxInstallationStatus.ACTIVE,
        )
        document = Document(
            source_id=source.id,
            filename="legacy.md",
            storage_path="uploads/legacy.md",
            status=DocumentStatus.READY,
            octx_installation_id=installation.id,
        )
        partition = UniversePartition(
            overview_id=overview.id,
            user_id=user.id,
            source_id=source.id,
            kind="source",
            key="legacy",
            label="Legacy",
            x=1,
            y=2,
        )
        session.add_all([installation, document, partition])
        await session.flush()

        session.add_all(
            [
                AgentBinding(
                    agent_id=agent.id,
                    target_type=BindingTargetType.SOURCE,
                    target_id=source.id,
                ),
                Message(thread_id=thread.id, role=MessageRole.USER, content="remember this"),
                ExplorationStep(session_id=exploration.id, query="legacy"),
                UniverseDirtySource(user_id=user.id, source_id=source.id, reason="seed"),
                OctxSourceBinding(
                    source_id=source.id,
                    asset_id=asset.id,
                    active_release_id=release.id,
                ),
                OctxDocumentBinding(
                    document_id=document.id,
                    asset_id=asset.id,
                    active_release_id=release.id,
                ),
                OctxTransfer(
                    direction=OctxTransferDirection.EXPORT,
                    status=OctxTransferStatus.READY,
                    asset_id=asset.id,
                    release_id=release.id,
                    installation_id=installation.id,
                    target_source_id=source.id,
                ),
                OctxOperationLease(
                    resource_key="source:legacy",
                    owner_token="fresh-test",
                    expires_at=datetime(2026, 8, 16, tzinfo=UTC),
                    heartbeat_at=datetime(2026, 8, 16, tzinfo=UTC),
                ),
                Job(
                    type=JobType.PROCESS_DOCUMENT,
                    status=JobStatus.QUEUED,
                    source_id=source.id,
                    document_id=document.id,
                ),
            ]
        )
        await session.commit()


@pytest.mark.asyncio
async def test_fresh_workspace_preserves_user_settings_and_legacy_engine_then_is_idempotent(
    tmp_path: Path,
) -> None:
    """Deleting the metadata before a backup or mutating the legacy engine loses recoverable data."""
    legacy_engine = tmp_path / "engine"
    legacy_engine.mkdir()
    with zipfile.ZipFile(FIXTURE_DIR / "fixture.zip") as archive:
        archive.extractall(legacy_engine)
    (legacy_engine / "octx").mkdir()
    (legacy_engine / "octx" / "retain.txt").write_text("retain", encoding="utf-8")
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "retain.txt").write_text("retain", encoding="utf-8")
    before = _tree_stats(legacy_engine)

    meta_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sag.db'}")

    @event.listens_for(meta_engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with meta_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(meta_engine, expire_on_commit=False)
    settings = Settings(
        data_dir=str(legacy_engine),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'sag.db'}",
        upload_dir=str(uploads),
        llm_api_key="fixture",
        embedding_api_key="fixture",
        embedding_dimensions=12,
        _env_file=None,
    )
    context = StorageUpgradeContext(settings=settings, session_factory=session_factory)
    pointer = tmp_path / ".storage-upgrades" / "active-engine.json"
    try:
        await _seed_all_domains(session_factory)

        first = await FreshKnowledgeWorkspaceAdapter().create(context)

        assert first.status == "fresh"
        assert first.backup_path is not None and first.backup_path.is_dir()
        assert _tree_stats(legacy_engine) == before
        active = ActiveEngineStore(pointer).resolve(legacy_engine)
        assert active != legacy_engine
        assert active.name.startswith("engine-0.8.2-fresh-")
        assert (uploads / "retain.txt").read_text(encoding="utf-8") == "retain"
        assert (legacy_engine / "octx" / "retain.txt").read_text(encoding="utf-8") == "retain"
        assert await _count(session_factory, "users") == 1
        assert await _count(session_factory, "settings") == 1
        for table in KNOWLEDGE_TABLES:
            assert await _count(session_factory, table) == 0

        second = await FreshKnowledgeWorkspaceAdapter().create(context)

        assert second == first
        assert ActiveEngineStore(pointer).resolve(legacy_engine) == active
        with sqlite3.connect(first.backup_path / "sag.db") as backup:
            assert backup.execute("SELECT count(*) FROM users").fetchone() == (1,)
            assert backup.execute("SELECT count(*) FROM documents").fetchone() == (1,)
    finally:
        await meta_engine.dispose()


@pytest.mark.asyncio
async def test_fresh_workspace_repairs_the_same_target_after_interrupted_initial_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted first start must not strand the durable fresh-workspace journal."""
    legacy_engine = tmp_path / "engine"
    legacy_engine.mkdir()
    with zipfile.ZipFile(FIXTURE_DIR / "fixture.zip") as archive:
        archive.extractall(legacy_engine)
    before = _tree_stats(legacy_engine)

    meta_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sag.db'}")

    @event.listens_for(meta_engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with meta_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(meta_engine, expire_on_commit=False)
    settings = Settings(
        data_dir=str(legacy_engine),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'sag.db'}",
        llm_api_key="fixture",
        embedding_api_key="fixture",
        embedding_dimensions=12,
        _env_file=None,
    )
    context = StorageUpgradeContext(settings=settings, session_factory=session_factory)
    original_start = DataEngine.start
    starts = 0

    async def interrupted_start(engine: DataEngine) -> None:
        nonlocal starts
        starts += 1
        await original_start(engine)
        if starts == 1:
            target = next(tmp_path.glob("engine-0.8.2-fresh-*"))
            with sqlite3.connect(target / "sag.db") as database:
                database.execute("DROP TABLE sag_schema_meta")
            raise RuntimeError("interrupted after partial target initialization")

    monkeypatch.setattr(DataEngine, "start", interrupted_start)
    adapter = FreshKnowledgeWorkspaceAdapter()
    journal_path = tmp_path / ".storage-upgrades" / FRESH_WORKSPACE_ID / "journal.json"
    try:
        await _seed_all_domains(session_factory)

        with pytest.raises(RuntimeError, match="interrupted"):
            await adapter.create(context)

        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        target = Path(journal["target_engine"])
        assert journal["phase"] == "target_created"
        assert target.is_dir()
        assert _tree_stats(legacy_engine) == before

        report = await adapter.create(context)

        assert report.status == "fresh"
        assert json.loads(journal_path.read_text(encoding="utf-8"))["target_engine"] == str(target)
        assert _tree_stats(legacy_engine) == before
        assert await _count(session_factory, "users") == 1
        assert await _count(session_factory, "settings") == 1
        for table in KNOWLEDGE_TABLES:
            assert await _count(session_factory, table) == 0
        active = ActiveEngineStore(tmp_path / ".storage-upgrades" / "active-engine.json").resolve(
            legacy_engine
        )
        assert active == target
        assert list(tmp_path.glob("engine-0.8.2-fresh-*")) == [target]
    finally:
        await meta_engine.dispose()
