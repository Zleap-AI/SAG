"""Open authentic SDK 0.13 SQLite/LanceDB data under SDK 0.15 offline."""

import hashlib
import json
import runpy
import sqlite3
import tarfile
from pathlib import Path

import lancedb
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from zleap.sag import DataEngine
from zleap.sag.pipeline import SearchOptions, SearchRequest, SearchScope

from sag_api.db.models import Document, Job, Message, Setting, Source
from sag_api.fnos.knowledge_upgrade import reset_legacy_knowledge
from sag_api.sag.document_cleanup import delete_document_records
from sag_api.sag.dto import ProcessCheckpoint
from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

FIXTURE = Path(__file__).parent / "fixtures" / "fnos_013"
GENERATOR = runpy.run_path(str(FIXTURE / "generate.py"))


def tree_hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def engine_rows(workspace):
    """Read values as well as IDs; a successful open alone proves too little."""
    with sqlite3.connect(workspace / "engine-v0.13-clean" / "sag.db") as connection:
        return {
            table: connection.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
            for table in (
                "data_source",
                "article",
                "article_section",
                "source_chunk",
                "source_event",
                "entity",
                "event_entity",
            )
        }


def vector_rows(workspace):
    connection = lancedb.connect(workspace / "engine-v0.13-clean" / "lancedb")
    return {
        name: sorted(connection.open_table(name).to_arrow().to_pylist(), key=lambda row: row["id"])
        for name in connection.list_tables().tables
    }


@pytest.fixture
def old_stores(tmp_path, monkeypatch):
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    archive = FIXTURE / "native-013.tar.gz"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == provenance["archive_sha256"]
    assert hashlib.sha256((FIXTURE / "generate.py").read_bytes()).hexdigest() == provenance["generator_sha256"]
    with tarfile.open(archive) as data:
        data.extractall(tmp_path, filter="data")
    assert tree_hashes(tmp_path) == provenance["files"]
    assert provenance["engine_version"] == "0.13.0"
    GENERATOR["install_model_boundaries"](monkeypatch)
    return tmp_path, provenance


async def search(engine, tenant, *, source_id=None):
    return await engine.search(
        SearchRequest(
            query="Private knowledge",
            scope=SearchScope(
                data_source_ids=(f"config-{tenant}",),
                source_ids=(source_id,) if source_id else (),
            ),
            options=SearchOptions(strategy="vector", return_type="chunk", top_k=20),
        )
    )


async def metadata_snapshot(workspace):
    database = create_async_engine(f"sqlite+aiosqlite:///{workspace / 'meta/sag.db'}")
    sessions = async_sessionmaker(database, expire_on_commit=False)
    try:
        async with sessions() as session:
            return {
                "documents": {
                    row.id: (row.source_id, row.sag_source_id, row.status, row.storage_path)
                    for row in (await session.scalars(select(Document))).all()
                },
                "sources": {row.id: row.sag_source_config_id for row in (await session.scalars(select(Source))).all()},
                "messages": {
                    row.id: (row.content, row.citations) for row in (await session.scalars(select(Message))).all()
                },
                "settings": {row.key: row.value for row in (await session.scalars(select(Setting))).all()},
            }
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_real_013_store_opens_without_reset(old_stores):
    root, provenance = old_stores
    for tenant in provenance["tenants"]:
        workspace = root / "users" / tenant
        before = await metadata_snapshot(workspace)
        old_engine = tree_hashes(workspace / "engine")
        database = create_async_engine(f"sqlite+aiosqlite:///{workspace / 'meta/sag.db'}")
        sessions = async_sessionmaker(database, expire_on_commit=False)
        try:
            # Runs the exact worker-start migration gate on two consecutive opens.
            for _ in range(2):
                async with sessions() as session:
                    assert await reset_legacy_knowledge(session, workspace / "engine") == 0
                async with DataEngine(GENERATOR["config"](workspace), data_source_id=f"config-{tenant}") as engine:
                    result = await search(engine, tenant)
                    assert result.chunks
                    assert all(hit.data_source_id == f"config-{tenant}" for hit in result.chunks)
                assert await metadata_snapshot(workspace) == before
                assert tree_hashes(workspace / "engine") == old_engine
        finally:
            await database.dispose()


