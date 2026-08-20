"""Behavioral tests for the TCP MCP proxy in front of the fnOS UDS gateway."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import uvicorn

from sag_api.fnos import cli
from sag_api.fnos.public_mcp import create_public_mcp_app


@dataclass
class _GatewayRecorder:
    requests: list[tuple[str, str, bytes, list[tuple[bytes, bytes]], bytes]] = field(default_factory=list)
    received: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event | None = None

    async def __call__(self, scope, receive, send) -> None:
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        self.requests.append((scope["method"], scope["path"], scope["query_string"], scope["headers"], bytes(body)))
        self.received.set()
        if self.release is not None:
            await self.release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"jsonrpc":"2.0"}'})


@asynccontextmanager
async def _gateway_server(socket_file: Path, recorder: _GatewayRecorder):
    server = uvicorn.Server(
        uvicorn.Config(recorder, uds=str(socket_file), lifespan="off", access_log=False, log_level="critical")
    )
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if socket_file.exists():
            break
        await asyncio.sleep(0.01)
    else:
        server.should_exit = True
        await task
        raise AssertionError("test UDS gateway did not start")
    try:
        yield recorder
    finally:
        server.should_exit = True
        await task
        socket_file.unlink(missing_ok=True)


@pytest.fixture
async def short_socket_file():
    directory = Path(tempfile.mkdtemp(prefix="sagpm-", dir="/tmp"))
    try:
        yield directory / "gateway.sock"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
async def public_client(short_socket_file: Path):
    socket_file = short_socket_file
    recorder = _GatewayRecorder()
    async with _gateway_server(socket_file, recorder):
        app = create_public_mcp_app(socket_file)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://public") as client:
                yield client, recorder


@pytest.mark.asyncio
async def test_public_proxy_forwards_bearer_mcp_to_uds_gateway(public_client) -> None:
    client, gateway = public_client

    response = await client.post(
        "/mcp/",
        headers={"Authorization": "Bearer grant"},
        json={"jsonrpc": "2.0", "method": "initialize"},
    )

    assert response.status_code == 200
    assert len(gateway.requests) == 1
    assert gateway.requests[-1][0:3] == ("POST", "/app/sag/mcp/", b"")
    assert dict(gateway.requests[-1][3])[b"authorization"] == b"Bearer grant"
    assert gateway.requests[-1][4] == b'{"jsonrpc":"2.0","method":"initialize"}'


@pytest.mark.asyncio
async def test_public_proxy_converts_hermes_token_and_strips_it_from_upstream_query(public_client) -> None:
    client, gateway = public_client

    response = await client.get("/mcp/?token=grant&source_id=src_1")

    assert response.status_code == 200
    assert gateway.requests[-1][1] == "/app/sag/mcp/"
    assert gateway.requests[-1][2] == b"source_id=src_1"
    assert dict(gateway.requests[-1][3])[b"authorization"] == b"Bearer grant"


@pytest.mark.asyncio
async def test_public_proxy_rejects_non_mcp_paths_without_talking_to_gateway(public_client) -> None:
    client, gateway = public_client

    response = await client.get("/api/v1/sources")

    assert response.status_code == 404
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_public_proxy_returns_retryable_status_when_concurrency_is_exhausted(short_socket_file: Path) -> None:
    socket_file = short_socket_file
    recorder = _GatewayRecorder(release=asyncio.Event())
    async with _gateway_server(socket_file, recorder):
        app = create_public_mcp_app(socket_file, max_concurrency=1)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://public") as client:
                first = asyncio.create_task(client.get("/mcp/", headers={"Authorization": "Bearer grant"}))
                await asyncio.wait_for(recorder.received.wait(), timeout=1)
                saturated = await client.get("/mcp/", headers={"Authorization": "Bearer grant"})
                recorder.release.set()
                await first

    assert saturated.status_code == 503
    assert saturated.headers["retry-after"] == "2"


def test_mcp_proxy_cli_runs_the_public_listener(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[object, str, int]] = []

    def fake_run(app, *, host: str, port: int, proxy_headers: bool, forwarded_allow_ips: str) -> None:
        calls.append((app, host, port))
        assert proxy_headers is False
        assert forwarded_allow_ips == ""

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)

    cli.main(["mcp-proxy", "--socket", str(tmp_path / "gateway.sock"), "--host", "0.0.0.0", "--port", "5667"])

    assert calls[0][1:] == ("0.0.0.0", 5667)
