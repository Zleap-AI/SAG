"""A JEV fanout owns source cleanup through repeated caller cancellation."""

import asyncio
import os
import socket
from contextlib import asynccontextmanager, suppress

import pytest

from sag_api.core.config import Settings
from sag_api.sag.engine_manager import EngineManager


@pytest.mark.asyncio
async def test_repeated_caller_cancellation_waits_for_real_source_socket_cleanup():
    owner = EngineManager(Settings(
        _env_file=None, jev_endpoint="https://scorer.invalid/score", jev_api_key="independent",
    ))
    entered = asyncio.Event()
    draining = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()
    left, right = socket.socketpair()
    descriptors = (left.fileno(), right.fileno())

    class Engine:
        async def search(self, _request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                draining.set()
                await release.wait()
                left.close()
                right.close()
                closed.set()

    @asynccontextmanager
    async def use(*_args):
        yield Engine()

    owner.use = use
    request = asyncio.create_task(owner.search_many(
        [("config", None)], "q", strategy="multi_jev",
    ))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        request.cancel("caller disconnected")
        await asyncio.wait_for(draining.wait(), 1)
        assert not request.done()

        # A shutdown or another abort may cancel the same request while its
        # first cancellation is waiting for a source's asynchronous finally.
        request.cancel("shutdown cancelled the request again")
        done, _pending = await asyncio.wait({request}, timeout=0.05)
        assert not done, "request reached a terminal state before source cleanup finished"
        assert not closed.is_set()
        for descriptor in descriptors:
            os.fstat(descriptor)

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(request), 1)
        assert closed.is_set()
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        release.set()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(request, 1)
        left.close()
        right.close()
