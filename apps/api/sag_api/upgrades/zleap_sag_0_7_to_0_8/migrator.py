from __future__ import annotations

import shutil
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from zleap.sag import DataEngine

from sag_api.core.config import Settings
from sag_api.sag.config_builder import build_engine_config
from sag_api.upgrades.active_engine import ActiveEngineStore
from sag_api.upgrades.backup import create_backup, prepare_staging
from sag_api.upgrades.contracts import StorageUpgradeContext, UpgradeReport
from sag_api.upgrades.detector import detect_storage
from sag_api.upgrades.directory_replace import (
    is_transient_windows_replace_error,
    replace_directory,
)
from sag_api.upgrades.journal import MigrationJournal, UpgradeLock
from sag_api.upgrades.swap import rollback_engine, swap_engine
from sag_api.upgrades.types import (
    MigrationPhase,
    StorageLayout,
    StorageProbe,
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


def _layout_with_engine(layout: StorageLayout, engine: Path) -> StorageLayout:
    return replace(layout, engine=engine)


def _contains_transient_windows_replace_error(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if is_transient_windows_replace_error(current):
            return True
        current = current.__cause__
    return False


def _side_by_side_target(layout: StorageLayout, journal: MigrationJournal) -> Path:
    suffix = uuid5(NAMESPACE_URL, journal.created_at).hex[:12]
    return layout.root / f"engine-0.8.2-migrated-{suffix}"


def _probe_engine(settings: Settings, layout: StorageLayout, engine: Path) -> StorageProbe:
    engine_settings = settings.model_copy(update={"data_dir": str(engine)})
    return detect_storage(_layout_with_engine(layout, engine), engine_settings)


def _activate_side_by_side_engine(
    settings: Settings,
    layout: StorageLayout,
    journal: MigrationJournal,
    staging: Path,
) -> Path:
    target = _side_by_side_target(layout, journal)
    if target.exists():
        if _probe_engine(settings, layout, target).version is not StorageVersion.CURRENT:
            raise StorageUpgradeError(
                "side-by-side migration target is incomplete",
                stage="swap",
                recoverable=True,
                diagnostic_path=journal.path,
            )
    else:
        if not staging.is_dir():
            raise StorageUpgradeError(
                "verified migration staging directory is missing",
                stage="swap",
                recoverable=True,
                diagnostic_path=journal.path,
            )
        replace_directory(staging, target)
    ActiveEngineStore(layout.upgrades / "active-engine.json").activate(layout.engine, target)
    return target


def _advance_pointer_swap(
    journal: MigrationJournal,
    *,
    active_engine: Path,
    legacy_engine: Path,
) -> None:
    journal.advance(
        MigrationPhase.SWAPPED,
        report={
            "mode": "pointer",
            "active_engine": str(active_engine),
            "legacy_engine": str(legacy_engine),
        },
    )


def _restore_legacy_after_pointer_failure(
    settings: Settings,
    layout: StorageLayout,
    journal: MigrationJournal,
    legacy_engine: Path,
) -> None:
    configured_engine = layout.engine
    if configured_engine.is_dir():
        configured_version = _probe_engine(settings, layout, configured_engine).version
        if configured_version in (StorageVersion.LEGACY_0_7, StorageVersion.CURRENT):
            return
        if configured_version is StorageVersion.EMPTY and legacy_engine != configured_engine:
            configured_engine.rmdir()
        else:
            raise StorageUpgradeError(
                "configured engine cannot be safely restored after checkpoint failure",
                stage="checkpoints",
                recoverable=True,
                backup_path=legacy_engine if legacy_engine.exists() else None,
                diagnostic_path=journal.path,
            )
    elif configured_engine.exists():
        raise StorageUpgradeError(
            "configured engine path is not a directory",
            stage="checkpoints",
            recoverable=True,
            backup_path=legacy_engine if legacy_engine.exists() else None,
            diagnostic_path=journal.path,
        )

    if not legacy_engine.is_dir():
        raise StorageUpgradeError(
            "legacy engine is unavailable after checkpoint failure",
            stage="checkpoints",
            recoverable=True,
            diagnostic_path=journal.path,
        )
    replace_directory(legacy_engine, configured_engine)


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
    pointer = ActiveEngineStore(layout.upgrades / "active-engine.json")
    swapped_report = journal.reports.get(MigrationPhase.SWAPPED.value)
    pointer_mode = isinstance(swapped_report, dict) and swapped_report.get("mode") == "pointer"
    if pointer_mode:
        reported_active = Path(str(swapped_report.get("active_engine", "")))
        pointer.activate(layout.engine, reported_active)
    active_engine = pointer.resolve(layout.engine)
    relational = _load_relational_report(active_engine, backup / "engine")
    plan = await plan_checkpoint_updates(session_factory, relational.generation_by_source)
    rollback = journal.path.parent / "original-engine"
    try:
        report = await apply_checkpoint_plan(session_factory, plan)
    except Exception:
        if active_engine == layout.engine:
            failed = journal.path.parent / "failed-target-engine"
            rollback_engine(layout.engine, rollback, failed)
        else:
            legacy_engine = (
                Path(str(swapped_report.get("legacy_engine", "")))
                if isinstance(swapped_report, dict)
                else layout.engine
            )
            _restore_legacy_after_pointer_failure(
                settings,
                layout,
                journal,
                legacy_engine,
            )
            pointer.path.unlink(missing_ok=True)
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
        pointer = ActiveEngineStore(layout.upgrades / "active-engine.json")
        if (
            journal is not None
            and journal.phase is MigrationPhase.COMPLETED
            and pointer.path.is_file()
        ):
            active_engine = pointer.resolve(layout.engine)
            if (
                active_engine != layout.engine
                and _probe_engine(settings, layout, active_engine).version
                is StorageVersion.CURRENT
            ):
                return UpgradeReport("current", journal.path)
        if journal is not None and journal.phase is MigrationPhase.SWAPPED:
            return await _finish_swapped_checkpoint(settings, session_factory, layout, journal)
        if journal is not None and journal.phase is MigrationPhase.VERIFIED:
            side_by_side_target = _side_by_side_target(layout, journal)
            if side_by_side_target.exists():
                active_engine = _activate_side_by_side_engine(
                    settings,
                    layout,
                    journal,
                    staging,
                )
                legacy_engine = rollback if rollback.is_dir() else layout.engine
                _advance_pointer_swap(
                    journal,
                    active_engine=active_engine,
                    legacy_engine=legacy_engine,
                )
                return await _finish_swapped_checkpoint(
                    settings,
                    session_factory,
                    layout,
                    journal,
                )
            if rollback.is_dir() and not layout.engine.exists():
                if not staging.is_dir():
                    raise StorageUpgradeError(
                        "verified migration has an incomplete engine swap layout",
                        stage="swap",
                        recoverable=False,
                        backup_path=rollback if rollback.exists() else None,
                        diagnostic_path=journal.path,
                    )
                try:
                    replace_directory(staging, layout.engine)
                except Exception as error:
                    if not _contains_transient_windows_replace_error(error):
                        raise
                    active_engine = _activate_side_by_side_engine(
                        settings,
                        layout,
                        journal,
                        staging,
                    )
                    _advance_pointer_swap(
                        journal,
                        active_engine=active_engine,
                        legacy_engine=rollback,
                    )
                else:
                    journal.advance(
                        MigrationPhase.SWAPPED,
                        report={"mode": "rename", "recovered": True},
                    )
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
            if rollback.is_dir():
                if (
                    staging.is_dir()
                    and _probe_engine(settings, layout, staging).version
                    is StorageVersion.CURRENT
                ):
                    active_engine = _activate_side_by_side_engine(
                        settings,
                        layout,
                        journal,
                        staging,
                    )
                    _advance_pointer_swap(
                        journal,
                        active_engine=active_engine,
                        legacy_engine=rollback,
                    )
                    return await _finish_swapped_checkpoint(
                        settings,
                        session_factory,
                        layout,
                        journal,
                    )
                raise StorageUpgradeError(
                    "verified migration has no usable current engine candidate",
                    stage="swap",
                    recoverable=False,
                    backup_path=rollback,
                    diagnostic_path=journal.path,
                )

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
            try:
                swap_engine(layout.engine, staging, rollback)
            except Exception as error:
                if not _contains_transient_windows_replace_error(error) or rollback.exists():
                    raise
                active_engine = _activate_side_by_side_engine(
                    settings,
                    layout,
                    journal,
                    staging,
                )
                _advance_pointer_swap(
                    journal,
                    active_engine=active_engine,
                    legacy_engine=layout.engine,
                )
            else:
                journal.advance(
                    MigrationPhase.SWAPPED,
                    report={"mode": "rename", "rollback": str(rollback)},
                )
        return await _finish_swapped_checkpoint(settings, session_factory, layout, journal)
