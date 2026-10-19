"""EngineManager retains real resource ownership until cancelled cleanup completes."""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

import pytest

from sag_api.sag import engine_manager


class _SocketResources:
    def __init__(self):
        self.sockets = socket.socketpair()
        self.close_started = asyncio.Event()
        self.close_allowed = asyncio.Event()
        self.interrupted = False

    async def aclose(self):
        self.close_started.set()
        try:
            await self.close_allowed.wait()
            for item in self.sockets:
                item.close()
        except asyncio.CancelledError:
            self.interrupted = True
            raise

    def assert_closed(self):
        assert [item.fileno() for item in self.sockets] == [-1, -1]
        assert not self.interrupted


class _SocketEngine:
    def __init__(self, *, fallback_resources=False):
        self.owned = _SocketResources()
        self.resources = self.owned
        self.fallback_resources = fallback_resources
        self.start_entered = asyncio.Event()
        self.start_allowed = asyncio.Event()
        self.start_error = None
        self.close_error = None

    async def init_schema(self):
        pass

    async def start(self):
        self.start_entered.set()
        if self.start_error is not None:
            raise self.start_error
        await self.start_allowed.wait()

    async def aclose(self):
        if self.fallback_resources:
            # SDK 0.13 can leave the public resource container after failed start.
            return
        try:
            await self.owned.aclose()
        finally:
            # SDK 0.15 drops its container even if close was interrupted. Retrying
            # engine.aclose() cannot recover resources after ownership is lost.
            self.resources = None
        if self.close_error is not None:
            raise self.close_error


@pytest.fixture
def resources():
    engines = []

    def create(**kwargs):
        engine = _SocketEngine(**kwargs)
        engines.append(engine)
        return engine

    yield create

    # Reclaim resources only after the test's business assertions, including RED.
    # No assertion relies on this fixture finalizer to close a descriptor.
    for engine in engines:
        engine.owned.close_allowed.set()
        for item in engine.owned.sockets:
            item.close()


async def _checkpoint():
    future = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(future.set_result, None)
    await future


async def _settle_callbacks():
    for _ in range(4):
        await _checkpoint()


def _manager():
    return engine_manager.EngineManager(SimpleNamespace(engine_cache_size=2))


