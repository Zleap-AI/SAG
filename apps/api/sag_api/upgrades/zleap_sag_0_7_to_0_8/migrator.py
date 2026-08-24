from __future__ import annotations

import shutil
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from zleap.sag import DataEngine

from sag_api.core.config import Settings
from sag_api.sag.config_builder import build_engine_config
from sag_api.upgrades.backup import create_backup, prepare_staging
from sag_api.upgrades.contracts import StorageUpgradeContext, UpgradeReport
from sag_api.upgrades.detector import detect_storage
from sag_api.upgrades.directory_replace import replace_directory
from sag_api.upgrades.journal import MigrationJournal, UpgradeLock
from sag_api.upgrades.swap import rollback_engine, swap_engine
from sag_api.upgrades.types import (
    MigrationPhase,
    StorageLayout,
    StorageUpgradeError,
    StorageVersion,
)
from sag_api.upgrades.verifier import verify_migration
from sag_api.upgrades.zleap_sag_0_7_to_0_8.checkpoints import (
    apply_checkpoint_plan,
    plan_checkpoint_updates,
)
from sag_api.upgrades.zleap_sag_0_7_to_0_8.relational import (
    GenerationIdentity,
    RelationalMigrationReport,
    migrate_relational,
)
from sag_api.upgrades.zleap_sag_0_7_to_0_8.vectors import (
    VectorMigrationReport,
    migrate_vectors,
)

MIGRATION_ID = "zleap-sag-0.7.1-to-0.8.2"


def _table_count(db: sqlite3.Connection, table: str) -> int:
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not exists:
        return 0
    return int(db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])


def _load_relational_report(target: Path, legacy: Path) -> RelationalMigrationReport:
    with sqlite3.connect(f"file:{target / 'sag.db'}?mode=ro", uri=True) as current:
        manifests = current.execute(
            "SELECT data_source_id,source_id,active_generation_id,source_version,chunk_version FROM sag_source_manifest"
        ).fetchall()
        events = current.execute("SELECT id,data_source_id,source_id FROM source_event").fetchall()
    identities = {
        (str(row[0]), str(row[1])): GenerationIdentity(str(row[2]), str(row[3]), str(row[4])) for row in manifests
    }
    with sqlite3.connect(f"file:{legacy / 'sag.db'}?mode=ro", uri=True) as old:
        row_counts = {
            table: _table_count(old, table)
            for table in (
                "source_config",
                "kb_document",
                "article",
                "article_section",
                "chat_conversation",
                "chat_message",
                "entity_type",
                "entity",
                "source_chunk",
                "source_event",
                "event_entity",
            )
        }
    return RelationalMigrationReport(
        generation_by_source=identities,
        source_by_event={
            str(event_id): (str(data_source_id), str(source_id)) for event_id, data_source_id, source_id in events
        },
        row_counts=row_counts,
    )


def _vector_report_from_journal(journal: MigrationJournal) -> VectorMigrationReport:
    report = journal.reports.get("vectors_migrated")
    if not isinstance(report, dict):
        raise StorageUpgradeError(
            "durable vector migration report is missing",
            stage="vectors",
            recoverable=True,
            diagnostic_path=journal.path,
        )
    row_counts = report.get("row_counts")
    dimensions = report.get("vector_dimensions")
    if not isinstance(row_counts, dict) or not isinstance(dimensions, list):
        raise StorageUpgradeError(
            "durable vector migration report is invalid",
            stage="vectors",
            recoverable=True,
            diagnostic_path=journal.path,
        )
    return VectorMigrationReport(
        {str(name): int(count) for name, count in row_counts.items()},
        {int(value) for value in dimensions},
    )


async def _finish_swapped_checkpoint(
    settings: Settings,
    session_factory: Any,
    layout: StorageLayout,
    journal: MigrationJournal,
) -> UpgradeReport:
    backup = layout.backups / MIGRATION_ID
    relational = _load_relational_report(layout.engine, backup / "engine")
    plan = await plan_checkpoint_updates(session_factory, relational.generation_by_source)
    rollback = journal.path.parent / "original-engine"
    try:
        report = await apply_checkpoint_plan(session_factory, plan)
    except Exception:
        failed = journal.path.parent / "failed-target-engine"
        rollback_engine(layout.engine, rollback, failed)
        journal.path.unlink(missing_ok=True)
        raise
    journal.advance(MigrationPhase.COMPLETED, report=asdict(report))
    return UpgradeReport("migrated", journal.path, backup)


