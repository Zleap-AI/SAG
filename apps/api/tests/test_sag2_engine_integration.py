"""Run zleap-sag2 read-only against a real zleap-sag SQLite store with an offline scorer."""

import sqlite3
from types import SimpleNamespace

import pytest

from sag_api.core.config import Settings
from sag_api.core.errors import ConfigurationError, ValidationError
from sag_api.sag.sag2_engine import Sag2EngineHolder

QUERY = "Alice 出生在哪里？"


class OverlapScorer:
    """Mimic zleap-sag2's ``JevScoring.score`` contract with query-term overlap; no network."""

    def __init__(self) -> None:
        self.calls = 0

    async def score(self, query, candidates, evidence):
        self.calls += 1
        terms = [term for term in ("Alice", "出生", "北京") if term in query]
        scored = []
        for candidate in candidates:
            text = f"{candidate.get('title', '')} {candidate.get('heading', '')} {candidate.get('content', '')}"
            overlap = sum(term in text for term in terms)
            scored.append({**candidate, "jev_score": round(min(1.0, 0.1 + 0.3 * overlap), 4)})
        usage = {
            "requests": 1, "judgments": len(candidates), "input_tokens": 100 * len(candidates),
            "output_tokens": 0, "batches": [{"judgments": len(candidates)}],
        }
        return scored, usage


async def seed_sag_store(data_dir) -> None:
    """Create the zleap-sag schema and publish one managed document, one retired generation, one legacy source."""
    from zleap.sag import DataEngine
    from zleap.sag.db import models as m

    from sag_api.sag.config_builder import build_engine_config

    config = build_engine_config(Settings(_env_file=None), overrides={"data_dir": str(data_dir)})
    engine = DataEngine(config, data_source_id="cfg-1", health_check=False)
    await engine.init_schema()
    await engine.start()
    try:
        factory = engine.resources.relational.session_factory()
        async with factory() as session:
            session.add(m.DataSource(id="cfg-1", name="demo"))
            session.add(m.SAGDataSourceManifest(data_source_id="cfg-1", revision=1, active=True))
            session.add(m.Article(
                id="doc1", data_source_id="cfg-1", title="Alice 的生平",
                extra_data={"sag_pipeline": {"generation_id": "gen1", "source_type": "article"}},
            ))
            session.add(m.SAGSourceManifest(
                data_source_id="cfg-1", source_id="doc1", source_type="article", active_generation_id="gen1",
                operation_id="op1", fence_scope="cfg-1", fence_token=1, status="ready",
                chunk_count=3, event_count=0, revision=1,
            ))
            rows = [
                ("c1", "gen1", "出生", "Alice 出生在北京，1990 年随家人移居上海。", 0),
                ("c2", "gen1", "教育", "Alice 在复旦大学学习计算机科学。", 1),
                ("c3", "gen1", "工作", "Bob 是 Alice 的同事。", 2),
                ("c0-stale", "gen0", "旧版本", "Alice 出生在广州（已退役版本）。", 0),
            ]
            for chunk_id, generation, heading, content, rank in rows:
                session.add(m.SourceChunk(
                    id=chunk_id, data_source_id="cfg-1", source_type="article", source_id="doc1",
                    generation_id=generation, article_id="doc1", content=content, heading=heading, rank=rank,
                    chunk_length=len(content), references=[],
                    extra_data={"title": "Alice 的生平", "chunk_type": "TEXT"},
                ))
            session.add(m.Article(id="legacy1", data_source_id="cfg-1", title="旧文档"))
            session.add(m.SourceChunk(
                id="l1", data_source_id="cfg-1", source_type="article", source_id="legacy1", generation_id=None,
                article_id="legacy1", content="没有 manifest 的旧来源段落，提到 Alice 喜欢咖啡。", heading="",
                rank=0, chunk_length=24, extra_data={}, references=[],
            ))
            await session.commit()
    finally:
        await engine.aclose()


def sag2_settings(data_dir, **changes) -> Settings:
    return Settings(_env_file=None).model_copy(update={
        "data_dir": str(data_dir),
        "jev_endpoint": "https://jev.invalid/score",
        "jev_api_key": "synthetic-jev-key",
        "search_strategy": "sag2",
        "sag2_candidate_k": 20,
        **changes,
    })


async def run_search(holder: Sag2EngineHolder, source_ids_by_config):
    return await holder.search(
        [("cfg-1", None)], QUERY, top_k=5, requested_sources=1, source_limit_applied=False,
        source_ids_by_config=source_ids_by_config,
    )