def _configure_initialization(monkeypatch, manager, engine):
    async def ready(*args):
        pass

    monkeypatch.setattr(engine_manager, "DataEngine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(manager, "_config_for", lambda source: object())
    monkeypatch.setattr(manager, "_configure_sqlite_document_store", ready)
    monkeypatch.setattr(manager, "_ensure_source_config", ready)
    monkeypatch.setattr(manager, "_ensure_universe_query_indexes", ready)


async def test_close_all_joins_every_detached_slot_before_propagating_repeated_cancellation(resources):
    manager = _manager()
    first, second = resources(), resources()
    manager._slots = {
        "first": engine_manager._Slot(engine=first),
        "second": engine_manager._Slot(engine=second),
    }
    closing = asyncio.create_task(manager.aclose_all())
    await asyncio.wait_for(first.owned.close_started.wait(), timeout=2)

    closing.cancel("original shutdown cancellation")
    await _settle_callbacks()
    closing.cancel("repeated shutdown cancellation")
    await _settle_callbacks()
    assert not closing.done(), "shutdown returned while the first detached slot still owned sockets"
    assert manager._slots == {}
    assert manager._create_lock.locked()

    first.owned.close_allowed.set()
    await asyncio.wait_for(second.owned.close_started.wait(), timeout=2)
    closing.cancel("cancellation while closing the second slot")
    await _settle_callbacks()
    assert not closing.done(), "shutdown skipped the second detached slot after cancellation"
    assert manager._create_lock.locked()
    second.owned.close_allowed.set()

    with pytest.raises(asyncio.CancelledError, match="original shutdown cancellation"):
        await closing
    first.owned.assert_closed()
    second.owned.assert_closed()
    assert not manager._create_lock.locked()


async def test_close_all_finishes_remaining_slots_when_an_engine_close_raises_cancellation(resources):
    manager = _manager()
    first, second = resources(), resources()
    first.close_error = asyncio.CancelledError("engine close cancellation")
    first.owned.close_allowed.set()
    second.owned.close_allowed.set()
    manager._slots = {
        "first": engine_manager._Slot(engine=first),
        "second": engine_manager._Slot(engine=second),
    }

    with pytest.raises(asyncio.CancelledError) as raised:
        await manager.aclose_all()

    first.owned.assert_closed()
    second.owned.assert_closed()
    assert raised.value.args == ("engine close cancellation",)
    assert manager._slots == {}


async def test_close_all_preserves_caller_cancellation_when_cleanup_cancel_finishes_same_turn(resources):
    manager = _manager()
    first, second = resources(), resources()
    first.close_error = asyncio.CancelledError("engine close cancellation")
    second.owned.close_allowed.set()
    manager._slots = {
        "first": engine_manager._Slot(engine=first),
        "second": engine_manager._Slot(engine=second),
    }
    closing = asyncio.create_task(manager.aclose_all())
    await asyncio.wait_for(first.owned.close_started.wait(), timeout=2)

    # The cleanup is ready before the caller wakes from cancel(), so it can
    # finish with its own cancellation before the caller handles its request.
    first.owned.close_allowed.set()
    closing.cancel("original shutdown cancellation")

    with pytest.raises(asyncio.CancelledError) as raised:
        await closing

    first.owned.assert_closed()
    second.owned.assert_closed()
    assert manager._slots == {}
    assert not manager._create_lock.locked()
    assert raised.value.args == ("original shutdown cancellation",)


@pytest.mark.parametrize("operation", ["release", "eviction"])
async def test_single_slot_close_keeps_lifecycle_lock_until_resources_are_released(
    resources,
    monkeypatch,
    operation,
):
    manager = _manager()
    old = resources()
    manager._slots["old"] = engine_manager._Slot(engine=old)
    if operation == "eviction":
        manager._cache_size = 1
        new = resources()
        new.start_allowed.set()
        new.owned.close_allowed.set()
        _configure_initialization(monkeypatch, manager, new)
        closing = asyncio.create_task(manager.provision("new"))
    else:
        closing = asyncio.create_task(manager.release("old"))
    await asyncio.wait_for(old.owned.close_started.wait(), timeout=2)

    reader_entered = asyncio.Event()

    async def read_runtime():
        async with manager._lifecycle_gate.read():
            reader_entered.set()

    reader = asyncio.create_task(read_runtime())
    closing.cancel("original slot cancellation")
    await _settle_callbacks()
    closing.cancel("repeated slot cancellation")
    await _settle_callbacks()
    assert not closing.done(), "slot cleanup returned while its sockets were still open"
    assert not reader_entered.is_set(), "lifecycle gate opened before slot cleanup finished"
    assert manager._create_lock.locked()
    old.owned.close_allowed.set()

    with pytest.raises(asyncio.CancelledError, match="original slot cancellation"):
        await closing
    old.owned.assert_closed()
    await asyncio.wait_for(reader, timeout=2)
    await manager.aclose_all()
    if operation == "eviction":
        new.owned.assert_closed()


@pytest.mark.parametrize("initial_exit", ["cancellation", "failure"])
@pytest.mark.parametrize("fallback_resources", [False, True])
async def test_initialization_exit_joins_engine_and_resource_cleanup_after_repeated_cancellation(
    resources,
    monkeypatch,
    initial_exit,
    fallback_resources,
):
    manager = _manager()
    engine = resources(fallback_resources=fallback_resources)
    original = RuntimeError("original initialization failure")
    if initial_exit == "failure":
        engine.start_error = original
    _configure_initialization(monkeypatch, manager, engine)
    provisioning = asyncio.create_task(manager.provision("source"))
    await asyncio.wait_for(engine.start_entered.wait(), timeout=2)
    if initial_exit == "cancellation":
        provisioning.cancel("original initialization cancellation")
    await asyncio.wait_for(engine.owned.close_started.wait(), timeout=2)
    provisioning.cancel("cancellation during initialization cleanup")
    await _settle_callbacks()
    provisioning.cancel("repeated initialization cleanup cancellation")
    await _settle_callbacks()
    assert not provisioning.done(), "initialization abandoned its unregistered resource cleanup"
    assert manager._slots == {}
    assert manager._create_lock.locked()
    engine.owned.close_allowed.set()

    if initial_exit == "cancellation":
        with pytest.raises(asyncio.CancelledError, match="original initialization cancellation"):
            await provisioning
    else:
        with pytest.raises(RuntimeError) as raised:
            await provisioning
        assert raised.value is original
    engine.owned.assert_closed()
    assert manager._slots == {}
