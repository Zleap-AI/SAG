from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import lancedb
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from zleap.sag import DataEngine
from zleap.sag.core.adapters.models import Filter, VectorQuery

import sag_api.db.models  # noqa: F401
from sag_api.core.config import Settings
from sag_api.db.base import Base
from sag_api.sag.config_builder import build_engine_config
from sag_api.upgrades.contracts import StorageUpgradeContext
from sag_api.upgrades.detector import detect_storage
from sag_api.upgrades.registry import select_adapter
from sag_api.upgrades.types import StorageLayout, StorageProbe, StorageVersion

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "zleap_sag_071"


def test_legacy_fixture_has_real_071_schema(tmp_path: Path) -> None:
    archive = FIXTURE_DIR / "fixture.zip"
    assert archive.is_file(), "run tests/scripts/build_zleap_sag_071_fixture.py"
    with zipfile.ZipFile(archive) as source:
        source.extractall(tmp_path)

    with sqlite3.connect(tmp_path / "sag.db") as db:
        article_columns = {row[1] for row in db.execute("PRAGMA table_info(article)").fetchall()}
        counts = {
            table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "source_config",
                "article",
                "source_chunk",
                "source_event",
                "entity",
                "event_entity",
            )
        }

    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert {"id", "source_config_id"} <= article_columns
    assert "data_source_id" not in article_columns
    assert counts == manifest["counts"]
    assert set(lancedb.connect(tmp_path / "lancedb").table_names()) >= {
        "source_chunks",
        "event_vectors",
        "entity_vectors",
        "event_entity_vectors",
    }


@pytest.mark.asyncio
async def test_real_071_storage_upgrades_end_to_end_and_is_idempotent(
    tmp_path: Path,
) -> None:
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    with zipfile.ZipFile(FIXTURE_DIR / "fixture.zip") as archive:
        archive.extractall(engine_dir)
    # A data source may be fully present in the relational store before any
    # chunk embedding has been generated for it.  That is a valid legacy state
    # and must not make the upgrade verifier reject the whole installation.
    legacy_vectors = lancedb.connect(engine_dir / "lancedb")
    legacy_vectors.open_table("source_chunks").delete(
        "source_config_id = '10000000-0000-0000-0000-000000000002'"
    )
    app_db = tmp_path / "sag.db"
    meta_engine = create_async_engine(f"sqlite+aiosqlite:///{app_db}")
    async with meta_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(meta_engine, expire_on_commit=False)
    settings = Settings(
        data_dir=str(engine_dir),
        database_url=f"sqlite+aiosqlite:///{app_db}",
        llm_api_key="fixture",
        embedding_api_key="fixture",
        embedding_dimensions=12,
        _env_file=None,
    )
    layout = StorageLayout.from_settings(settings)
    try:
        adapter = select_adapter(
            StorageProbe(StorageVersion.LEGACY_0_7, "fixture"),
            target_version="0.8.2",
        )
        assert adapter is not None
        context = StorageUpgradeContext(settings=settings, session_factory=session_factory)
        first = await adapter.migrate(context)
        second = await adapter.migrate(context)
        assert first.status == "migrated"
        assert second.status == "current"
        assert first.backup_path is not None and first.backup_path.is_dir()
        assert detect_storage(layout, settings).version is StorageVersion.CURRENT

        with sqlite3.connect(engine_dir / "sag.db") as db:
            assert db.execute("SELECT count(*) FROM data_source").fetchone() == (2,)
            assert db.execute("SELECT count(*) FROM source_chunk").fetchone() == (3,)
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []

        engine = DataEngine(build_engine_config(settings), health_check=False)
        await engine.start()
        try:
            source_id = "10000000-0000-0000-0000-000000000001"
            hits = await engine.resources.vector.query(
                "source_chunks",
                VectorQuery(
                    vector=[1.0, *([0.0] * 11)],
                    vector_field="content_vector",
                    filters=Filter.eq("data_source_id", source_id),
                    limit=3,
                ),
            )
            assert hits and all(hit.payload["data_source_id"] == source_id for hit in hits)
        finally:
            await engine.aclose()
    finally:
        await meta_engine.dispose()
