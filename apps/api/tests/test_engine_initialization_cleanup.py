"""Unregistered engines must release resources on every initialization exit."""

import asyncio
import errno
import os
from types import SimpleNamespace

import pytest

from sag_api.sag import engine_manager

_STAGES = ("schema", "start", "sqlite", "source", "indexes")


@pytest.fixture
def initialization(monkeypatch):
    engines = []

    def prepare(*, fail_at=None, error=None, block_at=None, close_error=None):
        reached = asyncio.Event()

        async def step(stage):
            if stage == block_at:
                reached.set()
                await asyncio.Event().wait()
            if stage == fail_at:
                raise error

        class ResourceEngine:
            def __init__(self, *args, **kwargs):
                # A real OS resource makes lost ownership observable without GC.
                self.fd = os.open(os.devnull, os.O_RDONLY)
                self.descriptor = self.fd
                engines.append(self)

            async def init_schema(self):
                await step("schema")

            async def start(self):
                await step("start")

            async def aclose(self):
                self.release_fd()
                if close_error is not None:
                    raise close_error

            def release_fd(self):
                if self.fd is not None:
                    os.close(self.fd)
                    self.fd = None

        async def configure_sqlite(engine):
            await step("sqlite")

        async def configure_source(source_config_id, source, engine):
            await step("source")

        async def configure_indexes(engine):
            await step("indexes")

        manager = engine_manager.EngineManager(SimpleNamespace(engine_cache_size=2))
        monkeypatch.setattr(engine_manager, "DataEngine", ResourceEngine)
        monkeypatch.setattr(manager, "_config_for", lambda source: object())
        monkeypatch.setattr(manager, "_configure_sqlite_document_store", configure_sqlite)
        monkeypatch.setattr(manager, "_ensure_source_config", configure_source)
        monkeypatch.setattr(manager, "_ensure_universe_query_indexes", configure_indexes)
        return manager, engines, reached

    yield prepare
    for engine in engines:
        engine.release_fd()


def assert_descriptor_closed(descriptor):
    with pytest.raises(OSError) as raised:
        os.fstat(descriptor)
    assert raised.value.errno == errno.EBADF


@pytest.mark.parametrize("stage", _STAGES)
async def test_initialization_failure_closes_unregistered_engine(initialization, stage):
    original = RuntimeError(f"failed {stage}")
    manager, engines, _ = initialization(fail_at=stage, error=original)

    with pytest.raises(RuntimeError) as raised:
        await manager.provision("source")

    assert raised.value is original
    assert manager._slots == {}
    assert engines[0].fd is None
    assert_descriptor_closed(engines[0].descriptor)


@pytest.mark.parametrize("stage", _STAGES)
async def test_initialization_cancellation_closes_unregistered_engine(initialization, stage):
    manager, engines, reached = initialization(block_at=stage)
    task = asyncio.create_task(manager.provision("source"))
    await asyncio.wait_for(reached.wait(), timeout=2)
    task.cancel("initialization cancelled")

    with pytest.raises(asyncio.CancelledError, match="initialization cancelled"):
        await task

    assert manager._slots == {}
    assert engines[0].fd is None
    assert_descriptor_closed(engines[0].descriptor)


@pytest.mark.parametrize("close_error", [RuntimeError("close failed"), asyncio.CancelledError("close cancelled")])
async def test_cleanup_failure_preserves_initialization_error(initialization, close_error):
    original = RuntimeError("source initialization failed")
    manager, engines, _ = initialization(fail_at="source", error=original, close_error=close_error)

    with pytest.raises(RuntimeError) as raised:
        await manager.provision("source")

    assert raised.value is original
    assert manager._slots == {}
    assert engines[0].fd is None
    assert_descriptor_closed(engines[0].descriptor)


@pytest.mark.parametrize("close_error", [RuntimeError("close failed"), asyncio.CancelledError("close cancelled")])
async def test_cleanup_failure_preserves_initialization_cancellation(initialization, close_error):
    manager, engines, reached = initialization(block_at="source", close_error=close_error)
    task = asyncio.create_task(manager.provision("source"))
    await asyncio.wait_for(reached.wait(), timeout=2)
    task.cancel("original initialization cancellation")

    with pytest.raises(asyncio.CancelledError, match="original initialization cancellation"):
        await task

    assert manager._slots == {}
    assert engines[0].fd is None
    assert_descriptor_closed(engines[0].descriptor)


async def test_repeated_initialization_failures_keep_real_descriptors_bounded(initialization):
    manager, engines, _ = initialization(fail_at="sqlite", error=RuntimeError("sqlite unavailable"))
    descriptors = []

    for attempt in range(32):
        with pytest.raises(RuntimeError, match="sqlite unavailable"):
            await manager.provision(f"source-{attempt}")
        descriptors.extend(engine.fd for engine in engines if engine.fd is not None)

    await manager.aclose_all()
    assert manager._slots == {}
    assert descriptors == []
    for engine in engines:
        assert_descriptor_closed(engine.descriptor)


async def test_successful_initialization_transfers_resource_ownership_to_slot(initialization):
    manager, engines, _ = initialization()

    await manager.provision("source")
    engine = engines[0]
    descriptor = engine.fd
    assert manager._slots["source"].engine is engine
    os.fstat(descriptor)
    await manager.provision("source")
    assert len(engines) == 1

    await manager.aclose_all()

    assert manager._slots == {}
    assert_descriptor_closed(descriptor)
