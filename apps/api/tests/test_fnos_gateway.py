"""Contract tests for the local fnOS gateway boundary."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from sag_api.fnos.identity import GatewayIdentity, InternalIdentitySigner
from sag_api.fnos.proxy import (
    HeaderPolicyError,
    filtered_request_headers,
    proxy_websocket,
    rewrite_worker_path,
)

FNOS_A = {
    "X-Trim-Userid": "1000",
    "X-Trim-Username": "Alice",
    "X-Trim-Isadmin": "false",
}


@pytest.mark.parametrize(
    ("incoming", "upstream"),
    [
        ("/app/sag/api/v1/sources", "/api/v1/sources"),
        ("/app/sag/mcp/", "/mcp/"),
    ],
)
def test_worker_path_rewrite_removes_only_the_gateway_prefix(incoming: str, upstream: str) -> None:
    """A path rewrite that removes API or MCP too would route a valid request incorrectly."""
    assert rewrite_worker_path(incoming) == upstream


def test_request_header_filter_removes_client_credentials_and_internal_headers() -> None:
    """Forwarding either credential lets a client impersonate a trusted local hop."""
    headers = filtered_request_headers(
        [
            (b"authorization", b"Bearer attacker"),
            (b"connection", b"keep-alive"),
            (b"x-sag-internal-uid", b"1"),
            (b"x-user-header", b"safe"),
        ]
    )

    assert headers == [(b"x-user-header", b"safe")]


@pytest.mark.parametrize("name", [b"host", b"content-length"])
def test_request_header_filter_rejects_duplicate_routing_or_length_headers(name: bytes) -> None:
    """Selecting one duplicate header permits request-smuggling ambiguity."""
    with pytest.raises(HeaderPolicyError):
        filtered_request_headers([(name, b"one"), (name, b"two")])


@pytest.mark.parametrize("origin", ["http://127.0.0.1:3091", "http://127.0.0.1:3191"])
def test_gateway_accepts_only_dynamic_loopback_web_origins(origin: str) -> None:
    from sag_api.fnos.gateway import create_gateway_app

    app = create_gateway_app(_Supervisor(_Worker()), InternalIdentitySigner(b"s" * 32), origin)
    assert isinstance(app, FastAPI)


@pytest.mark.parametrize("origin", ["http://localhost:3091", "https://127.0.0.1:3091", "http://127.0.0.1:80", "http://127.0.0.1:3091/path"])
def test_gateway_rejects_non_loopback_or_non_origin_web_targets(origin: str) -> None:
    from sag_api.fnos.gateway import create_gateway_app

    with pytest.raises(ValueError, match="must be loopback"):
        create_gateway_app(_Supervisor(_Worker()), InternalIdentitySigner(b"s" * 32), origin)


class _Worker:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.second_chunk_released = False
        self.first_chunk_sent = asyncio.Event()
        self.release_second_chunk = asyncio.Event()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle), base_url="http://worker")
        self.socket_file = Path("/tmp/fake-worker.sock")

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/v1/test-stream":

            async def chunks():
                self.first_chunk_sent.set()
                yield b"event: first\\n\\n"
                await self.release_second_chunk.wait()
                self.second_chunk_released = True
                yield b"data: second\\n\\n"

            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Stream(chunks()))
        if request.url.path == "/api/v1/missing":
            return httpx.Response(404, content=b"not found")
        if request.url.path == "/api/v1/echo":
            return httpx.Response(200, content=await request.aread())
        return httpx.Response(200, content=bytes(request.url.query))


class _Stream(httpx.AsyncByteStream):
    def __init__(self, iterator):
        self._iterator = iterator

    async def __aiter__(self):
        async for chunk in self._iterator:
            yield chunk


@dataclass
class _Lease:
    handle: _Worker
    released: bool = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.released = True


class _Supervisor:
    def __init__(self, worker: _Worker) -> None:
        self.worker = worker
        self.identities: list[GatewayIdentity] = []
        self.leases: list[_Lease] = []
        self.closed = False

    async def acquire(self, identity: GatewayIdentity) -> _Lease:
        self.identities.append(identity)
        lease = _Lease(self.worker)
        self.leases.append(lease)
        return lease

    async def reap_idle(self, _now: float) -> list[int]:
        return []

    async def close(self) -> None:
        self.closed = True
        await self.worker.client.aclose()


class _StartingSupervisor(_Supervisor):
    async def acquire(self, identity: GatewayIdentity) -> _Lease:
        from sag_api.fnos.supervisor import WorkerStartError

        raise WorkerStartError("ready", RuntimeError("database initializing"))


@pytest.fixture
def worker() -> _Worker:
    return _Worker()


@pytest.fixture
def gateway(worker: _Worker) -> FastAPI:
    from sag_api.fnos.gateway import create_gateway_app

    supervisor = _Supervisor(worker)
    app = create_gateway_app(
        supervisor,
        InternalIdentitySigner(b"s" * 32),
        "http://127.0.0.1:3091",
    )
    app.state.supervisor = supervisor
    return app


@pytest.fixture
async def gateway_client(gateway: FastAPI):
    transport = httpx.ASGITransport(app=gateway)
    async with gateway.router.lifespan_context(gateway):
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            yield client


@pytest.mark.asyncio
async def test_worker_routes_require_the_gateway_uid(gateway_client: httpx.AsyncClient) -> None:
    """Allowing anonymous API traffic would create an unbound worker request."""
    response = await gateway_client.get("/app/sag/api/v1/anything")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_initializing_worker_returns_retriable_response(worker: _Worker) -> None:
    from sag_api.fnos.gateway import create_gateway_app

    app = create_gateway_app(_StartingSupervisor(worker), InternalIdentitySigner(b"s" * 32), "http://127.0.0.1:3091")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
            response = await client.get("/app/sag/api/v1/sources", headers=FNOS_A)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "2"
    assert response.json()["detail"].startswith("SAG is initializing")


@pytest.mark.asyncio
async def test_page_routes_require_the_gateway_uid(gateway_client: httpx.AsyncClient) -> None:
    """Pages must not bypass identity validation merely because they use the shared web server."""
    response = await gateway_client.get("/app/sag/")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_worker_proxy_keeps_query_and_signed_identity_but_strips_client_auth(
    gateway_client: httpx.AsyncClient, worker: _Worker
) -> None:
    """Dropping the query or forwarding bearer auth changes the authenticated worker request."""
    response = await gateway_client.get(
        "/app/sag/api/v1/anything?cursor=two",
        headers={**FNOS_A, "Authorization": "Bearer attacker", "X-SAG-Internal-Uid": "1"},
    )

    assert response.status_code == 200
    assert response.content == b"cursor=two"
    request = worker.requests[-1]
    assert request.headers.get("authorization") is None
    assert request.headers["x-sag-internal-uid"] == "1000"
    assert request.headers["x-sag-internal-request-id"]
    assert request.headers["x-request-id"] != ""


@pytest.mark.asyncio
async def test_worker_404_and_no_set_cookie_are_preserved_without_gateway_cookie(
    gateway_client: httpx.AsyncClient
) -> None:
    """Converting 404 or injecting a cookie would make the gateway alter Worker semantics."""
    response = await gateway_client.get("/app/sag/api/v1/missing", headers=FNOS_A)
    assert response.status_code == 404
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_request_body_is_forwarded_as_a_stream(gateway_client: httpx.AsyncClient) -> None:
    """Replacing the request iterator with request.body() would buffer large uploads at the gateway."""
    payload = b"x" * (25 * 1024 * 1024)
    response = await gateway_client.post("/app/sag/api/v1/echo", headers=FNOS_A, content=payload)
    assert response.status_code == 200
    assert response.content == payload


@pytest.mark.asyncio
async def test_worker_lease_lasts_until_the_stream_finishes(
    gateway_client: httpx.AsyncClient, gateway: FastAPI
) -> None:
    """Releasing before the response iterator finishes would permit idle reaping mid-stream."""
    response = await gateway_client.get("/app/sag/api/v1/missing", headers=FNOS_A)
    assert response.status_code == 404
    assert gateway.state.supervisor.leases[-1].released is True


@pytest.mark.asyncio
async def test_sse_is_forwarded_incrementally(gateway_client: httpx.AsyncClient, worker: _Worker) -> None:
    """Buffering upstream responses would withhold the first event until the second chunk is available."""
    task = asyncio.create_task(
        gateway_client.get("/app/sag/api/v1/test-stream", headers=FNOS_A, timeout=1)
    )
    await worker.first_chunk_sent.wait()
    assert not task.done()
    assert worker.second_chunk_released is False
    worker.release_second_chunk.set()
    response = await task
    assert response.content.startswith(b"event:")


class _ClientWebSocket:
    def __init__(self) -> None:
        self.messages = [
            {"type": "websocket.receive", "text": "text frame", "bytes": None},
            {"type": "websocket.receive", "text": None, "bytes": b"binary frame"},
        ]
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.close_code: int | None = None

    async def receive(self):
        if self.messages:
            return self.messages.pop(0)
        await asyncio.Event().wait()

    async def send_text(self, value: str) -> None:
        self.sent_text.append(value)

    async def send_bytes(self, value: bytes) -> None:
        self.sent_bytes.append(value)

    async def close(self, code: int) -> None:
        self.close_code = code


class _WorkerWebSocket:
    close_code = 4001

    def __init__(self) -> None:
        self.sent: list[str | bytes] = []

    async def send(self, value: str | bytes) -> None:
        self.sent.append(value)

    async def close(self, code: int) -> None:
        self.close_code = code

    def __aiter__(self):
        return self

    async def __anext__(self):
        while len(self.sent) < 2:
            await asyncio.sleep(0)
        if hasattr(self, "_sent_reply"):
            raise StopAsyncIteration
        self._sent_reply = True
        return "worker reply"


@pytest.mark.asyncio
async def test_websocket_proxy_relays_text_binary_and_worker_close_code() -> None:
    """A frame type or close code lost at the gateway breaks interactive Worker features."""
    client = _ClientWebSocket()
    worker = _WorkerWebSocket()

    await proxy_websocket(client, worker, "/api/v1/test-ws", {"X-SAG-Internal-Uid": "1000"})

    assert worker.sent == ["text frame", b"binary frame"]
    assert client.sent_text == ["worker reply"]
    assert client.close_code == 4001