async def migrate_071_to_082(context: StorageUpgradeContext) -> UpgradeReport:
    settings = context.settings
    session_factory = context.session_factory
    if settings.sag_vector_provider != "lancedb" or settings.sag_relational_provider not in (
        None,
        "sqlite",
    ):
        return UpgradeReport("not_applicable")

    layout = StorageLayout.from_settings(settings)
    state_root = layout.upgrades / MIGRATION_ID
    journal_path = state_root / "journal.json"
    rollback = state_root / "original-engine"
    staging = layout.staging / MIGRATION_ID / "engine"

    with UpgradeLock(layout.upgrades / "upgrade.lock", timeout=0):
        journal = MigrationJournal.load(journal_path) if journal_path.is_file() else None
        if journal is not None and journal.phase is MigrationPhase.SWAPPED:
            return await _finish_swapped_checkpoint(settings, session_factory, layout, journal)
        if journal is not None and journal.phase is MigrationPhase.VERIFIED:
            if not layout.engine.exists():
                if not rollback.is_dir() or not staging.is_dir():
                    raise StorageUpgradeError(
                        "verified migration has an incomplete engine swap layout",
                        stage="swap",
                        recoverable=False,
                        backup_path=rollback if rollback.exists() else None,
                        diagnostic_path=journal.path,
                    )
                replace_directory(staging, layout.engine)
                journal.advance(MigrationPhase.SWAPPED, report={"recovered": True})
                return await _finish_swapped_checkpoint(
                    settings,
                    session_factory,
                    layout,
                    journal,
                )
            probe = detect_storage(layout, settings)
            if probe.version is StorageVersion.CURRENT and rollback.is_dir():
                journal.advance(MigrationPhase.SWAPPED, report={"recovered": True})
                return await _finish_swapped_checkpoint(settings, session_factory, layout, journal)

        probe = detect_storage(layout, settings)
        if probe.version is StorageVersion.EMPTY:
            return UpgradeReport("empty")
        if probe.version is StorageVersion.CURRENT:
            return UpgradeReport("current", journal_path if journal else None)
        if probe.version is StorageVersion.UNSUPPORTED:
            return UpgradeReport("not_applicable")
        if probe.version is StorageVersion.UNKNOWN:
            raise StorageUpgradeError(
                probe.reason,
                stage="detect",
                recoverable=True,
                diagnostic_path=journal_path,
            )
        if not settings.storage_upgrade_enabled:
            raise StorageUpgradeError(
                "legacy zleap-sag storage requires SAG_STORAGE_UPGRADE_ENABLED=true",
                stage="detect",
                recoverable=True,
                diagnostic_path=journal_path,
            )

        if journal is None or journal.phase is MigrationPhase.COMPLETED:
            journal = MigrationJournal.create(journal_path, migration_id=MIGRATION_ID)

        backup = create_backup(layout, MIGRATION_ID, source_version="0.7.1")
        if journal.phase is MigrationPhase.DETECTED:
            journal.advance(
                MigrationPhase.BACKED_UP,
                report={"manifest": str(backup.manifest_path)},
            )

        if journal.phase is MigrationPhase.BACKED_UP:
            if staging.exists():
                shutil.rmtree(staging)
            staging = prepare_staging(layout, MIGRATION_ID)
            relational = await migrate_relational(
                backup.engine_path / "sag.db",
                staging,
                settings=settings,
            )
            journal.advance(
                MigrationPhase.RELATIONAL_MIGRATED,
                report={"row_counts": relational.row_counts},
            )
        else:
            relational = _load_relational_report(staging, backup.engine_path)

        target_engine: DataEngine | None = None
        try:
            if journal.phase is MigrationPhase.RELATIONAL_MIGRATED:
                target_engine = DataEngine(
                    build_engine_config(settings, overrides={"data_dir": str(staging)}),
                    health_check=False,
                )
                await target_engine.start()
                vectors = await migrate_vectors(backup.engine_path / "lancedb", target_engine, relational)
                journal.advance(
                    MigrationPhase.VECTORS_MIGRATED,
                    report={
                        "row_counts": vectors.row_counts,
                        "vector_dimensions": sorted(vectors.vector_dimensions),
                    },
                )
            else:
                vectors = _vector_report_from_journal(journal)

            if journal.phase is MigrationPhase.VECTORS_MIGRATED:
                plan = await plan_checkpoint_updates(session_factory, relational.generation_by_source)
                journal.advance(
                    MigrationPhase.CHECKPOINTS_MIGRATED,
                    report={"planned": len(plan.updates)},
                )

            if journal.phase is MigrationPhase.CHECKPOINTS_MIGRATED:
                if target_engine is None:
                    target_engine = DataEngine(
                        build_engine_config(settings, overrides={"data_dir": str(staging)}),
                        health_check=False,
                    )
                    await target_engine.start()
                verification = await verify_migration(
                    backup.engine_path,
                    staging,
                    target_engine,
                    relational,
                    vectors,
                )
                journal.advance(
                    MigrationPhase.VERIFIED,
                    report=asdict(verification),
                )
        finally:
            if target_engine is not None:
                await target_engine.aclose()

        if journal.phase is MigrationPhase.VERIFIED:
            swap_engine(layout.engine, staging, rollback)
            journal.advance(MigrationPhase.SWAPPED, report={"rollback": str(rollback)})
        return await _finish_swapped_checkpoint(settings, session_factory, layout, journal)
