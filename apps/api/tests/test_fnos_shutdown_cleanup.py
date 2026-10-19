"""Shutdown failures must not leave known fnOS worker subprocesses alive."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import time

import httpx
import pytest

from sag_api.fnos.identity import GatewayIdentity, InternalIdentitySigner
from sag_api.fnos.supervisor import WorkerSupervisor


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, *, fail_close=False, fail_ready=False):
        self.fail_close = fail_close
        self.fail_ready = fail_ready

    async def handle_async_request(self, request):
        return httpx.Response(503 if self.fail_ready else 200)

    async def aclose(self):
        if self.fail_close:
            raise RuntimeError("transport close failed")


class _LiveFactory:
    def __init__(self):
        self.children = []
        self.fail_close_uids = set()
        self.fail_ready_uids = set()

    async def spawn(self, identity, _paths):
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)")
        self.children.append(process)
        return process, httpx.AsyncClient(
            transport=_Transport(
                fail_close=identity.uid in self.fail_close_uids,
                fail_ready=identity.uid in self.fail_ready_uids,
            ),
            base_url="http://worker",
        )


@pytest.fixture
async def live_factory():
    factory = _LiveFactory()
    try:
        yield factory
    finally:
        for process in factory.children:
            if process.returncode is None:
                process.kill()
            await process.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_count", [1, 2])
async def test_worker_shutdown_stops_every_child_when_one_client_close_fails(tmp_path, live_factory, worker_count):
    live_factory.fail_close_uids.add(1000)
    supervisor = WorkerSupervisor(tmp_path / "data", tmp_path / "temp", process_factory=live_factory)
    for uid in range(1000, 1000 + worker_count):
        await supervisor.acquire(GatewayIdentity(uid, f"user-{uid}", False))

    with pytest.raises(RuntimeError, match="transport close failed"):
        await supervisor.close()

    assert all(process.returncode is not None for process in live_factory.children)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_start_releases_reservation_even_when_client_close_fails(
    tmp_path, live_factory, monkeypatch, cancelled
):
    from sag_api.fnos import supervisor as module

    live_factory.fail_close_uids.add(1000)
    live_factory.fail_ready_uids.add(1000)
    monkeypatch.setattr(module, "READY_TIMEOUT_SECONDS", 0)
    supervisor = WorkerSupervisor(tmp_path / "data", tmp_path / "temp", process_factory=live_factory)
    original_ready = supervisor._wait_until_ready
    original_cancel = asyncio.CancelledError("startup cancelled")
    reservations = []

    async def wait_until_ready(process, client):
        reservations.extend(supervisor._starting.values())
        if cancelled:
            raise original_cancel
        await original_ready(process, client)

    monkeypatch.setattr(supervisor, "_wait_until_ready", wait_until_ready)
    if cancelled:
        with pytest.raises(asyncio.CancelledError) as caught:
            await supervisor.acquire(GatewayIdentity(1000, "Alice", False))
        assert caught.value is original_cancel
    else:
        with pytest.raises(module.WorkerStartError) as caught:
            await supervisor.acquire(GatewayIdentity(1000, "Alice", False))
        assert caught.value.phase == "ready"
        assert isinstance(caught.value.__cause__, TimeoutError)

    assert not supervisor._starting
    assert reservations[0].done.is_set()
    await asyncio.wait_for(supervisor.close(), timeout=1)
    assert live_factory.children[0].returncode is not None


@pytest.mark.asyncio
async def test_gateway_shutdown_stops_workers_when_web_transport_close_fails(tmp_path, live_factory, monkeypatch):
    from sag_api.fnos import gateway

    supervisor = WorkerSupervisor(tmp_path / "data", tmp_path / "temp", process_factory=live_factory)
    await supervisor.acquire(GatewayIdentity(1000, "Alice", False))
    original_client = httpx.AsyncClient

    def client_with_failing_web_transport(*args, **kwargs):
        if kwargs.get("base_url") == "http://127.0.0.1:3091":
            kwargs["transport"] = _Transport(fail_close=True)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(gateway.httpx, "AsyncClient", client_with_failing_web_transport)
    app = gateway.create_gateway_app(supervisor, InternalIdentitySigner(b"s" * 32), "http://127.0.0.1:3091")
    with pytest.raises(RuntimeError, match="transport close failed"):
        async with app.router.lifespan_context(app):
            pass

    assert live_factory.children[0].returncode is not None


class _ClosingTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.entered = asyncio.Event()
        self.allow = asyncio.Event()
        self.sockets = socket.socketpair()
        self.descriptors = [item.fileno() for item in self.sockets]

    async def handle_async_request(self, request):
        return httpx.Response(200, json={"active": 0})

    async def aclose(self):
        self.entered.set()
        await self.allow.wait()
        for item in self.sockets:
            item.close()


class _ClosingFactory:
    def __init__(self):
        self.children = []
        self.transports = []

    async def spawn(self, identity, _paths):
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)")
        self.children.append(process)
        transport = _ClosingTransport()
        self.transports.append(transport)
        return process, httpx.AsyncClient(transport=transport, base_url="http://worker")


@pytest.fixture
async def closing_factory():
    factory = _ClosingFactory()
    try:
        yield factory
    finally:
        for transport in factory.transports:
            transport.allow.set()
            for item in transport.sockets:
                item.close()
        for process in factory.children:
            if process.returncode is None:
                process.kill()
            await process.wait()


def _assert_descriptors_closed(transport):
    for descriptor in transport.descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


async def test_cancelled_idle_reaper_joins_real_worker_and_client_cleanup(tmp_path, closing_factory):
    supervisor = WorkerSupervisor(
        tmp_path / "data", tmp_path / "temp", process_factory=closing_factory, idle_timeout=0,
    )
    lease = await supervisor.acquire(GatewayIdentity(1000, "Alice", False))
    await lease.release()
    transport = closing_factory.transports[0]
    reaping = asyncio.create_task(supervisor.reap_idle(time.monotonic()))
    try:
        await asyncio.wait_for(transport.entered.wait(), 1)
        reaping.cancel("gateway stopped the idle reaper")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not reaping.done(), "the reaper dropped ownership before worker/client cleanup completed"
        assert closing_factory.children[0].returncode is None
        for descriptor in transport.descriptors:
            os.fstat(descriptor)
        transport.allow.set()
        with pytest.raises(asyncio.CancelledError, match="gateway stopped the idle reaper"):
            await reaping
        await supervisor.close()
        assert closing_factory.children[0].returncode is not None
        _assert_descriptors_closed(transport)
    finally:
        transport.allow.set()
        await asyncio.gather(reaping, return_exceptions=True)
        await asyncio.gather(*(task for _, task in supervisor._teardown_tasks.values()), return_exceptions=True)


async def test_repeated_cancelled_close_joins_every_real_worker_before_propagating(tmp_path, closing_factory):
    supervisor = WorkerSupervisor(tmp_path / "data", tmp_path / "temp", process_factory=closing_factory)
    for uid in (1000, 1001):
        await supervisor.acquire(GatewayIdentity(uid, f"user-{uid}", False))
    first, second = closing_factory.transports
    closing = asyncio.create_task(supervisor.close())
    try:
        await asyncio.wait_for(first.entered.wait(), 1)
        closing.cancel("original shutdown cancellation")
        await asyncio.sleep(0)
        closing.cancel("repeated shutdown cancellation")
        await asyncio.sleep(0)
        assert not closing.done(), "shutdown returned while the first worker still held its sockets"
        first.allow.set()
        await asyncio.wait_for(second.entered.wait(), 1)
        closing.cancel("shutdown cancellation while closing the second worker")
        await asyncio.sleep(0)
        assert not closing.done(), "shutdown skipped a remaining worker after cancellation"
        second.allow.set()
        with pytest.raises(asyncio.CancelledError, match="original shutdown cancellation"):
            await closing
        await supervisor.close()
        assert all(process.returncode is not None for process in closing_factory.children)
        for transport in closing_factory.transports:
            _assert_descriptors_closed(transport)
    finally:
        for transport in closing_factory.transports:
            transport.allow.set()
        await asyncio.gather(closing, return_exceptions=True)
        await asyncio.gather(*(task for _, task in supervisor._teardown_tasks.values()), return_exceptions=True)


async def test_queued_acquire_rejects_existing_worker_after_shutdown_starts(tmp_path, live_factory, monkeypatch):
    supervisor = WorkerSupervisor(tmp_path / "data", tmp_path / "temp", process_factory=live_factory)
    identity = GatewayIdentity(1000, "Alice", False)
    lease = await supervisor.acquire(identity)
    await lease.release()
    queued = asyncio.Event()
    shutdown_entered = asyncio.Event()
    allow_shutdown = asyncio.Event()
    close_workers = supervisor._close_workers

    class QueuedStartLock(asyncio.Lock):
        async def __aenter__(self):
            queued.set()
            return await super().__aenter__()

    async def gated_close_workers():
        shutdown_entered.set()
        await allow_shutdown.wait()
        await close_workers()

    start_lock = QueuedStartLock()
    await start_lock.acquire()
    supervisor._start_locks[lease.handle.key] = start_lock
    monkeypatch.setattr(supervisor, "_close_workers", gated_close_workers)
    acquiring = asyncio.create_task(supervisor.acquire(identity))
    closing = None
    try:
        # The request has passed the first closed check and is queued on its
        # start lock before close marks the supervisor closed.
        await asyncio.wait_for(queued.wait(), 1)
        closing = asyncio.create_task(supervisor.close())
        await asyncio.wait_for(shutdown_entered.wait(), 1)
        start_lock.release()
        with pytest.raises(RuntimeError, match="worker supervisor is closed"):
            await acquiring
    finally:
        if start_lock.locked():
            start_lock.release()
        allow_shutdown.set()
        await asyncio.gather(acquiring, return_exceptions=True)
        if closing is not None:
            await closing
        else:
            await supervisor.close()
    assert live_factory.children[0].returncode is not None
