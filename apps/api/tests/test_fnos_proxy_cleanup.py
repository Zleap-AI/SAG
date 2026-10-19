"""A proxy owns real upstream descriptors until asynchronous close finishes."""

import asyncio
import gzip
import socket

import httpx
import pytest

from sag_api.fnos.proxy import ProxyStreamingResponse


@pytest.mark.asyncio
@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
@pytest.mark.parametrize("cancel_count", [1, 3])
async def test_direct_asgi_cancellation_joins_upstream_close_before_releasing_lease(
    spec_version, cancel_count
):
    close_started = asyncio.Event()
    finish_close = asyncio.Event()
    left, right = socket.socketpair()
    released = []

    class Body(httpx.AsyncByteStream):
        close_count = 0

        async def __aiter__(self):
            yield b"synthetic response"

        async def aclose(self):
            self.close_count += 1
            close_started.set()
            await finish_close.wait()
            left.close()
            right.close()

    async def release():
        # Returning capacity before closing the old connection breaks ownership.
        assert left.fileno() == right.fileno() == -1
        released.append(True)

    async def send(_message):
        pass

    async def receive():
        await asyncio.Event().wait()

    body = Body()
    response = ProxyStreamingResponse(httpx.Response(200, stream=body), headers={}, release=release)
    task = asyncio.create_task(response(
        {"type": "http", "asgi": {"spec_version": spec_version}}, receive, send,
    ))
    try:
        await asyncio.wait_for(close_started.wait(), timeout=2)
        for _ in range(cancel_count):
            task.cancel("request stopped during upstream close")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done(), "ASGI exited before its upstream descriptors were closed"
            assert not released
            assert left.fileno() >= 0 and right.fileno() >= 0

        finish_close.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert left.fileno() == right.fileno() == -1
        assert body.close_count == 1
        assert released == [True]
    finally:
        finish_close.set()
        await asyncio.gather(task, return_exceptions=True)
        left.close()
        right.close()


@pytest.mark.asyncio
async def test_proxy_forwards_compressed_raw_bytes_without_decoding():
    payload = gzip.compress(b"synthetic compressed response")
    sent = []

    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield payload[:8]
            yield payload[8:]

        async def aclose(self):
            await asyncio.sleep(0)
            self.closed = True

    body = Body()
    upstream = httpx.Response(200, stream=body, headers={"content-encoding": "gzip"})
    response = ProxyStreamingResponse(upstream, headers={"content-encoding": "gzip"})

    async def send(message):
        sent.append(message)

    async def receive():
        await asyncio.Event().wait()

    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert b"".join(message.get("body", b"") for message in sent) == payload
    assert body.closed and upstream.is_closed
