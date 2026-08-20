"""HTTP policy helpers for the fnOS UDS gateway."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any


class HeaderPolicyError(ValueError):
    """An inbound request has ambiguous or unsafe proxy headers."""


_HOP_BY_HOP = {
    b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
    b"te", b"trailer", b"transfer-encoding", b"upgrade",
}
_FORBIDDEN = _HOP_BY_HOP | {b"authorization"}


def rewrite_worker_path(path: str, prefix: str = "/app/sag") -> str:
    if not path.startswith(f"{prefix}/"):
        raise ValueError("path is outside the fnOS gateway prefix")
    suffix = path[len(prefix) :]
    if not (suffix.startswith("/api/") or suffix.startswith("/mcp/")):
        raise ValueError("path is not a Worker route")
    return suffix


def filtered_request_headers(headers: Iterable[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """Drop client-controlled credentials and reject routing ambiguities."""
    result: list[tuple[bytes, bytes]] = []
    seen: set[bytes] = set()
    for name, value in headers:
        lowered = name.lower()
        if lowered in {b"host", b"content-length"}:
            if lowered in seen:
                raise HeaderPolicyError(f"duplicate {lowered.decode()}")
            seen.add(lowered)
        if (
            lowered in _FORBIDDEN
            or lowered == b"x-request-id"
            or lowered.startswith(b"x-sag-internal-")
        ):
            continue
        result.append((lowered, value))
    return result


def bearer_token(headers: Iterable[tuple[bytes, bytes]]) -> str | None:
    """Return one well-formed bearer token without trusting duplicate headers."""

    values = [value for name, value in headers if name.lower() == b"authorization"]
    if len(values) != 1:
        return None
    try:
        value = values[0].decode("latin-1").strip()
    except UnicodeDecodeError:
        return None
    if not value.lower().startswith("bearer "):
        return None
    token = value[7:].strip()
    return token or None


def filtered_response_headers(headers: Iterable[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """Do not let private upstreams install cookies or connection controls."""
    return [
        (name, value)
        for name, value in headers
        if name.lower() not in _HOP_BY_HOP and name.lower() != b"set-cookie"
    ]


async def proxy_websocket(
    client_ws: Any,
    worker_socket: Any,
    upstream_path: str,
    signed_headers: dict[str, str],
) -> None:
    """Relay a WebSocket without exposing the Worker outside its Unix socket.

    The path and signed headers are supplied by the gateway when it establishes
    ``worker_socket``.  They stay in the signature so this operation's security
    boundary is explicit at its call sites.
    """
    del upstream_path, signed_headers

    async def client_to_worker() -> None:
        while True:
            message = await client_ws.receive()
            message_type = message["type"]
            if message_type == "websocket.disconnect":
                await worker_socket.close(code=message.get("code", 1000))
                return
            if message.get("text") is not None:
                await worker_socket.send(message["text"])
            elif message.get("bytes") is not None:
                await worker_socket.send(message["bytes"])

    async def worker_to_client() -> None:
        try:
            async for message in worker_socket:
                if isinstance(message, str):
                    await client_ws.send_text(message)
                else:
                    await client_ws.send_bytes(message)
        finally:
            await client_ws.close(code=getattr(worker_socket, "close_code", 1000) or 1000)

    client_task = asyncio.create_task(client_to_worker())
    worker_task = asyncio.create_task(worker_to_client())
    done, pending = await asyncio.wait(
        {client_task, worker_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()
