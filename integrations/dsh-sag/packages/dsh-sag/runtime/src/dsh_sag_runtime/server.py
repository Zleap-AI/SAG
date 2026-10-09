"""Concurrent JSON-RPC request state machine for the stdio sidecar."""

from __future__ import annotations

import asyncio
from typing import Any

from .errors import safe_error_data
from .protocol import (
    RPC_PROTOCOL_VERSION,
    CancelRequest,
    InitializeRequest,
    ReadRequest,
    RpcErrorData,
    RpcErrorResponse,
    RpcRequest,
    RpcResponse,
    SearchRequest,
    ShutdownRequest,
    error_response,
    success_response,
)

__all__ = ["RuntimeServer", "safe_error_data"]


class RuntimeServer:
    """Own request admission, correlation, cancellation, and runtime shutdown."""

    def __init__(self, pool: Any, search: Any, read: Any, namespaces: tuple[str, ...], *, engine_version: str) -> None:
        self._pool = pool
        self._search = search
        self._read = read
        self._namespaces = namespaces
        self._engine_version = engine_version
        self._state = "new"
        self._live: dict[int, asyncio.Task[dict[str, Any]]] = {}
        self._pool_closed = False

    def _state_error(self, request_id: int, code: str, message: str) -> RpcErrorResponse:
        return error_response(request_id, -32002, message, RpcErrorData(code=code, retryable=False, message=message))

    async def accept(self, request: RpcRequest) -> RpcResponse | None:
        if isinstance(request, CancelRequest):
            task = self._live.get(request.params.id)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            return None

        request_id = request.id
        if request_id in self._live:
            return self._state_error(request_id, "DSH_SAG_DUPLICATE_ID", "request id is already live")
        if self._state in {"shutting", "closed"}:
            return self._state_error(request_id, "DSH_SAG_SHUTTING_DOWN", "SAG runtime is shutting down")
        if isinstance(request, InitializeRequest):
            if self._state != "new":
                return self._state_error(request_id, "DSH_SAG_ALREADY_INITIALIZED", "SAG runtime is already initialized")
            self._state = "initializing"
        elif self._state != "ready":
            return self._state_error(request_id, "DSH_SAG_NOT_INITIALIZED", "initialize must complete before other requests")
        elif isinstance(request, ShutdownRequest):
            self._state = "shutting"

        task = asyncio.create_task(self._execute(request))
        self._live[request_id] = task
        try:
            result = await task
            return success_response(request_id, result)
        except asyncio.CancelledError:
            if isinstance(request, InitializeRequest):
                self._state = "new"
            return error_response(
                request_id,
                -32800,
                "request cancelled",
                RpcErrorData(code="DSH_SAG_CANCELLED", retryable=True, message="request cancelled"),
            )
        except BaseException as error:
            if isinstance(request, InitializeRequest):
                self._state = "new"
            data = safe_error_data(error)
            return error_response(request_id, -32602 if isinstance(error, ValueError) else -32603, data.message or "SAG runtime request failed", data)
        finally:
            self._live.pop(request_id, None)

    async def _execute(self, request: InitializeRequest | SearchRequest | ReadRequest | ShutdownRequest) -> dict[str, Any]:
        if isinstance(request, InitializeRequest):
            await self._pool.start()
            capabilities = await self._pool.capabilities()
            if not capabilities.evidence_read:
                raise RuntimeError("zleap-sag runtime does not support evidence reads")
            health = await self._pool.health()
            health_state = getattr(health.state, "value", health.state)
            self._state = "ready"
            return {
                "protocolVersion": RPC_PROTOCOL_VERSION,
                "engineVersion": self._engine_version,
                "health": str(health_state),
                "evidenceRead": True,
                "namespaces": list(self._namespaces),
            }
        if isinstance(request, SearchRequest):
            return await self._search.search(request.params)
        if isinstance(request, ReadRequest):
            return await self._read.read(request.params)
        current = asyncio.current_task()
        others = [task for task in self._live.values() if task is not current and not task.done()]
        for task in others:
            task.cancel()
        if others:
            await asyncio.gather(*others, return_exceptions=True)
        await self._close_pool()
        return {}

    async def _close_pool(self) -> None:
        if not self._pool_closed:
            self._pool_closed = True
            await self._pool.aclose()

    async def aclose(self) -> None:
        if self._state == "closed":
            return
        self._state = "closed"
        current = asyncio.current_task()
        tasks = [task for task in self._live.values() if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._close_pool()