@pytest.mark.asyncio
async def test_sag2_reads_published_sag_chunks_in_place(tmp_path):
    await seed_sag_store(tmp_path)
    scorer = OverlapScorer()
    holder = Sag2EngineHolder(sag2_settings(tmp_path), scorer_factory=lambda _config: scorer)
    try:
        outcome = await run_search(holder, {"cfg-1": ("doc1", "legacy1")})
        ids = [section.chunk_id for section in outcome.sections]
        assert ids[0] == "c1"
        assert "c0-stale" not in ids  # retired generation stays invisible
        assert "l1" in ids  # legacy source without a manifest remains searchable
        first = outcome.sections[0]
        assert (first.source_config_id, first.source_id, first.heading) == ("cfg-1", "doc1", "出生")
        assert all(section.metadata["score_source"] == "jev" for section in outcome.sections)
        assert outcome.stats["effective_strategy"] == "sag2"
        assert outcome.stats["jev_usage"]["judgments"] >= 4
        assert outcome.stats["sag2"]["scope_chunks"] == 4
        assert scorer.calls >= 1

        # Publication pointers scope the request before scoring.
        scoped = await run_search(holder, {"cfg-1": ("doc1",)})
        assert "l1" not in [section.chunk_id for section in scoped.sections]
        assert [section.chunk_id for section in scoped.sections][0] == "c1"

        # Read-only attach: only the two auxiliary tables are added to the SAG database.
        connection = sqlite3.connect(tmp_path / "sag.db")
        try:
            names = {
                row[0] for row in connection.execute(
                    "select name from sqlite_master where type='table' and name like 'sag2%'",
                )
            }
        finally:
            connection.close()
        assert names == {"sag2_state", "sag2_search_sessions"}
        assert holder.started
    finally:
        await holder.aclose()
    assert not holder.started


@pytest.mark.asyncio
async def test_sag2_refuses_to_start_on_an_uninitialised_workspace(tmp_path):
    holder = Sag2EngineHolder(sag2_settings(tmp_path / "missing"))
    with pytest.raises(ConfigurationError) as error:
        await run_search(holder, None)
    assert error.value.code == "sag2_storage_not_ready"
    assert not (tmp_path / "missing" / "sag.db").exists()


@pytest.mark.asyncio
async def test_scope_guard_ignores_retired_rows_but_keeps_whole_source_scan_limit(tmp_path):
    await seed_sag_store(tmp_path)
    with sqlite3.connect(tmp_path / "sag.db") as connection:
        # Reprocessing can leave many retired generations awaiting cleanup.
        connection.executemany(
            "INSERT INTO source_chunk (id, data_source_id, source_id, source_type, generation_id, "
            "article_id, content, rank, chunk_length) VALUES (?, 'cfg-1', 'doc1', 'article', "
            "'retired', 'doc1', 'retired evidence', 0, 16)",
            [(f"retired-{i}",) for i in range(20_001)],
        )
    scorer = OverlapScorer()
    holder = Sag2EngineHolder(sag2_settings(tmp_path), scorer_factory=lambda _config: scorer)
    try:
        outcome = await run_search(holder, {"cfg-1": ("doc1",)})
        assert outcome.sections[0].chunk_id == "c1"
        assert outcome.stats["sag2"]["scope_chunks"] == 4
        # Selecting one document does not narrow SDK 0.1.0's namespace scan.
        holder._settings.sag2_max_scope_chunks = 3
        calls = scorer.calls
        with pytest.raises(ValidationError, match="信源扫描范围"):
            await run_search(holder, {"cfg-1": ("legacy1",)})
        assert scorer.calls == calls
    finally:
        await holder.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
async def test_scope_count_matches_sdk_loaded_rows_including_legacy_schemas(tmp_path, managed):
    from sqlalchemy import create_engine
    from sqlalchemy.ext.asyncio import create_async_engine
    from zleap.sag2._sag1 import Sag1Reader

    path = tmp_path / "scope.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE source_chunk (id TEXT, data_source_id TEXT, source_id TEXT, source_type TEXT, "
            "content TEXT, rank INTEGER" + (", generation_id TEXT" if managed else "") + ")",
        )
        if managed:
            connection.execute(
                "CREATE TABLE sag_source_manifest (data_source_id TEXT, source_id TEXT, "
                "status TEXT, active_generation_id TEXT)",
            )
            connection.execute("INSERT INTO sag_source_manifest VALUES ('A', 'doc', 'ready', 'current')")
            connection.execute("CREATE TABLE sag_data_source_manifest (data_source_id TEXT, active BOOLEAN)")
            connection.execute("INSERT INTO sag_data_source_manifest VALUES ('inactive', 0)")
        rows = [("current", "A", "doc", "article", "text", 0),
                ("legacy", "A", "legacy", "article", "text", 0),
                ("other", "B", "doc", "article", "text", 0)]
        if managed:
            rows = [(*row, "current" if row[0] == "current" else None) for row in rows]
            rows += [("retired", "A", "doc", "article", "text", 0, "old"),
                     ("staging", "A", "new", "article", "text", 0, "unpublished"),
                     ("inactive", "inactive", "doc", "article", "text", 0, None)]
        connection.executemany("INSERT INTO source_chunk VALUES (" + ",".join("?" * len(rows[0])) + ")", rows)
    sync_engine = create_engine(f"sqlite:///{path}")
    try:
        with sync_engine.connect() as connection:
            reader = Sag1Reader.detect(connection)
    finally:
        sync_engine.dispose()
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    holder_engine = SimpleNamespace(store=SimpleNamespace(engine=engine, sag1_reader=reader))
    try:
        for sources, expected in [(["A"], 2), (["B"], 1), (["inactive"], 0), (["A", "inactive"], 2), ([], 0)]:
            async with engine.connect() as connection:
                loaded = await reader._rows(connection, "source_chunk", sources)
            assert len(loaded) == expected
            assert await Sag2EngineHolder._count_scope(holder_engine, sources) == expected
    finally:
        await engine.dispose()
