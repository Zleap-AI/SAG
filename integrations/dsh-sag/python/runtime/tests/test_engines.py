import asyncio
from dataclasses import dataclass

import pytest

from dsh_sag_runtime.engines import EnginePool


@dataclass
class FakeHealth:
    state: str = "available"


class FakeEngine:
    def __init__(self, namespace: str | None) -> None:
        self.namespace = namespace
        self.starts = 0
        self.closes = 0

    async def start(self) -> None:
        self.starts += 1

    async def aclose(self) -> None:
        self.closes += 1

    async def health(self) -> FakeHealth:
        return FakeHealth()


class Factory:
    def __init__(self) -> None:
        self.engines: list[FakeEngine] = []

    def __call__(self, _config: object, namespace: str | None) -> FakeEngine:
        engine = FakeEngine(namespace)
        self.engines.append(engine)
        return engine


@pytest.mark.asyncio
async def test_pool_starts_one_unbound_search_engine_and_reuses_read_engine() -> None:
    factory = Factory()
    pool = EnginePool(object(), {"a", "b"}, max_read_engines=2, factory=factory)
    await pool.start()

    assert (await pool.search_engine()).namespace is None
    async with pool.read_engine("a") as first:
        pass
    async with pool.read_engine("a") as second:
        assert second is first

    assert [engine.namespace for engine in factory.engines] == [None, "a"]
    await pool.aclose()
    assert all(engine.closes == 1 for engine in factory.engines)


@pytest.mark.asyncio
async def test_pool_evicts_only_least_recently_used_idle_engine() -> None:
    factory = Factory()
    pool = EnginePool(object(), {"a", "b", "c"}, max_read_engines=2, factory=factory)
    await pool.start()
    async with pool.read_engine("a") as a:
        pass
    async with pool.read_engine("b") as b:
        pass
    async with pool.read_engine("a"):
        pass
    async with pool.read_engine("c"):
        pass

    assert b.closes == 1
    assert a.closes == 0
    await pool.aclose()


@pytest.mark.asyncio
async def test_pool_waits_instead_of_evicting_a_busy_engine() -> None:
    factory = Factory()
    pool = EnginePool(object(), {"a", "b"}, max_read_engines=1, factory=factory)
    await pool.start()
    entered = asyncio.Event()

    async def acquire_b() -> None:
        async with pool.read_engine("b"):
            entered.set()

    async with pool.read_engine("a") as a:
        task = asyncio.create_task(acquire_b())
        await asyncio.sleep(0)
        assert not entered.is_set()
        assert a.closes == 0
    await asyncio.wait_for(task, 1)
    assert a.closes == 1
    await pool.aclose()


@pytest.mark.asyncio
async def test_pool_serializes_concurrent_creation_and_rejects_after_close() -> None:
    factory = Factory()
    pool = EnginePool(object(), {"a"}, max_read_engines=1, factory=factory)
    await pool.start()
    gate = asyncio.Event()

    async def use() -> FakeEngine:
        async with pool.read_engine("a") as engine:
            await gate.wait()
            return engine

    first = asyncio.create_task(use())
    second = asyncio.create_task(use())
    await asyncio.sleep(0)
    gate.set()
    assert await first is await second
    assert [engine.namespace for engine in factory.engines].count("a") == 1
    await pool.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await pool.search_engine()
