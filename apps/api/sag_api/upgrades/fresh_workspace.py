from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from zleap.sag import DataEngine

from sag_api.sag.config_builder import build_engine_config
from sag_api.upgrades.active_engine import ActiveEngineStore
from sag_api.upgrades.backup import create_backup, create_sqlite_backup
from sag_api.upgrades.contracts import FreshWorkspacePhase, StorageUpgradeContext, UpgradeReport
from sag_api.upgrades.detector import detect_storage
from sag_api.upgrades.journal import UpgradeLock
from sag_api.upgrades.types import StorageLayout, StorageUpgradeError, StorageVersion

FRESH_WORKSPACE_ID = "fresh-knowledge-workspace"
TARGET_VERSION = "0.8.2"
KNOWLEDGE_TABLES = (
    "messages",
    "threads",
    "agent_bindings",
    "exploration_steps",
    "exploration_sessions",
    "universe_partitions",
    "universe_dirty_sources",
    "universe_overviews",
    "octx_operation_leases",
    "octx_document_bindings",
    "octx_source_bindings",
    "octx_transfers",
    "jobs",
    "documents",
    "octx_installations",
    "octx_releases",
    "octx_assets",
    "agents",
    "sources",
)
FRESH_WORKSPACE_PHASES = tuple(FreshWorkspacePhase)
PRESERVED_METADATA_DIR = "preserved-metadata"


def _metadata_snapshot_is_valid(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
            return database.execute("PRAGMA quick_check").fetchone() == ("ok",)
    except sqlite3.Error:
        return False


def _preserve_metadata_in_place(
    layout: StorageLayout,
    journal: FreshWorkspaceJournal,
) -> Path:
    if layout.sag_db is None or not layout.sag_db.is_file():
        raise StorageUpgradeError(
            "Windows fresh workspace requires the local metadata database",
            stage="fresh_backup",
            recoverable=True,
            diagnostic_path=journal.path,
        )
    backup_root = journal.path.parent / PRESERVED_METADATA_DIR
    backup_root.mkdir(parents=True, exist_ok=True)
    snapshot = backup_root / "sag.db"
    if not _metadata_snapshot_is_valid(snapshot):
        snapshot.unlink(missing_ok=True)
        create_sqlite_backup(layout.sag_db, snapshot)
    if not _metadata_snapshot_is_valid(snapshot):
        raise StorageUpgradeError(
            "Windows metadata snapshot failed validation",
            stage="fresh_backup",
            recoverable=True,
            diagnostic_path=journal.path,
        )
    return backup_root


@dataclass
class FreshWorkspaceJournal:
    path: Path
    journal_id: str
    phase: FreshWorkspacePhase
    target_engine: Path
    reports: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, path: Path, *, root: Path) -> FreshWorkspaceJournal:
        journal_id = uuid4().hex
        journal = cls(
            path=path,
            journal_id=journal_id,
            phase=FreshWorkspacePhase.TARGET_CREATED,
            target_engine=root / f"engine-{TARGET_VERSION}-fresh-{journal_id}",
        )
        journal._write()
        return journal

    @classmethod
    def load(cls, path: Path) -> FreshWorkspaceJournal:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            target_engine = Path(str(payload["target_engine"])).expanduser().resolve()
            journal = cls(
                path=path,
                journal_id=str(payload["journal_id"]),
                phase=FreshWorkspacePhase(payload["phase"]),
                target_engine=target_engine,
                reports=dict(payload.get("reports", {})),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise StorageUpgradeError(
                "fresh workspace journal is invalid",
                stage="fresh_journal",
                recoverable=True,
                diagnostic_path=path,
            ) from error
        expected = path.parents[2] / f"engine-{TARGET_VERSION}-fresh-{journal.journal_id}"
        if journal.target_engine != expected:
            raise StorageUpgradeError(
                "fresh workspace journal target is invalid",
                stage="fresh_journal",
                recoverable=False,
                diagnostic_path=path,
            )
        return journal

    def advance(self, phase: FreshWorkspacePhase, *, report: Any | None = None) -> None:
        current_index = FRESH_WORKSPACE_PHASES.index(self.phase)
        target_index = FRESH_WORKSPACE_PHASES.index(phase)
        if target_index != current_index + 1:
            raise StorageUpgradeError(
                f"fresh workspace phase must advance exactly once: {self.phase} -> {phase}",
                stage="fresh_journal",
                recoverable=True,
                diagnostic_path=self.path,
            )
        self.phase = phase
        if report is not None:
            self.reports[phase.value] = report
        self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "journal_id": self.journal_id,
            "phase": self.phase.value,
            "reports": self.reports,
            "target_engine": str(self.target_engine),
        }
        descriptor, temporary = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


async def clear_knowledge_domain(session_factory: Any) -> dict[str, int]:
    """Delete all knowledge-owned metadata in one FK-safe transaction."""
    deleted: dict[str, int] = {}
    async with session_factory.begin() as session:
        for table in KNOWLEDGE_TABLES:
            result = await session.execute(text(f'DELETE FROM "{table}"'))
            deleted[table] = int(result.rowcount or 0)
    return deleted