@pytest.mark.asyncio
async def test_unmarked_real_013_preserves_knowledge_rows_vectors_and_citations(old_stores):
    root, provenance = old_stores
    for tenant in provenance["tenants"]:
        workspace = root / "users" / tenant
        with sqlite3.connect(workspace / "meta/sag.db") as connection:
            connection.execute("DELETE FROM settings WHERE key = 'fnos_knowledge_engine_0_13'")
        before = await metadata_snapshot(workspace)
        rows = engine_rows(workspace)
        vectors = vector_rows(workspace)
        files = tree_hashes(workspace / "engine-v0.13-clean")
        originals = tree_hashes(workspace / "uploads")
        database = create_async_engine(f"sqlite+aiosqlite:///{workspace / 'meta/sag.db'}")
        sessions = async_sessionmaker(database, expire_on_commit=False)
        try:
            async with sessions() as session:
                assert await reset_legacy_knowledge(session, workspace / "engine") == 0
            after = await metadata_snapshot(workspace)
            marker = after["settings"].pop("fnos_knowledge_engine_0_13")
            assert marker["knowledge_reset"] is True
            assert after == before
            assert engine_rows(workspace) == rows
            assert vector_rows(workspace) == vectors
            assert tree_hashes(workspace / "engine-v0.13-clean") == files
            assert tree_hashes(workspace / "uploads") == originals
            async with DataEngine(GENERATOR["config"](workspace), data_source_id=f"config-{tenant}") as engine:
                assert (await search(engine, tenant)).chunks
        finally:
            await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["corrupt", "empty", "future", "missing-table"])
@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_current_engine_never_resets_knowledge(old_stores, defect, committed):
    root, provenance = old_stores
    workspace = root / "users" / provenance["tenants"][0]
    if not committed:
        with sqlite3.connect(workspace / "meta/sag.db") as connection:
            connection.execute("DELETE FROM settings WHERE key = 'fnos_knowledge_engine_0_13'")
    engine_file = workspace / "engine-v0.13-clean/sag.db"
    if defect in {"corrupt", "empty"}:
        engine_file.write_bytes(b"not SQLite" if defect == "corrupt" else b"")
    else:
        with sqlite3.connect(engine_file) as connection:
            if defect == "future":
                connection.execute("UPDATE sag_schema_meta SET schema_version = 999")
            else:
                connection.execute("DROP TABLE source_event")
    before = await metadata_snapshot(workspace)
    before_files = tree_hashes(workspace)
    database = create_async_engine(f"sqlite+aiosqlite:///{workspace / 'meta/sag.db'}")
    sessions = async_sessionmaker(database, expire_on_commit=False)
    try:
        async with sessions() as session:
            with pytest.raises(ValueError, match="无法确认|unknown|unsupported|invalid"):
                await reset_legacy_knowledge(session, workspace / "engine")
        assert await metadata_snapshot(workspace) == before
        assert tree_hashes(workspace) == before_files
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_upgrade_preserves_ids_vectors_and_citations(old_stores):
    root, provenance = old_stores
    for tenant in provenance["tenants"]:
        workspace = root / "users" / tenant
        original_rows = engine_rows(workspace)
        original_vectors = vector_rows(workspace)
        original_meta = await metadata_snapshot(workspace)
        assert original_rows["source_event"]
        assert all(row["content_vector"] == [1.0] + [0.0] * 15 for row in original_vectors["source_chunks"])
        cited = original_meta["messages"][f"message-{tenant}"][1][0]["chunk_id"]
        async with DataEngine(GENERATOR["config"](workspace), data_source_id=f"config-{tenant}") as engine:
            result = await search(engine, tenant)
            assert cited in {hit.chunk_id or hit.id for hit in result.chunks}
            joined_text = "\n".join(hit.content for hit in result.chunks)
            assert "R001" in joined_text and "R002" in joined_text
            assert "100.25" in joined_text and "200.5" in joined_text
        assert engine_rows(workspace) == original_rows
        assert vector_rows(workspace) == original_vectors
        assert await metadata_snapshot(workspace) == original_meta


