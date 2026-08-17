from __future__ import annotations

from pathlib import Path

import pytest

from sag_api.core.config import Settings
from sag_api.upgrades.contracts import StorageUpgradeContext
from sag_api.upgrades.contracts import UpgradeReport as StorageMigrationResult
from sag_api.upgrades.journal import MigrationJournal
from sag_api.upgrades.swap import swap_engine
from sag_api.upgrades.types import MigrationPhase, StorageLayout, StorageUpgradeError
from sag_api.upgrades.zleap_sag_0_7_to_0_8.adapter import ZleapSag071To082Adapter
from sag_api.upgrades.zleap_sag_0_7_to_0_8.migrator import MIGRATION_ID


def test_swap_restores_original_when_second_rename_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = tmp_path / "engine"
    staging = tmp_path / "staging"
    rollback = tmp_path / "rollback"
    engine.mkdir()
    staging.mkdir()
    (engine / "marker").write_text("legacy", encoding="utf-8")
    (staging / "marker").write_text("current", encoding="utf-8")

    from sag_api.upgrades import swap as swap_module

    original_replace = swap_module.os.replace
    calls = 0

    def fail_second(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected swap failure")
        original_replace(source, destination)

    monkeypatch.setattr(swap_module.os, "replace", fail_second)
    with pytest.raises(StorageUpgradeError, match="atomic engine swap"):
        swap_engine(engine, staging, rollback)

    assert (engine / "marker").read_text(encoding="utf-8") == "legacy"
    assert (staging / "marker").read_text(encoding="utf-8") == "current"
    assert not rollback.exists()


@pytest.mark.asyncio
async def test_verified_phase_completes_swap_after_process_dies_between_renames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        data_dir=str(tmp_path / "engine"),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'sag.db'}",
        _env_file=None,
    )
    layout = StorageLayout.from_settings(settings)
    state_root = layout.upgrades / MIGRATION_ID
    rollback = state_root / "original-engine"
    staging = layout.staging / MIGRATION_ID / "engine"
    rollback.mkdir(parents=True)
    staging.mkdir(parents=True)
    (rollback / "marker").write_text("legacy", encoding="utf-8")
    (staging / "marker").write_text("current", encoding="utf-8")

    journal = MigrationJournal.create(state_root / "journal.json", migration_id=MIGRATION_ID)
    for phase in (
        MigrationPhase.BACKED_UP,
        MigrationPhase.RELATIONAL_MIGRATED,
        MigrationPhase.VECTORS_MIGRATED,
        MigrationPhase.CHECKPOINTS_MIGRATED,
        MigrationPhase.VERIFIED,
    ):
        journal.advance(phase)

    async def finish(_settings, _session_factory, recovered_layout, recovered_journal):
        assert recovered_journal.phase is MigrationPhase.SWAPPED
        assert (recovered_layout.engine / "marker").read_text(encoding="utf-8") == "current"
        return StorageMigrationResult("migrated", recovered_journal.path, rollback)

    monkeypatch.setattr("sag_api.upgrades.zleap_sag_0_7_to_0_8.migrator._finish_swapped_checkpoint", finish)

    result = await ZleapSag071To082Adapter().migrate(
        StorageUpgradeContext(settings=settings, session_factory=None)
    )

    assert result.status == "migrated"
    assert (rollback / "marker").read_text(encoding="utf-8") == "legacy"
