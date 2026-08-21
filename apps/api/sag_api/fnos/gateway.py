"""The single fnOS gateway that connects requests to private SAG Workers."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from websockets.asyncio.client import unix_connect

from sag_api.core.errors import AuthError
from sag_api.fnos.identity import InternalIdentitySigner, parse_gateway_identity
from sag_api.fnos.proxy import (
    HeaderPolicyError,
    bearer_token,
    filtered_request_headers,
    filtered_response_headers,
    proxy_websocket,
    rewrite_worker_path,
)
from sag_api.fnos.supervisor import WorkerCapacityError, WorkerStartError

log = logging.getLogger(__name__)


def _is_loopback_web_origin(origin: str) -> bool:
    """Only proxy UI requests to a non-public, local Next listener."""
    parsed = urlsplit(origin)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and port is not None
        and 1024 <= port <= 65535
        and not parsed.username
        and not parsed.password
        and parsed.path == ""
        and not parsed.query
        and not parsed.fragment
    )


def create_gateway_app(
    supervisor,
    signer: InternalIdentitySigner,
    web_origin: str,
    prefix: str = "/app/sag",
    mcp_routing_key: bytes | None = None,
) -> FastAPI:
    if not _is_loopback_web_origin(web_origin):
        raise ValueError("fnOS gateway web origin must be loopback")
    web_client = httpx.AsyncClient(base_url=web_origin)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def reap_workers() -> None:
            while True:
                await asyncio.sleep(30)
                await supervisor.reap_idle(time.monotonic())

        reaper = asyncio.create_task(reap_workers())
        try:
            yield
        finally:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)
            await web_client.aclose()
            await supervisor.close()

    app = FastAPI(lifespan=lifespan)

    async def stream_response(response: httpx.Response, lease=None):
        """Relay bytes without releasing the Worker while its response is active."""
        try:
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            except httpx.StreamConsumed:
                # Mock transports and already-buffered error responses may have
                # consumed their stream before this boundary receives them.
                yield response.content
        finally:
            await response.aclose()
            if lease is not None:
                await _release_lease(lease)

    async def _release_lease(lease) -> None:
        release = getattr(lease, "release", None)
        if release is not None:
            await release()
        else:
            await lease.__aexit__(None, None, None)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    def query_mcp_token(request: Request) -> str | None:
        # fnOS' public Nginx gateway reserves `token` for its own
        # browser-auth flow and rejects an unknown value before this UDS
        # application receives the request. Keep the MCP credential in a
        # product-specific parameter so HTTP/SSE-only clients (Hermes) can
        # reach the Streamable HTTP endpoint.
        values = request.query_params.getlist("sag_mcp_token")
        return values[0].strip() if len(values) == 1 and values[0].strip() else None

    @app.api_route("/{requested_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def route(request: Request, requested_path: str):
        path = f"/{requested_path}"
        if not (path == prefix or path.startswith(f"{prefix}/")):
            return JSONResponse({"detail": "not found"}, status_code=404)
        external_mcp_token = None
        identity = None
        if path.startswith(f"{prefix}/mcp/") and mcp_routing_key is not None:
            from sag_api.services.fnos_mcp_access import route_grant

            candidate = bearer_token(request.scope["headers"]) or query_mcp_token(request)
            if candidate is not None:
                identity = route_grant(candidate, routing_key=mcp_routing_key)
                if identity is not None:
                    external_mcp_token = candidate
        try:
            if identity is None:
                identity = parse_gateway_identity(request.headers)
        except AuthError as error:
            return JSONResponse({"detail": str(error)}, status_code=401)
        if path.startswith(f"{prefix}/mcp/") and external_mcp_token is not None and request.method in {"GET", "HEAD"}:
            return Response(status_code=200, media_type="application/json")
        try:
            headers = filtered_request_headers(request.scope["headers"])
        except HeaderPolicyError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)
        if path.startswith(f"{prefix}/api/") or path.startswith(f"{prefix}/mcp/"):
            try:
                upstream_path = rewrite_worker_path(path, prefix)
                lease = await supervisor.acquire(identity)
            except WorkerCapacityError as error:
                return JSONResponse(
                    {"detail": "worker capacity exhausted"},
                    status_code=error.status_code,
                    headers={"Retry-After": str(error.retry_after)},
                )
            except WorkerStartError:
                request_id = str(uuid4())
                return JSONResponse(
                    {"detail": "SAG is initializing this fnOS workspace; retry shortly", "request_id": request_id},
                    status_code=503,
                    headers={"Retry-After": "2", "X-Request-Id": request_id},
                )
            try:
                request_id = str(uuid4())
                signed = signer.sign(identity, request_id, int(time.time()))
                upstream_headers = headers + [
                    (name.lower().encode(), value.encode()) for name, value in signed.items()
                ] + [(b"x-request-id", request_id.encode())]
                if external_mcp_token is not None:
                    upstream_headers.append((b"authorization", f"Bearer {external_mcp_token}".encode()))
                upstream_params = [
                    (key, value)
                    for key, value in request.query_params.multi_items()
                    if key != "sag_mcp_token"
                ]
                upstream = lease.handle.client.build_request(
                    request.method,
                    upstream_path,
                    params=upstream_params,
                    headers=upstream_headers,
                    content=request.stream(),
                )
                response = await lease.handle.client.send(upstream, stream=True)
            except Exception:
                await _release_lease(lease)
                raise
            response_headers = filtered_response_headers(response.headers.raw)
            return StreamingResponse(
                stream_response(response, lease),
                status_code=response.status_code,
                headers={
                    name.decode("latin-1"): value.decode("latin-1")
                    for name, value in response_headers
                },
            )
        upstream = web_client.build_request(
            request.method,
            path,
            params=request.query_params,
            headers=headers,
            content=request.stream(),
        )
        response = await web_client.send(upstream, stream=True)
        if response.status_code >= 400 and (
            path.startswith(f"{prefix}/_next/")
            or path.startswith(f"{prefix}/api/v1/attachments/")
            or path.startswith(f"{prefix}/icon")
        ):
            log.warning("fnOS resource proxy failed path=%s status=%s", path, response.status_code)
        return StreamingResponse(
            stream_response(response),
            status_code=response.status_code,
            headers={
                name.decode("latin-1"): value.decode("latin-1")
                for name, value in filtered_response_headers(response.headers.raw)
            },
        )

    @app.websocket("/{requested_path:path}")
    async def websocket_route(websocket: WebSocket, requested_path: str) -> None:
        path = f"/{requested_path}"
        if not (path.startswith(f"{prefix}/api/") or path.startswith(f"{prefix}/mcp/")):
            await websocket.close(code=1008)
            return
        try:
            identity = parse_gateway_identity(websocket.headers)
            headers = filtered_request_headers(websocket.scope["headers"])
            upstream_path = rewrite_worker_path(path, prefix)
            lease = await supervisor.acquire(identity)
        except (AuthError, HeaderPolicyError, ValueError):
            await websocket.close(code=1008)
            return
        except WorkerCapacityError:
            await websocket.close(code=1013)
            return
        request_id = str(uuid4())
        signed = signer.sign(identity, request_id, int(time.time()))
        extra_headers = [
            (name.decode("latin-1"), value.decode("latin-1")) for name, value in headers
        ] + list(signed.items()) + [("X-Request-Id", request_id)]
        query = f"?{websocket.scope['query_string'].decode('ascii')}" if websocket.scope["query_string"] else ""
        worker_uri = f"ws://localhost{upstream_path}{query}"
        lease.streams += 1
        try:
            async with unix_connect(
                str(lease.handle.socket_file),
                uri=worker_uri,
                additional_headers=extra_headers,
            ) as worker_socket:
                await websocket.accept()
                await proxy_websocket(websocket, worker_socket, upstream_path, signed)
        except WebSocketDisconnect:
            pass
        finally:
            lease.streams -= 1
            await _release_lease(lease)

    return app