@pytest.mark.asyncio
async def test_013_checkpoint_resumes_under_015(old_stores):
    root, provenance = old_stores
    for tenant in provenance["tenants"]:
        workspace = root / "users" / tenant
        database = create_async_engine(f"sqlite+aiosqlite:///{workspace / 'meta/sag.db'}")
        sessions = async_sessionmaker(database, expire_on_commit=False)
        try:
            async with sessions() as session:
                job = await session.get(Job, f"job-{tenant}")
                checkpoint = ProcessCheckpoint.model_validate(job.payload["process_checkpoint"])
            assert checkpoint.chunk_ids and checkpoint.chunk_version and not checkpoint.event_ids
            old_chunks = engine_rows(workspace)["source_chunk"]
            checkpoints = []

            async def save(value, target=checkpoints):
                target.append(value.model_copy(deep=True))

            async with DataEngine(GENERATOR["config"](workspace), data_source_id=f"config-{tenant}") as engine:
                processor = IncrementalDocumentProcessor(engine, f"config-{tenant}", max_concurrency=1)
                # No original/parsed file is supplied: recovery must use persisted
                # chunk/source versions from 0.13, rather than ingest a replacement.
                outcome = await processor.process(
                    None,
                    checkpoint=checkpoint,
                    on_checkpoint=save,
                    should_pause=GENERATOR["never_pause"],
                )
                assert not outcome.paused and outcome.event_count > 0
                assert outcome.source_id == checkpoint.source_id
                assert outcome.chunk_ids == checkpoint.chunk_ids
                assert outcome.processed_chunk_ids == checkpoint.chunk_ids
                assert checkpoints[-1].chunk_version == checkpoint.chunk_version
                result = await search(engine, tenant, source_id=checkpoint.source_id)
                assert {hit.chunk_id or hit.id for hit in result.chunks} == set(checkpoint.chunk_ids)
            assert engine_rows(workspace)["source_chunk"] == old_chunks
        finally:
            await database.dispose()


@pytest.mark.asyncio
async def test_existing_store_supports_new_ingest_replace_delete(old_stores):
    root, provenance = old_stores
    tenant, other_tenant = provenance["tenants"]
    workspace = root / "users" / tenant
    untouched = tree_hashes(root / "users" / other_tenant)
    old_book = provenance["checkpoints"][tenant]["book"]
    original_meta = await metadata_snapshot(workspace)
    sheet = provenance["checkpoints"][tenant]["sheet"]
    replacement = workspace / "uploads" / "replacement.md"
    replacement.write_text("# Replacement\n\nNew content after upgrading the existing store. " * 8)
    checkpoints = []

    async def save(value):
        checkpoints.append(value.model_copy(deep=True))

    async with DataEngine(GENERATOR["config"](workspace), data_source_id=f"config-{tenant}") as engine:
        processor = IncrementalDocumentProcessor(engine, f"config-{tenant}", max_concurrency=1)
        new = await processor.process(
            replacement,
            checkpoint=ProcessCheckpoint(),
            on_checkpoint=save,
            should_pause=GENERATOR["never_pause"],
        )
        assert new.event_count > 0
        result = await search(engine, tenant)
        assert set(old_book["chunk_ids"]) | set(new.chunk_ids) <= {hit.chunk_id or hit.id for hit in result.chunks}
        # This is the app's production replacement/deletion writer, using the
        # actual 0.15 SQL and vector providers over a 0.13-created store.
        deleted = await delete_document_records(
            f"config-{tenant}",
            old_book["source_id"],
            session_factory=engine.resources.relational.session_factory(),
            vector_store=engine.resources.vector,
        )
        assert set(deleted.chunk_ids) == set(old_book["chunk_ids"])
        assert set(deleted.event_ids) == set(old_book["event_ids"])
        after = await search(engine, tenant)
        visible = {hit.chunk_id or hit.id for hit in after.chunks}
        assert not visible.intersection(old_book["chunk_ids"])
        assert set(new.chunk_ids) | set(sheet["chunk_ids"]) <= visible
    persisted = vector_rows(workspace)
    assert not {row["id"] for row in persisted["source_chunks"]}.intersection(old_book["chunk_ids"])
    assert set(new.chunk_ids) <= {row["id"] for row in persisted["source_chunks"]}
    # Derivative replacement never deletes original files, chat history or app IDs.
    assert await metadata_snapshot(workspace) == original_meta
    assert tree_hashes(root / "users" / other_tenant) == untouched
