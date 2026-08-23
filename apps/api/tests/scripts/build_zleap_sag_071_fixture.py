"""Build the checked-in zleap-sag 0.7.1 storage upgrade fixture.

The builder must run with the exact 0.7.1 package ahead of the API environment
on ``PYTHONPATH``.  CI never runs this file; it consumes the generated archive.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import zleap.sag.db.models  # noqa: F401  Registers the 0.7.1 ORM metadata.
from zleap.sag.core.storage.lancedb_store import LanceDBStore
from zleap.sag.db.base import Base

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "zleap_sag_071"
FIXED_TIME = "2026-01-02 03:04:05"

SOURCE_A = "10000000-0000-0000-0000-000000000001"
SOURCE_B = "10000000-0000-0000-0000-000000000002"
ARTICLE_A = "20000000-0000-0000-0000-000000000001"
ARTICLE_B = "20000000-0000-0000-0000-000000000002"
CHUNKS = [
    "30000000-0000-0000-0000-000000000001",
    "30000000-0000-0000-0000-000000000002",
    "30000000-0000-0000-0000-000000000003",
]
EVENTS = [
    "40000000-0000-0000-0000-000000000001",
    "40000000-0000-0000-0000-000000000002",
    "40000000-0000-0000-0000-000000000003",
]
ENTITIES = [
    "50000000-0000-0000-0000-000000000001",
    "50000000-0000-0000-0000-000000000002",
    "50000000-0000-0000-0000-000000000003",
]
ENTITY_TYPES = [
    "60000000-0000-0000-0000-000000000001",
    "60000000-0000-0000-0000-000000000002",
]
EVENT_ENTITIES = [
    "70000000-0000-0000-0000-000000000001",
    "70000000-0000-0000-0000-000000000002",
    "70000000-0000-0000-0000-000000000003",
]


def _require_071() -> None:
    version = importlib.metadata.version("zleap-sag")
    if version != "0.7.1":
        raise SystemExit(f"fixture builder requires zleap-sag==0.7.1 on PYTHONPATH; found {version}")


def _create_relational(db_path: Path) -> None:
    engine = __import__("sqlalchemy").create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()

    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executemany(
            "INSERT INTO source_config "
            "(id,name,description,target_config,created_time,updated_time) VALUES (?,?,?,?,?,?)",
            [
                (SOURCE_A, "Legacy source A", "fixture", "{}", FIXED_TIME, FIXED_TIME),
                (SOURCE_B, "Legacy source B", "fixture", "{}", FIXED_TIME, FIXED_TIME),
            ],
        )
        db.executemany(
            "INSERT INTO article "
            "(id,source_config_id,title,source_id,summary,content,status,parse_status,created_time) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    ARTICLE_A,
                    SOURCE_A,
                    "Alpha",
                    "legacy-a",
                    "Alpha summary",
                    "Alpha body",
                    "COMPLETED",
                    "COMPLETED",
                    FIXED_TIME,
                ),
                (
                    ARTICLE_B,
                    SOURCE_B,
                    "Beta",
                    "legacy-b",
                    "Beta summary",
                    "Beta body",
                    "COMPLETED",
                    "COMPLETED",
                    FIXED_TIME,
                ),
            ],
        )
        chunk_rows = [
            (CHUNKS[0], SOURCE_A, "ARTICLE", ARTICLE_A, ARTICLE_A, "Alpha one", "Alpha", 0, 9),
            (CHUNKS[1], SOURCE_A, "ARTICLE", ARTICLE_A, ARTICLE_A, "Alpha two", "Alpha", 1, 9),
            (CHUNKS[2], SOURCE_B, "ARTICLE", ARTICLE_B, ARTICLE_B, "Beta one", "Beta", 0, 8),
        ]
        db.executemany(
            "INSERT INTO source_chunk "
            "(id,source_config_id,source_type,source_id,article_id,content,heading,rank,"
            "chunk_length,created_time,updated_time) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(*row, FIXED_TIME, FIXED_TIME) for row in chunk_rows],
        )
        db.executemany(
            "INSERT INTO entity_type "
            "(id,scope,source_config_id,type,name,description,weight,similarity_threshold,"
            "is_active,is_default,created_time,updated_time) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    ENTITY_TYPES[0],
                    "source",
                    SOURCE_A,
                    "person",
                    "Person",
                    "fixture",
                    1,
                    0.8,
                    1,
                    0,
                    FIXED_TIME,
                    FIXED_TIME,
                ),
                (
                    ENTITY_TYPES[1],
                    "source",
                    SOURCE_B,
                    "place",
                    "Place",
                    "fixture",
                    1,
                    0.8,
                    1,
                    0,
                    FIXED_TIME,
                    FIXED_TIME,
                ),
            ],
        )
        entity_rows = [
            (ENTITIES[0], SOURCE_A, ENTITY_TYPES[0], "person", "Alice", "alice"),
            (ENTITIES[1], SOURCE_A, ENTITY_TYPES[0], "person", "Bob", "bob"),
            (ENTITIES[2], SOURCE_B, ENTITY_TYPES[1], "place", "Shanghai", "shanghai"),
        ]
        db.executemany(
            "INSERT INTO entity "
            "(id,source_config_id,entity_type_id,type,name,normalized_name,created_time,updated_time) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(*row, FIXED_TIME, FIXED_TIME) for row in entity_rows],
        )
        event_rows = [
            (EVENTS[0], SOURCE_A, ARTICLE_A, CHUNKS[0], "Alpha event", "Summary A1", "Body A1", 0),
            (EVENTS[1], SOURCE_A, ARTICLE_A, CHUNKS[1], "Beta event", "Summary A2", "Body A2", 1),
            (EVENTS[2], SOURCE_B, ARTICLE_B, CHUNKS[2], "Gamma event", "Summary B1", "Body B1", 0),
        ]
        db.executemany(
            "INSERT INTO source_event "
            "(id,source_config_id,source_type,source_id,article_id,chunk_id,title,summary,"
            "content,rank,level,created_time,updated_time) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    event_id,
                    source_id,
                    "ARTICLE",
                    article_id,
                    article_id,
                    chunk_id,
                    title,
                    summary,
                    content,
                    rank,
                    0,
                    FIXED_TIME,
                    FIXED_TIME,
                )
                for event_id, source_id, article_id, chunk_id, title, summary, content, rank in event_rows
            ],
        )
        db.executemany(
            "INSERT INTO event_entity (id,event_id,entity_id,weight,description,created_time) VALUES (?,?,?,?,?,?)",
            [(EVENT_ENTITIES[index], EVENTS[index], ENTITIES[index], 1, "fixture", FIXED_TIME) for index in range(3)],
        )
        db.commit()


async def _create_vectors(lance_path: Path) -> None:
    store = LanceDBStore(str(lance_path))
    now = datetime.fromisoformat(FIXED_TIME).replace(tzinfo=UTC)
    source_by_index = [SOURCE_A, SOURCE_A, SOURCE_B]
    article_by_index = [ARTICLE_A, ARTICLE_A, ARTICLE_B]
    vectors = [[1.0 if offset == index else 0.0 for offset in range(12)] for index in range(3)]

    await store.bulk_index(
        "source_chunks",
        [
            {
                "_id": CHUNKS[index],
                "chunk_id": CHUNKS[index],
                "source_id": article_by_index[index],
                "source_config_id": source_by_index[index],
                "chunk_type": "ARTICLE",
                "heading": f"Heading {index}",
                "content": f"Content {index}",
                "heading_vector": vector,
                "content_vector": vector,
                "references": [],
                "is_delete": False,
                "created_time": now,
                "updated_time": now,
                "rank": index,
                "content_length": 9,
            }
            for index, vector in enumerate(vectors)
        ],
    )
    await store.bulk_index(
        "event_vectors",
        [
            {
                "_id": EVENTS[index],
                "event_id": EVENTS[index],
                "source_config_id": source_by_index[index],
                "source_type": "ARTICLE",
                "source_id": article_by_index[index],
                "title": f"Event {index}",
                "summary": f"Summary {index}",
                "content": f"Body {index}",
                "category": "fixture",
                "tags": [],
                "entity_ids": [ENTITIES[index]],
                "title_vector": vector,
                "content_vector": vector,
                "is_delete": False,
                "created_time": now,
            }
            for index, vector in enumerate(vectors)
        ],
    )
    await store.bulk_index(
        "entity_vectors",
        [
            {
                "_id": ENTITIES[index],
                "entity_id": ENTITIES[index],
                "source_config_id": source_by_index[index],
                "type": "fixture",
                "name": f"Entity {index}",
                "vector": vector,
                "is_delete": False,
                "created_time": now,
            }
            for index, vector in enumerate(vectors)
        ],
    )
    await store.bulk_index(
        "event_entity_vectors",
        [
            {
                "_id": EVENT_ENTITIES[index],
                "event_id": EVENTS[index],
                "entity_id": ENTITIES[index],
                "source_config_id": source_by_index[index],
                "description": "fixture",
                "vector": vector,
                "is_delete": False,
                "created_time": now,
            }
            for index, vector in enumerate(vectors)
        ],
    )


def _write_archive(storage_root: Path) -> None:
    manifest = {
        "zleap_sag_version": "0.7.1",
        "counts": {
            "source_config": 2,
            "article": 2,
            "source_chunk": 3,
            "source_event": 3,
            "entity": 3,
            "event_entity": 3,
        },
        "vector_tables": [
            "source_chunks",
            "event_vectors",
            "entity_vectors",
            "event_entity_vectors",
        ],
        "vector_dimensions": 12,
        "source_ids": [SOURCE_A, SOURCE_B],
        "article_ids": [ARTICLE_A, ARTICLE_B],
        "chunk_ids": CHUNKS,
        "event_ids": EVENTS,
        "entity_ids": ENTITIES,
    }
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    (FIXTURE_ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    archive = FIXTURE_ROOT / "fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path in sorted(storage_root.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(storage_root))


def main() -> None:
    _require_071()
    temp_root = Path(tempfile.mkdtemp(prefix="sag-zleap-071-fixture-"))
    try:
        _create_relational(temp_root / "sag.db")
        asyncio.run(_create_vectors(temp_root / "lancedb"))
        _write_archive(temp_root)
    finally:
        shutil.rmtree(temp_root)
    print("0.7.1 fixture created")


if __name__ == "__main__":
    main()
