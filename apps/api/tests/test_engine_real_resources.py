"""Initialization exits must release real locked SQLite/LanceDB resources."""

import asyncio
import gc
import os
from pathlib import Path

import lancedb
import pytest
from zleap.sag import DataEngine
from zleap.sag.db import schema
from zleap.sag.exceptions import StateError

from sag_api.core.config import Settings
from sag_api.sag import engine_manager


def _open_sqlite_descriptors(path):
    identity = (path.stat().st_dev, path.stat().st_ino)
    directory = Path("/proc/self/fd") if Path("/proc/self/fd").is_dir() else Path("/dev/fd")
    descriptors = []
    for item in directory.iterdir():
        try:
            descriptor = int(item.name)
            status = os.fstat(descriptor)
        except (ValueError, OSError):
            continue
        if (status.st_dev, status.st_ino) == identity:
            descriptors.append((descriptor, identity))
    return descriptors


def _still_open(descriptor, identity):
    try:
        status = os.fstat(descriptor)
    except OSError:
        return False
    return (status.st_dev, status.st_ino) == identity


def _assert_resources_closed(state, connections):
    live_sqlite = [descriptor for descriptor, identity in state["descriptors"] if _still_open(descriptor, identity)]
    live_lancedb = sum(connection.is_open() for connection in connections)
    assert connections
    assert not live_sqlite and live_lancedb == 0, {
        "open_sqlite_descriptors": live_sqlite,
        "open_lancedb_connections": live_lancedb,
    }


async def _cancel_initialization(manager, reached, source_id, message):
    task = asyncio.create_task(manager.provision(source_id))
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        task.cancel(message)
        with pytest.raises(asyncio.CancelledError, match=message):
            await task
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.fixture
async def real_initialization(monkeypatch, tmp_path):
    engines = []
    connections = []
    reached = asyncio.Event()
    release = asyncio.Event()
    state = {"stage": None, "error": None, "close_error": None, "descriptors": []}
    settings = Settings(_env_file=None, data_dir=str(tmp_path / "engine"), embedding_schema_dimensions=8)
    manager = engine_manager.EngineManager(settings)

    def construct_engine(*args, **kwargs):
        engine = DataEngine(*args, **kwargs)
        engines.append(engine)
        return engine

    connect = lancedb.connect_async

    async def connect_lancedb(*args, **kwargs):
        connection = await connect(*args, **kwargs)
        connections.append(connection)
        return connection

    async def interrupt(stage):
        if stage != state["stage"]:
            return
        descriptors = _open_sqlite_descriptors(tmp_path / "engine" / "sag.db")
        assert descriptors, "the real SQLite pool must hold a database descriptor before interruption"
        state["descriptors"] = descriptors
        reached.set()
        if state["error"] is not None:
            raise state["error"]
        await release.wait()

    prepare_schema = schema.prepare_relation_schema

    async def prepare_relation_schema(*args, **kwargs):
        result = await prepare_schema(*args, **kwargs)
        await interrupt("schema" if kwargs["explicit_init"] else "start")
        return result

    configure_sqlite = manager._configure_sqlite_document_store

    async def configure_document_store(engine):
        await configure_sqlite(engine)
        await interrupt("sqlite")

    close_engine = DataEngine.aclose

    async def close_with_failure(engine):
        if state["close_error"] is not None:
            raise state["close_error"]
        await close_engine(engine)

    monkeypatch.setattr(engine_manager, "DataEngine", construct_engine)
    monkeypatch.setattr(lancedb, "connect_async", connect_lancedb)
    monkeypatch.setattr(schema, "prepare_relation_schema", prepare_relation_schema)
    monkeypatch.setattr(manager, "_configure_sqlite_document_store", configure_document_store)
    monkeypatch.setattr(DataEngine, "aclose", close_with_failure)
    yield manager, state, reached, release, connections
    release.set()
    await manager.aclose_all()
    for engine in engines:
        # Public container cleanup keeps an intentionally failing regression from
        # leaking real handles into later tests when vendor aclose() returns early.
        try:
            resources = engine.resources
        except StateError:
            continue
        await resources.aclose()


@pytest.mark.parametrize("stage", ["schema", "start", "sqlite"])
async def test_real_resources_are_closed_when_initialization_fails(real_initialization, stage):
    manager, state, _, _, connections = real_initialization
    state.update(stage=stage, error=RuntimeError(f"failed after real {stage}"))

    with pytest.raises(Exception, match=f"failed after real {stage}"):
        await manager.provision("real-source")

    assert manager._slots == {}
    _assert_resources_closed(state, connections)


@pytest.mark.parametrize("stage", ["schema", "start", "sqlite"])
async def test_real_resources_are_closed_when_initialization_is_cancelled(real_initialization, stage):
    manager, state, reached, _, connections = real_initialization
    state["stage"] = stage

    await _cancel_initialization(manager, reached, "real-source", "cancel real initialization")

    assert manager._slots == {}
    _assert_resources_closed(state, connections)


@pytest.mark.parametrize("close_error", [RuntimeError("close failed"), asyncio.CancelledError("close cancelled")])
async def test_real_partial_start_is_released_when_engine_close_fails(real_initialization, close_error):
    manager, state, reached, _, connections = real_initialization
    state.update(stage="start", close_error=close_error)

    await _cancel_initialization(manager, reached, "real-source", "original cancellation")

    assert manager._slots == {}
    _assert_resources_closed(state, connections)


async def test_repeated_real_start_cancellation_releases_descriptors_without_gc(real_initialization):
    manager, state, reached, _, connections = real_initialization
    state["stage"] = "start"
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        # The fixture also retains every actual engine until after these assertions.
        for attempt in range(5):
            reached.clear()
            await _cancel_initialization(manager, reached, f"real-source-{attempt}", "repeated cancellation")

            assert manager._slots == {}
            _assert_resources_closed(state, connections)
    finally:
        if was_enabled:
            gc.enable()
