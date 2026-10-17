"""Shutdown failures must not leave known fnOS worker subprocesses alive."""

from __future__ import annotations

import asyncio
import sys

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