class FreshKnowledgeWorkspaceAdapter:
    """Create a clean current engine while preserving account and application settings."""

    async def create(
        self,
        context: StorageUpgradeContext,
        *,
        preserve_legacy_in_place: bool = False,
    ) -> UpgradeReport:
        settings = context.settings
        layout = StorageLayout.from_settings(settings)
        state_root = layout.upgrades / FRESH_WORKSPACE_ID
        journal_path = state_root / "journal.json"
        pointer = ActiveEngineStore(layout.upgrades / "active-engine.json")

        with UpgradeLock(layout.upgrades / "upgrade.lock", timeout=0):
            journal = (
                FreshWorkspaceJournal.load(journal_path)
                if journal_path.is_file()
                else FreshWorkspaceJournal.create(journal_path, root=layout.root)
            )
            self._validate_target(layout, journal)

            if journal.phase is FreshWorkspacePhase.TARGET_CREATED:
                await self._create_target(settings, journal)
            else:
                self._require_current_target(settings, journal)

            if journal.phase is FreshWorkspacePhase.TARGET_CREATED:
                if preserve_legacy_in_place:
                    backup_path = _preserve_metadata_in_place(layout, journal)
                    journal.advance(
                        FreshWorkspacePhase.BUSINESS_BACKED_UP,
                        report={
                            "mode": "preserve_in_place",
                            "metadata_backup": str(backup_path / "sag.db"),
                            "preserved_engine": str(layout.engine),
                        },
                    )
                else:
                    backup = create_backup(
                        layout,
                        f"fresh-{journal.journal_id}",
                        source_version="fresh-reset",
                    )
                    journal.advance(
                        FreshWorkspacePhase.BUSINESS_BACKED_UP,
                        report={"manifest": str(backup.manifest_path)},
                    )
                    backup_path = backup.backup_root
            else:
                backup_path = self._backup_from_journal(journal)

            if journal.phase is FreshWorkspacePhase.BUSINESS_BACKED_UP:
                deleted = await clear_knowledge_domain(context.session_factory)
                journal.advance(FreshWorkspacePhase.BUSINESS_CLEARED, report=deleted)

            if journal.phase is FreshWorkspacePhase.BUSINESS_CLEARED:
                pointer.activate(layout.engine, journal.target_engine)
                journal.advance(
                    FreshWorkspacePhase.POINTER_ACTIVATED,
                    report={"target_engine": str(journal.target_engine)},
                )

            if journal.phase is FreshWorkspacePhase.POINTER_ACTIVATED:
                pointer.activate(layout.engine, journal.target_engine)
                journal.advance(FreshWorkspacePhase.COMPLETED)

            return UpgradeReport("fresh", journal.path, backup_path)

    @staticmethod
    def _validate_target(layout: StorageLayout, journal: FreshWorkspaceJournal) -> None:
        if journal.target_engine.parent != layout.root:
            raise StorageUpgradeError(
                "fresh workspace target must remain below the storage root",
                stage="fresh_target",
                recoverable=False,
                diagnostic_path=journal.path,
            )

    @staticmethod
    def _backup_from_journal(journal: FreshWorkspaceJournal) -> Path:
        report = journal.reports.get(FreshWorkspacePhase.BUSINESS_BACKED_UP.value)
        if isinstance(report, dict) and report.get("mode") == "preserve_in_place":
            snapshot_value = report.get("metadata_backup")
            snapshot = Path(str(snapshot_value)) if snapshot_value else None
            if snapshot is None or not _metadata_snapshot_is_valid(snapshot):
                raise StorageUpgradeError(
                    "Windows metadata snapshot is missing",
                    stage="fresh_backup",
                    recoverable=False,
                    diagnostic_path=journal.path,
                )
            return snapshot.parent
        manifest = report.get("manifest") if isinstance(report, dict) else None
        path = Path(str(manifest)) if manifest else None
        if path is None or not path.is_file():
            raise StorageUpgradeError(
                "fresh workspace backup is missing",
                stage="fresh_backup",
                recoverable=False,
                diagnostic_path=journal.path,
            )
        return path.parent

    @staticmethod
    def _require_current_target(settings, journal: FreshWorkspaceJournal) -> None:
        target_settings = settings.model_copy(update={"data_dir": str(journal.target_engine)})
        target_layout = StorageLayout.from_settings(target_settings)
        if detect_storage(target_layout, target_settings).version is not StorageVersion.CURRENT:
            raise StorageUpgradeError(
                "fresh workspace target is incomplete",
                stage="fresh_target",
                recoverable=True,
                diagnostic_path=journal.path,
            )

    @staticmethod
    async def _create_target(settings, journal: FreshWorkspaceJournal) -> None:
        if journal.target_engine.exists():
            try:
                FreshKnowledgeWorkspaceAdapter._require_current_target(settings, journal)
                return
            except StorageUpgradeError:
                shutil.rmtree(journal.target_engine)

        engine = DataEngine(
            build_engine_config(settings, overrides={"data_dir": str(journal.target_engine)}),
            health_check=False,
        )
        try:
            await engine.start()
        finally:
            await engine.aclose()

        try:
            FreshKnowledgeWorkspaceAdapter._require_current_target(settings, journal)
        except StorageUpgradeError as error:
            raise StorageUpgradeError(
                "fresh workspace target did not initialize a current storage schema",
                stage="fresh_target",
                recoverable=True,
                diagnostic_path=journal.path,
            ) from error
