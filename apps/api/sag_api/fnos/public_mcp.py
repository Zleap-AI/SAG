"""Public Streamable HTTP MCP proxy for fnOS Native packages.

The fnOS desktop router authenticates requests before they reach the package
UDS. External Agents cannot use that route, so this listener exposes only MCP
and forwards it to the already-running UDS Gateway.  It intentionally does not
own a WorkerSupervisor or any tenant state.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from sag_api.fnos.proxy import bearer_token, filtered_response_headers

_FORWARDED_METHODS = ["GET", "POST", "DELETE", "HEAD", "OPTIONS"]
_DROP_REQUEST_HEADERS = {
    b"authorization",
    b"connection",
    b"content-length",
    b"host",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
}


def _query_token(request: Request) -> str | None:
    values = request.query_params.getlist("token")
    return values[0].strip() if len(values) == 1 and values[0].strip() else None


def _request_headers(request: Request, token: str) -> list[tuple[bytes, bytes]]:
    headers = [
        (name.lower(), value)
        for name, value in request.scope["headers"]
        if name.lower() not in _DROP_REQUEST_HEADERS
    ]
    headers.append((b"authorization", f"Bearer {token}".encode("ascii")))
    return headers


def _response_headers(headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    return {
        name.decode("latin-1"): value.decode("latin-1")
        for name, value in filtered_response_headers(headers)
        if name.lower() != b"content-length"
    }


def create_public_mcp_app(gateway_socket: Path, *, max_concurrency: int = 2) -> FastAPI:
    """Create an MCP-only TCP proxy that reuses the existing UDS Gateway."""
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be positive")
    socket_file = Path(gateway_socket)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        transport = httpx.AsyncHTTPTransport(uds=str(socket_file))
        app.state.client = httpx.AsyncClient(
            transport=transport,
            base_url="http://fnos-gateway",
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=5.0),
        )
        app.state.semaphore = asyncio.Semaphore(max_concurrency)
        try:
            yield
        finally:
            await app.state.client.aclose()

    app = FastAPI(lifespan=lifespan)

    async def proxy_mcp(request: Request, subpath: str = ""):
        token = bearer_token(request.scope["headers"]) or _query_token(request)
        if token is None:
            return JSONResponse({"detail": "MCP authorization is required"}, status_code=401)
        semaphore: asyncio.Semaphore = app.state.semaphore
        if semaphore.locked():
            return JSONResponse(
                {"detail": "MCP proxy is busy; retry shortly"},
                status_code=503,
                headers={"Retry-After": "2"},
            )
        await semaphore.acquire()
        upstream_response: httpx.Response | None = None
        try:
            upstream_path = f"/app/sag/mcp/{subpath}" if subpath else "/app/sag/mcp/"
            upstream_query = [
                (key, value) for key, value in request.query_params.multi_items() if key != "token"
            ]
            upstream = app.state.client.build_request(
                request.method,
                upstream_path,
                params=upstream_query,
                headers=_request_headers(request, token),
                content=request.stream(),
            )
            upstream_response = await app.state.client.send(upstream, stream=True)
        except httpx.HTTPError:
            semaphore.release()
            return JSONResponse({"detail": "SAG MCP gateway is unavailable"}, status_code=502)

        async def body():
            try:
                async for chunk in upstream_response.aiter_raw():
                    yield chunk
            finally:
                await upstream_response.aclose()
                semaphore.release()

        return StreamingResponse(
            body(),
            status_code=upstream_response.status_code,
            headers=_response_headers(upstream_response.headers.raw),
        )

    app.add_api_route("/mcp", proxy_mcp, methods=_FORWARDED_METHODS)
    app.add_api_route("/mcp/{subpath:path}", proxy_mcp, methods=_FORWARDED_METHODS)
    return app
