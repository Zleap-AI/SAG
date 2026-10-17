"""Application startup must release resources acquired before its yield."""

import asyncio
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api import main


def open_file_descriptors(path: Path) -> set[int]:
    metadata = path.stat()
    directory = "/proc/self/fd" if Path("/proc/self/fd").exists() else "/dev/fd"
    found = set()
    for name in os.listdir(directory):
        try:
            descriptor = int(name)
            current = os.fstat(descriptor)
        except (ValueError, OSError):
            continue
        if (current.st_dev, current.st_ino) == (metadata.st_dev, metadata.st_ino):
            found.add(descriptor)
    return found


@pytest.mark.parametrize("cancelled", [False, True])
async def test_startup_failure_closes_real_sqlite_pool(tmp_path, monkeypatch, cancelled):
    from sag_api.services import settings_service

    database_file = tmp_path / "startup.db"
    database = create_async_engine(f"sqlite+aiosqlite:///{database_file}")
    original = asyncio.CancelledError("startup cancelled") if cancelled else RuntimeError("settings failed")

    async def initialize():
        async with database.begin() as connection:
            await connection.execute(text("SELECT 1"))

    async def fail(_session_factory):
        assert open_file_descriptors(database_file)
        raise original

    monkeypatch.setattr(main, "init_db", initialize)
    monkeypatch.setattr(main, "dispose_db", database.dispose)
    monkeypatch.setattr(settings_service, "apply_startup_overrides", fail)
    try:
        with pytest.raises(type(original)) as caught:
            async with main.lifespan(FastAPI()):
                pytest.fail("startup should fail before yield")
        assert caught.value is original
        assert open_file_descriptors(database_file) == set()
    finally:
        await database.dispose()


async def test_queue_startup_failure_removes_real_litellm_callback(tmp_path, monkeypatch):
    import litellm

    from sag_api.db.base import Base

    database_file = tmp_path / "queue-startup.db"
    database = create_async_engine(f"sqlite+aiosqlite:///{database_file}")
    sessions = async_sessionmaker(database, expire_on_commit=False)
    original = RuntimeError("queue startup failed")

    async def initialize():
        async with database.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def fail(_self):
        raise original

    monkeypatch.setattr(main, "init_db", initialize)
    monkeypatch.setattr(main, "dispose_db", database.dispose)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    monkeypatch.setattr(main.InProcessAsyncQueue, "start", fail)
    before = tuple(litellm.callbacks)
    app = FastAPI()
    try:
        with pytest.raises(RuntimeError) as caught:
            async with main.lifespan(app):
                pytest.fail("queue startup should fail")
        assert caught.value is original
        assert tuple(litellm.callbacks) == before
        assert app.state.agent_runtime.status.value == "stopped"
        assert open_file_descriptors(database_file) == set()
    finally:
        for callback in tuple(litellm.callbacks):
            if callback not in before:
                main.uninstall_litellm_policy(callback)
        await app.state.agent_runtime.stop()
        await database.dispose()


async def test_startup_cleanup_failure_preserves_original_failure(monkeypatch):
    original = RuntimeError("database startup failed")

    async def fail_initialize():
        raise original

    async def fail_cleanup(_app):
        raise RuntimeError("cleanup also failed")

    monkeypatch.setattr(main, "init_db", fail_initialize)
    monkeypatch.setattr(main, "_shutdown_runtime", fail_cleanup)
    with pytest.raises(RuntimeError) as caught:
        async with main.lifespan(FastAPI()):
            pytest.fail("startup should fail")
    assert caught.value is original


async def test_shutdown_keeps_database_open_if_queue_still_owns_it(tmp_path, monkeypatch):
    database_file = tmp_path / "live-worker.db"
    database = create_async_engine(f"sqlite+aiosqlite:///{database_file}")

    class LiveQueue:
        async def stop(self):
            raise RuntimeError("worker did not stop")

    app = FastAPI()
    app.state.job_queue = LiveQueue()
    monkeypatch.setattr(main, "dispose_db", database.dispose)
    try:
        async with database.begin() as connection:
            await connection.execute(text("SELECT 1"))
        with pytest.raises(RuntimeError, match="worker did not stop"):
            await main._shutdown_runtime(app)
        assert open_file_descriptors(database_file)
    finally:
        await database.dispose()
