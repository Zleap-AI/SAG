from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import lancedb

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
    assert set(lancedb.connect(tmp_path / "lancedb").list_tables().tables) >= {
        "source_chunks",
        "event_vectors",
        "entity_vectors",
        "event_entity_vectors",
    }
